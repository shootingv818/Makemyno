"""
tabchi.py — the group engine, with the secretary living inside it.
=================================================================

TABCHI: the customer supplies group links, the account joins them, and every
interval it posts one of the configured texts into those groups.

SECRETARY: a sub-screen of the same account, because it is the same always-on
shape — a periodic pass over one session. It answers new private chats with a
configured text, once per person.

REBUILT FROM THE BASE PROJECT'S AUTOMATION, MINUS THE PARTS THAT DO NOT BELONG
------------------------------------------------------------------------------
Kept: rotating texts, the account's own group list, join-by-link, the interval,
and auto-muting a group that keeps failing.

Dropped: the SHARED verified-link pool. In the base project a link that one
account joined successfully was added to a pool every other account then joined
from. In a multi-tenant service that pushes one customer's hard-won groups onto
every other customer's accounts, and gets everybody banned in the same groups.
Here a link belongs to the account that is a member of it, full stop.

WHY THE ENGINE LOOKS LIKE THIS
------------------------------
  * One task per account, and each pass claims the session in the busy registry.
    A second connection revokes the session, so a tabchi pass and a send can
    never overlap.
  * Texts are rotated randomly, never the same one twice in a row, so the
    account does not post an identical message on a fixed schedule.
  * A small random pause between groups, so one pass is not a burst.
  * Accounts are staggered, so five accounts never post into the same group in
    the same second.
  * A group that fails repeatedly is muted automatically. Retrying a group that
    kicked us out, every interval, forever, is how an account looks like a bot.
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

# account_id -> {"task": Task, "stop": bool}
_tabchi_tasks: dict = {}
_secretary_tasks: dict = {}


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="rb")


def _pick_text(texts: list, last_index) -> tuple:
    """A random text, avoiding an immediate repeat.

    Posting the identical message on a fixed schedule is the easiest pattern for
    a platform to flag, so rotation is part of the safety story, not decoration.
    """
    if not texts:
        return None, None
    if len(texts) == 1:
        return 0, texts[0]
    index = random.randrange(len(texts))
    if index == last_index:
        index = (index + 1) % len(texts)
    return index, texts[index]


# --------------------------------------------------------------------------- #
# Tabchi engine
# --------------------------------------------------------------------------- #
async def _tabchi_pass(customer_id, acc: dict) -> dict:
    """One pass over an account's joined groups. Returns {sent, failed, muted}."""
    aid, phone = acc["id"], acc["phone"]
    texts = [t["text"] for t in db.tabchi_texts(customer_id, aid)
             if (t.get("text") or "").strip()]
    if not texts:
        return {"sent": 0, "failed": 0, "muted": 0, "reason": "no_texts"}

    groups = db.tabchi_groups(customer_id, aid, joined_only=True)
    if not groups:
        return {"sent": 0, "failed": 0, "muted": 0, "reason": "no_groups"}

    key = _key(customer_id, phone)
    result = {"sent": 0, "failed": 0, "muted": 0, "reason": ""}

    async with busy.hold(key, "tabchi", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            result["reason"] = "busy"
            return result

        w = worker.worker_for_account(acc)
        remote = bool(w and not worker.is_local(w))
        last_index = None

        if remote:
            # The worker rotates and paces on its side; one call per pass keeps
            # the tunnel quiet.
            try:
                res = await worker.api_call(w, "POST", "/groups/send", {
                    "customer_id": customer_id, "phone": phone,
                    "texts": texts,
                    "guids": [g["guid"] for g in groups if g.get("guid")],
                    "delay_min": config.TABCHI_GROUP_DELAY_MIN,
                    "delay_max": config.TABCHI_GROUP_DELAY_MAX}, timeout=1800)
            except Exception as exc:  # noqa: BLE001
                result["reason"] = type(exc).__name__
                return result
            result["sent"] = int(res.get("sent") or 0)
            failures = {f.get("guid") for f in (res.get("failures") or [])}
            for group in groups:
                if group.get("guid") in failures:
                    result["failed"] += 1
                    fails = db.tabchi_group_fail(customer_id, group["id"])
                    if fails >= config.TABCHI_GROUP_MAX_FAILS:
                        result["muted"] += 1
                elif group.get("guid"):
                    db.tabchi_group_ok(customer_id, group["id"])
            return result

        import account_conn
        for group in groups:
            guid = group.get("guid")
            if not guid:
                continue
            last_index, text = _pick_text(texts, last_index)

            async def _one(client, g=guid, t=text):
                return await rb.send_text(client, g, t)

            try:
                await account_conn.call(customer_id, phone, _one,
                                        timeout=config.SEND_TIMEOUT)
                result["sent"] += 1
                db.tabchi_group_ok(customer_id, group["id"])
            except account_conn.InvalidAuthError:
                result["reason"] = "auth_failed"
                return result
            except Exception:      # noqa: BLE001
                result["failed"] += 1
                fails = db.tabchi_group_fail(customer_id, group["id"])
                if fails >= config.TABCHI_GROUP_MAX_FAILS:
                    result["muted"] += 1
            await asyncio.sleep(random.uniform(config.TABCHI_GROUP_DELAY_MIN,
                                               config.TABCHI_GROUP_DELAY_MAX))
    return result


async def _tabchi_loop(customer_id, account_id) -> None:
    """The always-on loop for one account."""
    control = _tabchi_tasks.setdefault(account_id, {})
    # Stagger the start so several accounts of one customer never fire together.
    if config.TABCHI_ACCOUNT_STAGGER > 0:
        await asyncio.sleep(random.uniform(0, float(config.TABCHI_ACCOUNT_STAGGER)))
    while True:
        try:
            row = db.tabchi_get(customer_id, account_id)
            if not row.get("enabled") or control.get("stop"):
                return
            acc = db.get_account(customer_id, account_id)
            if not acc or acc["status"] != "active":
                db.tabchi_set(customer_id, account_id, enabled=False)
                return
            if db.are_sends_frozen():
                await asyncio.sleep(60)
                continue

            result = await _tabchi_pass(customer_id, acc)
            if result["sent"]:
                db.tabchi_incr_sent(customer_id, account_id, result["sent"])
                db.usage_incr(customer_id, "tabchi", result["sent"])
            db.tabchi_set(customer_id, account_id, last_run=cards.now())

            if result.get("reason") == "auth_failed":
                db.tabchi_set(customer_id, account_id, enabled=False)
                await logbus.event("🔴 - #tabchi_stopped", [
                    cards.kv("Customer", customer_id),
                    cards.kv("Phone", acc["phone"]),
                    cards.kv("Reason", "session invalid"),
                ])
                return
            if result["muted"]:
                await logbus.warn("tabchi_group_muted", [
                    cards.kv("Customer", customer_id),
                    cards.kv("Phone", acc["phone"]),
                    cards.kv("Muted", result["muted"]),
                ])

            interval = int(row.get("interval_sec")
                           or config.TABCHI_DEFAULT_INTERVAL)
            await asyncio.sleep(max(config.TABCHI_MIN_INTERVAL, interval))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context=f"tabchi loop {account_id}",
                               customer=customer_id, notify=False)
            await asyncio.sleep(60)


def start_tabchi(customer_id, account_id) -> None:
    control = _tabchi_tasks.get(account_id)
    if control and control.get("task") and not control["task"].done():
        return
    control = {"stop": False}
    _tabchi_tasks[account_id] = control
    control["task"] = asyncio.create_task(_tabchi_loop(customer_id, account_id))


async def stop_tabchi(account_id) -> None:
    control = _tabchi_tasks.pop(account_id, None)
    if not control:
        return
    control["stop"] = True
    task = control.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# --------------------------------------------------------------------------- #
# Secretary engine
# --------------------------------------------------------------------------- #
async def _secretary_pass(customer_id, acc: dict) -> dict:
    """Answer private chats we have not answered yet."""
    aid, phone = acc["id"], acc["phone"]
    row = db.secretary_get(customer_id, aid)
    mode = row.get("mode") or "text"
    text = row.get("text") or ""
    marker = db.get_marker(customer_id)
    if mode == "text" and not text.strip():
        return {"replied": 0, "reason": "no_text"}

    key = _key(customer_id, phone)
    result = {"replied": 0, "reason": ""}

    async with busy.hold(key, "secretary", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            result["reason"] = "busy"
            return result

        w = worker.worker_for_account(acc)
        remote = bool(w and not worker.is_local(w))

        if remote:
            # The already-replied ledger lives on the master because it must
            # survive a worker restart; the worker is told who to skip.
            try:
                res = await worker.api_call(w, "POST", "/secretary/pass", {
                    "customer_id": customer_id, "phone": phone,
                    "mode": mode, "text": text, "marker": marker,
                    "skip": _replied_guids(customer_id, aid),
                    "delay": config.SECRETARY_REPLY_DELAY}, timeout=1800)
            except Exception as exc:  # noqa: BLE001
                result["reason"] = type(exc).__name__
                return result
            for guid in res.get("replied") or []:
                db.secretary_mark_replied(customer_id, aid, guid)
                result["replied"] += 1
            return result

        import account_conn

        async def _chats(client):
            return await rb.get_chats_user_guids(client)

        try:
            guids = await account_conn.call(customer_id, phone, _chats,
                                            timeout=180) or []
        except account_conn.InvalidAuthError:
            result["reason"] = "auth_failed"
            return result

        from_guid = message_id = None
        if mode == "marker":
            async def _find(client):
                return (await rb.get_self_guid(client),
                        await rb.find_marked_message(client, marker))
            from_guid, found = await account_conn.call(customer_id, phone,
                                                      _find, timeout=120)
            if not found:
                result["reason"] = "no_marker"
                return result
            message_id = rb._msg_id_of(found)      # noqa: SLF001

        for guid in guids:
            if db.secretary_was_replied(customer_id, aid, guid):
                continue
            if mode == "marker":
                async def _one(client, g=guid):
                    return await rb.forward_message(client, from_guid, g,
                                                    message_id)
            else:
                async def _one(client, g=guid):
                    return await rb.send_text(client, g, text)
            try:
                await account_conn.call(customer_id, phone, _one, timeout=60)
                db.secretary_mark_replied(customer_id, aid, guid)
                result["replied"] += 1
            except account_conn.InvalidAuthError:
                result["reason"] = "auth_failed"
                return result
            except Exception:      # noqa: BLE001
                continue
            await asyncio.sleep(config.SECRETARY_REPLY_DELAY)
    return result


def _replied_guids(customer_id, account_id, limit: int = 2000) -> list:
    """Who this account has already answered, so a worker knows who to skip.

    Capped, because the payload travels over the tunnel on every pass. The most
    recent entries are the ones that matter: an old chat that resurfaces is
    indistinguishable from a new one anyway, and answering it twice a year apart
    is not the failure worth paying unbounded bandwidth to prevent.
    """
    return db.secretary_replied_recent(customer_id, account_id, limit)


async def _secretary_loop(customer_id, account_id) -> None:
    control = _secretary_tasks.setdefault(account_id, {})
    await asyncio.sleep(random.uniform(0, 15))
    while True:
        try:
            row = db.secretary_get(customer_id, account_id)
            if not row.get("enabled") or control.get("stop"):
                return
            acc = db.get_account(customer_id, account_id)
            if not acc or acc["status"] != "active":
                db.secretary_set(customer_id, account_id, enabled=False)
                return

            result = await _secretary_pass(customer_id, acc)
            if result["replied"]:
                db.secretary_incr(customer_id, account_id, result["replied"])
                db.usage_incr(customer_id, "secretary", result["replied"])
            if result.get("reason") == "auth_failed":
                db.secretary_set(customer_id, account_id, enabled=False)
                return

            interval = int(row.get("interval_sec") or config.SECRETARY_INTERVAL)
            await asyncio.sleep(max(config.SECRETARY_MIN_INTERVAL, interval))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context=f"secretary loop {account_id}",
                               customer=customer_id, notify=False)
            await asyncio.sleep(60)


def start_secretary(customer_id, account_id) -> None:
    control = _secretary_tasks.get(account_id)
    if control and control.get("task") and not control["task"].done():
        return
    control = {"stop": False}
    _secretary_tasks[account_id] = control
    control["task"] = asyncio.create_task(_secretary_loop(customer_id, account_id))


async def stop_secretary(account_id) -> None:
    control = _secretary_tasks.pop(account_id, None)
    if not control:
        return
    control["stop"] = True
    task = control.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# --------------------------------------------------------------------------- #
# Joining groups from the link list
# --------------------------------------------------------------------------- #
async def _join_groups(customer_id, acc: dict, msg=None) -> dict:
    """Join every not-yet-joined link on this account's own list."""
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    pending = [g for g in db.tabchi_groups(customer_id, aid) if not g["joined"]]
    result = {"joined": 0, "failed": 0, "total": len(pending)}
    if not pending:
        return result

    async with busy.hold(key, "join", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            result["reason"] = "busy"
            return result

        w = worker.worker_for_account(acc)
        remote = bool(w and not worker.is_local(w))
        import account_conn

        for group in pending:
            link = group.get("link") or ""
            try:
                if remote:
                    res = await worker.api_call(w, "POST", "/group/join", {
                        "customer_id": customer_id, "phone": phone,
                        "link": link}, timeout=180)
                    guid = res.get("guid") or ""
                else:
                    async def _one(client, lnk=link):
                        return await rb.join_group_by_link(client, lnk)
                    raw = await account_conn.call(customer_id, phone, _one,
                                                  timeout=120)
                    guid = rb.join_result_group_guid(raw) or ""
                if guid:
                    db.tabchi_group_joined(customer_id, group["id"], guid)
                    result["joined"] += 1
                else:
                    result["failed"] += 1
            except account_conn.InvalidAuthError:
                result["reason"] = "auth_failed"
                break
            except Exception:      # noqa: BLE001
                result["failed"] += 1
            await asyncio.sleep(config.GROUP_JOIN_DELAY)

    await logbus.customer_action(db.get_customer(customer_id), "tabchi_join", [
        cards.kv("Phone", phone),
        cards.kv("Joined", result["joined"]),
        cards.kv("Failed", result["failed"]),
    ], platform="Rubika")

    if msg is not None:
        try:
            await msg.edit(cards.panel_card("🔗 - #group_join", [
                cards.kv("Phone", phone),
                cards.kv("Joined", result["joined"]),
                cards.kv("Failed", result["failed"]),
                cards.kv("Total tried", result["total"]),
            ]), buttons=[[Button.inline("🔗 لیست گروه‌ها",
                                        f"tbgroups_{aid}".encode())]])
        except Exception:
            pass
    return result


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def section_card(customer_id) -> str:
    accounts = db.list_accounts(customer_id)
    on = len(db.tabchi_enabled_accounts(customer_id))
    sec_on = len(db.secretary_enabled_accounts(customer_id))
    return cards.card("📢 تبچی", [
        cards.kv("Accounts", len(accounts)),
        cards.kv("Tabchi on", f"{on}/{len(accounts)}"),
        cards.kv("Secretary on", f"{sec_on}/{len(accounts)}"),
        cards.LINE,
        "لینک گروه‌ها را می‌دهی، اکانت عضو می‌شود، و هر بازه‌ای",
        "که تعیین کنی متن‌ها را در گروه‌ها می‌فرستد.",
    ])


def account_card(customer_id, acc: dict) -> str:
    aid = acc["id"]
    row = db.tabchi_get(customer_id, aid)
    sec = db.secretary_get(customer_id, aid)
    texts = db.tabchi_texts(customer_id, aid)
    groups = db.tabchi_groups(customer_id, aid)
    joined = [g for g in groups if g["joined"]]
    muted = [g for g in groups if g["muted"]]
    rows = [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Status", "🟢 ON" if row.get("enabled") else "⚪️ OFF"),
        cards.kv("Interval", f"{row.get('interval_sec')} seconds"),
        cards.kv("Texts", len(texts)),
        cards.kv("Groups", f"{len(joined)} joined / {len(groups)} listed"
                 + (f" · {len(muted)} muted" if muted else "")),
        cards.kv("Total sent", cards.num(row.get("sent_total") or 0)),
        cards.kv("Last run", (row.get("last_run") or "—")[:16]),
        cards.LINE,
        cards.kv("Secretary", ("🟢 ON" if sec.get("enabled") else "⚪️ OFF")
                 + f"  ({'فوروارد مارکر' if sec.get('mode') == 'marker' else 'متن'})"),
        cards.kv("Replied", cards.num(sec.get("replied_total") or 0)),
    ]
    return cards.panel_card("📢 - #tabchi_account", rows)


def account_buttons(customer_id, acc: dict) -> list:
    aid = acc["id"]
    row = db.tabchi_get(customer_id, aid)
    toggle = ("⏹ خاموش کردن" if row.get("enabled") else "▶️ روشن کردن")
    return [
        [Button.inline("➕ افزودن متن", f"tbtext_{aid}".encode()),
         Button.inline("🗑 پاک کردن متن‌ها", f"tbtclear_{aid}".encode())],
        [Button.inline("🔗 لیست گروه‌ها", f"tbgroups_{aid}".encode())],
        [Button.inline("⏱ تنظیم بازه", f"tbint_{aid}".encode()),
         Button.inline(toggle, f"tbtog_{aid}".encode())],
        [Button.inline("🤖 منشی", f"tbsec_{aid}".encode())],
        [Button.inline("📋 اعمال روی همه‌ی اکانت‌ها", f"tbapply_{aid}".encode())],
        [Button.inline("🔙 تبچی", b"tabchi")],
    ]


def groups_card(customer_id, acc: dict) -> str:
    groups = db.tabchi_groups(customer_id, acc["id"])
    rows = [cards.kv("Phone", acc["phone"]),
            cards.kv("Listed", len(groups)), cards.LINE]
    if groups:
        for group in groups[:25]:
            if group["muted"]:
                mark = "🔇"
            elif group["joined"]:
                mark = "🟢"
            else:
                mark = "▫️"
            label = group.get("name") or group.get("link") or "—"
            rows.append(f"{mark} {label[:44]}")
    else:
        rows.append("هنوز لینکی اضافه نشده.")
    rows += [cards.LINE,
             "🟢 عضو شده · ▫️ عضو نشده · 🔇 خفه‌شده (چند بار خطا داد)"]
    return cards.panel_card("🔗 - #tabchi_groups", rows)


def secretary_card(customer_id, acc: dict) -> str:
    sec = db.secretary_get(customer_id, acc["id"])
    return cards.panel_card("🤖 - #secretary", [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Status", "🟢 ON" if sec.get("enabled") else "⚪️ OFF"),
        cards.kv("Mode", "📎 فوروارد مارکر" if sec.get("mode") == "marker"
                 else "✍️ متن"),
        cards.kv("Text", f"«{(sec.get('text') or '—')[:60]}»"),
        cards.kv("Interval", f"{sec.get('interval_sec')} seconds"),
        cards.kv("Replied", cards.num(sec.get("replied_total") or 0)),
        cards.LINE,
        "به هر نفر فقط یک بار جواب می‌دهد.",
    ])


# --------------------------------------------------------------------------- #
# Handler registration
# --------------------------------------------------------------------------- #
def setup(bot, state, gate, safe_edit, respond, register_steps) -> None:
    global _bot, _state, _gate, _safe_edit, _respond
    _bot, _state, _gate, _safe_edit, _respond = bot, state, gate, safe_edit, respond

    from telethon import events

    async def _account_or_answer(event, aid):
        acc = db.get_account(event.sender_id, aid)
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
        return acc

    @bot.on(events.CallbackQuery(data=b"tabchi"))
    async def tabchi_home(event):
        if not await gate(event):
            return
        uid = event.sender_id
        state.pop(uid, None)
        accounts = db.list_accounts(uid)
        rows = []
        for acc in accounts[:config.ACC_PAGE_SIZE]:
            row = db.tabchi_get(uid, acc["id"])
            sec = db.secretary_get(uid, acc["id"])
            mark = "🟢" if row.get("enabled") else "⚪️"
            sec_mark = "🤖" if sec.get("enabled") else ""
            rows.append([Button.inline(
                f"{mark} {acc['phone']} {sec_mark}".strip(),
                f"tbacc_{acc['id']}".encode())])
        rows.append(_back(b"rb"))
        body = section_card(uid) if accounts else cards.card("📢 تبچی", [
            "اکانت فعالی نداری."])
        await safe_edit(event, body, buttons=rows)

    @bot.on(events.CallbackQuery(pattern=rb"tbacc_(\d+)"))
    async def tabchi_account(event):
        if not await gate(event):
            return
        acc = await _account_or_answer(event, int(event.pattern_match.group(1)))
        if not acc:
            return
        await safe_edit(event, account_card(event.sender_id, acc),
                        buttons=account_buttons(event.sender_id, acc))

    @bot.on(events.CallbackQuery(pattern=rb"tbtog_(\d+)"))
    async def tabchi_toggle(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        row = db.tabchi_get(uid, aid)
        turning_on = not row.get("enabled")
        if turning_on:
            if not db.tabchi_texts(uid, aid):
                await event.answer("اول حداقل یک متن اضافه کن.", alert=True)
                return
            if not db.tabchi_groups(uid, aid, joined_only=True):
                await event.answer("اول لینک گروه اضافه کن و عضو شو.",
                                   alert=True)
                return
        db.tabchi_set(uid, aid, enabled=turning_on)
        if turning_on:
            start_tabchi(uid, aid)
        else:
            await stop_tabchi(aid)
        await logbus.customer_action(db.get_customer(uid), "tabchi_toggled", [
            cards.kv("Phone", acc["phone"]),
            cards.kv("State", "ON" if turning_on else "OFF"),
        ], platform="Rubika")
        await safe_edit(event, account_card(uid, acc),
                        buttons=account_buttons(uid, acc))

    @bot.on(events.CallbackQuery(pattern=rb"tbtext_(\d+)"))
    async def tabchi_add_text(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        state[event.sender_id] = {"step": "tb_text", "account_id": aid}
        await safe_edit(event, cards.card("➕ افزودن متن", [
            "متنی که در گروه‌ها فرستاده می‌شود را بفرست.",
            "چند متن اضافه کن تا هر بار یکی تصادفی انتخاب شود",
            "و پیام تکراری نفرستد.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"tbacc_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"tbtclear_(\d+)"))
    async def tabchi_clear_texts(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        removed = db.tabchi_clear_texts(uid, aid)
        await event.answer(f"{removed} متن پاک شد.", alert=True)
        acc = db.get_account(uid, aid)
        if acc:
            await safe_edit(event, account_card(uid, acc),
                            buttons=account_buttons(uid, acc))

    @bot.on(events.CallbackQuery(pattern=rb"tbint_(\d+)"))
    async def tabchi_interval(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        state[event.sender_id] = {"step": "tb_interval", "account_id": aid}
        await safe_edit(event, cards.card("⏱ تنظیم بازه", [
            f"عدد بین {config.TABCHI_MIN_INTERVAL} و "
            f"{config.TABCHI_MAX_INTERVAL} ثانیه بفرست.",
            "مثال: 1800 یعنی هر نیم ساعت.",
            "بازه‌ی کوتاه ریسک محدود شدن اکانت را بالا می‌برد.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"tbacc_{aid}".encode())]])

    # ---- groups -------------------------------------------------------- #
    @bot.on(events.CallbackQuery(pattern=rb"tbgroups_(\d+)"))
    async def tabchi_groups(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        groups = db.tabchi_groups(uid, aid)
        pending = [g for g in groups if not g["joined"]]
        muted = [g for g in groups if g["muted"]]
        buttons = [[Button.inline("➕ افزودن لینک", f"tbgadd_{aid}".encode())]]
        if pending:
            buttons.append([Button.inline(f"✅ عضو شدن ({len(pending)})",
                                          f"tbgjoin_{aid}".encode())])
        if muted:
            buttons.append([Button.inline(f"🔊 رفع خفگی ({len(muted)})",
                                          f"tbgunmute_{aid}".encode())])
        buttons.append([Button.inline("🗑 پاک کردن لیست",
                                      f"tbgclear_{aid}".encode())])
        buttons.append([Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())])
        await safe_edit(event, groups_card(uid, acc), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"tbgadd_(\d+)"))
    async def tabchi_group_add(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        state[event.sender_id] = {"step": "tb_link", "account_id": aid}
        await safe_edit(event, cards.card("➕ افزودن لینک گروه", [
            "لینک گروه را بفرست. چند لینک را می‌توانی هر کدام",
            "در یک خط بفرستی.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"tbgroups_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"tbgjoin_(\d+)"))
    async def tabchi_group_join(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await event.answer(busy.reason(key), alert=True)
            return
        msg = await safe_edit(event, cards.card("🔗 عضو شدن در گروه‌ها", [
            cards.kv("Phone", acc["phone"]), "⏳ شروع شد ..."]))
        asyncio.create_task(_join_groups(uid, acc, msg))

    @bot.on(events.CallbackQuery(pattern=rb"tbgunmute_(\d+)"))
    async def tabchi_group_unmute(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        restored = db.tabchi_unmute_all(uid, aid)
        await event.answer(f"{restored} گروه دوباره فعال شد.", alert=True)
        acc = db.get_account(uid, aid)
        if acc:
            await safe_edit(event, groups_card(uid, acc), buttons=[
                [Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"tbgclear_(\d+)"))
    async def tabchi_group_clear(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        removed = db.tabchi_clear_groups(uid, aid)
        await event.answer(f"{removed} گروه از لیست پاک شد.", alert=True)
        acc = db.get_account(uid, aid)
        if acc:
            await safe_edit(event, groups_card(uid, acc), buttons=[
                [Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())]])

    # ---- secretary ----------------------------------------------------- #
    async def _render_secretary(event, aid):
        """Draw the secretary screen.

        Separate from the handler so the toggle and the mode switch can redraw
        without re-entering the gate — going through the handler a second time
        would charge the customer two actions against their rate limit for one
        tap.
        """
        uid = event.sender_id
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        sec = db.secretary_get(uid, aid)
        toggle = ("⏹ خاموش کردن" if sec.get("enabled") else "▶️ روشن کردن")
        await safe_edit(event, secretary_card(uid, acc), buttons=[
            [Button.inline("✍️ تنظیم متن", f"tbsectext_{aid}".encode()),
             Button.inline("⏱ بازه", f"tbsecint_{aid}".encode())],
            [Button.inline("🔀 تغییر حالت", f"tbsecmode_{aid}".encode()),
             Button.inline(toggle, f"tbsectog_{aid}".encode())],
            [Button.inline("📋 اعمال روی همه", f"tbsecapply_{aid}".encode())],
            [Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())],
        ])

    @bot.on(events.CallbackQuery(pattern=rb"tbsec_(\d+)"))
    async def secretary_screen(event):
        if not await gate(event):
            return
        await _render_secretary(event, int(event.pattern_match.group(1)))

    @bot.on(events.CallbackQuery(pattern=rb"tbsectog_(\d+)"))
    async def secretary_toggle(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        sec = db.secretary_get(uid, aid)
        turning_on = not sec.get("enabled")
        if turning_on and (sec.get("mode") or "text") == "text" \
                and not (sec.get("text") or "").strip():
            await event.answer("اول متن پاسخ را تنظیم کن.", alert=True)
            return
        db.secretary_set(uid, aid, enabled=turning_on)
        if turning_on:
            start_secretary(uid, aid)
        else:
            await stop_secretary(aid)
        await logbus.customer_action(db.get_customer(uid), "secretary_toggled", [
            cards.kv("Phone", acc["phone"]),
            cards.kv("State", "ON" if turning_on else "OFF"),
        ], platform="Rubika")
        await _render_secretary(event, aid)

    @bot.on(events.CallbackQuery(pattern=rb"tbsecmode_(\d+)"))
    async def secretary_mode(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        sec = db.secretary_get(uid, aid)
        new_mode = "text" if (sec.get("mode") or "text") == "marker" else "marker"
        db.secretary_set(uid, aid, mode=new_mode)
        await _render_secretary(event, aid)

    @bot.on(events.CallbackQuery(pattern=rb"tbsectext_(\d+)"))
    async def secretary_text(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        state[event.sender_id] = {"step": "tb_sectext", "account_id": aid}
        await safe_edit(event, cards.card("✍️ متن پاسخ منشی", [
            "متنی که به پیوی‌های جدید فرستاده می‌شود را بفرست.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"tbsec_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"tbsecint_(\d+)"))
    async def secretary_interval(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        state[event.sender_id] = {"step": "tb_secint", "account_id": aid}
        await safe_edit(event, cards.card("⏱ بازه‌ی منشی", [
            f"عدد بین {config.SECRETARY_MIN_INTERVAL} و "
            f"{config.SECRETARY_MAX_INTERVAL} ثانیه بفرست.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"tbsec_{aid}".encode())]])

    # ---- apply to all -------------------------------------------------- #
    @bot.on(events.CallbackQuery(pattern=rb"tbapply_(\d+)"))
    async def tabchi_apply(event):
        """Copy this account's texts and interval onto every other account.

        Without this a customer with twenty accounts retypes the same text twenty
        times. Group links are NOT copied: a link list belongs to the account
        that is actually a member of those groups.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        copied = db.tabchi_apply_to_all(uid, aid)
        await logbus.customer_action(db.get_customer(uid), "tabchi_apply_to_all", [
            cards.kv("Source", acc["phone"]),
            cards.kv("Applied to", copied),
        ], platform="Rubika")
        await safe_edit(event, cards.card("📋 اعمال شد", [
            cards.kv("From", acc["phone"], width=10),
            cards.kv("Accounts", copied, width=10),
            cards.LINE,
            "متن‌ها و بازه روی بقیه‌ی اکانت‌ها اعمال شد.",
            "لینک گروه‌ها کپی نشد، چون هر اکانت عضو گروه‌های خودش است.",
        ]), buttons=[[Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"tbsecapply_(\d+)"))
    async def secretary_apply(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = await _account_or_answer(event, aid)
        if not acc:
            return
        copied = db.secretary_apply_to_all(uid, aid)
        await safe_edit(event, cards.card("📋 اعمال شد", [
            cards.kv("From", acc["phone"], width=10),
            cards.kv("Accounts", copied, width=10),
            "تنظیمات منشی روی بقیه‌ی اکانت‌ها اعمال شد.",
        ]), buttons=[[Button.inline("🔙 منشی", f"tbsec_{aid}".encode())]])

    register_steps(_STEPS)


# --------------------------------------------------------------------------- #
# Wizard steps
# --------------------------------------------------------------------------- #
async def _step_text(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    text = (event.raw_text or "").strip()
    if not text:
        await _respond(event, "متن خالی بود.")
        return
    db.tabchi_add_text(uid, aid, text)
    count = len(db.tabchi_texts(uid, aid))
    await logbus.customer_action(db.get_customer(uid), "tabchi_text_added", [
        cards.kv("Text", text[:120]), cards.kv("Total texts", count),
    ], platform="Rubika")
    await _respond(event, cards.card("✅ اضافه شد", [
        cards.kv("متن‌ها", count, width=8)]),
        buttons=[[Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())]])


async def _step_link(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    added = skipped = 0
    for raw in (event.raw_text or "").splitlines():
        link = raw.strip()
        if not link or len(link) < 8:
            continue
        if db.tabchi_add_group(uid, aid, link):
            added += 1
        else:
            skipped += 1
    if not added and not skipped:
        await _respond(event, "لینک معتبری پیدا نشد.")
        return
    await _respond(event, cards.card("✅ ثبت شد", [
        cards.kv("Added", added, width=10),
        cards.kv("Duplicate", skipped, width=10),
        "برای عضو شدن، «عضو شدن» را بزن.",
    ]), buttons=[[Button.inline("🔗 لیست گروه‌ها", f"tbgroups_{aid}".encode())]])


async def _step_interval(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    try:
        value = int(float((event.raw_text or "").strip()))
    except (TypeError, ValueError):
        await _respond(event, "عدد نامعتبر بود.")
        return
    db.tabchi_set(uid, aid, interval_sec=value)
    applied = db.tabchi_get(uid, aid)["interval_sec"]
    await _respond(event, cards.card("✅ ثبت شد", [
        cards.kv("بازه", f"{applied} ثانیه", width=8)]),
        buttons=[[Button.inline("🔙 اکانت", f"tbacc_{aid}".encode())]])


async def _step_sectext(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    text = (event.raw_text or "").strip()
    if not text:
        await _respond(event, "متن خالی بود.")
        return
    db.secretary_set(uid, aid, text=text)
    await _respond(event, cards.card("✅ ثبت شد", [
        cards.kv("متن منشی", text[:100], width=10)]),
        buttons=[[Button.inline("🔙 منشی", f"tbsec_{aid}".encode())]])


async def _step_secint(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    try:
        value = int(float((event.raw_text or "").strip()))
    except (TypeError, ValueError):
        await _respond(event, "عدد نامعتبر بود.")
        return
    db.secretary_set(uid, aid, interval_sec=value)
    applied = db.secretary_get(uid, aid)["interval_sec"]
    await _respond(event, cards.card("✅ ثبت شد", [
        cards.kv("بازه", f"{applied} ثانیه", width=8)]),
        buttons=[[Button.inline("🔙 منشی", f"tbsec_{aid}".encode())]])


_STEPS = {
    "tb_text": _step_text,
    "tb_link": _step_link,
    "tb_interval": _step_interval,
    "tb_sectext": _step_sectext,
    "tb_secint": _step_secint,
}


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
async def restore_engines() -> None:
    """Relaunch every enabled tabchi and secretary after a restart.

    These are always-on features: a customer who switched tabchi on expects it to
    keep running across a service restart, and silently not restarting looks
    exactly like the feature being broken.
    """
    started_tabchi = started_secretary = 0
    try:
        for row in db.owner_tabchi_enabled():
            start_tabchi(row["customer_id"], row["account_id"])
            started_tabchi += 1
        for row in db.owner_secretary_enabled():
            start_secretary(row["customer_id"], row["account_id"])
            started_secretary += 1
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="restore engines", notify=False)
        return
    if started_tabchi or started_secretary:
        await logbus.event("♻️ - #engines_restored", [
            cards.kv("Tabchi", started_tabchi),
            cards.kv("Secretary", started_secretary),
        ])


async def stop_all() -> None:
    for aid in list(_tabchi_tasks.keys()):
        await stop_tabchi(aid)
    for aid in list(_secretary_tasks.keys()):
        await stop_secretary(aid)


def running() -> dict:
    """What is live right now (diagnostics)."""
    return {
        "tabchi": [aid for aid, c in _tabchi_tasks.items()
                   if c.get("task") and not c["task"].done()],
        "secretary": [aid for aid, c in _secretary_tasks.items()
                      if c.get("task") and not c["task"].done()],
    }
