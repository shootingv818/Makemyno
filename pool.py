"""
pool.py — the pool brain: several accounts working ONE number space in parallel.
================================================================================

The plain brain gives one account a list of numbers, probes them, and sends. That
is fine for a few hundred numbers and painfully slow for the daily allowance: one
account probing sequentially will not spend 2000 probes in an afternoon.

The pool brain hands the SAME number space to several accounts at once. Each
leases a small block, probes it, keeps the hits as its own contacts, and when the
shared target is reached each account messages the people IT found.

WHY AN AFFINE PERMUTATION INSTEAD OF RANDOM NUMBERS
---------------------------------------------------
Discovery generates random suffixes and checks a "seen" cache to avoid repeats.
That works for one account. Run five accounts against one prefix and they collide
constantly: account B burns a probe on a number account A checked two seconds ago
and has not recorded yet. Probes are the metered, expensive operation, so paying
for the same number twice is the one thing this feature cannot afford.

Instead the suffix space is walked as

    suffix(i) = (A * i + offset) mod 10^k     with gcd(A, 10^k) = 1

which is a bijection over the whole space. Leasing disjoint index ranges
therefore yields disjoint phone numbers BY CONSTRUCTION — no cache, no
collisions, no wasted probes — while the output is scattered rather than
sequential, so the traffic does not look like a counter walking upward.

THE RULES THIS ENGINE FOLLOWS
-----------------------------
  * ONE BLOCK AT A TIME, LEASED ATOMICALLY. `db.pool_lease_block` bumps the
    cursor inside an immediate transaction, so two accounts can never receive the
    same range.
  * A SMALL BLOCK (POOL_BLOCK). The target is global, so a large block means the
    last round overshoots and probes numbers nobody needed.
  * EVERY PROBE IS CHARGED to the daily budget, exactly like discovery. Running
    five accounts must not become a way around the cap — it is a way to spend it
    faster, not a way to spend more.
  * ONE ACCOUNT NEVER STOPS THE JOB. An account whose session dies is marked and
    dropped; the others carry on. A pool that dies with its weakest member would
    be less reliable than the single-account brain it replaces.
  * ONLY CONFIRMED SENDS ARE RECORDED, so a resumed job continues instead of
    messaging people a second time.
  * EACH ACCOUNT SENDS TO ITS OWN CONTACTS. A contact belongs to the session that
    added it; asking account B to message a guid account A discovered means B has
    no contact for that person.
"""
from __future__ import annotations

import asyncio
import random

from telethon import Button

import busy
import cards
import config
import db
import logbus
import rubika_client as rb
import worker

_bot = None
_state: dict = {}
_gate = None
_safe_edit = None
_respond = None

# job_id -> {"stop": bool, "lock": asyncio.Lock, ...}
_jobs: dict = {}

# Statuses that mean "this account could not take part". They survive the sending
# phase untouched, because they carry the reason the customer is owed.
# `budget_spent`, `exhausted` and `done` are NOT here: those accounts leeched
# successfully and still have contacts to message.
_RETIRED = {"failed", "busy", "frozen"}


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="rb")


# --------------------------------------------------------------------------- #
# The number space
# --------------------------------------------------------------------------- #
def affine_params(prefix: str) -> tuple:
    """(suffix_width, A, offset) for a full-period permutation of the suffixes.

    A must be coprime to 10^k for suffix(i) = (A*i + offset) mod 10^k to visit
    every value exactly once. Forcing the last digit to 7 guarantees it: 7 is
    neither even nor a multiple of 5, so it shares no factor with 10^k. The
    golden-ratio scale is there so consecutive indices land far apart rather than
    marching in a line.
    """
    digits = _normalize_prefix(prefix)
    k = max(0, 11 - len(digits))
    space = 10 ** k
    if space <= 1:
        return k, 1, 0
    a = int(space * 0.6180339887)
    a -= a % 10
    a += 7
    a %= space
    if a == 0:
        a = 7 % space or 7
    return k, a, random.randrange(space)


def _normalize_prefix(prefix: str) -> str:
    digits = "".join(ch for ch in str(prefix or "") if ch.isdigit())
    if digits.startswith("98"):
        digits = "0" + digits[2:]
    if digits and not digits.startswith("0"):
        digits = "0" + digits
    return digits[:11]


def number_at(job: dict, index: int) -> str:
    """The phone number at index `i` of this job's permuted space."""
    prefix = _normalize_prefix(job["prefix"])
    width = int(job["suffix_width"] or 0)
    if width <= 0:
        return prefix
    space = 10 ** width
    suffix = (int(job["affine_a"]) * int(index) + int(job["affine_offset"])) % space
    return prefix + str(suffix).zfill(width)


def space_size(job: dict) -> int:
    return 10 ** int(job["suffix_width"] or 0)


# --------------------------------------------------------------------------- #
# The leeching phase
# --------------------------------------------------------------------------- #
async def _probe_block(customer_id, acc: dict, numbers: list, ctl: dict) -> list:
    """Probe a leased block on one account. Returns [(phone, guid), ...].

    Raises InvalidAuthError so the caller can retire this account without taking
    the rest of the pool down with it.
    """
    import account_conn
    phone = acc["phone"]
    delay = config.clamp_discovery_delay(
        db.get_setting(customer_id, "discovery_delay"))
    found = []

    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        res = await worker.api_call(w, "POST", "/probe", {
            "customer_id": customer_id, "phone": phone,
            "numbers": numbers, "delay": delay}, timeout=900)
        for item in res.get("found") or []:
            if item.get("guid"):
                found.append((item["phone"], item["guid"]))
        for number in numbers:
            db.number_record(number, any(f[0] == number for f in found))
        return found

    for number in numbers:
        if ctl.get("stop"):
            break

        async def _one(client, p=number):
            return await rb.add_contact(
                client, p, first_name=config.CONTACT_DEFAULT_FIRST)

        try:
            raw = await account_conn.call(customer_id, phone, _one, timeout=60)
            guid = rb._guid_of(raw) if raw else None       # noqa: SLF001
            db.number_record(number, bool(guid))
            if guid:
                found.append((number, guid))
        except account_conn.InvalidAuthError:
            raise
        except Exception:      # noqa: BLE001
            db.number_record(number, False)
        await asyncio.sleep(delay)
    return found


async def _leech_account(customer_id, job_id, acc: dict, ctl: dict) -> None:
    """One account's leeching loop: lease a block, probe it, repeat.

    Every exit path is deliberate — target reached, budget spent, space
    exhausted, stopped, or this one account dying — because a loop that leases
    blocks forever would burn the customer's whole allowance on a job that had
    already finished.
    """
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)

    async with busy.hold(key, "pool", customer_id=customer_id,
                         extra={"account_id": aid, "job_id": job_id}) as held:
        if not held.ok:
            db.pool_set_account(customer_id, job_id, aid, status="busy",
                                note=busy.reason(key))
            return

        job = db.pool_get_job(customer_id, job_id)
        found_total = 0
        # A hard bound that does not depend on the budget shrinking. Every other
        # exit is a correctness check; this one is the backstop for when a
        # correctness check is itself broken. Without it, a probe that stops being
        # charged turns this into a loop that walks all ten million numbers behind
        # a four-digit prefix.
        rounds = 0
        max_rounds = max(1, config.POOL_MAX_ROUNDS)
        while rounds < max_rounds:
            rounds += 1
            if ctl.get("stop"):
                db.pool_set_account(customer_id, job_id, aid, status="stopped")
                return
            if db.pool_hit_count(customer_id, job_id) >= int(job["target"]):
                db.pool_set_account(customer_id, job_id, aid, status="done")
                db.pool_set_halt(customer_id, job_id, "target")
                return
            if db.are_sends_frozen():
                db.pool_set_account(customer_id, job_id, aid, status="frozen")
                return

            budget = db.probe_budget_left(customer_id)
            if budget <= 0:
                db.pool_set_account(customer_id, job_id, aid,
                                    status="budget_spent")
                db.pool_set_halt(customer_id, job_id, "budget_spent")
                return

            size = min(config.POOL_BLOCK, budget)
            start, end = db.pool_lease_block(customer_id, job_id, size)
            if end <= start:
                db.pool_set_account(customer_id, job_id, aid, status="done")
                return
            if start >= space_size(job):
                # The whole prefix has been walked; there is nothing left to try.
                db.pool_set_account(customer_id, job_id, aid, status="exhausted")
                db.pool_set_halt(customer_id, job_id, "exhausted")
                return

            numbers = [number_at(job, i) for i in range(start, end)
                       if i < space_size(job)]
            if not numbers:
                db.pool_set_account(customer_id, job_id, aid, status="exhausted")
                return

            db.probe_spend(customer_id, len(numbers))
            db.pool_incr_probed(customer_id, job_id, len(numbers))
            ctl["probed"] = ctl.get("probed", 0) + len(numbers)

            try:
                hits = await _probe_block(customer_id, acc, numbers, ctl)
            except Exception as exc:  # noqa: BLE001
                # THE POOL SURVIVES ITS WEAKEST MEMBER. This account is retired
                # with a reason; everybody else keeps leeching.
                db.pool_set_account(customer_id, job_id, aid, status="failed",
                                    note=type(exc).__name__)
                await logbus.error(exc, context=f"pool leech {phone}",
                                   customer=customer_id, notify=False)
                return

            for number, guid in hits:
                if db.pool_add_contact(customer_id, job_id, aid, number, guid):
                    found_total += 1
            db.pool_set_account(customer_id, job_id, aid, found=found_total)
            ctl["found"] = db.pool_hit_count(customer_id, job_id)

        # Fell out of the loop on the round cap rather than a real stop condition.
        db.pool_set_account(customer_id, job_id, aid, status="done",
                            note="round cap")
        db.pool_set_halt(customer_id, job_id, "round_cap")


# --------------------------------------------------------------------------- #
# The sending phase
# --------------------------------------------------------------------------- #
async def _send_account(customer_id, job_id, acc: dict, ctl: dict) -> None:
    """Message the contacts THIS account discovered.

    A contact belongs to the session that added it, so the account that found a
    person is the only one that can reach them.
    """
    import account_conn
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    job = db.pool_get_job(customer_id, job_id)
    if not job:
        return
    # An account the leeching phase already retired must keep the status and note
    # that say WHY. Overwriting them with a cheerful "done" destroys the only
    # explanation the customer gets for why one account found nothing.
    row = next((r for r in db.pool_accounts(customer_id, job_id)
                if r["account_id"] == aid), None)
    if row and row["status"] in _RETIRED:
        return

    if db.are_sends_frozen():
        db.pool_set_account(customer_id, job_id, aid, status="frozen")
        return

    targets = db.pool_account_guids(customer_id, job_id, aid, unsent_only=True)
    if not targets:
        db.pool_set_account(customer_id, job_id, aid, status="done")
        return

    delay = config.clamp_delay(db.get_setting(customer_id, "send_delay"))
    sent = 0

    async with busy.hold(key, "pool_send", customer_id=customer_id,
                         extra={"account_id": aid, "job_id": job_id}) as held:
        if not held.ok:
            db.pool_set_account(customer_id, job_id, aid, status="busy")
            return

        from_guid = message_id = None
        if job["mode"] == "marker":
            async def _find(client):
                return (await rb.get_self_guid(client),
                        await rb.find_marked_message(client, job["content"]))
            try:
                from_guid, found = await account_conn.call(
                    customer_id, phone, _find, timeout=120)
            except Exception as exc:  # noqa: BLE001
                db.pool_set_account(customer_id, job_id, aid, status="failed",
                                    note=type(exc).__name__)
                return
            if not found:
                db.pool_set_account(customer_id, job_id, aid, status="failed",
                                    note="marker not found")
                return
            message_id = rb._msg_id_of(found)      # noqa: SLF001

        for target in targets:
            if ctl.get("stop"):
                db.pool_set_account(customer_id, job_id, aid, status="stopped")
                break
            guid = target["guid"]
            if job["mode"] == "marker":
                async def _one(client, g=guid):
                    return await rb.forward_message(client, from_guid, g,
                                                    message_id)
            else:
                async def _one(client, g=guid):
                    return await rb.send_text(client, g, job["content"])
            try:
                await account_conn.call(customer_id, phone, _one,
                                        timeout=config.SEND_TIMEOUT)
                # Recorded only after the platform accepted it, so a restart
                # resumes rather than repeating.
                db.pool_mark_sent(customer_id, job_id, guid)
                sent += 1
                db.pool_set_account(customer_id, job_id, aid, sent=sent)
                db.incr_account_sent(customer_id, aid, 1)
            except account_conn.InvalidAuthError:
                db.pool_set_account(customer_id, job_id, aid, status="failed",
                                    note="session invalid")
                return
            except Exception:      # noqa: BLE001
                pass
            await asyncio.sleep(delay)

    db.usage_incr(customer_id, "send", sent)
    current = next((r for r in db.pool_accounts(customer_id, job_id)
                    if r["account_id"] == aid), None)
    if current and current["status"] not in _RETIRED | {"stopped"}:
        db.pool_set_account(customer_id, job_id, aid, status="done")


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
async def run_job(customer_id, job_id, msg=None) -> None:
    ctl = _jobs.setdefault(job_id, {"stop": False, "found": 0, "probed": 0})
    ctl.setdefault("stop", False)
    job = db.pool_get_job(customer_id, job_id)
    if not job:
        _jobs.pop(job_id, None)
        return

    account_rows = db.pool_accounts(customer_id, job_id)
    accounts = [db.get_account(customer_id, r["account_id"]) for r in account_rows]
    accounts = [a for a in accounts if a and a["status"] == "active"]
    if not accounts:
        db.pool_set_status(customer_id, job_id, "failed")
        _jobs.pop(job_id, None)
        return

    cust = db.get_customer(customer_id)
    await logbus.customer_action(cust, "pool_start", [
        cards.kv("Job", f"#{job_id}"),
        cards.kv("Prefix", job["prefix"]),
        cards.kv("Target", job["target"]),
        cards.kv("Accounts", len(accounts)),
        cards.kv("Mode", job["mode"]),
        cards.kv("Budget left", db.probe_budget_left(customer_id)),
    ], platform="Rubika")

    progress = None
    if msg is not None:
        progress = asyncio.create_task(_progress_loop(customer_id, job_id, ctl, msg))
    try:
        # ---- leech together ------------------------------------------------ #
        if job["status"] == "leeching":
            await asyncio.gather(*[
                _leech_account(customer_id, job_id, acc, ctl)
                for acc in accounts], return_exceptions=True)
            if ctl.get("stop"):
                db.pool_set_status(customer_id, job_id, "stopped")
            elif db.are_sends_frozen():
                # The emergency stop has to hold the job at the door, not merely
                # end the leeching and then send anyway.
                db.pool_set_status(customer_id, job_id, "leeching")
            else:
                db.pool_set_status(customer_id, job_id, "sending")

        # ---- then each account sends its own ------------------------------- #
        job = db.pool_get_job(customer_id, job_id)
        if job and job["status"] == "sending" and not ctl.get("stop"):
            ctl["phase"] = "sending"
            await asyncio.gather(*[
                _send_account(customer_id, job_id, acc, ctl)
                for acc in accounts], return_exceptions=True)
            db.pool_set_status(customer_id, job_id,
                               "stopped" if ctl.get("stop") else "done")
    except Exception as exc:  # noqa: BLE001
        db.pool_set_status(customer_id, job_id, "failed")
        await logbus.error(exc, context=f"pool job {job_id}", customer=customer_id)
    finally:
        if progress:
            progress.cancel()
        _jobs.pop(job_id, None)

    await _finish(customer_id, job_id, msg)


async def stop_job(customer_id, job_id) -> None:
    # setdefault, not assignment: if run_job has not created the entry yet the
    # stop would otherwise be lost to the race, and the customer's stop button
    # would silently do nothing.
    ctl = _jobs.setdefault(job_id, {"stop": False})
    ctl["stop"] = True


def running() -> list:
    return list(_jobs)


async def _finish(customer_id, job_id, msg=None) -> None:
    job = db.pool_get_job(customer_id, job_id)
    if not job:
        return
    counts = db.pool_counts(customer_id, job_id)
    rows = [
        cards.kv("Job", f"#{job_id}"),
        cards.kv("Prefix", job["prefix"]),
        cards.kv("Target", job["target"]),
        cards.kv("Found", cards.num(counts["found"])),
        cards.kv("Sent", cards.num(counts["sent"])),
        cards.kv("Probed", cards.num(job["probed"] or 0)),
        cards.kv("Budget left", cards.num(db.probe_budget_left(customer_id))),
        cards.kv("Result", _status_label(job["status"])),
    ]
    # Why leeching stopped is a fact about the JOB, not about any one account:
    # every account hits the same wall at the same moment. Recording it per
    # account would also mean overwriting that account's send outcome.
    if job.get("halt_reason"):
        rows.append(cards.kv("Stopped because", _HALT_LABEL.get(
            job["halt_reason"], job["halt_reason"])))
    rows.append(cards.LINE)
    for row in db.pool_accounts(customer_id, job_id):
        note = f" — {row['note']}" if row.get("note") else ""
        rows.append(f"📱 {row['phone']} → 🔎{row['found']} 📤{row['sent']}"
                    f"  ({row['status']}{note})")
    await logbus.customer_action(db.get_customer(customer_id), "pool_done", rows,
                                platform="Rubika")
    text = cards.panel_card("🌊 - #pool_report", rows)
    buttons = [_back(b"rbbrain")]
    if msg is not None:
        try:
            await msg.edit(text, buttons=buttons)
            return
        except Exception:
            pass
    try:
        await _bot.send_message(int(customer_id), text, buttons=buttons)
    except Exception:
        pass


_HALT_LABEL = {
    "budget_spent": "📊 سهمیه‌ی بررسی امروز تمام شد",
    "exhausted": "🔚 همه‌ی شماره‌های این پیش‌شماره بررسی شد",
    "target": "🎯 به هدف رسید",
    "round_cap": "🛑 به سقف امن دوره‌ها رسید",
}


def _status_label(status: str) -> str:
    return {"done": "🏁 پایان", "stopped": "⛔ متوقف شد", "failed": "⚠️ خطا",
            "leeching": "🔎 در حال کشف", "sending": "📤 در حال ارسال"}.get(
        status, status)


async def _progress_loop(customer_id, job_id, ctl, msg) -> None:
    """Live progress, like every other long job in the panel — a customer who
    cannot see movement assumes it broke and starts it again."""
    last = ""
    try:
        while True:
            await asyncio.sleep(config.CONTACT_PROGRESS_EVERY)
            text = progress_card(customer_id, job_id, ctl)
            if text != last:
                last = text
                try:
                    await msg.edit(text, buttons=[
                        [Button.inline("⛔ توقف", f"plstop_{job_id}".encode())]])
                except Exception:
                    pass
    except asyncio.CancelledError:
        return


def progress_card(customer_id, job_id, ctl=None) -> str:
    job = db.pool_get_job(customer_id, job_id)
    if not job:
        return cards.panel_card("🌊 - #pool", ["این کار پیدا نشد."])
    counts = db.pool_counts(customer_id, job_id)
    target = max(1, int(job["target"]))
    phase = "📤 ارسال" if job["status"] == "sending" else "🔎 کشف"
    rows = [
        cards.kv("Job", f"#{job_id}"),
        cards.kv("Phase", phase),
        cards.kv("Prefix", job["prefix"]),
        cards.bar(min(counts["found"], target), target),
        cards.kv("Found", f"{cards.num(counts['found'])} / {cards.num(target)}"),
        cards.kv("Sent", cards.num(counts["sent"])),
        cards.kv("Probed", cards.num(job["probed"] or 0)),
        cards.kv("Budget left", cards.num(db.probe_budget_left(customer_id))),
        cards.LINE,
    ]
    for row in db.pool_accounts(customer_id, job_id):
        mark = {"active": "🟢", "done": "✅", "failed": "🔴", "busy": "🟡",
                "stopped": "⛔", "budget_spent": "📊",
                "exhausted": "🔚"}.get(row["status"], "▫️")
        rows.append(f"{mark} {row['phone']} → 🔎{row['found']} 📤{row['sent']}")
    return cards.panel_card("🌊 - #pool_progress", rows)


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def menu_card(customer_id) -> str:
    return cards.card("🌊 مغز استخری", [
        "چند اکانت با هم روی یک پیش‌شماره کار می‌کنند: هر کدام",
        "بخشی از شماره‌ها را برمی‌دارد، بررسی می‌کند، و آخر کار",
        "هر اکانت به کسانی که خودش پیدا کرده پیام می‌دهد.",
        cards.LINE,
        "هیچ شماره‌ای دو بار بررسی نمی‌شود، پس سهمیه‌ی روزانه",
        "هدر نمی‌رود — فقط چند برابر سریع‌تر خرج می‌شود.",
        cards.LINE,
        cards.kv("سهمیه‌ی امروز", cards.num(db.probe_budget_left(customer_id)),
                 width=12),
    ])


def picker_buttons(customer_id, chosen: list) -> list:
    rows = []
    for acc in db.list_accounts(customer_id):
        if acc["status"] != "active":
            continue
        mark = "✅" if acc["id"] in chosen else "⬜️"
        rows.append([Button.inline(f"{mark} {acc['phone']}",
                                   f"plsel_{acc['id']}".encode())])
    if len(chosen) >= 2:
        rows.append([Button.inline(f"🚀 شروع ({len(chosen)} اکانت)", b"plgo")])
    rows.append([Button.inline("🔙 مغز", b"rbbrain")])
    return rows


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def setup(bot, state, gate, safe_edit, respond, register_steps) -> None:
    global _bot, _state, _gate, _safe_edit, _respond
    _bot, _state, _gate, _safe_edit, _respond = bot, state, gate, safe_edit, respond

    from telethon import events

    async def _render_picker(event):
        uid = event.sender_id
        chosen = _state.setdefault(uid, {}).setdefault("pool", [])
        active = [a for a in db.list_accounts(uid) if a["status"] == "active"]
        if len(active) < 2:
            await safe_edit(event, cards.card("🌊 مغز استخری", [
                "برای این حالت حداقل دو اکانت سالم لازم است.",
                "با یک اکانت همان «مغز» معمولی سریع‌تر است.",
            ]), buttons=[_back(b"rbbrain")])
            return
        await safe_edit(event, menu_card(uid) + "\n" + cards.panel_card(
            "🌊 - #pool_pick", [
                cards.kv("Selected", f"{len(chosen)} / {len(active)}"),
                "اکانت‌هایی که با هم کار کنند را انتخاب کن."]),
            buttons=picker_buttons(uid, chosen))

    @bot.on(events.CallbackQuery(data=b"rbpool"))
    async def pool_home(event):
        if not await gate(event):
            return
        await _render_picker(event)

    @bot.on(events.CallbackQuery(pattern=rb"plsel_(\d+)"))
    async def pool_select(event):
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        if not db.get_account(uid, aid):
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        chosen = _state.setdefault(uid, {}).setdefault("pool", [])
        if aid in chosen:
            chosen.remove(aid)
        else:
            chosen.append(aid)
        await _render_picker(event)

    @bot.on(events.CallbackQuery(data=b"plgo"))
    async def pool_go(event):
        if not await gate(event):
            return
        uid = event.sender_id
        chosen = list(_state.get(uid, {}).get("pool") or [])
        if len(chosen) < 2:
            await event.answer("حداقل دو اکانت انتخاب کن.", alert=True)
            return
        if db.probe_budget_left(uid) <= 0:
            await event.answer("سهمیه‌ی بررسی امروز تمام شده.", alert=True)
            return
        _state[uid] = {"step": "pl_prefix", "pool": chosen}
        await safe_edit(event, cards.card("🌊 پیش‌شماره", [
            "پیش‌شماره را بفرست، مثل 0912 یا 09123",
            cards.LINE,
            "پیش‌شماره‌ی بلندتر یعنی فضای کوچک‌تر و نتیجه‌ی سریع‌تر.",
        ]), buttons=[_back(b"rbpool")])

    @bot.on(events.CallbackQuery(data=b"plmode_marker"))
    async def pool_mode_marker(event):
        """Forward a marked message instead of typing text.

        Nothing to ask for: the marker is already configured in the content
        screen, so this launches straight away.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        st = _state.get(uid) or {}
        marker = db.get_marker(uid)
        st["step"] = None
        await _launch(event, st, "marker", marker)

    @bot.on(events.CallbackQuery(data=b"plmode_text"))
    async def pool_mode_text(event):
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        st = _state.get(uid) or {}
        st["step"] = "pl_text"
        _state[uid] = st
        await safe_edit(event, cards.card("✍️ متن پیام", [
            "متنی که برای هر مخاطب فرستاده شود را بنویس.",
        ]), buttons=[_back(b"rbpool")])

    @bot.on(events.CallbackQuery(pattern=rb"plstop_(\d+)"))
    async def pool_stop(event):
        if not await gate(event, count_action=False):
            return
        job_id = int(event.pattern_match.group(1))
        if not db.pool_get_job(event.sender_id, job_id):
            await event.answer("این کار پیدا نشد.", alert=True)
            return
        await stop_job(event.sender_id, job_id)
        await event.answer("در حال توقف ...", alert=True)

    @bot.on(events.CallbackQuery(data=b"pljobs"))
    async def pool_jobs(event):
        if not await gate(event):
            return
        uid = event.sender_id
        jobs = db.pool_list_jobs(uid, limit=8)
        if not jobs:
            await safe_edit(event, cards.card("🌊 کارهای استخری", [
                "هنوز کاری اجرا نشده."]), buttons=[_back(b"rbbrain")])
            return
        rows = []
        for job in jobs:
            counts = db.pool_counts(uid, job["id"])
            rows.append([Button.inline(
                f"#{job['id']} {job['prefix']} → {counts['found']}/{job['target']}"
                f" ({_status_label(job['status'])})",
                f"pljob_{job['id']}".encode())])
        rows.append(_back(b"rbbrain"))
        await safe_edit(event, cards.card("🌊 کارهای استخری", [
            cards.kv("Jobs", len(jobs), width=8)]), buttons=rows)

    @bot.on(events.CallbackQuery(pattern=rb"pljob_(\d+)"))
    async def pool_job_detail(event):
        if not await gate(event):
            return
        uid = event.sender_id
        job_id = int(event.pattern_match.group(1))
        job = db.pool_get_job(uid, job_id)
        if not job:
            await event.answer("این کار پیدا نشد.", alert=True)
            return
        buttons = []
        if job["status"] in ("leeching", "sending"):
            buttons.append([Button.inline("⛔ توقف",
                                          f"plstop_{job_id}".encode())])
        buttons.append([Button.inline("🔙 کارها", b"pljobs")])
        await safe_edit(event, progress_card(uid, job_id), buttons=buttons)

    register_steps(_STEPS)


# --------------------------------------------------------------------------- #
# Wizard steps
# --------------------------------------------------------------------------- #
async def _step_prefix(event, st):
    uid = event.sender_id
    prefix = _normalize_prefix(event.raw_text or "")
    if len(prefix) < 4 or len(prefix) >= 11:
        await _respond(event, cards.card("⚠️ پیش‌شماره نامعتبر", [
            "بین ۴ تا ۱۰ رقم بفرست، مثل 0912"]))
        return
    st.update({"step": "pl_target", "prefix": prefix})
    _state[uid] = st
    _, _, _ = affine_params(prefix)
    space = 10 ** (11 - len(prefix))
    await _respond(event, cards.card("🎯 هدف", [
        cards.kv("پیش‌شماره", prefix, width=12),
        cards.kv("فضای شماره", cards.num(space), width=12),
        cards.LINE,
        "چند مخاطب می‌خواهی؟ عدد بفرست.",
        f"سهمیه‌ی بررسی امروز: {cards.num(db.probe_budget_left(uid))}",
    ]))


async def _step_target(event, st):
    uid = event.sender_id
    try:
        target = int(float((event.raw_text or "").strip()))
    except (TypeError, ValueError):
        await _respond(event, "عدد نامعتبر بود.")
        return
    if target < 1:
        await _respond(event, "عدد باید بزرگ‌تر از صفر باشد.")
        return
    target = min(target, config.POOL_MAX_TARGET)
    st.update({"step": "pl_mode", "target": target})
    _state[uid] = st
    await _respond(event, cards.card("✉️ محتوای پیام", [
        cards.kv("هدف", cards.num(target), width=12),
        cards.LINE,
        "چه چیزی فرستاده شود؟",
    ]), buttons=[
        [Button.inline("📎 فوروارد پیام مارک‌شده", b"plmode_marker")],
        [Button.inline("✍️ متن", b"plmode_text")],
        _back(b"rbpool"),
    ])


async def _step_text(event, st):
    uid = event.sender_id
    text = (event.raw_text or "").strip()
    if not text:
        await _respond(event, "متن خالی بود.")
        return
    st["content"] = text
    _state[uid] = st
    await _launch(event, st, "text", text)


async def _launch(event, st, mode: str, content: str) -> None:
    uid = event.sender_id
    chosen = list(st.get("pool") or [])
    prefix = st.get("prefix")
    target = int(st.get("target") or 0)
    accounts = [db.get_account(uid, aid) for aid in chosen]
    accounts = [a for a in accounts if a and a["status"] == "active"]
    if len(accounts) < 2 or not prefix or target <= 0:
        await _respond(event, cards.card("⚠️ اطلاعات ناقص", [
            "از اول شروع کن: مغز → مغز استخری."]),
            buttons=[_back(b"rbbrain")])
        return

    width, a, offset = affine_params(prefix)
    job_id = db.pool_create_job(uid, prefix, target, width, a, offset, mode,
                                content, accounts)
    _state.pop(uid, None)
    msg = await event.respond(cards.panel_card("🌊 - #pool_started", [
        cards.kv("Job", f"#{job_id}"),
        cards.kv("Prefix", prefix),
        cards.kv("Target", cards.num(target)),
        cards.kv("Accounts", len(accounts)),
        "⏳ شروع شد ...",
    ]), buttons=[[Button.inline("⛔ توقف", f"plstop_{job_id}".encode())]])
    asyncio.create_task(run_job(uid, job_id, msg))


async def _step_mode(event, st):
    """The mode is chosen with a button, but the customer may well type instead.

    Without this the wizard sits in a step nothing handles and the bot answers
    nothing at all, which is indistinguishable from being broken. Saying "use the
    buttons" costs one line and removes a dead end.
    """
    await _respond(event, cards.card("✉️ محتوای پیام", [
        "با دکمه‌ها انتخاب کن: فوروارد پیام مارک‌شده، یا متن.",
    ]), buttons=[
        [Button.inline("📎 فوروارد پیام مارک‌شده", b"plmode_marker")],
        [Button.inline("✍️ متن", b"plmode_text")],
        _back(b"rbpool"),
    ])


_STEPS = {
    "pl_prefix": _step_prefix,
    "pl_target": _step_target,
    "pl_mode": _step_mode,
    "pl_text": _step_text,
}


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
async def restore_pending() -> None:
    """Resume jobs that were mid-flight when the process died.

    A pool job can represent hundreds of spent probes. Losing it on a restart
    would mean the customer paid the daily allowance for nothing, so an
    unfinished job is picked up rather than abandoned.
    """
    try:
        jobs = db.owner_pool_unfinished()
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="pool restore", notify=False)
        return

    resumed = 0
    for job in jobs:
        # Per job, not around the whole loop: recovery must never be
        # all-or-nothing at boot, or one unreadable row stops every other
        # customer's work from coming back.
        try:
            asyncio.create_task(run_job(job["customer_id"], job["id"]))
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context=f"pool restore job {job['id']}",
                               notify=False)
    if resumed:
        await logbus.event("♻️ - #pool_resumed", [cards.kv("Jobs", resumed)])
