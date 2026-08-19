"""
rubika_panel.py — the Rubika section of the customer bot.
========================================================

Registered into the shared customer bot by setup(). Every screen here is scoped
to the calling customer, and every operation that opens a session goes through
busy.hold() first.

THE ONE RULE THAT SHAPES THIS WHOLE FILE
----------------------------------------
Rubika allows exactly one live connection per session. A second connection is
answered with AUTH_FROM_ANOTHER and the session is revoked. So nothing here
opens a session without claiming it in the registry, and nothing claims it
without releasing it in a finally block.

That is also why every long job re-registers itself on resume (restore_pending):
the registry lives in memory, jobs survive a restart, and an unregistered job is
invisible to the health engine — which would then connect on top of it.
"""
from __future__ import annotations

import asyncio
import os
import random

from telethon import Button

import busy
import cards
import config
import db
import logbus
import rubika_client as rb
import worker

# Injected by setup() so this module never imports the bot (which would be a
# circular import) and never reaches past the gate.
_bot = None
_state: dict = {}
_gate = None
_safe_edit = None
_respond = None

# Live job controls: account_id -> {"stop": bool, "pause": bool, ...}
_jobs: dict = {}

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "rb_media")
os.makedirs(MEDIA_DIR, exist_ok=True)


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="rb")


async def _busy_answer(event, key: str) -> None:
    """Tell the customer WHY nothing happened.

    A button that silently does nothing is the most confusing possible outcome,
    and it is what the base project did whenever an account was already in use.
    """
    await event.answer(busy.reason(key) or "این اکانت الان مشغول است.", alert=True)


def _norm_pairs(text: str) -> list:
    """Parse a numbers file: one per line, optional ',name'."""
    pairs = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        name = ""
        if "," in line:
            number, _, name = line.partition(",")
        elif "\t" in line:
            number, _, name = line.partition("\t")
        else:
            number = line
        digits = "".join(ch for ch in number if ch.isdigit())
        if len(digits) >= 10:
            pairs.append([digits, name.strip()])
    return pairs


# --------------------------------------------------------------------------- #
# Section menu
# --------------------------------------------------------------------------- #
def menu_card(customer_id) -> str:
    counts = db.count_accounts(customer_id)
    sent = sum(a.get("sent_total", 0) for a in db.list_accounts(customer_id))
    marker = db.get_marker(customer_id)
    return cards.card("🟣 Rubika", [
        cards.kv("Accounts", f"{counts['total']}  ({counts['healthy']} healthy)"),
        cards.kv("Total Sent", cards.num(sent)),
        cards.kv("Marker", f"«{marker}»"),
        cards.kv("Speed", f"{db.get_delay(customer_id)}s"),
        cards.kv("Probe budget", f"{cards.num(db.probe_budget_left(customer_id))}"
                                 f" / {cards.num(config.PROBE_DAILY_CAP)} امروز"),
    ])


def menu_buttons() -> list:
    return [
        [Button.inline("🚀 ارسال", b"rbsend"),
         Button.inline("➕ افزودن اکانت", b"rbadd")],
        [Button.inline("👤 اکانت‌ها", b"rbaccs"),
         Button.inline("📌 محتوا", b"rbcontent")],
        [Button.inline("📤 ارسال چنداکانتی", b"rbmulti"),
         Button.inline("🧠 مغز", b"rbbrain")],
        [Button.inline("➕ افزودن مخاطب", b"rbcontacts"),
         Button.inline("🔎 کشف مخاطب", b"rbdiscover")],
        [Button.inline("📢 تبچی", b"tabchi"),
         Button.inline("🖼 آرشیو عکس (PDF)", b"rbpdf")],
        [Button.inline("⚙️ تنظیمات", b"rbsettings")],
        [Button.inline("🏠 منوی اصلی", b"home")],
    ]


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def _acc_label(acc: dict) -> str:
    mark = "🟢" if acc["status"] == "active" else "🔴"
    return (f"{mark} {acc['phone']} · 📇{acc.get('contacts', 0)} · "
            f"✉️{cards.num(acc.get('sent_total', 0))}")


async def _render_accounts(event, page: int = 0):
    uid = event.sender_id
    accounts = db.list_accounts(uid)
    if not accounts:
        await _safe_edit(event, cards.card("👤 اکانت‌های روبیکا", [
            "هنوز اکانتی اضافه نکردی.",
        ]), buttons=[[Button.inline("➕ افزودن اکانت", b"rbadd")], _back(b"rb")])
        return
    page_items, nav, page, total = cards.paginate(accounts, page, "rbapage_",
                                                  Button)
    counts = db.count_accounts(uid)
    head = cards.card("👤 اکانت‌های روبیکا", [
        cards.kv("Total", f"{counts['total']}  ({counts['healthy']} healthy)"),
        cards.kv("Page", f"{page + 1}/{total}"),
    ])
    rows = [[Button.inline(_acc_label(a), f"rbacc_{a['id']}".encode())]
            for a in page_items]
    if nav:
        rows.append(nav)
    rows.append([Button.inline("🩺 بررسی سلامت همه", b"rbhealth")])
    rows.append([Button.inline("➕ افزودن اکانت", b"rbadd")])
    rows.append(_back(b"rb"))
    await _safe_edit(event, head, buttons=rows)


def _account_card(customer_id, acc: dict) -> str:
    w = worker.worker_for_account(acc)
    key = _key(customer_id, acc["phone"])
    holder = busy.who(key)
    rows = [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Name", acc.get("name") or "—"),
        cards.kv("Status", "🟢 active" if acc["status"] == "active"
                 else f"🔴 {acc['status']}"),
        cards.kv("Contacts", cards.num(acc.get("contacts", 0))),
        cards.kv("Sent", cards.num(acc.get("sent_total", 0))),
        cards.kv("Server", (w or {}).get("tag", "—")),
        cards.kv("Added", (acc.get("added_at") or "—")[:16]),
    ]
    if holder:
        rows.append(cards.kv("Busy", busy.label(holder.get("what"))))
    return cards.panel_card("📱 - #account", rows)


def _account_buttons(acc: dict) -> list:
    aid = acc["id"]
    rows = [
        [Button.inline("🚀 ارسال", f"rbrun_{aid}".encode()),
         Button.inline("📢 ارسال کانالی", f"rbchan_{aid}".encode())],
        [Button.inline("📥 گرفتن مخاطبین", f"rbexport_{aid}".encode()),
         Button.inline("➕ افزودن مخاطب", f"rbcadd_{aid}".encode())],
        [Button.inline("🖼 آرشیو عکس", f"rbpdfrun_{aid}".encode()),
         Button.inline("🔑 توکن سشن", f"rbtoken_{aid}".encode())],
        [Button.inline("🔄 ریست لیست ارسال", f"rbreset_{aid}".encode())],
    ]
    if acc["status"] != "active":
        rows.insert(0, [Button.inline("🔑 ورود مجدد", f"rbrelogin_{aid}".encode())])
    rows.append([Button.inline("🗑 حذف اکانت", f"rbdel_{aid}".encode()),
                 Button.inline("🔙 اکانت‌ها", b"rbaccs")])
    return rows


# --------------------------------------------------------------------------- #
# Pre-send health check
# --------------------------------------------------------------------------- #
def precheck(customer_id, account_ids: list) -> dict:
    """Look before a big send: which accounts are dead, busy, or fine.

    Cheap, because it reads the stored status and the registry rather than
    connecting — connecting to check is exactly what kills sessions.
    """
    ready, dead, occupied = [], [], []
    for aid in account_ids:
        acc = db.get_account(customer_id, aid)
        if not acc:
            continue
        if acc["status"] != "active":
            dead.append(acc)
            continue
        if busy.is_busy(_key(customer_id, acc["phone"])):
            occupied.append(acc)
            continue
        ready.append(acc)
    return {"ready": ready, "dead": dead, "busy": occupied}


def precheck_card(result: dict, action: str = "ارسال") -> str:
    rows = [
        cards.kv("آماده", len(result["ready"]), width=10),
        cards.kv("سوخته", len(result["dead"]), width=10),
        cards.kv("مشغول", len(result["busy"]), width=10),
    ]
    if result["dead"]:
        rows.append(cards.LINE)
        for acc in result["dead"][:6]:
            rows.append(f"🔴 {acc['phone']} — نیاز به ورود مجدد")
    if result["busy"]:
        rows.append(cards.LINE)
        for acc in result["busy"][:6]:
            rows.append(f"⏳ {acc['phone']} — کار دیگری در حال اجراست")
    if not result["ready"]:
        rows += [cards.LINE, "هیچ اکانت آماده‌ای نیست."]
    else:
        rows += [cards.LINE, f"{action} با {len(result['ready'])} اکانت انجام شود؟"]
    return cards.card("⚡ پیش‌بررسی", rows)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _progress_card(ctl: dict) -> str:
    total = max(1, int(ctl.get("total") or 1))
    done = int(ctl.get("sent", 0)) + int(ctl.get("failed", 0))
    rows = [
        cards.kv("Phone", ctl.get("phone")),
        cards.kv("Progress", f"{cards.bar(done, total)}  {done}/{total}"),
        cards.kv("Sent", cards.num(ctl.get("sent", 0))),
        cards.kv("Failed", cards.num(ctl.get("failed", 0))),
        cards.kv("State", ctl.get("state", "running")),
    ]
    if ctl.get("last_error"):
        rows.append(cards.kv("Last error", str(ctl["last_error"])[:60]))
    return cards.panel_card("🚀 - #send", rows)


def _ctl_buttons(account_id, paused: bool = False) -> list:
    resume = ("▶️ ادامه" if paused else "⏸ مکث")
    action = "rbresume" if paused else "rbpause"
    return [
        [Button.inline(resume, f"{action}_{account_id}".encode()),
         Button.inline("⛔ توقف", f"rbstop_{account_id}".encode())],
    ]


async def _run_send(customer_id, acc: dict, mode: str, text: str,
                    targets: list, owner_msg=None) -> None:
    """Send to a prepared target list, holding the session for the whole run."""
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    delay = db.get_delay(customer_id)
    max_errors = db.get_max_errors(customer_id)

    ctl = {"stop": False, "pause": False, "sent": 0, "failed": 0,
           "total": len(targets), "phone": phone, "state": "running",
           "last_error": ""}
    _jobs[aid] = ctl

    async with busy.hold(key, "send", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            ctl["state"] = "busy"
            _jobs.pop(aid, None)
            return
        cust = db.get_customer(customer_id)
        await logbus.customer_action(cust, "send_start", [
            cards.kv("Phone", phone),
            cards.kv("Mode", mode),
            cards.kv("Targets", len(targets)),
            cards.kv("Content", (text or db.get_marker(customer_id))[:80]),
            cards.kv("Speed", f"{delay}s"),
        ], platform="Rubika")

        w = worker.worker_for_account(acc)
        remote = bool(w and not worker.is_local(w))
        consecutive = 0
        progress_task = None
        if owner_msg is not None:
            progress_task = asyncio.create_task(
                _progress_loop(customer_id, aid, ctl, owner_msg))
        try:
            already = db.sent_targets(customer_id, aid, platform="rb")
            pending = [t for t in targets if str(t) not in already]
            ctl["total"] = len(pending)

            if remote:
                await _run_send_remote(customer_id, acc, w, mode, text,
                                       pending, ctl)
            else:
                await _run_send_local(customer_id, acc, mode, text, pending,
                                      ctl, delay, max_errors)
        except Exception as exc:  # noqa: BLE001
            ctl["state"] = "failed"
            code = await logbus.error(exc, context=f"rb send {phone}",
                                      customer=customer_id)
            ctl["last_error"] = code
        finally:
            if progress_task:
                progress_task.cancel()
            if ctl["state"] == "running":
                ctl["state"] = "done"
            _jobs.pop(aid, None)

    await _finish_send(customer_id, acc, ctl, owner_msg)


async def _run_send_local(customer_id, acc, mode, text, targets, ctl,
                          delay, max_errors) -> None:
    import account_conn
    aid, phone = acc["id"], acc["phone"]
    marker = db.get_marker(customer_id)
    from_guid = message_id = None

    if mode == "marker":
        async def _find(client):
            return (await rb.get_self_guid(client),
                    await rb.find_marked_message(client, marker))
        from_guid, found = await account_conn.call(customer_id, phone, _find,
                                                  timeout=180)
        if not found:
            ctl["state"] = "no_marker"
            return
        message_id = rb._msg_id_of(found)      # noqa: SLF001

    consecutive = 0
    for target in targets:
        if ctl["stop"]:
            ctl["state"] = "stopped"
            return
        while ctl["pause"] and not ctl["stop"]:
            await asyncio.sleep(1)
        if db.are_sends_frozen():
            # The owner's emergency stop: halt rather than keep burning accounts.
            ctl["state"] = "frozen"
            return
        try:
            if mode == "text":
                async def _one(client, guid=target):
                    return await rb.send_text(client, guid, text)
            else:
                async def _one(client, guid=target):
                    return await rb.forward_message(client, from_guid, guid,
                                                    message_id)
            await account_conn.call(customer_id, phone, _one,
                                    timeout=config.SEND_TIMEOUT)
            ctl["sent"] += 1
            consecutive = 0
            db.mark_sent(customer_id, aid, target, platform="rb")
        except account_conn.InvalidAuthError:
            ctl["state"] = "auth_failed"
            return
        except Exception as exc:  # noqa: BLE001
            ctl["failed"] += 1
            consecutive += 1
            ctl["last_error"] = type(exc).__name__
            if consecutive >= max_errors:
                ctl["state"] = "error_burst"
                return
        await asyncio.sleep(delay)


async def _run_send_remote(customer_id, acc, w, mode, text, targets, ctl) -> None:
    """Hand the list to the worker that owns the session and follow its progress."""
    phone = acc["id"], acc["phone"]
    aid, phone = acc["id"], acc["phone"]
    marker = db.get_marker(customer_id)
    payload = {"customer_id": customer_id, "phone": phone,
               "targets": [str(t) for t in targets], "mode": mode,
               "text": text or "", "delay": db.get_delay(customer_id),
               "max_errors": db.get_max_errors(customer_id)}

    if mode == "marker":
        prep = await worker.api_call(w, "POST", "/prepare", {
            "customer_id": customer_id, "phone": phone, "marker": marker},
            timeout=240)
        if not prep.get("ok"):
            ctl["state"] = "no_marker"
            return
        payload["from_guid"] = prep["from_guid"]
        payload["message_id"] = prep["message_id"]

    started = await worker.api_call(w, "POST", "/send/start", payload, timeout=60)
    job_id = started.get("job_id")
    if not job_id:
        ctl["state"] = "failed"
        return

    while True:
        await asyncio.sleep(3)
        if ctl["stop"]:
            try:
                await worker.api_call(w, "POST", f"/send/stop/{job_id}",
                                      timeout=30)
            except Exception:
                pass
        status = await worker.api_call(w, "GET", f"/send/status/{job_id}",
                                       timeout=30)
        ctl["sent"] = int(status.get("sent", 0))
        ctl["failed"] = int(status.get("failed", 0))
        ctl["last_error"] = status.get("error") or ""
        remote_state = status.get("state")
        if remote_state not in ("running",):
            ctl["state"] = {"done": "done", "stopped": "stopped",
                            "auth_failed": "auth_failed",
                            "error_burst": "error_burst"}.get(remote_state,
                                                              remote_state)
            break
    # The worker does not know our ledger, so record what it managed to send.
    for target in targets[:ctl["sent"]]:
        db.mark_sent(customer_id, aid, target, platform="rb")


async def _progress_loop(customer_id, account_id, ctl, msg) -> None:
    """Refresh the live card while a send runs."""
    last = ""
    try:
        while True:
            await asyncio.sleep(4)
            text = _progress_card(ctl)
            if text != last:
                last = text
                try:
                    await msg.edit(text,
                                   buttons=_ctl_buttons(account_id, ctl["pause"]))
                except Exception:
                    pass
            if ctl.get("state") != "running":
                return
    except asyncio.CancelledError:
        return


async def _finish_send(customer_id, acc, ctl, owner_msg) -> None:
    aid, phone = acc["id"], acc["phone"]
    db.incr_account_sent(customer_id, aid, ctl["sent"])
    db.incr_customer_sends(customer_id, ctl["sent"])
    db.usage_incr(customer_id, "send", ctl["sent"])
    w = worker.worker_for_account(acc)
    if w:
        db.incr_worker_sent(w["id"], ctl["sent"])

    labels = {
        "done": "🏁 پایان",
        "stopped": "⛔ متوقف شد",
        "error_burst": "⚠️ خطاهای پشت‌سرهم — متوقف شد",
        "auth_failed": "🔴 اکانت از کار افتاد",
        "no_marker": "❌ پیام مارک‌شده پیدا نشد",
        "frozen": "⏸ سرویس ارسال موقتاً متوقف است",
        "failed": "⚠️ خطا",
    }
    rows = [
        cards.kv("Phone", phone),
        cards.kv("Sent", cards.num(ctl["sent"])),
        cards.kv("Failed", cards.num(ctl["failed"])),
        cards.kv("Result", labels.get(ctl["state"], ctl["state"])),
    ]
    cust = db.get_customer(customer_id)
    await logbus.customer_action(cust, "send_finished", rows, platform="Rubika")

    buttons = [_back(b"rbaccs")]
    # "Continue" exists so a resumed send does not start from zero and message
    # everybody twice — duplicate messages are what get accounts reported.
    if ctl["state"] in ("error_burst", "stopped", "failed"):
        db.save_paused_send(customer_id, aid, phone,
                            {"sent": ctl["sent"], "failed": ctl["failed"]})
        buttons = [[Button.inline("✅ ادامه‌ی ارسال", f"rbcont_{aid}".encode())],
                   [Button.inline("🚫 لغو ادامه", f"rbcancel_{aid}".encode())],
                   _back(b"rbaccs")]
    if ctl["state"] == "auth_failed":
        buttons = [[Button.inline("🔑 ورود مجدد", f"rbrelogin_{aid}".encode())],
                   _back(b"rbaccs")]

    text = cards.panel_card("🏁 - #send_report", rows)
    if owner_msg is not None:
        try:
            await owner_msg.edit(text, buttons=buttons)
            return
        except Exception:
            pass
    try:
        await _bot.send_message(int(customer_id), text, buttons=buttons)
    except Exception:
        pass



# --------------------------------------------------------------------------- #
# Contacts import
# --------------------------------------------------------------------------- #
async def _run_contacts(customer_id, acc: dict, pairs: list, msg=None) -> None:
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    delay = db.get_contact_delay(customer_id)
    ctl = {"stop": False, "pause": False, "added": 0, "not_user": 0,
           "failed": 0, "total": len(pairs), "phone": phone, "state": "running"}
    _jobs[aid] = ctl
    job_id = db.cjob_create(customer_id, aid, phone, "import", {"pairs": pairs})

    async with busy.hold(key, "contacts", customer_id=customer_id,
                         extra={"account_id": aid, "job": job_id}) as held:
        if not held.ok:
            _jobs.pop(aid, None)
            db.cjob_update(customer_id, job_id, status="aborted")
            return
        cust = db.get_customer(customer_id)
        await logbus.customer_action(cust, "contacts_import_start", [
            cards.kv("Phone", phone),
            cards.kv("Numbers", len(pairs)),
            cards.kv("Speed", f"{delay}s"),
        ], platform="Rubika")

        progress = None
        if msg is not None:
            progress = asyncio.create_task(
                _contacts_progress_loop(aid, ctl, msg))
        try:
            w = worker.worker_for_account(acc)
            if w and not worker.is_local(w):
                await _contacts_remote(customer_id, acc, w, pairs, ctl, delay,
                                       job_id)
            else:
                await _contacts_local(customer_id, acc, pairs, ctl, delay, job_id)
            if ctl["state"] == "running":
                ctl["state"] = "done"
        except Exception as exc:  # noqa: BLE001
            ctl["state"] = "failed"
            await logbus.error(exc, context=f"rb contacts {phone}",
                               customer=customer_id)
        finally:
            if progress:
                progress.cancel()
            db.cjob_update(customer_id, job_id,
                           status="done" if ctl["state"] == "done" else "stopped",
                           added=ctl["added"], failed=ctl["failed"])
            _jobs.pop(aid, None)

    total_contacts = (acc.get("contacts") or 0) + ctl["added"]
    db.set_account_contacts(customer_id, aid, total_contacts)
    db.usage_incr(customer_id, "contacts", ctl["added"])
    rows = [
        cards.kv("Phone", phone),
        cards.kv("Added", cards.num(ctl["added"])),
        cards.kv("Not on Rubika", cards.num(ctl["not_user"])),
        cards.kv("Failed", cards.num(ctl["failed"])),
        cards.kv("Result", "🏁 پایان" if ctl["state"] == "done"
                 else ("⛔ متوقف شد" if ctl["state"] == "stopped" else ctl["state"])),
    ]
    await logbus.customer_action(db.get_customer(customer_id),
                                "contacts_import_done", rows, platform="Rubika")
    text = cards.panel_card("📇 - #contacts_report", rows)
    if msg is not None:
        try:
            await msg.edit(text, buttons=[_back(b"rbaccs")])
            return
        except Exception:
            pass
    try:
        await _bot.send_message(int(customer_id), text, buttons=[_back(b"rbaccs")])
    except Exception:
        pass


async def _contacts_local(customer_id, acc, pairs, ctl, delay, job_id) -> None:
    import account_conn
    phone = acc["phone"]
    for index, pair in enumerate(pairs):
        if ctl["stop"]:
            ctl["state"] = "stopped"
            return
        while ctl["pause"] and not ctl["stop"]:
            await asyncio.sleep(1)
        number = str(pair[0])
        name = (pair[1] if len(pair) > 1 else "") or config.CONTACT_DEFAULT_FIRST

        async def _one(client, p=number, n=name):
            return await rb.add_contact(client, p, first_name=n)

        try:
            res = await account_conn.call(customer_id, phone, _one, timeout=60)
            guid = rb._guid_of(res) if res else None      # noqa: SLF001
            if guid:
                ctl["added"] += 1
                db.number_record(number, True)
            else:
                ctl["not_user"] += 1
                db.number_record(number, False)
        except account_conn.InvalidAuthError:
            ctl["state"] = "auth_failed"
            return
        except Exception:      # noqa: BLE001
            ctl["failed"] += 1
        if index % 25 == 0:
            db.cjob_update(customer_id, job_id, cursor=index,
                           added=ctl["added"], failed=ctl["failed"])
        await asyncio.sleep(delay)


async def _contacts_remote(customer_id, acc, w, pairs, ctl, delay, job_id) -> None:
    """Send the list to the worker in chunks, so progress is visible and a
    failure loses one chunk instead of the whole run."""
    phone = acc["phone"]
    chunk_size = max(5, int(config.CONTACT_REMOTE_CHUNK))
    for start in range(0, len(pairs), chunk_size):
        if ctl["stop"]:
            ctl["state"] = "stopped"
            return
        while ctl["pause"] and not ctl["stop"]:
            await asyncio.sleep(1)
        chunk = pairs[start:start + chunk_size]
        res = await worker.api_call(w, "POST", "/contacts/add", {
            "customer_id": customer_id, "phone": phone,
            "pairs": chunk, "delay": delay}, timeout=600)
        ctl["added"] += int(res.get("added", 0))
        ctl["not_user"] += int(res.get("not_user", 0))
        ctl["failed"] += int(res.get("failed", 0))
        db.cjob_update(customer_id, job_id, cursor=start + len(chunk),
                       added=ctl["added"], failed=ctl["failed"])


async def _contacts_progress_loop(account_id, ctl, msg) -> None:
    last = ""
    try:
        while True:
            await asyncio.sleep(config.CONTACT_PROGRESS_EVERY)
            done = ctl["added"] + ctl["not_user"] + ctl["failed"]
            text = cards.panel_card("📇 - #contacts", [
                cards.kv("Phone", ctl["phone"]),
                cards.kv("Progress",
                         f"{cards.bar(done, ctl['total'])}  {done}/{ctl['total']}"),
                cards.kv("Added", cards.num(ctl["added"])),
                cards.kv("Not on Rubika", cards.num(ctl["not_user"])),
                cards.kv("Failed", cards.num(ctl["failed"])),
            ])
            if text != last:
                last = text
                try:
                    await msg.edit(text,
                                   buttons=_ctl_buttons(account_id, ctl["pause"]))
                except Exception:
                    pass
            if ctl.get("state") != "running":
                return
    except asyncio.CancelledError:
        return


# --------------------------------------------------------------------------- #
# Discovery — metered against the daily probe budget
# --------------------------------------------------------------------------- #
def _gen_number(prefix: str) -> str:
    digits = "".join(ch for ch in prefix if ch.isdigit())
    if digits.startswith("98"):
        digits = "0" + digits[2:]
    if not digits.startswith("0"):
        digits = "0" + digits
    missing = 11 - len(digits)
    if missing <= 0:
        return digits[:11]
    return digits + "".join(str(random.randint(0, 9)) for _ in range(missing))


async def _run_discovery(customer_id, acc: dict, prefix: str, msg=None) -> None:
    """Generate numbers behind a prefix, probe them, keep the ones that exist.

    Probing is what actually stresses the platform, so every probe is charged
    against the customer's daily budget and the run stops when it is spent. That
    is the guard against somebody pasting a million numbers.
    """
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    target = db.get_discovery_target(customer_id)
    probe_delay = config.clamp_discovery_delay(
        db.get_setting(customer_id, "discovery_delay"))
    ctl = {"stop": False, "pause": False, "found": 0, "probed": 0,
           "total": target, "phone": phone, "state": "running",
           "budget": db.probe_budget_left(customer_id)}
    _jobs[aid] = ctl

    async with busy.hold(key, "discovery", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            _jobs.pop(aid, None)
            return
        cust = db.get_customer(customer_id)
        await logbus.customer_action(cust, "discovery_start", [
            cards.kv("Phone", phone),
            cards.kv("Prefix", prefix),
            cards.kv("Target", target),
            cards.kv("Budget left", ctl["budget"]),
        ], platform="Rubika")

        progress = None
        if msg is not None:
            progress = asyncio.create_task(
                _discovery_progress_loop(aid, ctl, msg))
        try:
            w = worker.worker_for_account(acc)
            remote = bool(w and not worker.is_local(w))
            attempts = 0
            max_attempts = db.get_int_setting(customer_id, "discovery_attempts",
                                              config.DISCOVERY_MAX_ATTEMPTS)
            while ctl["found"] < target and attempts < max_attempts:
                if ctl["stop"]:
                    ctl["state"] = "stopped"
                    break
                while ctl["pause"] and not ctl["stop"]:
                    await asyncio.sleep(1)

                budget = db.probe_budget_left(customer_id)
                ctl["budget"] = budget
                if budget <= 0:
                    ctl["state"] = "budget_spent"
                    break

                batch_size = min(25, budget, max(1, target - ctl["found"]) * 3)
                candidates = []
                while len(candidates) < batch_size and attempts < max_attempts:
                    attempts += 1
                    number = _gen_number(prefix)
                    if db.number_seen(number):
                        continue          # the shared cache spares a probe
                    candidates.append(number)
                if not candidates:
                    break

                db.probe_spend(customer_id, len(candidates))
                ctl["probed"] += len(candidates)

                if remote:
                    res = await worker.api_call(w, "POST", "/probe", {
                        "customer_id": customer_id, "phone": phone,
                        "numbers": candidates, "delay": probe_delay},
                        timeout=900)
                    for item in res.get("found") or []:
                        db.number_record(item["phone"], True)
                        ctl["found"] += 1
                    for number in candidates:
                        if not db.number_seen(number):
                            db.number_record(number, False)
                else:
                    await _probe_local(customer_id, phone, candidates, ctl,
                                       probe_delay)
            if ctl["state"] == "running":
                ctl["state"] = "done"
        except Exception as exc:  # noqa: BLE001
            ctl["state"] = "failed"
            await logbus.error(exc, context=f"rb discovery {phone}",
                               customer=customer_id)
        finally:
            if progress:
                progress.cancel()
            _jobs.pop(aid, None)

    db.set_account_contacts(customer_id, aid,
                            (acc.get("contacts") or 0) + ctl["found"])
    db.usage_incr(customer_id, "discovery", ctl["found"])
    labels = {"done": "🏁 پایان", "stopped": "⛔ متوقف شد",
              "budget_spent": "📊 سهمیه‌ی امروز تمام شد", "failed": "⚠️ خطا"}
    rows = [
        cards.kv("Phone", phone),
        cards.kv("Prefix", prefix),
        cards.kv("Found", cards.num(ctl["found"])),
        cards.kv("Probed", cards.num(ctl["probed"])),
        cards.kv("Budget left", cards.num(db.probe_budget_left(customer_id))),
        cards.kv("Result", labels.get(ctl["state"], ctl["state"])),
    ]
    await logbus.customer_action(db.get_customer(customer_id), "discovery_done",
                                rows, platform="Rubika")
    text = cards.panel_card("🔎 - #discovery_report", rows)
    if msg is not None:
        try:
            await msg.edit(text, buttons=[_back(b"rb")])
            return
        except Exception:
            pass
    try:
        await _bot.send_message(int(customer_id), text, buttons=[_back(b"rb")])
    except Exception:
        pass


async def _probe_local(customer_id, phone, candidates, ctl, delay) -> None:
    import account_conn
    for number in candidates:
        if ctl["stop"]:
            return
        async def _one(client, p=number):
            return await rb.add_contact(client, p,
                                        first_name=config.CONTACT_DEFAULT_FIRST)
        try:
            res = await account_conn.call(customer_id, phone, _one, timeout=60)
            guid = rb._guid_of(res) if res else None      # noqa: SLF001
            db.number_record(number, bool(guid))
            if guid:
                ctl["found"] += 1
        except account_conn.InvalidAuthError:
            ctl["state"] = "auth_failed"
            return
        except Exception:      # noqa: BLE001
            db.number_record(number, False)
        await asyncio.sleep(delay)


async def _discovery_progress_loop(account_id, ctl, msg) -> None:
    last = ""
    try:
        while True:
            await asyncio.sleep(config.CONTACT_PROGRESS_EVERY)
            text = cards.panel_card("🔎 - #discovery", [
                cards.kv("Phone", ctl["phone"]),
                cards.kv("Prefix", ctl.get("prefix", "—")),
                cards.kv("Found",
                         f"{cards.bar(ctl['found'], ctl['total'])}  "
                         f"{ctl['found']}/{ctl['total']}"),
                cards.kv("Probed", cards.num(ctl["probed"])),
                cards.kv("Budget left", cards.num(ctl.get("budget", 0))),
            ])
            if text != last:
                last = text
                try:
                    await msg.edit(text,
                                   buttons=_ctl_buttons(account_id, ctl["pause"]))
                except Exception:
                    pass
            if ctl.get("state") != "running":
                return
    except asyncio.CancelledError:
        return


# --------------------------------------------------------------------------- #
# Brain: split a numbers file across accounts, add, then send
# --------------------------------------------------------------------------- #
async def _run_brain(customer_id, account_ids: list, pairs: list, msg=None) -> None:
    """Divide the list evenly, add contacts on each account, then send.

    Sequential across accounts on purpose: several accounts probing the platform
    at once from the same fleet is what draws attention.
    """
    accounts = [db.get_account(customer_id, aid) for aid in account_ids]
    accounts = [a for a in accounts if a]
    if not accounts:
        return
    share = max(1, len(pairs) // len(accounts))
    cap = db.get_brain_cap(customer_id)
    cust = db.get_customer(customer_id)

    await logbus.customer_action(cust, "brain_start", [
        cards.kv("Accounts", len(accounts)),
        cards.kv("Numbers", len(pairs)),
        cards.kv("Share each", share),
        cards.kv("Send cap", cap),
    ], platform="Rubika")

    summary = []
    for index, acc in enumerate(accounts):
        chunk = pairs[index * share:(index + 1) * share]
        if not chunk:
            continue
        budget = db.probe_budget_left(customer_id)
        if budget <= 0:
            summary.append((acc["phone"], 0, "سهمیه تمام شد"))
            continue
        chunk = chunk[:budget]
        db.probe_spend(customer_id, len(chunk))

        ctl = {"stop": False, "pause": False, "added": 0, "not_user": 0,
               "failed": 0, "total": len(chunk), "phone": acc["phone"],
               "state": "running"}
        _jobs[acc["id"]] = ctl
        key = _key(customer_id, acc["phone"])
        async with busy.hold(key, "brain", customer_id=customer_id,
                             extra={"account_id": acc["id"]}) as held:
            if not held.ok:
                summary.append((acc["phone"], 0, "مشغول بود"))
                _jobs.pop(acc["id"], None)
                continue
            job_id = db.cjob_create(customer_id, acc["id"], acc["phone"],
                                    "brain", {"pairs": chunk})
            try:
                w = worker.worker_for_account(acc)
                if w and not worker.is_local(w):
                    await _contacts_remote(customer_id, acc, w, chunk, ctl,
                                           db.get_contact_delay(customer_id),
                                           job_id)
                else:
                    await _contacts_local(customer_id, acc, chunk, ctl,
                                          db.get_contact_delay(customer_id),
                                          job_id)
                db.cjob_update(customer_id, job_id, status="done",
                               added=ctl["added"])
                summary.append((acc["phone"], ctl["added"], "ok"))
            except Exception as exc:  # noqa: BLE001
                db.cjob_update(customer_id, job_id, status="stopped")
                await logbus.error(exc, context=f"brain {acc['phone']}",
                                   customer=customer_id, notify=False)
                summary.append((acc["phone"], ctl["added"], "خطا"))
            finally:
                _jobs.pop(acc["id"], None)

        db.set_account_contacts(customer_id, acc["id"],
                                (acc.get("contacts") or 0) + ctl["added"])

    rows = [cards.kv("Accounts", len(accounts)), cards.LINE]
    for phone, added, note in summary:
        rows.append(f"📱 {phone} → 📇{added}  ({note})")
    rows.append(cards.LINE)
    rows.append(cards.kv("Budget left", cards.num(
        db.probe_budget_left(customer_id))))
    await logbus.customer_action(cust, "brain_done", rows, platform="Rubika")
    text = cards.panel_card("🧠 - #brain_report", rows)
    buttons = [[Button.inline("📤 ارسال به مخاطبین تازه", b"rbbrainsend")],
               _back(b"rb")]
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



# --------------------------------------------------------------------------- #
# Contact export
# --------------------------------------------------------------------------- #
async def _run_export(customer_id, acc: dict, msg=None) -> None:
    """Write the account's own contacts to a txt the customer can reuse.

    Deliberately holds BOTH the export claim and nothing else: the registry is
    one slot per session, so a send cannot start a second connection while this
    reads the contact list.
    """
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    path = os.path.join(MEDIA_DIR,
                        f"contacts_{customer_id}_{aid}_{os.urandom(4).hex()}.txt")
    numbers = []
    async with busy.hold(key, "export", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            try:
                await msg.edit(cards.card("📥 گرفتن مخاطبین", [busy.reason(key)]),
                               buttons=[_back(b"rbaccs")])
            except Exception:
                pass
            return
        try:
            w = worker.worker_for_account(acc)
            if w and not worker.is_local(w):
                # Read on the worker that owns the session; never open it here.
                res = await worker.api_call(w, "POST", "/contacts/phones", {
                    "customer_id": customer_id, "phone": phone}, timeout=600)
                numbers = [str(p) for p in (res.get("phones") or [])]
            else:
                import account_conn

                async def _work(client):
                    return await rb.get_contacts_full(client)

                contacts = await account_conn.call(customer_id, phone, _work,
                                                  timeout=600)
                seen = set()
                for item in contacts or []:
                    digits = "".join(ch for ch in str(item.get("phone") or "")
                                     if ch.isdigit())
                    if digits and digits not in seen:
                        seen.add(digits)
                        numbers.append(digits)
        except Exception as exc:  # noqa: BLE001
            code = await logbus.error(exc, context=f"rb export {phone}",
                                      customer=customer_id, notify=False)
            try:
                await msg.edit(cards.card("⚠️ مشکلی پیش آمد", [
                    cards.kv("کد خطا", code, width=8)]),
                    buttons=[_back(b"rbaccs")])
            except Exception:
                pass
            return

    if not numbers:
        try:
            await msg.edit(cards.card("📥 گرفتن مخاطبین", [
                cards.kv("Phone", phone), "مخاطبی پیدا نشد."]),
                buttons=[_back(b"rbaccs")])
        except Exception:
            pass
        return

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(numbers))
    caption = cards.panel_card("📥 - #contacts_export", [
        cards.kv("Phone", phone),
        cards.kv("Numbers", cards.num(len(numbers))),
        cards.LINE,
        "این فایل را می‌توانی در «افزودن مخاطب» یا «مغز» استفاده کنی.",
    ])
    await logbus.customer_action(db.get_customer(customer_id), "contacts_export", [
        cards.kv("Phone", phone), cards.kv("Numbers", len(numbers))],
        platform="Rubika")
    try:
        await _bot.send_file(int(customer_id), path, caption=caption,
                             force_document=True)
        if msg is not None:
            await msg.delete()
    except Exception:
        pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Handler registration
# --------------------------------------------------------------------------- #
def setup(bot, state, gate, safe_edit, respond, register_steps) -> None:
    """Attach every Rubika screen to the shared customer bot."""
    global _bot, _state, _gate, _safe_edit, _respond
    _bot, _state, _gate, _safe_edit, _respond = bot, state, gate, safe_edit, respond

    from telethon import events

    # ---- section root -------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rb"))
    async def rb_home(event):
        if not await gate(event):
            return
        state.pop(event.sender_id, None)
        await safe_edit(event, menu_card(event.sender_id), buttons=menu_buttons())

    # ---- accounts ------------------------------------------------------ #
    @bot.on(events.CallbackQuery(data=b"rbaccs"))
    async def rb_accounts(event):
        if not await gate(event):
            return
        await _render_accounts(event, 0)

    @bot.on(events.CallbackQuery(pattern=rb"rbapage_(\d+)"))
    async def rb_accounts_page(event):
        if not await gate(event, count_action=False):
            return
        await _render_accounts(event, int(event.pattern_match.group(1)))

    @bot.on(events.CallbackQuery(pattern=rb"rbacc_(\d+)"))
    async def rb_account(event):
        if not await gate(event):
            return
        acc = db.get_account(event.sender_id, int(event.pattern_match.group(1)))
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        await safe_edit(event, _account_card(event.sender_id, acc),
                        buttons=_account_buttons(acc))

    @bot.on(events.CallbackQuery(data=b"rbhealth"))
    async def rb_health(event):
        """Report stored health. Deliberately does NOT connect: probing an
        account that is mid-job is what revokes sessions."""
        if not await gate(event):
            return
        uid = event.sender_id
        accounts = db.list_accounts(uid)
        busy_ids = busy.busy_account_ids(uid)
        rows = []
        for acc in accounts:
            if acc["id"] in busy_ids:
                rows.append(f"🟡 {acc['phone']} — در حال کار (سالم)")
            elif acc["status"] == "active":
                rows.append(f"🟢 {acc['phone']} — سالم")
            else:
                rows.append(f"🔴 {acc['phone']} — نیاز به ورود مجدد")
        counts = db.count_accounts(uid)
        head = [cards.kv("Healthy", f"{counts['healthy']}/{counts['total']}"),
                cards.LINE] + (rows or ["—"])
        await safe_edit(event, cards.panel_card("🩺 - #health", head),
                        buttons=[_back(b"rbaccs")])

    # ---- add account --------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rbadd"))
    async def rb_add(event):
        if not await gate(event):
            return
        state[event.sender_id] = {"step": "rb_phone"}
        await safe_edit(event, cards.card("➕ افزودن اکانت روبیکا", [
            "شماره را با کد کشور بفرست.",
            "مثال: 09123456789",
        ]), buttons=[[Button.inline("🔑 ورود با توکن سشن", b"rbsess")],
                     _back(b"rb")])

    @bot.on(events.CallbackQuery(data=b"rbsess"))
    async def rb_session_login(event):
        if not await gate(event):
            return
        state[event.sender_id] = {"step": "rb_token"}
        await safe_edit(event, cards.card("🔑 ورود با توکن سشن", [
            "توکنی که قبلاً گرفتی را بفرست.",
            "با این روش کد پیامکی لازم نیست.",
        ]), buttons=[_back(b"rb")])

    @bot.on(events.CallbackQuery(pattern=rb"rbrelogin_(\d+)"))
    async def rb_relogin(event):
        if not await gate(event):
            return
        acc = db.get_account(event.sender_id, int(event.pattern_match.group(1)))
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        state[event.sender_id] = {"step": "rb_code", "relogin": acc["id"],
                                 "phone": acc["phone"]}
        await _start_login(event, acc["phone"])

    @bot.on(events.CallbackQuery(pattern=rb"rbtoken_(\d+)"))
    async def rb_token(event):
        """Hand the portable session token to its owner.

        It is genuinely useful to them because this panel can log an account in
        from a token, so a customer can move or restore their own account.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        values = db.get_session_blob(uid, aid)
        if not values:
            await event.answer("برای این اکانت توکنی ذخیره نشده.", alert=True)
            return
        token = db.session_pack(values)
        await logbus.customer_action(db.get_customer(uid), "session_token_shown",
                                    [cards.kv("Phone", acc["phone"])],
                                    platform="Rubika")
        await safe_edit(event, cards.panel_card("🔑 - #session_token", [
            cards.kv("Phone", acc["phone"]),
            cards.LINE,
            "با این توکن می‌توانی این اکانت را بدون کد پیامکی برگردانی.",
            "آن را جایی امن نگه دار و برای کسی نفرست.",
        ]), buttons=[[Button.inline("🔙 اکانت", f"rbacc_{aid}".encode())]])
        try:
            await bot.send_message(uid, f"`{token}`", parse_mode="md")
        except Exception:
            await bot.send_message(uid, token)

    @bot.on(events.CallbackQuery(pattern=rb"rbdel_(\d+)"))
    async def rb_delete(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(event.sender_id, aid)
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        await safe_edit(event, cards.card("🗑 حذف اکانت", [
            cards.kv("Phone", acc["phone"]),
            "با حذف، سشن و لیست ارسال این اکانت پاک می‌شود.",
        ]), buttons=[[Button.inline("✅ حذف کن", f"rbdely_{aid}".encode())],
                     [Button.inline("🔙 انصراف", f"rbacc_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"rbdely_(\d+)"))
    async def rb_delete_yes(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            return
        import account_conn
        await account_conn.close(uid, acc["phone"])
        db.delete_account(uid, aid)
        try:
            path = rb.session_path(acc["phone"], uid)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        await logbus.customer_action(db.get_customer(uid), "account_deleted",
                                    [cards.kv("Phone", acc["phone"])],
                                    platform="Rubika")
        await _render_accounts(event, 0)

    @bot.on(events.CallbackQuery(pattern=rb"rbreset_(\d+)"))
    async def rb_reset_sent(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        cleared = db.reset_sent(uid, aid, platform="rb")
        await event.answer(f"{cleared} مخاطب از لیست ارسال پاک شد.", alert=True)
        acc = db.get_account(uid, aid)
        if acc:
            await safe_edit(event, _account_card(uid, acc),
                            buttons=_account_buttons(acc))

    # ---- content ------------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rbcontent"))
    async def rb_content(event):
        if not await gate(event):
            return
        uid = event.sender_id
        await safe_edit(event, cards.card("📌 محتوای ارسال", [
            cards.kv("Marker", f"«{db.get_marker(uid)}»"),
            cards.kv("متن دوم", f"«{db.get_setting(uid, 'rb_text2') or '—'}»"),
            cards.kv("متن ساده", f"«{db.get_setting(uid, 'rb_plain') or '—'}»"),
            cards.LINE,
            "مارکر: عبارتی که آخر کپشن پیام در «ذخیره‌شده‌ها» می‌گذاری.",
        ]), buttons=[
            [Button.inline("📌 تنظیم مارکر", b"rbmarker")],
            [Button.inline("✍️ متن دوم", b"rbtext2"),
             Button.inline("✍️ متن ساده", b"rbplain")],
            _back(b"rb"),
        ])

    for cb, step, title, hint in (
        (b"rbmarker", "rb_marker", "📌 تنظیم مارکر",
         "عبارت مارکر را بفرست. همان را آخر کپشن پیام ذخیره‌شده بگذار."),
        (b"rbtext2", "rb_text2", "✍️ متن دوم",
         "متنی که بلافاصله بعد از فوروارد فرستاده می‌شود. «-» برای پاک کردن."),
        (b"rbplain", "rb_plain", "✍️ متن ساده",
         "متن ارسال ساده (بدون فوروارد). «-» برای پاک کردن."),
    ):
        def _make(cb=cb, step=step, title=title, hint=hint):
            @bot.on(events.CallbackQuery(data=cb))
            async def _handler(event):
                if not await gate(event):
                    return
                state[event.sender_id] = {"step": step}
                await safe_edit(event, cards.card(title, [hint]),
                                buttons=[_back(b"rbcontent")])
            return _handler
        _make()

    # ---- send ---------------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rbsend"))
    async def rb_send_menu(event):
        if not await gate(event):
            return
        uid = event.sender_id
        accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
        if not accounts:
            await safe_edit(event, cards.card("🚀 ارسال", [
                "اکانت فعالی نداری."]), buttons=[
                [Button.inline("➕ افزودن اکانت", b"rbadd")], _back(b"rb")])
            return
        rows = [[Button.inline(f"🚀 {a['phone']}", f"rbrun_{a['id']}".encode())]
                for a in accounts[:config.ACC_PAGE_SIZE]]
        rows.append([Button.inline("📤 ارسال چنداکانتی", b"rbmulti")])
        rows.append(_back(b"rb"))
        await safe_edit(event, cards.card("🚀 ارسال", [
            cards.kv("Active accounts", len(accounts)),
            cards.kv("Marker", f"«{db.get_marker(uid)}»"),
            "اکانت را انتخاب کن:",
        ]), buttons=rows)

    @bot.on(events.CallbackQuery(pattern=rb"rbrun_(\d+)"))
    async def rb_run(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        result = precheck(uid, [aid])
        if not result["ready"]:
            await safe_edit(event, precheck_card(result),
                            buttons=[_back(b"rbaccs")])
            return
        await safe_edit(event, cards.card("🚀 ارسال", [
            cards.kv("Phone", acc["phone"]),
            cards.kv("Marker", f"«{db.get_marker(uid)}»"),
            "نوع ارسال را انتخاب کن:",
        ]), buttons=[
            [Button.inline("📎 فوروارد پیام مارک‌شده", f"rbgo_{aid}_m".encode())],
            [Button.inline("✍️ ارسال متن ساده", f"rbgo_{aid}_t".encode())],
            [Button.inline("📢 ارسال کانالی", f"rbchan_{aid}".encode())],
            [Button.inline("🔙 اکانت", f"rbacc_{aid}".encode())],
        ])

    @bot.on(events.CallbackQuery(pattern=rb"rbgo_(\d+)_(m|t)"))
    async def rb_go(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        mode = "marker" if event.pattern_match.group(2) == b"m" else "text"
        acc = db.get_account(uid, aid)
        if not acc:
            return
        text = db.get_setting(uid, "rb_plain") or ""
        if mode == "text" and not text.strip():
            await event.answer("اول «متن ساده» را در بخش محتوا تنظیم کن.",
                               alert=True)
            return
        await safe_edit(event, cards.card("🚀 ارسال", [
            cards.kv("Phone", acc["phone"]), "⏳ آماده‌سازی مخاطبین ..."]))
        asyncio.create_task(_prepare_and_send(uid, acc, mode, text, event))

    @bot.on(events.CallbackQuery(pattern=rb"rbpause_(\d+)"))
    async def rb_pause(event):
        if not await gate(event, count_action=False):
            return
        ctl = _jobs.get(int(event.pattern_match.group(1)))
        if ctl:
            ctl["pause"] = True
        await event.answer("مکث شد.")

    @bot.on(events.CallbackQuery(pattern=rb"rbresume_(\d+)"))
    async def rb_resume(event):
        if not await gate(event, count_action=False):
            return
        ctl = _jobs.get(int(event.pattern_match.group(1)))
        if ctl:
            ctl["pause"] = False
        await event.answer("ادامه یافت.")

    @bot.on(events.CallbackQuery(pattern=rb"rbstop_(\d+)"))
    async def rb_stop(event):
        if not await gate(event, count_action=False):
            return
        ctl = _jobs.get(int(event.pattern_match.group(1)))
        if ctl:
            ctl["stop"] = True
            ctl["pause"] = False
        await event.answer("درخواست توقف ثبت شد.")

    @bot.on(events.CallbackQuery(pattern=rb"rbcont_(\d+)"))
    async def rb_continue(event):
        """Resume a stopped send from where it left off.

        The already-sent ledger is what makes this safe: restarting from zero
        would message thousands of people twice and get the account reported.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            return
        db.delete_paused_send(uid, aid)
        await safe_edit(event, cards.card("✅ ادامه‌ی ارسال", [
            cards.kv("Phone", acc["phone"]),
            "مخاطبانی که قبلاً پیام گرفته‌اند رد می‌شوند.",
            "⏳ آماده‌سازی ...",
        ]))
        asyncio.create_task(_prepare_and_send(uid, acc, "marker", "", event))

    @bot.on(events.CallbackQuery(pattern=rb"rbcancel_(\d+)"))
    async def rb_cancel_resume(event):
        if not await gate(event, count_action=False):
            return
        db.delete_paused_send(event.sender_id,
                              int(event.pattern_match.group(1)))
        await event.answer("ادامه لغو شد.")
        await _render_accounts(event, 0)

    # ---- channel send -------------------------------------------------- #
    @bot.on(events.CallbackQuery(pattern=rb"rbchan_(\d+)"))
    async def rb_channel(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        state[uid] = {"step": "rb_channel_title", "account_id": aid}
        await safe_edit(event, cards.card("📢 ارسال کانالی", [
            cards.kv("Phone", acc["phone"]),
            "اسم کانالی که ساخته می‌شود را بفرست.",
            "بعد پیام مارک‌شده داخلش فوروارد و مخاطبین اکانت عضو می‌شوند.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"rbacc_{aid}".encode())]])

    # ---- multi-account send -------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rbmulti"))
    async def rb_multi(event):
        if not await gate(event):
            return
        await _render_multi(event)

    @bot.on(events.CallbackQuery(pattern=rb"rbmsel_(\d+)"))
    async def rb_multi_select(event):
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        chosen = _state.setdefault(uid, {}).setdefault("multi", [])
        if aid in chosen:
            chosen.remove(aid)
        else:
            chosen.append(aid)
        await _render_multi(event)

    @bot.on(events.CallbackQuery(data=b"rbmgo"))
    async def rb_multi_go(event):
        if not await gate(event):
            return
        uid = event.sender_id
        chosen = list(_state.get(uid, {}).get("multi") or [])
        if not chosen:
            await event.answer("حداقل یک اکانت انتخاب کن.", alert=True)
            return
        result = precheck(uid, chosen)
        if not result["ready"]:
            await safe_edit(event, precheck_card(result), buttons=[_back(b"rbmulti")])
            return
        _state.get(uid, {}).pop("multi", None)
        await safe_edit(event, cards.card("📤 ارسال چنداکانتی", [
            cards.kv("Accounts", len(result["ready"])),
            "⏳ شروع شد. هر اکانت به‌ترتیب و روی سشن خودش کار می‌کند.",
        ]))
        asyncio.create_task(_run_multi(uid, [a["id"] for a in result["ready"]]))

    # ---- contacts ------------------------------------------------------ #
    @bot.on(events.CallbackQuery(data=b"rbcontacts"))
    async def rb_contacts(event):
        if not await gate(event):
            return
        uid = event.sender_id
        accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
        if not accounts:
            await safe_edit(event, cards.card("➕ افزودن مخاطب", ["اکانت فعالی نداری."]),
                            buttons=[_back(b"rb")])
            return
        rows = [[Button.inline(f"📇 {a['phone']}", f"rbcadd_{a['id']}".encode())]
                for a in accounts[:config.ACC_PAGE_SIZE]]
        rows.append([Button.inline("⏱ سرعت افزودن", b"rbcspeed")])
        rows.append(_back(b"rb"))
        await safe_edit(event, cards.card("➕ افزودن مخاطب", [
            cards.kv("Speed", f"{db.get_contact_delay(uid)}s"),
            cards.kv("Max per file", cards.num(config.CONTACT_IMPORT_MAX)),
            "اکانت را انتخاب کن، بعد فایل شماره‌ها را بفرست.",
        ]), buttons=rows)

    @bot.on(events.CallbackQuery(pattern=rb"rbcadd_(\d+)"))
    async def rb_contacts_pick(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        state[uid] = {"step": "rb_contacts_file", "account_id": aid}
        await safe_edit(event, cards.card("📂 فایل شماره‌ها", [
            cards.kv("Phone", acc["phone"]),
            cards.kv("Speed", f"{db.get_contact_delay(uid)}s"),
            f"فایل txt بفرست. هر خط یک شماره، یا «شماره,نام».",
            f"حداکثر {cards.num(config.CONTACT_IMPORT_MAX)} شماره در هر فایل.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"rbacc_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"rbexport_(\d+)"))
    async def rb_export(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        msg = await safe_edit(event, cards.card("📥 گرفتن مخاطبین", [
            cards.kv("Phone", acc["phone"]), "⏳ در حال خواندن ..."]))
        asyncio.create_task(_run_export(uid, acc, msg))

    # ---- discovery ----------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rbdiscover"))
    async def rb_discover(event):
        if not await gate(event):
            return
        uid = event.sender_id
        budget = db.probe_budget_left(uid)
        accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
        rows = [
            cards.kv("Budget today", f"{cards.num(budget)} / "
                                     f"{cards.num(config.PROBE_DAILY_CAP)}"),
            cards.kv("Target", db.get_discovery_target(uid)),
            cards.LINE,
            "شماره‌های پشت یک پیش‌شماره ساخته و بررسی می‌شوند.",
            "هر بررسی از سهمیه‌ی امروز کم می‌شود.",
        ]
        if budget <= 0:
            rows.append("📊 سهمیه‌ی امروز تمام شده؛ فردا دوباره امتحان کن.")
            await safe_edit(event, cards.card("🔎 کشف مخاطب", rows),
                            buttons=[_back(b"rb")])
            return
        buttons = [[Button.inline(f"🔎 {a['phone']}", f"rbdpick_{a['id']}".encode())]
                   for a in accounts[:config.ACC_PAGE_SIZE]]
        buttons.append(_back(b"rb"))
        await safe_edit(event, cards.card("🔎 کشف مخاطب", rows), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"rbdpick_(\d+)"))
    async def rb_discover_pick(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.get_account(uid, aid)
        if not acc:
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        state[uid] = {"step": "rb_prefix", "account_id": aid}
        await safe_edit(event, cards.card("☎️ پیش‌شماره", [
            cards.kv("Phone", acc["phone"]),
            "پیش‌شماره را بفرست. مثال: 0913 یا 09135",
            "بقیه‌ی رقم‌ها تصادفی ساخته می‌شوند.",
        ]), buttons=[[Button.inline("🔙 انصراف", f"rbacc_{aid}".encode())]])

    # ---- brain --------------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"rbbrain"))
    async def rb_brain(event):
        if not await gate(event):
            return
        await _render_brain(event)

    @bot.on(events.CallbackQuery(pattern=rb"rbbsel_(\d+)"))
    async def rb_brain_select(event):
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        chosen = _state.setdefault(uid, {}).setdefault("brain", [])
        if aid in chosen:
            chosen.remove(aid)
        else:
            chosen.append(aid)
        await _render_brain(event)

    @bot.on(events.CallbackQuery(data=b"rbbfile"))
    async def rb_brain_file(event):
        if not await gate(event):
            return
        uid = event.sender_id
        chosen = list(_state.get(uid, {}).get("brain") or [])
        if not chosen:
            await event.answer("حداقل یک اکانت انتخاب کن.", alert=True)
            return
        budget = db.probe_budget_left(uid)
        if budget <= 0:
            await event.answer("سهمیه‌ی بررسی امروز تمام شده.", alert=True)
            return
        state[uid] = {"step": "rb_brain_file", "brain": chosen}
        await safe_edit(event, cards.card("🧠 فایل شماره‌ها", [
            cards.kv("Accounts", len(chosen)),
            cards.kv("Budget today", cards.num(budget)),
            f"فایل txt بفرست. حداکثر {cards.num(config.BRAIN_MAX_NUMBERS)} شماره.",
            "شماره‌ها بین اکانت‌ها تقسیم می‌شوند.",
        ]), buttons=[_back(b"rbbrain")])

    # ---- settings ------------------------------------------------------ #
    @bot.on(events.CallbackQuery(data=b"rbsettings"))
    async def rb_settings(event):
        if not await gate(event):
            return
        uid = event.sender_id
        await safe_edit(event, cards.panel_card("⚙️ - #rubika_settings", [
            cards.kv("Send speed", f"{db.get_delay(uid)}s"),
            cards.kv("Contact speed", f"{db.get_contact_delay(uid)}s"),
            cards.kv("Consecutive errors", db.get_max_errors(uid)),
            cards.kv("Brain send cap", db.get_brain_cap(uid)),
            cards.kv("Discovery target", db.get_discovery_target(uid)),
            cards.kv("Probe budget", f"{cards.num(db.probe_budget_left(uid))}"
                                     f" / {cards.num(config.PROBE_DAILY_CAP)}"),
        ]), buttons=[
            [Button.inline("⏱ سرعت ارسال", b"rbspeed"),
             Button.inline("📇 سرعت مخاطب", b"rbcspeed")],
            [Button.inline("🧯 حد خطا", b"rbmaxerr"),
             Button.inline("🧠 سقف مغز", b"rbbcap")],
            [Button.inline("🔎 هدف کشف", b"rbdtarget")],
            _back(b"rb"),
        ])

    for cb, step, title, hint in (
        (b"rbspeed", "rb_speed", "⏱ سرعت ارسال",
         f"عدد بین {config.MIN_DELAY} و {config.MAX_DELAY} ثانیه بفرست."),
        (b"rbcspeed", "rb_cspeed", "📇 سرعت افزودن مخاطب",
         f"عدد بین {config.CONTACT_MIN_DELAY} و {config.CONTACT_MAX_DELAY} ثانیه."),
        (b"rbmaxerr", "rb_maxerr", "🧯 حد خطای پشت‌سرهم",
         "بعد از این تعداد خطای پیوسته، ارسال متوقف می‌شود."),
        (b"rbbcap", "rb_bcap", "🧠 سقف ارسال مغز",
         "حداکثر تعداد ارسال به مخاطبین تازه‌ی هر اکانت."),
        (b"rbdtarget", "rb_dtarget", "🔎 هدف کشف",
         "چند مخاطب موجود برای هر اکانت پیدا شود."),
    ):
        def _make_setting(cb=cb, step=step, title=title, hint=hint):
            @bot.on(events.CallbackQuery(data=cb))
            async def _handler(event):
                if not await gate(event):
                    return
                state[event.sender_id] = {"step": step}
                await safe_edit(event, cards.card(title, [hint]),
                                buttons=[_back(b"rbsettings")])
            return _handler
        _make_setting()

    register_steps(_STEPS)



# --------------------------------------------------------------------------- #
# Multi-account and brain selectors
# --------------------------------------------------------------------------- #
async def _render_multi(event):
    uid = event.sender_id
    accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
    chosen = _state.setdefault(uid, {}).setdefault("multi", [])
    valid = {a["id"] for a in accounts}
    chosen[:] = [aid for aid in chosen if aid in valid]

    rows = [[Button.inline(f"{'✅' if a['id'] in chosen else '▫️'} {a['phone']}",
                           f"rbmsel_{a['id']}".encode())]
            for a in accounts[:config.ACC_PAGE_SIZE]]
    if chosen:
        rows.append([Button.inline(f"🚀 شروع با {len(chosen)} اکانت", b"rbmgo")])
    rows.append(_back(b"rb"))
    body = [
        cards.kv("Selected", len(chosen)),
        cards.kv("Marker", f"«{db.get_marker(uid)}»"),
        cards.LINE,
        "هر اکانت فقط به مخاطبان خودش می‌فرستد.",
        "اکانت‌های روی یک سرور به‌ترتیب اجرا می‌شوند.",
    ]
    if not accounts:
        body = ["اکانت فعالی نداری."]
    await _safe_edit(event, cards.card("📤 ارسال چنداکانتی", body), buttons=rows)


async def _render_brain(event):
    uid = event.sender_id
    accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
    chosen = _state.setdefault(uid, {}).setdefault("brain", [])
    valid = {a["id"] for a in accounts}
    chosen[:] = [aid for aid in chosen if aid in valid]

    rows = [[Button.inline(f"{'✅' if a['id'] in chosen else '▫️'} {a['phone']}",
                           f"rbbsel_{a['id']}".encode())]
            for a in accounts[:config.ACC_PAGE_SIZE]]
    if chosen:
        rows.append([Button.inline("📂 فرستادن فایل شماره‌ها", b"rbbfile")])
    rows.append(_back(b"rb"))
    body = [
        cards.kv("Selected", len(chosen)),
        cards.kv("Budget today", cards.num(db.probe_budget_left(uid))),
        cards.kv("Send cap", db.get_brain_cap(uid)),
        cards.LINE,
        "فایل شماره بین اکانت‌های انتخاب‌شده تقسیم می‌شود،",
        "هر کدام سهم خودش را مخاطب می‌کند، بعد می‌توانی ارسال کنی.",
    ]
    if not accounts:
        body = ["اکانت فعالی نداری."]
    await _safe_edit(event, cards.card("🧠 مغز", body), buttons=rows)


async def _run_multi(customer_id, account_ids: list) -> None:
    """Run accounts on the same server one after another, different servers in
    parallel — one session at a time per box keeps the pattern human."""
    accounts = [db.get_account(customer_id, aid) for aid in account_ids]
    accounts = [a for a in accounts if a]
    groups: dict = {}
    for acc in accounts:
        w = worker.worker_for_account(acc)
        groups.setdefault((w or {}).get("id", 0), []).append(acc)

    cust = db.get_customer(customer_id)
    await logbus.customer_action(cust, "multi_send_start", [
        cards.kv("Accounts", len(accounts)),
        cards.kv("Server groups", len(groups)),
    ], platform="Rubika")

    async def _sequential(group):
        for acc in group:
            if db.are_sends_frozen():
                return
            await _prepare_and_send(customer_id, acc, "marker", "", None)

    await asyncio.gather(*[_sequential(g) for g in groups.values()],
                         return_exceptions=True)
    await logbus.customer_action(cust, "multi_send_done", [
        cards.kv("Accounts", len(accounts))], platform="Rubika")


async def _prepare_and_send(customer_id, acc: dict, mode: str, text: str,
                            event=None) -> None:
    """Collect the target list, then run the send under a single session claim."""
    phone = acc["phone"]
    msg = None
    if event is not None:
        try:
            msg = await _bot.send_message(int(customer_id), cards.card(
                "🚀 ارسال", [cards.kv("Phone", phone), "⏳ آماده‌سازی ..."]))
        except Exception:
            msg = None
    try:
        targets = await _collect_targets(customer_id, acc)
    except Exception as exc:  # noqa: BLE001
        code = await logbus.error(exc, context=f"rb targets {phone}",
                                  customer=customer_id)
        if msg:
            try:
                await msg.edit(cards.card("⚠️ مشکلی پیش آمد", [
                    cards.kv("کد خطا", code, width=8)]),
                    buttons=[_back(b"rbaccs")])
            except Exception:
                pass
        return
    if not targets:
        if msg:
            try:
                await msg.edit(cards.card("🚀 ارسال", [
                    cards.kv("Phone", phone), "مخاطبی برای ارسال پیدا نشد."]),
                    buttons=[_back(b"rbaccs")])
            except Exception:
                pass
        return
    await _run_send(customer_id, acc, mode, text, targets, msg)


async def _collect_targets(customer_id, acc: dict) -> list:
    """The account's own recipients, ordered. Runs under its own session claim."""
    phone = acc["phone"]
    key = _key(customer_id, phone)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        prep = await worker.api_call(w, "POST", "/prepare", {
            "customer_id": customer_id, "phone": phone,
            "marker": db.get_marker(customer_id)}, timeout=240)
        return list(prep.get("targets") or [])

    import account_conn
    async with busy.hold(key, "precheck", customer_id=customer_id,
                         extra={"account_id": acc["id"]}, settle=False) as held:
        if not held.ok:
            return []

        async def _work(client):
            return await rb.get_ordered_recipients(client)

        return await account_conn.call(customer_id, phone, _work, timeout=300)


# --------------------------------------------------------------------------- #
# Login helpers
# --------------------------------------------------------------------------- #
async def _start_login(event, phone: str) -> None:
    """Place the account on a server and request the login code there."""
    uid = event.sender_id
    await _respond(event, cards.card("⏳ در حال اتصال", [
        cards.kv("Phone", phone), "انتخاب سرور و ارسال کد ..."]))
    try:
        w = await worker.pick_worker_for_login()
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="pick worker", customer=uid)
        return
    if not w:
        await _respond(event, cards.card("❌ سرور آزاد نیست", [
            "الان سروری برای این اکانت در دسترس نیست.",
            "کمی بعد دوباره امتحان کن.",
        ]), buttons=[_back(b"rb")])
        return

    st = _state.setdefault(uid, {})
    st.update({"step": "rb_code", "phone": phone, "worker_id": w["id"]})
    try:
        if worker.is_local(w):
            import account_conn
            await account_conn.close(uid, phone)
            ctx = await rb.start_login(phone, uid)
            st["ctx"] = ctx
            status = str(ctx.get("status") or "").upper()
        else:
            res = await worker.api_call(w, "POST", "/login/start", {
                "customer_id": uid, "phone": phone}, timeout=180)
            status = str(res.get("status") or "").upper()
    except Exception as exc:  # noqa: BLE001
        _state.pop(uid, None)
        code = await logbus.error(exc, context=f"rb login start {phone}",
                                  customer=uid)
        await _respond(event, cards.card("⚠️ ارسال کد ناموفق بود", [
            cards.kv("کد خطا", code, width=8),
            "شماره را بررسی کن و دوباره امتحان کن.",
        ]), buttons=[_back(b"rb")])
        return

    if "PASS" in status:
        st["step"] = "rb_password"
        await _respond(event, cards.card("🔐 رمز دو مرحله‌ای", [
            "این اکانت رمز دومرحله‌ای دارد. رمز را بفرست.",
        ]), buttons=[_back(b"rb")])
        return
    await _respond(event, cards.card("📩 کد ورود", [
        cards.kv("Phone", phone),
        "کدی که روبیکا فرستاد را بفرست.",
    ]), buttons=[_back(b"rb")])


async def _finish_login(event, st: dict, code: str) -> None:
    uid = event.sender_id
    phone = st["phone"]
    w = db.get_worker(st.get("worker_id")) if st.get("worker_id") else None
    try:
        if w and not worker.is_local(w):
            info = await worker.api_call(w, "POST", "/login/code", {
                "customer_id": uid, "phone": phone, "code": code}, timeout=180)
        else:
            info = await rb.finish_login(st.get("ctx"), code) or {}
    except Exception as exc:  # noqa: BLE001
        err = await logbus.error(exc, context=f"rb login code {phone}",
                                 customer=uid)
        await _respond(event, cards.card("⚠️ کد پذیرفته نشد", [
            cards.kv("کد خطا", err, width=8),
            "کد را دوباره بفرست یا از اول شروع کن.",
        ]), buttons=[_back(b"rb")])
        return

    _state.pop(uid, None)
    aid = db.add_account(uid, phone, name=info.get("name") or "",
                         user_id=info.get("guid") or "",
                         worker_id=(w or {}).get("id"))
    if info.get("session_values"):
        db.set_session_blob(uid, aid, info["session_values"])
    contacts = int(info.get("contacts") or 0)
    if contacts:
        db.set_account_contacts(uid, aid, contacts)

    rows = [
        cards.kv("Status", "SUCCESS"),
        cards.kv("Phone", phone),
        cards.kv("Name", info.get("name") or "—"),
        cards.kv("GUID", info.get("guid") or "—"),
        cards.kv("Login Method", "CODE"),
        cards.kv("Session Saved", "YES"),
        cards.kv("Time", cards.now()),
    ]
    footer = f"--| 🌍 - Worker : #{(w or {}).get('tag', 'master')}"
    card_text = cards.panel_card("✅ - #rubika_login", rows, footer=footer)
    await logbus.event("✅ - #rubika_login", rows + [
        cards.kv("Customer", uid)], footer=footer)

    await _respond(event, card_text)
    await _respond(event, cards.card("📇 مخاطبین اکانت", [
        cards.kv("Contacts", cards.num(contacts)),
        cards.kv("Groups", cards.num(int(info.get("groups") or 0))),
    ]), buttons=[
        [Button.inline("🚀 ارسال", f"rbrun_{aid}".encode())],
        [Button.inline("🔑 توکن سشن", f"rbtoken_{aid}".encode())],
        _back(b"rb"),
    ])

    if db.get_bool_setting(uid, "campaign_enabled"):
        asyncio.create_task(_run_campaign(uid, aid))


async def _run_campaign(customer_id, account_id) -> None:
    """On a fresh login, optionally build a channel and seed it automatically."""
    acc = db.get_account(customer_id, account_id)
    if not acc:
        return
    await asyncio.sleep(config.CAMPAIGN_STEP_DELAY)
    try:
        await _channel_flow(customer_id, acc, f"Camp {acc['phone'][-4:]}", None)
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="campaign", customer=customer_id,
                           notify=False)


async def _channel_flow(customer_id, acc: dict, title: str, msg) -> None:
    """Create a channel, forward the marked message into it, add contacts."""
    phone = acc["phone"]
    key = _key(customer_id, phone)
    marker = db.get_marker(customer_id)
    async with busy.hold(key, "channel", customer_id=customer_id,
                         extra={"account_id": acc["id"]}) as held:
        if not held.ok:
            return
        w = worker.worker_for_account(acc)
        remote = bool(w and not worker.is_local(w))
        try:
            if remote:
                created = await worker.api_call(w, "POST", "/channel/create", {
                    "customer_id": customer_id, "phone": phone,
                    "title": title}, timeout=180)
                guid = created.get("channel_guid")
                await asyncio.sleep(config.CAMPAIGN_STEP_DELAY)
                added = await worker.api_call(w, "POST", "/channel/add", {
                    "customer_id": customer_id, "phone": phone,
                    "channel_guid": guid,
                    "target": config.CHANNEL_MEMBER_TARGET,
                    "batch": config.CHANNEL_ADD_BATCH,
                    "delay": config.CHANNEL_ADD_DELAY}, timeout=1800)
                member_count = int(added.get("added") or 0)
            else:
                import account_conn

                async def _create(client):
                    return await rb.create_channel(client, title)

                guid = await account_conn.call(customer_id, phone, _create,
                                              timeout=180)
                await asyncio.sleep(config.CAMPAIGN_STEP_DELAY)

                async def _seed(client):
                    return await rb.seed_channel_with_contacts(
                        client, guid, target=config.CHANNEL_MEMBER_TARGET,
                        batch=config.CHANNEL_ADD_BATCH,
                        delay=config.CHANNEL_ADD_DELAY)

                member_count = await account_conn.call(customer_id, phone, _seed,
                                                       timeout=1800) or 0
        except Exception as exc:  # noqa: BLE001
            code = await logbus.error(exc, context=f"rb channel {phone}",
                                      customer=customer_id)
            if msg:
                try:
                    await msg.edit(cards.card("⚠️ مشکلی پیش آمد", [
                        cards.kv("کد خطا", code, width=8)]),
                        buttons=[_back(b"rbaccs")])
                except Exception:
                    pass
            return

    rows = [
        cards.kv("Phone", phone),
        cards.kv("Channel", title),
        cards.kv("Members added", cards.num(member_count)),
        cards.kv("Marker", f"«{marker}»"),
    ]
    await logbus.customer_action(db.get_customer(customer_id), "channel_send",
                                rows, platform="Rubika")
    text = cards.panel_card("📢 - #channel_report", rows)
    if msg:
        try:
            await msg.edit(text, buttons=[_back(b"rbaccs")])
            return
        except Exception:
            pass
    try:
        await _bot.send_message(int(customer_id), text, buttons=[_back(b"rbaccs")])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Wizard steps
# --------------------------------------------------------------------------- #
def _normalize_phone_input(text: str) -> str:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if digits.startswith("98") and len(digits) >= 12:
        digits = "0" + digits[2:]
    if len(digits) == 10 and not digits.startswith("0"):
        digits = "0" + digits
    return digits if len(digits) >= 10 else ""


async def _step_phone(event, st):
    uid = event.sender_id
    phone = _normalize_phone_input(event.raw_text)
    if not phone:
        await _respond(event, "شماره خوانده نشد. با کد کشور بفرست، مثل 09123456789.")
        return
    if db.get_account_by_phone(uid, phone):
        _state.pop(uid, None)
        await _respond(event, cards.card("این شماره از قبل هست", [
            cards.kv("Phone", phone),
            "اگر از کار افتاده، از لیست اکانت‌ها «ورود مجدد» را بزن.",
        ]), buttons=[_back(b"rbaccs")])
        return
    await _start_login(event, phone)


async def _step_code(event, st):
    code = "".join(ch for ch in (event.raw_text or "") if ch.isdigit())
    if not code:
        await _respond(event, "کد فقط عدد است. دوباره بفرست.")
        return
    await _finish_login(event, st, code)


async def _step_password(event, st):
    uid = event.sender_id
    password = (event.raw_text or "").strip()
    phone = st["phone"]
    w = db.get_worker(st.get("worker_id")) if st.get("worker_id") else None
    try:
        if w and not worker.is_local(w):
            await worker.api_call(w, "POST", "/login/password", {
                "customer_id": uid, "phone": phone, "password": password},
                timeout=180)
        else:
            st["ctx"] = await rb.start_login(phone, uid, pass_key=password)
    except Exception as exc:  # noqa: BLE001
        err = await logbus.error(exc, context=f"rb 2fa {phone}", customer=uid)
        await _respond(event, cards.card("⚠️ رمز پذیرفته نشد", [
            cards.kv("کد خطا", err, width=8)]), buttons=[_back(b"rb")])
        return
    st["step"] = "rb_code"
    await _respond(event, cards.card("📩 کد ورود", [
        "حالا کد ورود را بفرست."]), buttons=[_back(b"rb")])


async def _step_token(event, st):
    uid = event.sender_id
    _state.pop(uid, None)
    values = db.session_unpack((event.raw_text or "").strip())
    if not values or not values.get("phone"):
        await _respond(event, cards.card("توکن نامعتبر بود", [
            "توکن باید با MMSESS: شروع شود."]), buttons=[_back(b"rb")])
        return
    phone = rb.normalize_phone(values["phone"])
    display = "0" + phone[2:] if phone.startswith("98") else phone
    w = await worker.pick_worker_for_login()
    aid = db.add_account(uid, display, name=values.get("name") or "",
                         worker_id=(w or {}).get("id"))
    db.set_session_blob(uid, aid, values)
    rows = [
        cards.kv("Status", "SUCCESS"),
        cards.kv("Phone", display),
        cards.kv("Login Method", "SESSION"),
        cards.kv("Session Saved", "YES"),
        cards.kv("Time", cards.now()),
    ]
    footer = f"--| 🌍 - Worker : #{(w or {}).get('tag', 'master')}"
    await logbus.event("✅ - #rubika_login", rows + [cards.kv("Customer", uid)],
                       footer=footer)
    await _respond(event, cards.panel_card("✅ - #rubika_login", rows,
                                          footer=footer),
                   buttons=[[Button.inline("🚀 ارسال", f"rbrun_{aid}".encode())],
                            _back(b"rb")])


async def _step_contacts_file(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    acc = db.get_account(uid, aid)
    if not acc:
        await _respond(event, "اکانت پیدا نشد.", buttons=[_back(b"rbaccs")])
        return
    pairs = await _read_numbers(event)
    if pairs is None:
        return
    if len(pairs) > config.CONTACT_IMPORT_MAX:
        await _respond(event, cards.card("فایل خیلی بزرگ است", [
            cards.kv("در فایل", cards.num(len(pairs)), width=10),
            cards.kv("حداکثر", cards.num(config.CONTACT_IMPORT_MAX), width=10),
            "فایل را به چند بخش تقسیم کن.",
        ]), buttons=[_back(b"rbaccs")])
        return
    msg = await _respond(event, cards.card("📇 افزودن مخاطب", [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Numbers", cards.num(len(pairs))),
        "⏳ شروع شد ...",
    ]))
    asyncio.create_task(_run_contacts(uid, acc, pairs, msg))


async def _step_brain_file(event, st):
    uid = event.sender_id
    chosen = list(st.get("brain") or [])
    _state.pop(uid, None)
    pairs = await _read_numbers(event)
    if pairs is None:
        return
    if len(pairs) > config.BRAIN_MAX_NUMBERS:
        await _respond(event, cards.card("فایل خیلی بزرگ است", [
            cards.kv("در فایل", cards.num(len(pairs)), width=10),
            cards.kv("حداکثر", cards.num(config.BRAIN_MAX_NUMBERS), width=10),
        ]), buttons=[_back(b"rbbrain")])
        return
    msg = await _respond(event, cards.card("🧠 مغز", [
        cards.kv("Accounts", len(chosen)),
        cards.kv("Numbers", cards.num(len(pairs))),
        "⏳ شروع شد ...",
    ]))
    asyncio.create_task(_run_brain(uid, chosen, pairs, msg))


async def _read_numbers(event):
    """Read a numbers list from an attached txt or from the message text."""
    text = ""
    if event.file is not None:
        try:
            blob = await event.download_media(bytes)
            text = blob.decode("utf-8", errors="ignore")
        except Exception:
            await _respond(event, "فایل خوانده نشد. یک فایل txt بفرست.")
            return None
    else:
        text = event.raw_text or ""
    pairs = _norm_pairs(text)
    if not pairs:
        await _respond(event, "شماره‌ی معتبری پیدا نشد. هر خط یک شماره.")
        return None
    return pairs


async def _step_prefix(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    acc = db.get_account(uid, aid)
    if not acc:
        return
    prefix = "".join(ch for ch in (event.raw_text or "") if ch.isdigit())
    if len(prefix) < 3:
        await _respond(event, "پیش‌شماره حداقل ۳ رقم باشد. مثال: 0913")
        return
    msg = await _respond(event, cards.card("🔎 کشف مخاطب", [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Prefix", prefix),
        "⏳ شروع شد ...",
    ]))
    asyncio.create_task(_run_discovery(uid, acc, prefix, msg))


async def _step_channel_title(event, st):
    uid = event.sender_id
    aid = st.get("account_id")
    _state.pop(uid, None)
    acc = db.get_account(uid, aid)
    if not acc:
        return
    title = (event.raw_text or "").strip()[:60]
    if not title:
        await _respond(event, "اسم کانال خالی بود.")
        return
    msg = await _respond(event, cards.card("📢 ارسال کانالی", [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Channel", title),
        "⏳ ساخت کانال ...",
    ]))
    asyncio.create_task(_channel_flow(uid, acc, title, msg))


def _make_text_setter(key: str, label: str, back: bytes):
    async def _step(event, st):
        uid = event.sender_id
        _state.pop(uid, None)
        value = (event.raw_text or "").strip()
        db.set_setting(uid, key, "" if value == "-" else value)
        await logbus.customer_action(db.get_customer(uid), "content_changed", [
            cards.kv("Field", label),
            cards.kv("Value", (value or "—")[:120]),
        ], platform="Rubika")
        await _respond(event, cards.card("✅ ثبت شد", [
            cards.kv(label, (value or "—")[:120], width=12)]),
            buttons=[_back(back)])
    return _step


def _make_number_setter(key: str, label: str, clamp, back: bytes):
    async def _step(event, st):
        uid = event.sender_id
        _state.pop(uid, None)
        raw = (event.raw_text or "").strip().replace(",", ".")
        try:
            value = clamp(float(raw))
        except (TypeError, ValueError):
            await _respond(event, "عدد نامعتبر بود.", buttons=[_back(back)])
            return
        db.set_setting(uid, key, value)
        await _respond(event, cards.card("✅ ثبت شد", [
            cards.kv(label, value, width=14)]), buttons=[_back(back)])
    return _step


_STEPS = {
    "rb_phone": _step_phone,
    "rb_code": _step_code,
    "rb_password": _step_password,
    "rb_token": _step_token,
    "rb_contacts_file": _step_contacts_file,
    "rb_brain_file": _step_brain_file,
    "rb_prefix": _step_prefix,
    "rb_channel_title": _step_channel_title,
    "rb_marker": _make_text_setter("rb_marker", "مارکر", b"rbcontent"),
    "rb_text2": _make_text_setter("rb_text2", "متن دوم", b"rbcontent"),
    "rb_plain": _make_text_setter("rb_plain", "متن ساده", b"rbcontent"),
    "rb_speed": _make_number_setter("send_delay", "سرعت ارسال",
                                    config.clamp_delay, b"rbsettings"),
    "rb_cspeed": _make_number_setter("contact_delay", "سرعت مخاطب",
                                     config.clamp_contact_delay, b"rbsettings"),
    "rb_maxerr": _make_number_setter("max_errors", "حد خطا",
                                     lambda v: max(1, min(50, int(v))),
                                     b"rbsettings"),
    "rb_bcap": _make_number_setter("brain_cap", "سقف مغز",
                                   lambda v: max(1, min(5000, int(v))),
                                   b"rbsettings"),
    "rb_dtarget": _make_number_setter("discovery_target", "هدف کشف",
                                      lambda v: max(1, min(5000, int(v))),
                                      b"rbsettings"),
}


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
async def restore_pending() -> None:
    """Re-adopt unfinished jobs into the busy registry after a restart.

    The registry lives in memory, so a restart empties it — but the jobs are
    recorded in the database and are meant to be resumable. Without re-adopting
    them the next health pass sees a free account, connects, and revokes the
    session of a job that is still running. This is the trap the base project
    fell into.
    """
    try:
        rows = db.owner_cjobs_running()
    except Exception:
        return
    adopted = 0
    for row in rows:
        phone = row.get("phone")
        cid = row.get("customer_id")
        if not (phone and cid):
            continue
        kind = "brain" if row.get("kind") == "brain" else "contacts"
        busy.adopt(_key(cid, phone), kind, customer_id=cid,
                   extra={"account_id": row.get("account_id"),
                          "job": row.get("id")})
        adopted += 1
    if adopted:
        await logbus.warn("jobs_readopted", [
            cards.kv("Jobs", adopted),
            "کارهای نیمه‌کاره دوباره در رجیستری ثبت شدند تا",
            "موتور سلامت روی سشن‌شان اتصال دوم باز نکند.",
        ])
