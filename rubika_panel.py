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
# customer_id -> the shared state behind one multi-account card, so the card can
# be rebuilt on demand while the run is in progress.
_multi_jobs: dict = {}

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
                    targets: list, owner_msg=None, progress: dict = None) -> None:
    """Send to a prepared target list, holding the session for the whole run."""
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    delay = db.get_delay(customer_id)
    max_errors = db.get_max_errors(customer_id)

    ctl = {"stop": False, "pause": False, "sent": 0, "failed": 0,
           "total": len(targets), "phone": phone, "state": "running",
           "last_error": "", "reason": ""}
    # The multi-account card reads the SAME dict the send loop writes, so its
    # numbers move with the run instead of appearing only at the end.
    if progress is not None:
        progress["ctl"] = ctl
        ctl["progress"] = progress
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
            cards.kv("Speed", f"{delay}s"),
        ] + [cards.LINE, "📝 Content:"] + cards.body(
            text or f"«{db.get_marker(customer_id)}» (مارکر)"), platform="Rubika")

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


def _demote_empty_result(ctl: dict) -> dict:
    """A run that reached NOBODY is not a finish, whatever the path reported.

    _finish_send is the single choke point every send arrives at — local, remote
    and multi-account — so the rule lives here once instead of being
    re-implemented in each loop and forgotten in one of them. It already was
    forgotten: a multi-account send with 34 targets reported "🏁 پایان" with Sent 0
    and Failed 0, which tells the customer their advert went out when nothing ever
    ran.

    A separate function, and returning ctl, so the rule can be asserted directly
    rather than by searching the source of a coroutine that also talks to Telegram.
    """
    if ctl.get("state") != "done":
        return ctl
    if not int(ctl.get("total") or 0):
        return ctl                      # nothing to send is not a failure
    if int(ctl.get("sent") or 0):
        return ctl
    ctl["state"] = "failed"
    if not ctl.get("reason"):
        ctl["reason"] = (
            ctl.get("last_error")
            or f"0 of {ctl['total']} targets were reached and nothing failed "
               "either — the send loop never ran")
    return ctl


async def _ctl_sleep(ctl, seconds: float, step: float = 2.0) -> None:
    """Sleep, but wake up as soon as the job is stopped.

    A flat asyncio.sleep(RESUME_WAIT) leaves a stop request unanswered for five
    minutes, so the customer presses stop and nothing appears to happen.
    """
    waited = 0.0
    while waited < seconds:
        if ctl.get("stop"):
            return
        chunk = min(step, seconds - waited)
        await asyncio.sleep(chunk)
        waited += chunk


async def _run_send_local(customer_id, acc, mode, text, targets, ctl,
                          delay, max_errors) -> None:
    """Send from this machine, on ONE connection the job owns, with a brake.

    Same two defects the worker path had, fixed the same way:

      * it used account_conn.call() PER RECIPIENT, over the shared warm socket.
        An auth-looking error there sends account_conn into verify_session_dead,
        which drops and reopens that very socket mid-send — and the rapid
        reconnect is itself what makes Rubika revoke a session. One muted
        recipient could therefore kill a healthy run. The job now owns a
        dedicated connection and confirms a suspect session elsewhere.
      * a burst of errors ended the run for good. Rubika throttles long before it
        revokes, so a burst pauses and resumes instead.
    """
    import account_conn
    aid, phone = acc["id"], acc["phone"]
    marker = db.get_marker(customer_id)
    from_guid = message_id = None

    targets = list(targets)
    total = len(targets)
    idx = 0
    retries = 0

    async def _send_one(client, guid):
        if mode == "text":
            return await rb.send_text(client, guid, text)
        return await rb.forward_message(client, from_guid, guid, message_id)

    while True:
        consecutive = 0
        hit_max = False
        # Decide whether we are allowed to send BEFORE opening anything. The
        # owner's emergency freeze and a stop request must both be honoured
        # without touching the session at all — opening a connection only to
        # discover we are frozen is exactly the pointless session churn the
        # freeze exists to prevent.
        if ctl["stop"]:
            ctl["state"] = "stopped"
            ctl["reason"] = "manual_stop"
            return
        if db.are_sends_frozen():
            ctl["state"] = "frozen"
            ctl["reason"] = "owner froze all sends"
            return
        async with account_conn.fresh_connection(customer_id, phone) as client:
            if mode == "marker" and message_id is None:
                # Resolved on the SAME connection we are about to send on, the
                # way the reference does it, instead of paying an extra
                # connect/disconnect cycle before the run even starts.
                from_guid = await rb.get_self_guid(client)
                message_id = await rb.find_marked_message(client, marker)
                # find_marked_message returns the id itself now, so this check
                # actually works. It used to receive a 2-tuple, which is truthy
                # even when the marker was missing, and then derived a None id.
                if not message_id:
                    # Say how far the search actually got. "marker not found"
                    # alone cannot tell an empty Saved chat from a search that
                    # stopped after one page from a marker that truly is absent,
                    # and those need different answers from the customer.
                    scan = rb.last_marker_scan()
                    ctl["state"] = "no_marker"
                    ctl["reason"] = (
                        f"no saved message contains «{marker}» "
                        f"(scanned {scan.get('scanned', 0)} messages"
                        + (f", stopped on {scan['error']}" if scan.get("error")
                           else "") + ")")
                    return

            while idx < total:
                if ctl["stop"]:
                    ctl["state"] = "stopped"
                    ctl["reason"] = "manual_stop"
                    return
                while ctl["pause"] and not ctl["stop"]:
                    await asyncio.sleep(1)
                if db.are_sends_frozen():
                    # The owner's emergency stop: halt rather than keep burning
                    # accounts.
                    ctl["state"] = "frozen"
                    ctl["reason"] = "owner froze all sends"
                    return
                target = targets[idx]
                idx += 1
                try:
                    await asyncio.wait_for(_send_one(client, target),
                                           timeout=config.SEND_TIMEOUT)
                    ctl["sent"] += 1
                    consecutive = 0
                    db.mark_sent(customer_id, aid, target, platform="rb")
                except Exception as exc:  # noqa: BLE001
                    # Never tear down this client over an auth-looking error;
                    # confirm on a separate connection and keep going if alive.
                    if account_conn.is_auth_error(exc):
                        dead = False
                        try:
                            dead = await asyncio.wait_for(
                                account_conn.verify_session_dead(customer_id,
                                                                 phone),
                                timeout=45)
                        except Exception:  # noqa: BLE001 - inconclusive = alive
                            dead = False
                        if dead:
                            await account_conn.notify_invalid(customer_id, phone)
                            ctl["state"] = "auth_failed"
                            ctl["last_error"] = (f"{type(exc).__name__}: "
                                                 f"{str(exc)[:120]}")
                            ctl["reason"] = "invalid_auth"
                            return
                    ctl["failed"] += 1
                    consecutive += 1
                    # The type alone was useless for diagnosis; keep the message.
                    ctl["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    if consecutive >= max_errors:
                        hit_max = True
                        break
                await _ctl_sleep(ctl, delay)

        if not hit_max:
            break                                # the whole list is done
        if retries >= config.RESUME_MAX_RETRIES:
            ctl["state"] = "error_burst"
            ctl["reason"] = (f"{max_errors} consecutive errors at {idx}/{total}; "
                             f"retries exhausted. last: {ctl['last_error']}")
            return
        retries += 1
        ctl["state"] = "waiting"
        ctl["reason"] = (f"paused after {max_errors} consecutive errors; "
                         f"resuming at {idx}/{total}")
        await _ctl_sleep(ctl, float(config.RESUME_WAIT))
        if ctl["stop"]:
            ctl["state"] = "stopped"
            ctl["reason"] = "manual_stop"
            return
        ctl["state"] = "running"
        # loop round: the `async with` above opens a brand-new client.

    # Never report a cheerful finish for a run that reached nobody.
    if total and not ctl["sent"]:
        ctl["state"] = "failed"
        ctl["reason"] = (ctl["last_error"]
                         or f"0 of {total} targets were reached")


async def _run_send_remote(customer_id, acc, w, mode, text, targets, ctl) -> None:
    """Hand the list to the worker that owns the session and follow its progress."""
    aid, phone = acc["id"], acc["phone"]
    marker = db.get_marker(customer_id)
    payload = {"customer_id": customer_id, "phone": phone,
               "targets": [str(t) for t in targets], "mode": mode,
               "text": text or "", "delay": db.get_delay(customer_id),
               "max_errors": db.get_max_errors(customer_id),
               # Auto-resume: a burst of platform errors has to pause the job and
               # pick it up again, not end it. Without these the worker gave up
               # permanently the first time Rubika throttled the account.
               "send_timeout": config.SEND_TIMEOUT,
               "resume_wait": config.RESUME_WAIT,
               "max_retries": config.RESUME_MAX_RETRIES}

    if mode == "marker":
        prep = await worker.api_call(w, "POST", "/prepare", {
            "customer_id": customer_id, "phone": phone, "mode": "marker",
            "marker": marker}, timeout=240)
        # /prepare now always returns ok with the recipient list; the marker is
        # reported separately. A marker send with no marked post cannot proceed.
        if not prep.get("ok") or not prep.get("message_id"):
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
        ctl["reason"] = status.get("reason") or ""
        remote_state = status.get("state")
        # "waiting" means the worker hit the error brake and is sitting out
        # resume_wait before carrying on. That is a LIVE job — treating it as a
        # terminal state would abandon a send that is about to resume.
        if remote_state in ("running", "waiting"):
            ctl["state"] = remote_state
            continue
        ctl["state"] = {"done": "done", "stopped": "stopped",
                        "auth_failed": "auth_failed",
                        "failed": "failed",
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

    _demote_empty_result(ctl)

    labels = {
        "done": "🏁 پایان",
        "stopped": "⛔ متوقف شد",
        "error_burst": "⚠️ خطاهای پشت‌سرهم — متوقف شد",
        "auth_failed": "🔴 اکانت از کار افتاد",
        "no_marker": "❌ پیام مارک‌شده پیدا نشد",
        "frozen": "⏸ سرویس ارسال موقتاً متوقف است",
        "failed": "⚠️ خطا",
        "waiting": "⏳ در انتظار ادامه",
    }
    rows = [
        cards.kv("Phone", phone),
        cards.kv("Sent", cards.num(ctl["sent"])),
        cards.kv("Failed", cards.num(ctl["failed"])),
        cards.kv("Result", labels.get(ctl["state"], ctl["state"])),
    ]
    # Anything that did not simply finish has to carry its reason, otherwise the
    # report says "⚠️ خطا" and nobody can tell what went wrong without SSH.
    if ctl["state"] != "done":
        reason = ctl.get("reason") or ctl.get("last_error") or ""
        if reason:
            rows.append(cards.kv("Reason", str(reason)[:180]))
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
                    # get_contact_phones, NOT get_contacts_full: the latter has no
                    # phone field, so this loop used to read item["phone"] off
                    # dicts that never carried it and produced an empty export
                    # every single time, without an error to show why.
                    return await rb.get_contact_phones(client)

                got = await account_conn.call(customer_id, phone, _work,
                                             timeout=600)
                numbers.extend(str(p) for p in (got or []))
        except Exception as exc:  # noqa: BLE001
            code = await logbus.error(exc, context=f"rb export {phone}",
                                      customer=customer_id, notify=False, kind="export")
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
            [Button.inline("🗑 پاک کردن همه", b"rbclearall")],
            _back(b"rb"),
        ])

    @bot.on(events.CallbackQuery(data=b"rbclearall"))
    async def rb_clear_all_ask(event):
        """Clear every content field in one press.

        Clearing them one by one meant three separate menus and sending "-" into
        each, which is why it was described as hard. Confirmed rather than
        immediate, because wiping a marker mid-campaign is not something to do by
        a mis-tap.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        await safe_edit(event, cards.card("🗑 پاک کردن محتوا", [
            cards.kv("Marker", f"«{db.get_marker(uid)}»"),
            cards.kv("متن دوم", f"«{db.get_setting(uid, 'rb_text2') or '—'}»"),
            cards.kv("متن ساده", f"«{db.get_setting(uid, 'rb_plain') or '—'}»"),
            cards.LINE,
            "هر سه مورد بالا پاک می‌شوند. مارکر به مقدار پیش‌فرض برمی‌گردد.",
        ]), buttons=[
            [Button.inline("✅ بله، پاک کن", b"rbclearall_yes")],
            [Button.inline("🔙 لغو", b"rbcontent")],
        ])

    @bot.on(events.CallbackQuery(data=b"rbclearall_yes"))
    async def rb_clear_all_do(event):
        if not await gate(event):
            return
        uid = event.sender_id
        for key in ("rb_marker", "rb_text2", "rb_plain"):
            db.set_setting(uid, key, "")
        await logbus.customer_action(db.get_customer(uid), "content_cleared", [
            cards.kv("Fields", "marker, text2, plain")], platform="Rubika")
        await safe_edit(event, cards.card("✅ پاک شد", [
            cards.kv("Marker", f"«{db.get_marker(uid)}»"),
            "مارکر به مقدار پیش‌فرض برگشت. متن دوم و متن ساده خالی شدند.",
        ]), buttons=[_back(b"rbcontent")])

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

    @bot.on(events.CallbackQuery(pattern=rb"rbmresume_(\d+)"))
    async def rb_multi_resume(event):
        """Restart a stopped multi-account run.

        Safe because _collect_targets excludes anyone the ledger already records as
        reached: restarting from zero would message thousands of people a second
        time, which is exactly what gets accounts reported.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        if int(event.pattern_match.group(1)) != int(uid):
            await event.answer("این کارت مال تو نیست.", alert=True)
            return
        if _multi_jobs.get(int(uid)):
            await event.answer("یک ارسال چنداکانتی همین حالا در جریان است.",
                               alert=True)
            return
        accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
        if not accounts:
            await event.answer("اکانت فعالی نداری.", alert=True)
            return
        await safe_edit(event, cards.card("📤 ارسال چنداکانتی", [
            cards.kv("Accounts", len(accounts)),
            "⏳ ادامه از همان‌جا. کسانی که قبلاً پیام گرفته‌اند دوباره پیام "
            "نمی‌گیرند.",
        ]))
        asyncio.create_task(_run_multi(uid, [a["id"] for a in accounts]))

    @bot.on(events.CallbackQuery(pattern=rb"rbmstop_(\d+)"))
    async def rb_multi_stop(event):
        """Stop a whole multi-account run: the running account AND the queue.

        Stopping only the account that happens to be sending leaves the next one
        to start a second later, which reads as the button being broken. This sets
        the run-level flag first — so nothing new begins — and then asks every
        account currently in flight to stop between recipients.
        """
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        if int(event.pattern_match.group(1)) != int(uid):
            await event.answer("این کارت مال تو نیست.", alert=True)
            return
        state = _multi_jobs.get(int(uid))
        if not state:
            await event.answer("ارسالی در جریان نیست.", alert=True)
            return
        state["stop"] = True
        stopped = 0
        for account_id in state.get("account_ids") or []:
            ctl = _jobs.get(int(account_id))
            if ctl and not ctl.get("stop"):
                ctl["stop"] = True
                ctl["pause"] = False
                stopped += 1
        await event.answer(
            f"توقف ثبت شد. {stopped} اکانت در حال توقف، بقیه شروع نمی‌شوند.")

    @bot.on(events.CallbackQuery(pattern=rb"rbmskip_(\d+)"))
    async def rb_multi_skip(event):
        """End the CURRENT account's turn and move on to the next one.

        Separate from "stop everything" on purpose: one account being throttled or
        chewing through a bad contact list is not a reason to abandon a campaign
        that four other accounts are running fine.
        """
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        if int(event.pattern_match.group(1)) != int(uid):
            await event.answer("این کارت مال تو نیست.", alert=True)
            return
        state = _multi_jobs.get(int(uid))
        if not state:
            await event.answer("ارسالی در جریان نیست.", alert=True)
            return
        skipped = []
        for account_id in state.get("account_ids") or []:
            ctl = _jobs.get(int(account_id))
            if ctl and not ctl.get("stop"):
                ctl["stop"] = True
                ctl["pause"] = False
                skipped.append(ctl.get("phone") or account_id)
        if not skipped:
            await event.answer("هیچ اکانتی همین حالا در حال ارسال نیست.",
                               alert=True)
            return
        await event.answer(f"نوبت {', '.join(str(s) for s in skipped)} تمام شد؛ "
                           "به اکانت بعدی می‌رود.")

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

    @bot.on(events.CallbackQuery(data=b"rbbrainsend"))
    async def rb_brain_send(event):
        """The shortcut off the brain report: the numbers were just added as
        contacts, so the obvious next step is sending to them.

        Every account that gained contacts is pre-selected, because the brain
        works across accounts and re-ticking fifteen boxes by hand is pure
        friction. The selection is still editable before launch.
        """
        if not await gate(event):
            return
        uid = event.sender_id
        gained = [a["id"] for a in db.list_accounts(uid)
                  if a["status"] == "active" and (a.get("contacts") or 0) > 0]
        if not gained:
            await event.answer("هنوز مخاطبی اضافه نشده.", alert=True)
            return
        _state.setdefault(uid, {})["multi"] = gained
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

    register_pdf_handlers(bot, gate, safe_edit)
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
    # The second brain mode: instead of splitting a file you already have, several
    # accounts discover numbers together behind one prefix.
    rows.append([Button.inline("🌊 مغز استخری", b"rbpool"),
                 Button.inline("📋 کارهای استخری", b"pljobs")])
    rows.append(_back(b"rb"))
    body = [
        cards.kv("Selected", len(chosen)),
        cards.kv("Budget today", cards.num(db.probe_budget_left(uid))),
        cards.kv("Send cap", db.get_brain_cap(uid)),
        cards.LINE,
        "فایل شماره بین اکانت‌های انتخاب‌شده تقسیم می‌شود،",
        "هر کدام سهم خودش را مخاطب می‌کند، بعد می‌توانی ارسال کنی.",
        cards.LINE,
        "🌊 مغز استخری: شماره‌ای نداری و می‌خواهی خودش پیدا کند —",
        "چند اکانت با هم یک پیش‌شماره را می‌گردند و هر کدام به",
        "کسانی که خودش پیدا کرده پیام می‌دهد.",
    ]
    if not accounts:
        body = ["اکانت فعالی نداری."]
    await _safe_edit(event, cards.card("🧠 مغز", body), buttons=rows)


_MULTI_LABELS = {
    "queued": "▫️ در صف",
    "preparing": "⏳ آماده‌سازی",
    "running": "▶️ در حال ارسال",
    "done": "✅ پایان",
    "failed": "⚠️ خطا",
    "no_marker": "❌ مارکر پیدا نشد",
    "auth_failed": "🔴 سشن باطل",
    "error_burst": "⛔ خطاهای پشت‌سرهم",
    "stopped": "⏹ متوقف",
    "frozen": "⏸ ارسال متوقف",
    "waiting": "⏳ در انتظار",
    "busy": "⏳ اکانت مشغول",
    "no_targets": "❌ مخاطبی نداشت",
}


def _multi_buttons(customer_id) -> list:
    """Stop controls for a running multi-account send.

    The card had no buttons at all, so the only way to end a multi-account run was
    to stop each account from its own screen — and an account that had not started
    yet had no screen. "Stop everything" is the button people actually want when a
    campaign is going wrong.
    """
    cid = int(customer_id)
    return [
        [Button.inline("⛔ توقف همه", f"rbmstop_{cid}".encode())],
        [Button.inline("⏹ توقف اکانت فعلی", f"rbmskip_{cid}".encode())],
    ]


def multi_card(state: dict) -> str:
    """The live multi-account card.

    There was no such card at all: _run_multi called _prepare_and_send with
    event=None, which is the branch that skips creating a message, so the customer
    got one line — "⏳ شروع شد" — and then silence until the whole run finished.
    Two accounts sending to a thousand people each looked identical to a crash.
    """
    # Pull the live numbers out of each account's control dict while it runs. The
    # slot's own sent/failed are only written when the account finishes, so
    # reading those alone would show 0 for the entire run — the exact complaint
    # that this card exists to answer.
    for slot in state["accounts"].values():
        ctl = slot.get("ctl")
        if ctl:
            slot["sent"] = int(ctl.get("sent") or 0)
            slot["failed"] = int(ctl.get("failed") or 0)
            slot["total"] = int(ctl.get("total") or slot.get("total") or 0)
            if ctl.get("state") and slot.get("state") == "running":
                slot["state"] = ctl["state"]
            if ctl.get("reason"):
                slot["reason"] = ctl["reason"]

    rows = [cards.kv("Accounts", len(state["accounts"]))]
    total = sum(int(a.get("total") or 0) for a in state["accounts"].values())
    sent = sum(int(a.get("sent") or 0) for a in state["accounts"].values())
    failed = sum(int(a.get("failed") or 0) for a in state["accounts"].values())
    if total:
        rows.append(cards.kv("Progress",
                             f"{cards.bar(sent + failed, total)}  "
                             f"{sent + failed}/{total}"))
    rows.append(cards.kv("Sent", cards.num(sent)))
    if failed:
        rows.append(cards.kv("Failed", cards.num(failed)))
    finished = sum(1 for a in state["accounts"].values()
                   if a.get("state") not in ("queued", "preparing", "running"))
    rows.append(cards.kv("Finished", f"{finished}/{len(state['accounts'])}"))
    rows.append(cards.LINE)
    for phone, acc in state["accounts"].items():
        label = _MULTI_LABELS.get(acc.get("state"), acc.get("state") or "—")
        line = f"{label}  {phone}"
        if acc.get("total"):
            line += f" → ✉️{cards.num(acc.get('sent') or 0)}/{cards.num(acc['total'])}"
        if acc.get("failed"):
            line += f"  ⚠️{cards.num(acc['failed'])}"
        rows.append(line)
        # The reason belongs next to the account it happened to. Without it the
        # customer sees a red mark and has no idea whether to re-login, press
        # continue, or fix their marker.
        if acc.get("reason"):
            rows.append(f"   ↳ {str(acc['reason'])[:110]}")
    return cards.panel_card("📤 - #multi_send", rows)


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

    state = {"accounts": {a["phone"]: {"state": "queued", "total": 0, "sent": 0,
                                       "failed": 0, "reason": "",
                                       "account_id": a["id"]}
                          for a in accounts},
             # Set by the stop button. Checked before each account starts, so a
             # stop also cancels the accounts that have not begun yet — pressing
             # stop and then watching the next account start anyway is the worst
             # possible answer.
             "stop": False,
             "account_ids": [a["id"] for a in accounts]}
    msg = None
    if _bot:
        try:
            msg = await _bot.send_message(int(customer_id), multi_card(state),
                                          buttons=_multi_buttons(customer_id))
        except Exception:      # noqa: BLE001
            msg = None
    _multi_jobs[int(customer_id)] = state

    async def _refresh() -> None:
        """Edit the one card in place until the run ends.

        Skips an unchanged edit: Telegram rejects an edit whose content is
        identical and that error is pure noise in the log.
        """
        last = ""
        while True:
            await asyncio.sleep(config.TG_STATS_REFRESH)
            if msg is None:
                return
            text = multi_card(state)
            if text != last:
                last = text
                try:
                    await msg.edit(text,
                                   buttons=_multi_buttons(customer_id))
                except Exception:      # noqa: BLE001
                    pass

    refresher = asyncio.create_task(_refresh()) if msg else None

    async def _sequential(group):
        for acc in group:
            slot = state["accounts"][acc["phone"]]
            # Honoured BEFORE the account starts. Without this a stop only ended
            # the account that happened to be running and the queue carried on,
            # which is indistinguishable from the button doing nothing.
            if state["stop"]:
                if slot["state"] in ("queued", "preparing"):
                    slot["state"] = "stopped"
                    slot["reason"] = "به‌درخواست شما متوقف شد"
                continue
            if db.are_sends_frozen():
                slot["state"] = "frozen"
                continue
            await _prepare_and_send(customer_id, acc, "marker", "", None,
                                    progress=slot)

    try:
        await asyncio.gather(*[_sequential(g) for g in groups.values()],
                            return_exceptions=True)
    finally:
        if refresher:
            refresher.cancel()
        _multi_jobs.pop(int(customer_id), None)

    if msg:
        # The finished card drops the stop buttons and offers continue instead,
        # so a stopped run is one press from resuming rather than a dead end.
        buttons = [_back(b"rbaccs")]
        if state["stop"]:
            buttons = [[Button.inline("✅ ادامه‌ی ارسال چنداکانتی",
                                      f"rbmresume_{int(customer_id)}".encode())],
                       _back(b"rbaccs")]
        try:
            await msg.edit(multi_card(state), buttons=buttons)
        except Exception:      # noqa: BLE001
            pass
    await logbus.customer_action(cust, "multi_send_done", [
        cards.kv("Accounts", len(accounts)),
        cards.kv("Sent", cards.num(sum(int(a.get("sent") or 0)
                                       for a in state["accounts"].values()))),
    ], platform="Rubika")


async def _prepare_and_send(customer_id, acc: dict, mode: str, text: str,
                            event=None, progress: dict = None) -> None:
    """Collect the target list, then run the send under a single session claim.

    `progress` is the multi-account card's slot for this account. It is filled in
    as the run proceeds so the shared card can show what each account is doing;
    the single-account path passes nothing and behaves exactly as before.
    """
    phone = acc["phone"]
    if progress is not None:
        progress["state"] = "preparing"
    msg = None
    if event is not None:
        try:
            msg = await _bot.send_message(int(customer_id), cards.card(
                "🚀 ارسال", [cards.kv("Phone", phone), "⏳ آماده‌سازی ..."]))
        except Exception:
            msg = None
    try:
        targets = await _collect_targets(customer_id, acc, mode)
    except Exception as exc:  # noqa: BLE001
        # A transient platform answer gets its Persian sentence on the card. The
        # customer was being shown a raw Python dict —
        # "ServerError: {'status': 'ERROR_TRY_AGAIN', ...}" — for a condition that
        # means nothing more than "ask again in a moment". The technical reason is
        # NOT dropped: logbus.error still records type, message and traceback
        # under the error code below.
        transient = rb.is_transient_failure(exc)
        if progress is not None:
            progress["state"] = "failed"
            progress["reason"] = (
                "سرور روبیکا موقتاً پاسخ نداد؛ چند دقیقه بعد دوباره بزن"
                if transient else f"{type(exc).__name__}: {str(exc)[:90]}")
        code = await logbus.error(exc, context=f"rb targets {phone}",
                                  customer=customer_id, kind="prepare")
        if msg:
            try:
                rows = [logbus.humanize_error(exc, kind="prepare")] if transient \
                    else []
                rows.append(cards.kv("کد خطا", code, width=8))
                await msg.edit(cards.card("⚠️ مشکلی پیش آمد", rows),
                               buttons=[_back(b"rbaccs")])
            except Exception:
                pass
        return
    if not targets:
        if progress is not None:
            progress["state"] = "no_targets"
            progress["reason"] = "این اکانت هیچ مخاطبی نداشت"
        if msg:
            try:
                await msg.edit(cards.card("🚀 ارسال", [
                    cards.kv("Phone", phone), "مخاطبی برای ارسال پیدا نشد."]),
                    buttons=[_back(b"rbaccs")])
            except Exception:
                pass
        return
    if progress is not None:
        progress["total"] = len(targets)
        progress["state"] = "running"
    await _run_send(customer_id, acc, mode, text, targets, msg,
                    progress=progress)


def _guids_only(items) -> list:
    """Normalise a recipient list to plain guid STRINGS.

    Every consumer wants a string: rb.send_text takes a guid, db.mark_sent stores
    one, and the worker payload does str(t). But get_ordered_recipients yields
    {"guid": ..., "name": ...} dicts, so the send loop was handing whole dicts to
    send_text and str()-ing dicts into the worker payload. Normalising once here
    means no consumer has to know which shape it received.
    """
    out = []
    for item in items or []:
        if isinstance(item, dict):
            guid = item.get("guid") or item.get("object_guid")
        else:
            guid = item
        if guid:
            out.append(str(guid))
    return out


async def _collect_targets(customer_id, acc: dict, mode: str = "marker") -> list:
    """The account's own recipients as guid strings, ordered.

    Runs under its own session claim. This is ONLY about *who* to send to, not
    about the marker: a plain-text send has no marker, so gating the recipient
    list on a marked post (as the worker used to) reported "no contacts" on
    accounts that had plenty. The marker is verified later, in the send phase,
    for marker mode only.
    """
    phone = acc["phone"]
    key = _key(customer_id, phone)
    import session_store
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        async def _prep_op():
            return await worker.api_call(w, "POST", "/prepare", {
                "customer_id": customer_id, "phone": phone, "mode": mode,
                "marker": db.get_marker(customer_id)},
                timeout=config.PREPARE_CALL_TIMEOUT)

        # A worker with no session file for this account reads ZERO contacts
        # rather than failing, so the repair path matters here as much as it does
        # for channels: it is the same missing session, one symptom later.
        #
        # run_resilient, not run_with_repair: a Rubika ERROR_TRY_AGAIN is not a
        # session problem and used to abort the account's whole campaign on the
        # first hiccup, before a single message went out.
        prep = await session_store.run_resilient(customer_id, acc, _prep_op)
        return _guids_only(prep.get("targets"))

    import account_conn
    async with busy.hold(key, "precheck", customer_id=customer_id,
                         extra={"account_id": acc["id"]}, settle=False) as held:
        if not held.ok:
            return []

        async def _work(client):
            return await rb.get_ordered_recipients(client)

        async def _work_op():
            return await account_conn.call(customer_id, phone, _work,
                                           timeout=config.PREPARE_TIMEOUT)

        got = await session_store.run_resilient(customer_id, acc, _work_op)
        # Defensive: a tuple here is the shape that produced "Targets: 2" and two
        # failed sends on an account with hundreds of contacts. The contract is a
        # list now, and anything else is coerced rather than silently messaged.
        if isinstance(got, tuple):
            got = got[0] if got and isinstance(got[0], list) else []
        return _guids_only(got)


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
                                  customer=uid, kind="login")
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
        # notify=False: this card IS the notification. The reason comes from
        # humanize_error, so a mistyped code says exactly that rather than handing
        # the customer an error code to forward to support about their own typo.
        await logbus.error(exc, context=f"rb login code {phone}",
                          customer=uid, kind="code", notify=False)
        await _respond(event, cards.card("⚠️ کد پذیرفته نشد", [
            logbus.humanize_error(exc, kind="code"),
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
        # Reported to the customer at the end, and updated live along the way.
        # The old report said only "Members added", so a channel that was created
        # WITHOUT the advert in it looked like a complete success — the one thing
        # the customer needed to know was the one thing missing.
        marker_found = forwarded = False
        forward_error = ""
        member_count = 0

        async def _live(*rows) -> None:
            """Edit the customer's card in place.

            Creating the channel and seeding it can take half an hour with the
            default target of 300 members in batches of 80. With no updates in
            between, a working campaign is indistinguishable from a hung one, and
            customers stop it and start again — which is how accounts get
            reported.
            """
            if msg is None:
                return
            try:
                await msg.edit(cards.card("📢 کانال", list(rows)))
            except Exception:      # noqa: BLE001 - a card is never worth failing
                pass

        await _live(cards.kv("Phone", phone), cards.kv("Channel", title),
                    "⏳ ساخت کانال ...")
        try:
            import session_store

            # Both steps run through run_with_repair. An INVALID_AUTH here is
            # usually not a dead account at all: it is a session file that is not
            # on the server running the work (a re-login that moved servers, a
            # token login that never wrote a file, a rebuilt worker). The repair
            # writes the stored session onto the right server and retries once.
            if remote:
                # The marker MUST go with this call: the worker forwards that
                # marked post into the new channel as its first message. Without
                # it every campaign produced an empty channel and then seeded
                # hundreds of members into it.
                async def _create_op():
                    return await worker.api_call(w, "POST", "/channel/create", {
                        "customer_id": customer_id, "phone": phone,
                        "title": title, "marker": marker or ""}, timeout=240)

                created = await session_store.run_with_repair(
                    customer_id, acc, _create_op)
                guid = created.get("channel_guid")
                if not guid:
                    raise RuntimeError(
                        "worker returned no channel_guid: "
                        f"{str(created)[:160]}")
                marker_found = bool(created.get("marker_found"))
                forwarded = bool(created.get("forwarded"))
                forward_error = created.get("forward_error") or ""
                await _live(
                    cards.kv("Phone", phone), cards.kv("Channel", title),
                    "✅ کانال ساخته شد",
                    cards.kv("Post", "✅ ارسال شد" if forwarded
                             else "❌ ارسال نشد"),
                    f"⏳ عضو کردن مخاطبین تا {config.CHANNEL_MEMBER_TARGET} نفر — "
                    "چند دقیقه طول می‌کشد.")
                if marker and not created.get("forwarded"):
                    # The channel exists, so this is not fatal — but it must not
                    # pass silently, or the owner sees an empty channel and no
                    # explanation anywhere.
                    await logbus.event("⚠️ - #rb_channel_no_post", [
                        cards.kv("Customer", customer_id),
                        cards.kv("Phone", phone),
                        cards.kv("Channel", guid),
                        cards.kv("Marker", marker),
                        cards.kv("MarkerFound",
                                 "yes" if created.get("marker_found") else "no"),
                        cards.kv("Error",
                                 created.get("forward_error") or "-"),
                    ])
                await asyncio.sleep(config.CAMPAIGN_STEP_DELAY)

                async def _add_op():
                    return await worker.api_call(w, "POST", "/channel/add", {
                        "customer_id": customer_id, "phone": phone,
                        "channel_guid": guid,
                        "target": config.CHANNEL_MEMBER_TARGET,
                        "batch": config.CHANNEL_ADD_BATCH,
                        "delay": config.CHANNEL_ADD_DELAY}, timeout=1800)

                added = await session_store.run_with_repair(
                    customer_id, acc, _add_op)
                member_count = int(added.get("added") or 0)
            else:
                import account_conn

                # Channel creation and member-adding are signed operations that
                # Rubika rejects with INVALID_AUTH over a reused warm socket (the
                # account may have JUST been sending). Each runs on its own fresh
                # single-use connection — exactly what the reference does.
                #
                # Finding the marked post, creating the channel and forwarding
                # that post into it all share ONE connection. Only create_channel
                # used to run here, so the channel came out empty.
                async def _create(client):
                    message_id = None
                    if marker:
                        message_id = await rb.find_marked_message(client, marker)
                    new_guid = await rb.create_channel_checked(client, title)
                    forwarded = False
                    forward_error = ""
                    if message_id and new_guid:
                        saved_guid = await rb.get_self_guid(client)
                        try:
                            await rb.forward_message(client, saved_guid,
                                                     new_guid, message_id)
                            forwarded = True
                        except Exception as exc:      # noqa: BLE001
                            forward_error = (f"{type(exc).__name__}: "
                                             f"{str(exc)[:160]}")
                    return new_guid, bool(message_id), forwarded, forward_error

                async def _create_op():
                    return await account_conn.signed_call(
                        customer_id, phone, _create, timeout=240)

                guid, marker_found, forwarded, forward_error = \
                    await session_store.run_with_repair(
                        customer_id, acc, _create_op)
                if not guid:
                    raise RuntimeError("create_channel returned no guid")
                await _live(
                    cards.kv("Phone", phone), cards.kv("Channel", title),
                    "✅ کانال ساخته شد",
                    cards.kv("Post", "✅ ارسال شد" if forwarded
                             else "❌ ارسال نشد"),
                    f"⏳ عضو کردن مخاطبین تا {config.CHANNEL_MEMBER_TARGET} نفر — "
                    "چند دقیقه طول می‌کشد.")
                if marker and not forwarded:
                    await logbus.event("⚠️ - #rb_channel_no_post", [
                        cards.kv("Customer", customer_id),
                        cards.kv("Phone", phone),
                        cards.kv("Channel", guid),
                        cards.kv("Marker", marker),
                        cards.kv("MarkerFound", "yes" if marker_found else "no"),
                        cards.kv("Error", forward_error or "-"),
                    ])
                await asyncio.sleep(config.CAMPAIGN_STEP_DELAY)

                async def _seed(client):
                    return await rb.seed_channel_with_contacts(
                        client, guid, target=config.CHANNEL_MEMBER_TARGET,
                        batch=config.CHANNEL_ADD_BATCH,
                        delay=config.CHANNEL_ADD_DELAY)

                async def _seed_op():
                    return await account_conn.signed_call(
                        customer_id, phone, _seed, timeout=1800)

                member_count = await session_store.run_with_repair(
                    customer_id, acc, _seed_op) or 0
        except Exception as exc:  # noqa: BLE001
            # A refusal that is NOT an auth problem gets its own sentence. Showing
            # only an error code for this sent the owner hunting a session bug
            # that did not exist — the session was signing fine the whole time.
            # Recognise the verdict however it arrives. Locally it is the real
            # exception; from a worker it comes back inside a WorkerAPIError whose
            # text carries the 403 detail, because an HTTP boundary cannot
            # transport a Python class.
            text = str(exc)
            denied = (isinstance(exc, rb.ChannelNotPermitted)
                      or type(exc).__name__ == "ChannelNotPermitted"
                      or "ChannelNotPermitted" in text
                      or "not permitted to create a channel" in text)
            code = await logbus.error(exc, context=f"rb channel {phone}",
                                      customer=customer_id)
            if denied:
                text = cards.card("⛔ این اکانت اجازهٔ ساخت کانال ندارد", [
                    cards.kv("Phone", phone),
                    cards.kv("کد خطا", code, width=8),
                    cards.LINE,
                    "سشن این اکانت سالم است و ارسال با آن کار می‌کند — روبیکا "
                    "فقط ساختِ کانال را برایش رد می‌کند. معمولاً برای شماره‌های "
                    "تازه یا محدودشده پیش می‌آید.",
                    "با یک اکانت دیگر امتحان کن، یا چند روز از این شماره برای "
                    "ارسال عادی استفاده کن و بعد دوباره تست کن.",
                ])
                if msg:
                    try:
                        await msg.edit(text, buttons=[_back(b"rbaccs")])
                        return
                    except Exception:
                        pass
                try:
                    await _bot.send_message(int(customer_id), text,
                                            buttons=[_back(b"rbaccs")])
                except Exception:
                    pass
                return
            if msg:
                try:
                    await msg.edit(cards.card("⚠️ مشکلی پیش آمد", [
                        cards.kv("کد خطا", code, width=8)]),
                        buttons=[_back(b"rbaccs")])
                except Exception:
                    pass
            return

    # Say plainly whether the advert reached the channel. A report of
    # "Members added: 300" on a channel with no post in it is worse than useless:
    # the customer believes the campaign ran.
    if not marker:
        post_line = "— بدون مارکر (پستی ارسال نشد)"
    elif forwarded:
        post_line = "✅ ارسال شد"
    elif not marker_found:
        post_line = f"❌ پیام نشان‌دار «{marker}» در Saved پیدا نشد"
    else:
        post_line = f"❌ ارسال نشد — {forward_error or 'دلیل نامعلوم'}"

    rows = [
        cards.kv("Phone", phone),
        cards.kv("Channel", title),
        cards.kv("Post", post_line),
        cards.kv("Members added",
                 f"{cards.num(member_count)} از {config.CHANNEL_MEMBER_TARGET}"),
        cards.kv("Marker", f"«{marker}»" if marker else "—"),
    ]
    if marker and not forwarded:
        rows.append("⚠️ کانال ساخته شد ولی پست تبلیغ داخلش نیست. مارکر را در "
                    "کپشن پیام ذخیره‌شده بگذار و دوباره بساز.")
    if not member_count:
        rows.append("⚠️ هیچ مخاطبی عضو نشد.")
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
    """A phone number, or "" when the input is not one.

    There used to be no UPPER bound: any input with ten or more digits was
    accepted. So a session token pasted at the phone prompt had its punctuation
    stripped and the remaining two hundred digits were sent to Rubika as a phone
    number, which answered INVALID_INPUT and logged an error whose "Where" field
    was the whole token. A number is at most 13 digits, so the bound is real, not
    a guess.
    """
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if digits.startswith("0098"):
        digits = digits[4:]
    if digits.startswith("98") and len(digits) >= 12:
        digits = "0" + digits[2:]
    if len(digits) == 10 and not digits.startswith("0"):
        digits = "0" + digits
    if not 10 <= len(digits) <= 13:
        return ""
    return digits


def _looks_like_session_token(text: str) -> bool:
    """Did the customer paste a session token where a phone was asked for?

    Worth detecting on purpose: the two are asked for in almost the same place,
    and "شماره خوانده نشد" for a perfectly good token sends people in circles.
    """
    raw = (text or "").strip()
    return raw.upper().startswith("MMSESS:") or len(raw) > 40


async def _step_phone(event, st):
    uid = event.sender_id
    phone = _normalize_phone_input(event.raw_text)
    if not phone:
        if _looks_like_session_token(event.raw_text):
            # Handle it here rather than sending the customer away. They pasted a
            # valid credential; refusing it on a technicality and telling them to
            # find another button is how a 240-digit "phone number" reached
            # Rubika and came back INVALID_INPUT with the whole token in the log.
            await _respond(event, cards.card("این توکن سشن است، نه شماره", [
                "با همین توکن واردت می‌کنم — لازم نیست جای دیگری بروی."]))
            await _step_token(event, st)
            return
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
        await logbus.error(exc, context=f"rb 2fa {phone}", customer=uid,
                          kind="password", notify=False)
        await _respond(event, cards.card("⚠️ رمز پذیرفته نشد", [
            logbus.humanize_error(exc, kind="password"),
        ]), buttons=[_back(b"rb")])
        return
    st["step"] = "rb_code"
    await _respond(event, cards.card("📩 کد ورود", [
        "حالا کد ورود را بفرست."]), buttons=[_back(b"rb")])


async def _step_token(event, st):
    uid = event.sender_id
    raw = (event.raw_text or "").strip()

    # Validate the token BEFORE anything is written, and keep the customer in
    # this step on every failure so they can simply paste again. The old version
    # popped the state and answered "توکن باید با MMSESS: شروع شود" for EVERY
    # problem — including a perfectly formatted token that was missing its auth —
    # so the one message people saw never matched what was actually wrong.
    def _retry(*rows):
        _state[uid] = {"step": "rb_token"}
        return _respond(event, cards.card("توکن پذیرفته نشد", list(rows)),
                        buttons=[_back(b"rb")])

    values = db.session_unpack(raw)
    if not values:
        await _retry("این متن یک توکن سشن نیست.",
                     "توکن با MMSESS: شروع می‌شود و از دکمهٔ «🔑 توکن سشن» روی "
                     "همان اکانت گرفته می‌شود.",
                     "دوباره بفرست یا لغو کن.")
        return
    missing = [name for name in ("phone", "auth", "private_key")
               if not values.get(name)]
    if missing:
        # Naming the missing field matters: a token with no private_key can READ
        # but can never SIGN, so it would be accepted here and then fail every
        # channel creation and every send with INVALID_AUTH — days later, looking
        # like a completely unrelated bug.
        await _retry(cards.kv("Missing", ", ".join(missing)),
                     "این توکن کامل نیست و با آن نمی‌شود کار کرد.",
                     "توکن را کامل و بدون بریدگی کپی کن و دوباره بفرست.")
        return

    _state.pop(uid, None)
    phone = rb.normalize_phone(values["phone"])
    values["phone"] = phone
    display = "0" + phone[2:] if phone.startswith("98") else phone

    if db.get_account_by_phone(uid, display):
        await _respond(event, cards.card("این شماره از قبل هست", [
            cards.kv("Phone", display),
            "اگر از کار افتاده، از لیست اکانت‌ها «ورود مجدد» را بزن.",
        ]), buttons=[_back(b"rbaccs")])
        return

    msg = await _respond(event, cards.card("🔑 ورود با توکن سشن", [
        cards.kv("Phone", display), "⏳ بررسی توکن ..."]))

    # PROVE the session works before an account row exists for it.
    #
    # This used to create the account, store the blob, write the session file and
    # report "Status: SUCCESS / Session Saved: YES" without ever connecting. A
    # dead or truncated token therefore produced a perfectly healthy-looking
    # account that failed on its first real operation with INVALID_AUTH — and the
    # customer had no reason to suspect the token, because the login had said
    # SUCCESS. The reference verifies first and REFUSES, which is the whole
    # difference.
    w = await worker.pick_worker_for_login()
    try:
        checked = await _verify_session_token(uid, w, phone, values)
        name = checked["name"]
        guid = checked["guid"]
        contacts = checked["contacts"]
    except Exception as exc:  # noqa: BLE001
        code = await logbus.error(exc, context=f"rb token login {display}",
                                  customer=uid)
        text = cards.card("❌ ورود با توکن انجام نشد", [
            cards.kv("Phone", display),
            cards.kv("کد خطا", code, width=8),
            cards.LINE,
            "این توکن پذیرفته نشد. اگر مطمئنی درست کپی شده، با کد ورود "
            "وارد شو.",
        ])
        try:
            await msg.edit(text, buttons=[_back(b"rb")])
        except Exception:
            await _respond(event, text, buttons=[_back(b"rb")])
        return

    if name:
        values["name"] = name
    if guid:
        values["guid"] = str(guid)
    aid = db.add_account(uid, display, name=values.get("name") or "",
                         worker_id=(w or {}).get("id"))
    db.set_session_blob(uid, aid, values)

    # Write the session file where the work will run. Storing the five values in
    # the database and stopping there is what made a token login report
    # "Session Saved: YES" while no server had a session for the account at all.
    import session_store
    placed = await session_store.place(uid, db.get_account(uid, aid))
    rows = [
        cards.kv("Status", "SUCCESS"),
        cards.kv("Phone", display),
        cards.kv("Login Method", "SESSION"),
        # Report what was actually established, not a flat YES. A login that only
        # got an inconclusive answer out of the worker must not claim it verified
        # the session — that is the same false confidence that made a dead token
        # look like a healthy account.
        cards.kv("Verified", "YES" if checked["verified"] else "UNCONFIRMED"),
        # None means "not counted here", which is different from zero. Printing 0
        # made an account with 1376 contacts look empty.
        cards.kv("Contacts", "—" if contacts is None else cards.num(contacts)),
        cards.kv("Session Saved", "YES" if placed else "NO"),
        cards.kv("Time", cards.now()),
    ]
    if not checked["verified"]:
        rows.append("⚠️ سشن نوشته شد ولی ورکر نتوانست تأییدش کند"
                    + (f" ({checked['note']})" if checked.get("note") else "")
                    + ". اگر اولین ارسال خطا داد، با کد ورود وارد شو.")
    if not placed:
        rows.append("⚠️ سشن روی سرور نوشته نشد — توکن را بررسی کن.")
    footer = f"--| 🌍 - Worker : #{(w or {}).get('tag', 'master')}"
    await logbus.event("✅ - #rubika_login", rows + [cards.kv("Customer", uid)],
                       footer=footer)
    await _respond(event, cards.panel_card("✅ - #rubika_login", rows,
                                          footer=footer),
                   buttons=[[Button.inline("🚀 ارسال", f"rbrun_{aid}".encode())],
                            _back(b"rb")])


async def _verify_session_token(customer_id, w, phone: str, values: dict):
    """Prove a pasted session actually works. Returns (name, guid, contacts).

    Raises when the session is not usable, so the caller never creates an account
    row for a token that cannot work. Two paths, both taken from the reference:

      LOCAL  — write the session, connect once, call get_me() and read the
               contact list, then disconnect. get_me is a signed call, so it
               proves the private_key is present and usable, not merely that the
               file exists.
      REMOTE — push the session to the worker (write-only, never connects, so it
               cannot provoke AUTH_FROM_ANOTHER) and then ask the worker's
               /account/verify. If the worker says the session is dead we refuse
               here instead of discovering it on the customer's first campaign.
    """
    import account_conn

    if w and not worker.is_local(w):
        pushed = await worker.push_session(w, customer_id, phone, values)
        if not pushed:
            raise RuntimeError(
                f"worker {w.get('tag')} could not store the session: "
                f"{worker.last_push_error() or 'no reason reported'}")
        verdict = await worker.api_call(w, "POST", "/account/verify", {
            "customer_id": customer_id, "phone": phone}, timeout=120)
        if verdict.get("dead"):
            raise RuntimeError("Rubika rejected this session (it is expired or "
                               "was revoked)")
        # An inconclusive probe is NOT proof of health, and must not be reported
        # as one. This returned a hard 0 for the contact count and the card then
        # printed "Contacts: 0" — which reads as "this account has no contacts".
        # The same account showed 1376 recipients on its first send minutes
        # later. The count was never read on this path at all; saying so is the
        # only honest option.
        proven = not verdict.get("skipped")
        return {"name": values.get("name") or "",
                "guid": values.get("guid") or "",
                "contacts": None,          # not counted on the remote path
                "verified": proven,
                "note": "" if proven else (verdict.get("reason") or
                                           "worker could not confirm")}

    # Local: import write-only first, then one connection that proves it signs.
    if not rb.import_session(phone, customer_id, values):
        raise RuntimeError("the session could not be written on this server")
    async with account_conn.fresh_connection(customer_id, phone) as client:
        me = await client.get_me()
        guid = rb._guid_of(me) or values.get("guid") or ""   # noqa: SLF001
        name = rb._name_of(me, "") or ""                     # noqa: SLF001
        contacts = len(await rb.get_contacts_full(client))
    return {"name": name, "guid": guid, "contacts": contacts,
            "verified": True, "note": ""}


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



# --------------------------------------------------------------------------- #
# PV photo archive -> PDF
# --------------------------------------------------------------------------- #
# TWO MODES, AND WHY THE FAST ONE IS SAFE
# ---------------------------------------
# "parallel" keeps several downloads in flight over the SAME connection. That is
# multiplexing, not a second connection — a second connection would revoke the
# session, which is the bug this whole project is built around avoiding. The
# bottleneck here is network latency per photo, so overlapping the waits is worth
# roughly six times the throughput.
#
# "safe" downloads strictly one at a time. It is what the base project did.
#
# "auto" starts parallel and FALLS BACK to safe after a run of failures, so a
# platform that rejects the faster pattern degrades instead of failing the export.
#
# Delivery is cumulative: a PDF every PV_EXPORT_PDF_BATCH photos, so the customer
# sees progress. Each photo is prepared exactly once (pdf_export.prepare_image),
# which is what keeps those repeated rebuilds cheap.
# --------------------------------------------------------------------------- #
PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pdf")
os.makedirs(PDF_DIR, exist_ok=True)


def _pdf_mode(customer_id) -> str:
    mode = (db.get_setting(customer_id, "pv_mode")
            or config.PV_EXPORT_MODE_DEFAULT)
    return mode if mode in ("auto", "parallel", "safe") else "auto"


def _pdf_parallel(customer_id) -> int:
    return config.clamp_pv_parallel(
        db.get_int_setting(customer_id, "pv_parallel", config.PV_EXPORT_PARALLEL))


class _PdfDelivery:
    """Collects prepared JPEGs and ships a cumulative PDF every N photos.

    Holding PREPARED bytes rather than raw photos is the memory difference
    between roughly 600 MB and 150 MB for a 2000-photo account.
    """

    def __init__(self, customer_id, phone: str, batch: int):
        self.customer_id = customer_id
        self.phone = phone
        self.batch = max(1, int(batch))
        self.jpegs: list = []
        self.files_sent = 0
        self._next_at = self.batch

    @property
    def found(self) -> int:
        return len(self.jpegs)

    async def add(self, jpeg: bytes) -> None:
        if not jpeg:
            return
        self.jpegs.append(jpeg)
        if self.found >= self._next_at:
            self._next_at += self.batch
            await self.flush(final=False)

    async def flush(self, final: bool) -> None:
        if not self.jpegs:
            return
        import pdf_export
        path = os.path.join(
            PDF_DIR, f"pv_{self.customer_id}_{self.phone}_{self.found}.pdf")
        try:
            pages = await asyncio.to_thread(pdf_export.build_pdf_from_jpegs,
                                            list(self.jpegs), path)
            caption = cards.panel_card(
                "🖼 - #pv_archive" + ("" if final else " (در حال ادامه)"), [
                    cards.kv("Phone", self.phone),
                    cards.kv("Photos", cards.num(pages)),
                    cards.kv("State", "🏁 پایان" if final else "⏳ ادامه دارد"),
                ])
            await _bot.send_file(int(self.customer_id), path, caption=caption,
                                 force_document=True)
            self.files_sent += 1
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="pdf build", customer=self.customer_id,
                               notify=False)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


async def _run_pdf(customer_id, acc: dict, msg=None) -> None:
    """Collect the account's private-chat photos and deliver them as PDFs."""
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)

    # A server-side cap, separate from the session registry: this job decodes
    # images in memory, and a handful started at once exhausts a small VPS.
    if not busy.take_slot("pdf", config.PV_EXPORT_MAX_CONCURRENT):
        if msg:
            try:
                await msg.edit(cards.card("🖼 آرشیو عکس", [
                    "الان در دسترس نیست — مشتری دیگری در حال استفاده است.",
                    "چند دقیقه بعد دوباره امتحان کن.",
                ]), buttons=[_back(b"rbaccs")])
            except Exception:
                pass
        return

    mode = _pdf_mode(customer_id)
    parallel = _pdf_parallel(customer_id)
    delivery = _PdfDelivery(customer_id, phone, config.PV_EXPORT_PDF_BATCH)
    ctl = {"stop": False, "pause": False, "state": "running", "phone": phone,
           "mode": mode, "fallback": False, "found": 0,
           "total": config.PV_EXPORT_MAX_PHOTOS}
    _jobs[aid] = ctl

    try:
        async with busy.hold(key, "pdf", customer_id=customer_id,
                             extra={"account_id": aid}) as held:
            if not held.ok:
                if msg:
                    try:
                        await msg.edit(cards.card("🖼 آرشیو عکس",
                                                  [busy.reason(key)]),
                                       buttons=[_back(b"rbaccs")])
                    except Exception:
                        pass
                return

            await logbus.customer_action(db.get_customer(customer_id),
                                        "pdf_export_start", [
                cards.kv("Phone", phone),
                cards.kv("Mode", mode),
                cards.kv("Parallel", parallel if mode != "safe" else 1),
                cards.kv("Batch", config.PV_EXPORT_PDF_BATCH),
            ], platform="Rubika")

            progress = None
            if msg is not None:
                progress = asyncio.create_task(_pdf_progress_loop(aid, ctl, msg,
                                                                  delivery))
            try:
                w = worker.worker_for_account(acc)
                if w and not worker.is_local(w):
                    await _pdf_remote(customer_id, acc, w, ctl, delivery, mode,
                                      parallel)
                else:
                    await _pdf_local(customer_id, acc, ctl, delivery, mode,
                                     parallel)
                if ctl["state"] == "running":
                    ctl["state"] = "done"
            except Exception as exc:  # noqa: BLE001
                ctl["state"] = "failed"
                ctl["error"] = await logbus.error(
                    exc, context=f"pdf export {phone}", customer=customer_id)
            finally:
                if progress:
                    progress.cancel()
    finally:
        busy.free_slot("pdf")
        _jobs.pop(aid, None)

    await delivery.flush(final=True)
    db.usage_incr(customer_id, "pdf", delivery.found)
    labels = {"done": "🏁 پایان", "stopped": "⛔ متوقف شد", "failed": "⚠️ خطا"}
    rows = [
        cards.kv("Phone", phone),
        cards.kv("Photos", cards.num(delivery.found)),
        cards.kv("Files sent", delivery.files_sent),
        cards.kv("Mode used", "🐢 آرام" if ctl["mode"] == "safe" else "⚡ سریع"),
    ]
    if ctl.get("fallback"):
        rows.append(cards.kv("Note", "حالت سریع به آرام تغییر کرد"))
    rows.append(cards.kv("Result", labels.get(ctl["state"], ctl["state"])))
    await logbus.customer_action(db.get_customer(customer_id), "pdf_export_done",
                                rows, platform="Rubika")
    text = cards.panel_card("🖼 - #pv_archive_report", rows)
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


async def _pdf_local(customer_id, acc, ctl, delivery, mode, parallel) -> None:
    """Collect on this machine, over ONE connection."""
    import account_conn
    import pdf_export
    phone = acc["phone"]
    use_parallel = mode in ("auto", "parallel") and parallel > 1

    async def _collect(client):
        guids = await rb.get_chat_list_guids(client, only_users=True)
        consecutive_bad = 0
        for guid in guids[:config.PV_EXPORT_MAX_CHATS]:
            if ctl["stop"] or delivery.found >= config.PV_EXPORT_MAX_PHOTOS:
                return
            inlines = []
            async for _mid, file_inline in rb.iter_chat_photos(client, guid):
                inlines.append(file_inline)
                if len(inlines) >= 400:
                    break
            if not inlines:
                continue

            if use_parallel and not ctl["fallback"]:
                semaphore = asyncio.Semaphore(parallel)

                async def _one(file_inline):
                    async with semaphore:
                        try:
                            return await rb.download_photo(client, file_inline)
                        except Exception:
                            return None

                step = parallel * 4
                for start in range(0, len(inlines), step):
                    if ctl["stop"] or delivery.found >= config.PV_EXPORT_MAX_PHOTOS:
                        return
                    chunk = inlines[start:start + step]
                    blobs = await asyncio.gather(*[_one(fi) for fi in chunk],
                                                 return_exceptions=True)
                    bad = 0
                    for blob in blobs:
                        if isinstance(blob, Exception) or not blob:
                            bad += 1
                            continue
                        jpeg = await asyncio.to_thread(
                            pdf_export.prepare_image, blob,
                            config.PV_EXPORT_PDF_QUALITY,
                            config.PV_EXPORT_PDF_MAX_EDGE)
                        await delivery.add(jpeg)
                        ctl["found"] = delivery.found
                    consecutive_bad = consecutive_bad + bad if bad else 0
                    if consecutive_bad >= config.PV_EXPORT_FALLBACK_ERRORS:
                        # Degrade instead of failing the whole export.
                        ctl["fallback"] = True
                        ctl["mode"] = "safe"
                        consecutive_bad = 0
                        break
            else:
                for file_inline in inlines:
                    if ctl["stop"] or delivery.found >= config.PV_EXPORT_MAX_PHOTOS:
                        return
                    try:
                        blob = await rb.download_photo(client, file_inline)
                    except Exception:
                        continue
                    jpeg = await asyncio.to_thread(
                        pdf_export.prepare_image, blob,
                        config.PV_EXPORT_PDF_QUALITY,
                        config.PV_EXPORT_PDF_MAX_EDGE)
                    await delivery.add(jpeg)
                    ctl["found"] = delivery.found

    await account_conn.call(customer_id, phone, _collect, timeout=3600)
    if ctl["stop"]:
        ctl["state"] = "stopped"


async def _pdf_remote(customer_id, acc, w, ctl, delivery, mode, parallel) -> None:
    """Start the collection on the worker and stream batches back by polling.

    Polling instead of one big response: a 2000-photo account returned in a single
    base64 body is roughly 800 MB through an SSH tunnel, which either times out or
    exhausts memory.
    """
    import base64
    phone = acc["phone"]
    started = await worker.api_call(w, "POST", "/pvexport/start", {
        "customer_id": customer_id, "phone": phone,
        "max_chats": config.PV_EXPORT_MAX_CHATS,
        "max_photos": config.PV_EXPORT_MAX_PHOTOS,
        "mode": mode, "parallel": parallel}, timeout=120)
    job_id = started.get("job_id")
    if not job_id:
        ctl["state"] = "failed"
        return

    fails = 0
    while True:
        await asyncio.sleep(config.PV_EXPORT_POLL_SEC)
        if ctl["stop"]:
            try:
                await worker.api_call(w, "POST", f"/pvexport/stop/{job_id}",
                                      timeout=30)
            except Exception:
                pass
        try:
            status = await worker.api_call(
                w, "GET", f"/pvexport/status/{job_id}?take=40", timeout=120)
            fails = 0
        except Exception:
            fails += 1
            if fails >= config.PV_EXPORT_MAX_POLL_FAILS:
                ctl["state"] = "failed"
                return
            continue

        for encoded in status.get("batch") or []:
            try:
                await delivery.add(base64.b64decode(encoded))
            except Exception:
                continue
        ctl["found"] = delivery.found
        if status.get("fallback"):
            ctl["fallback"] = True
            ctl["mode"] = "safe"

        remote_state = status.get("state")
        if remote_state != "running" and not status.get("pending"):
            ctl["state"] = {"done": "running", "stopped": "stopped",
                            "failed": "failed"}.get(remote_state, remote_state)
            if ctl["state"] == "running":
                ctl["state"] = "running"     # let the caller mark it done
            return


async def _pdf_progress_loop(account_id, ctl, msg, delivery) -> None:
    last = ""
    try:
        while True:
            await asyncio.sleep(config.CONTACT_PROGRESS_EVERY)
            text = cards.panel_card("🖼 - #pv_archive", [
                cards.kv("Phone", ctl["phone"]),
                cards.kv("Photos", cards.num(delivery.found)),
                cards.kv("Files sent", delivery.files_sent),
                cards.kv("Mode", "🐢 آرام" if ctl["mode"] == "safe"
                         else "⚡ سریع"),
                cards.kv("Next file at", delivery._next_at),   # noqa: SLF001
            ])
            if text != last:
                last = text
                try:
                    await msg.edit(text, buttons=[[Button.inline(
                        "⛔ توقف", f"rbstop_{account_id}".encode())]])
                except Exception:
                    pass
            if ctl.get("state") != "running":
                return
    except asyncio.CancelledError:
        return


def register_pdf_handlers(bot, gate, safe_edit) -> None:
    """PDF screens. Split out so the section menu file stays readable."""
    from telethon import events

    @bot.on(events.CallbackQuery(data=b"rbpdf"))
    async def rb_pdf(event):
        if not await gate(event):
            return
        uid = event.sender_id
        accounts = [a for a in db.list_accounts(uid) if a["status"] == "active"]
        mode = _pdf_mode(uid)
        rows = [
            cards.kv("Mode", {"auto": "⚡ خودکار (سریع با فال‌بک)",
                              "parallel": "⚡ سریع",
                              "safe": "🐢 آرام"}[mode]),
            cards.kv("Every", f"{config.PV_EXPORT_PDF_BATCH} عکس یک فایل"),
            cards.kv("Max photos", cards.num(config.PV_EXPORT_MAX_PHOTOS)),
            cards.LINE,
            "عکس‌های چت‌های خصوصی جمع و به‌صورت PDF فرستاده می‌شوند.",
            f"هر {config.PV_EXPORT_PDF_BATCH} عکس یک فایل می‌گیری،",
            "پس لازم نیست تا آخر منتظر بمانی.",
        ]
        buttons = [[Button.inline(f"🖼 {a['phone']}",
                                  f"rbpdfrun_{a['id']}".encode())]
                   for a in accounts[:config.ACC_PAGE_SIZE]]
        buttons.append([Button.inline("⚙️ حالت جمع‌آوری", b"rbpdfmode")])
        buttons.append(_back(b"rb"))
        if not accounts:
            rows = ["اکانت فعالی نداری."]
        await safe_edit(event, cards.card("🖼 آرشیو عکس (PDF)", rows),
                        buttons=buttons)

    @bot.on(events.CallbackQuery(data=b"rbpdfmode"))
    async def rb_pdf_mode(event):
        if not await gate(event):
            return
        uid = event.sender_id
        current = _pdf_mode(uid)
        rows = [
            cards.kv("Current", current),
            cards.LINE,
            "⚡ سریع — چند دانلود هم‌زمان روی همان یک اتصال.",
            "   حدود ۶ برابر سریع‌تر. اتصال دوم باز نمی‌شود،",
            "   پس سشن اکانت در خطر نیست.",
            "🐢 آرام — یکی‌یکی. کندتر، مطمئن‌تر.",
            "⚡ خودکار — با سریع شروع می‌کند و اگر به مشکل",
            "   بخورد خودش آرام می‌شود؛ کار نیمه‌کاره نمی‌ماند.",
        ]
        await safe_edit(event, cards.card("⚙️ حالت جمع‌آوری", rows), buttons=[
            [Button.inline(("✅ " if current == "auto" else "") + "⚡ خودکار",
                           b"rbpdfset_auto")],
            [Button.inline(("✅ " if current == "parallel" else "") + "⚡ سریع",
                           b"rbpdfset_parallel")],
            [Button.inline(("✅ " if current == "safe" else "") + "🐢 آرام",
                           b"rbpdfset_safe")],
            _back(b"rbpdf"),
        ])

    @bot.on(events.CallbackQuery(pattern=rb"rbpdfset_(auto|parallel|safe)"))
    async def rb_pdf_mode_set(event):
        if not await gate(event):
            return
        mode = event.pattern_match.group(1).decode()
        db.set_setting(event.sender_id, "pv_mode", mode)
        await rb_pdf_mode(event)

    @bot.on(events.CallbackQuery(pattern=rb"rbpdfrun_(\d+)"))
    async def rb_pdf_run(event):
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
        if busy.slot_used("pdf") >= config.PV_EXPORT_MAX_CONCURRENT:
            await event.answer("الان در دسترس نیست — مشتری دیگری در حال "
                               "استفاده است. چند دقیقه بعد امتحان کن.",
                               alert=True)
            return
        msg = await safe_edit(event, cards.card("🖼 آرشیو عکس", [
            cards.kv("Phone", acc["phone"]),
            cards.kv("Mode", _pdf_mode(uid)),
            "⏳ شروع جمع‌آوری ...",
        ]))
        asyncio.create_task(_run_pdf(uid, acc, msg))
