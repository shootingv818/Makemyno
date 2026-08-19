"""
telegram_multi_send.py — sequential multi-account sending for Telegram.
======================================================================

Each selected account sends to ITS OWN contacts, one account at a time. The job
lives in the database, so a restart resumes instead of starting over.

FOUR THINGS THAT MAKE THIS SAFE, AND THE FAILURE EACH ONE PREVENTS
------------------------------------------------------------------
1. Persistent recipient rows. Restarting from zero would message everybody a
   second time, and duplicate messages are what get accounts reported.

2. Cross-account dedup within the job (db.tgm_*_uid_sent). A customer with five
   accounts usually has overlapping contacts; without this, every shared contact
   receives the same message five times.

3. One account at a time, and the session is claimed in the busy registry for
   the whole of that account's turn. A second connection on one session is
   answered by Telegram with a revocation.

4. FloodWait is not a failure. Telegram answers "wait N seconds" under load; the
   old behaviour counted that as an error and eventually gave up on a perfectly
   healthy account. Here it pauses and continues, while a genuine auth failure
   stops that account immediately.

Stop is cooperative: the flag is set, the loop notices at its next checkpoint and
finishes cleanly, so nothing is left half-written.
"""
from __future__ import annotations

import asyncio
import time

import busy
import cards
import config
import db
import logbus
import telegram_client as tg

# job_id -> asyncio.Task
_tasks: dict = {}
# job_id -> {"stop": bool}
_controls: dict = {}

# Injected so a running job can refresh the customer's live card.
_bot = None


def bind(bot) -> None:
    global _bot
    _bot = bot


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="tg")


def _target_key(entity) -> str:
    """A stable identity for a recipient, used for dedup."""
    uid = getattr(entity, "id", None)
    return str(uid) if uid is not None else str(entity)


def _is_flood(exc: BaseException) -> int:
    """Seconds to wait when Telegram asks us to slow down, else 0."""
    name = type(exc).__name__
    if "FloodWait" in name:
        seconds = int(getattr(exc, "seconds", 0) or 0)
        return min(max(seconds, 1) + 1, config.TG_FLOOD_MAX_WAIT)
    return 0


def _is_fatal_account_error(exc: BaseException) -> bool:
    """Errors that mean this ACCOUNT is finished, not this recipient.

    Distinguishing the two is the difference between skipping one contact and
    hammering a dead account for hours.
    """
    name = type(exc).__name__
    text = str(exc).upper()
    return (name in ("AuthKeyUnregisteredError", "UserDeactivatedError",
                     "UserDeactivatedBanError", "SessionRevokedError",
                     "AuthKeyDuplicatedError", "PhoneNumberBannedError")
            or "AUTH_KEY" in text or "USER_DEACTIVATED" in text
            or "SESSION_REVOKED" in text)


def _is_permanent_recipient_error(exc: BaseException) -> bool:
    """Errors that mean this RECIPIENT cannot be messaged. Retrying is pointless."""
    name = type(exc).__name__
    text = str(exc).upper()
    return (name in ("UserPrivacyRestrictedError", "UserIsBlockedError",
                     "PeerIdInvalidError", "UserBannedInChannelError",
                     "InputUserDeactivatedError", "ChatWriteForbiddenError")
            or "PRIVACY" in text or "BLOCKED" in text
            or "PEER_ID_INVALID" in text or "USER_IS_BOT" in text)


# --------------------------------------------------------------------------- #
# Job creation
# --------------------------------------------------------------------------- #
async def create_job(customer_id, account_ids: list, content: list,
                     target_mode: str = "both") -> dict:
    """Build a job and enumerate each account's own recipients.

    Enumeration opens each session in turn (claimed in the registry), because the
    contact list has to come from the account itself.
    """
    if not account_ids:
        raise ValueError("no accounts selected")
    if not content:
        raise ValueError("no content configured")

    delay = config.clamp_tg_delay(
        db.get_float_setting(customer_id, "tg_send_delay", config.TG_SEND_DELAY))
    job_id = db.tgm_create_job(customer_id, content, delay, target_mode)

    idx = 0
    total = mutual_total = 0
    for ordinal, account_id in enumerate(account_ids):
        acc = db.tg_get_account(customer_id, account_id)
        if not acc:
            continue
        db.tgm_add_account(customer_id, job_id, account_id, acc["phone"], ordinal)
        try:
            targets = await _discover_targets(customer_id, acc, target_mode)
        except Exception as exc:  # noqa: BLE001
            db.tgm_update_account(customer_id, job_id, account_id,
                                 state="failed",
                                 last_error=f"{type(exc).__name__}")
            await logbus.error(exc, context=f"tg discover {acc['phone']}",
                               customer=customer_id, notify=False)
            continue
        idx = db.tgm_add_recipients(customer_id, job_id, account_id, targets, idx)
        db.tgm_update_account(customer_id, job_id, account_id,
                             total=len(targets), state="pending")
        total += len(targets)
        mutual_total += sum(1 for _k, _p, mutual in targets if mutual)

    db.tgm_update_job(customer_id, job_id, total=total, mutual_total=mutual_total)
    job = db.tgm_get_job(customer_id, job_id)
    await logbus.customer_action(db.get_customer(customer_id), "tg_multi_created", [
        cards.kv("Job", job_id),
        cards.kv("Accounts", len(account_ids)),
        cards.kv("Recipients", total),
        cards.kv("Mutual", mutual_total),
        cards.kv("Content", f"{len(content)} item(s)"),
        cards.kv("Target", target_mode),
    ], platform="Telegram")
    return job


async def _discover_targets(customer_id, acc: dict, target_mode: str) -> list:
    """One account's own recipients as [(key, payload, mutual)].

    Mutuals are marked so the sender can reach them first: they added the account
    back, so they are the least likely to report it.
    """
    key = _key(customer_id, acc["phone"])
    async with busy.hold(key, "multi", customer_id=customer_id,
                         extra={"account_id": acc["id"]}, settle=False) as held:
        if not held.ok:
            raise RuntimeError(f"account busy: {busy.label('multi')}")
        client = await tg.get_client(customer_id, acc["id"])
        out = []
        if target_mode in ("both", "contacts"):
            mutuals, others = await tg.get_contacts_ordered(client)
            for user in mutuals:
                out.append((_target_key(user),
                            {"kind": "user", "id": getattr(user, "id", None)},
                            True))
            for user in others:
                out.append((_target_key(user),
                            {"kind": "user", "id": getattr(user, "id", None)},
                            False))
        if target_mode in ("both", "groups"):
            for group in await tg.get_group_entities(client):
                out.append((_target_key(group),
                            {"kind": "group", "id": getattr(group, "id", None)},
                            False))
        # de-duplicate inside one account's own list
        seen, unique = set(), []
        for item in out:
            if item[0] in seen:
                continue
            seen.add(item[0])
            unique.append(item)
        return unique


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
async def start(customer_id, job_id) -> dict:
    job = db.tgm_get_job(customer_id, job_id)
    if not job:
        raise ValueError("unknown job")
    if job_id in _tasks and not _tasks[job_id].done():
        return job
    _controls[job_id] = {"stop": False}
    db.tgm_update_job(customer_id, job_id, state="running", stop_requested=0)
    _tasks[job_id] = asyncio.create_task(_run(customer_id, job_id))
    return db.tgm_get_job(customer_id, job_id)


async def stop(customer_id, job_id, grace: float = 3.0) -> dict:
    """Ask a job to stop, give it time to finish cleanly, then cancel.

    Cooperative first: the loop notices the flag at its next checkpoint and stops
    between recipients, so no send is left half-recorded. Cancelling outright is
    only the fallback.
    """
    db.tgm_update_job(customer_id, job_id, stop_requested=1,
                      state="stop_requested")
    control = _controls.get(job_id)
    if control:
        control["stop"] = True
    task = _tasks.get(job_id)
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except asyncio.TimeoutError:
            task.cancel()
        except Exception:
            pass
    return db.tgm_get_job(customer_id, job_id)


async def resume(customer_id, job_id) -> dict:
    """Continue a stopped job. Already-sent recipients stay skipped."""
    job = db.tgm_get_job(customer_id, job_id)
    if not job:
        raise ValueError("unknown job")
    db.tgm_update_job(customer_id, job_id, stop_requested=0, state="running",
                      last_error="")
    return await start(customer_id, job_id)


async def _run(customer_id, job_id) -> None:
    control = _controls.setdefault(job_id, {"stop": False})
    try:
        for account in db.tgm_job_accounts(customer_id, job_id):
            if control["stop"] or db.are_sends_frozen():
                break
            if account["state"] in ("done", "failed"):
                continue
            await _run_account(customer_id, job_id, account, control)

        counts = db.tgm_counts(customer_id, job_id)
        pending_left = counts.get("pending", 0)
        if control["stop"]:
            state = "stopped"
        elif db.are_sends_frozen():
            state = "frozen"
        elif pending_left:
            state = "paused"
        else:
            state = "done"
        db.tgm_update_job(customer_id, job_id, state=state,
                          finished_at=cards.now(), current_phone="")
        await _report(customer_id, job_id, state)
    except asyncio.CancelledError:
        db.tgm_update_job(customer_id, job_id, state="stopped",
                          finished_at=cards.now())
        raise
    except Exception as exc:  # noqa: BLE001
        code = await logbus.error(exc, context=f"tg multi {job_id}",
                                  customer=customer_id)
        db.tgm_update_job(customer_id, job_id, state="failed", last_error=code,
                          finished_at=cards.now())
    finally:
        _controls.pop(job_id, None)
        _tasks.pop(job_id, None)


async def _run_account(customer_id, job_id, account: dict, control: dict) -> None:
    """One account's whole turn, under a single session claim."""
    account_id, phone = account["account_id"], account["phone"]
    job = db.tgm_get_job(customer_id, job_id)
    if not job:
        return
    content = job.get("content") or []
    delay = float(job.get("delay") or config.TG_SEND_DELAY)
    key = _key(customer_id, phone)

    db.tgm_update_job(customer_id, job_id, current_phone=phone)
    db.tgm_update_account(customer_id, job_id, account_id, state="running")

    async with busy.hold(key, "multi", customer_id=customer_id,
                         extra={"account_id": account_id}) as held:
        if not held.ok:
            db.tgm_update_account(customer_id, job_id, account_id,
                                 state="skipped", last_error="busy")
            return

        try:
            client = await tg.get_client(customer_id, account_id)
        except Exception as exc:  # noqa: BLE001
            db.tgm_update_account(customer_id, job_id, account_id, state="failed",
                                 last_error=type(exc).__name__)
            db.tg_set_status(customer_id, account_id, "dead")
            await logbus.event("🔴 - #tg_account_down", [
                cards.kv("Customer", customer_id),
                cards.kv("Phone", phone),
                cards.kv("Reason", type(exc).__name__),
            ])
            return

        sent = failed = skipped = 0
        consecutive = 0
        max_errors = db.get_max_errors(customer_id)
        stop_reason = ""

        while True:
            if control["stop"] or db.are_sends_frozen():
                stop_reason = "stopped"
                break
            batch = db.tgm_pending_recipients(customer_id, job_id, account_id,
                                              limit=200)
            if not batch:
                break
            for row in batch:
                if control["stop"] or db.are_sends_frozen():
                    stop_reason = "stopped"
                    break
                target = row.get("target") or {}
                uid = row["target_key"]

                # Another account in this job already reached this person.
                if db.tgm_uid_already_sent(customer_id, job_id, uid):
                    db.tgm_set_recipient(customer_id, job_id, row["idx"],
                                         "skipped", "already reached")
                    skipped += 1
                    continue

                try:
                    await _deliver(client, target, content, delay)
                    db.tgm_set_recipient(customer_id, job_id, row["idx"], "sent")
                    db.tgm_mark_uid_sent(customer_id, job_id, uid)
                    db.mark_sent(customer_id, account_id, uid, platform="tg")
                    sent += 1
                    consecutive = 0
                except Exception as exc:  # noqa: BLE001
                    wait = _is_flood(exc)
                    if wait:
                        # Not a failure: Telegram is asking us to slow down.
                        # Counting this as an error is how healthy accounts used
                        # to get abandoned.
                        db.tgm_update_account(customer_id, job_id, account_id,
                                             last_error=f"floodwait {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if _is_fatal_account_error(exc):
                        stop_reason = "auth_failed"
                        db.tg_set_status(customer_id, account_id, "dead")
                        break
                    permanent = _is_permanent_recipient_error(exc)
                    db.tgm_set_recipient(
                        customer_id, job_id, row["idx"],
                        "skipped" if permanent else "failed",
                        type(exc).__name__)
                    if permanent:
                        skipped += 1
                    else:
                        failed += 1
                        consecutive += 1
                    if consecutive >= max_errors:
                        stop_reason = "error_burst"
                        break
                await asyncio.sleep(delay)
            if stop_reason:
                break

        db.tgm_bump_job(customer_id, job_id, sent=sent, failed=failed,
                        skipped=skipped)
        db.tgm_update_account(customer_id, job_id, account_id,
                             sent_count=sent, failed_count=failed,
                             consec_fail=consecutive,
                             state={"auth_failed": "failed",
                                    "error_burst": "stopped",
                                    "stopped": "stopped"}.get(stop_reason, "done"),
                             last_error=stop_reason)
        if sent:
            db.tg_incr_sent(customer_id, account_id, sent)
            db.incr_customer_sends(customer_id, sent)
            db.usage_incr(customer_id, "send", sent)

        await logbus.customer_action(db.get_customer(customer_id),
                                    "tg_multi_account_done", [
            cards.kv("Job", job_id),
            cards.kv("Phone", phone),
            cards.kv("Sent", cards.num(sent)),
            cards.kv("Failed", cards.num(failed)),
            cards.kv("Skipped", cards.num(skipped)),
            cards.kv("Result", stop_reason or "done"),
        ], platform="Telegram")

    # Stagger between accounts so several accounts never post at the same moment.
    if config.TABCHI_ACCOUNT_STAGGER > 0 and not control["stop"]:
        await asyncio.sleep(min(float(config.TABCHI_ACCOUNT_STAGGER), 30.0))


async def _deliver(client, target: dict, content: list, delay: float) -> None:
    """Send every configured content item to one recipient."""
    entity = target.get("id")
    if entity is None:
        raise ValueError("target has no id")
    typing = 0.0
    if config.TG_TYPING_MAX > 0:
        import random
        typing = random.uniform(config.TG_TYPING_MIN, config.TG_TYPING_MAX)
    for item in content:
        kind = (item or {}).get("kind") or "text"
        text = (item or {}).get("text") or ""
        path = (item or {}).get("file_path") or ""
        if kind == "media" and path:
            await tg.send_media(client, entity, path, caption=text)
        else:
            await tg.send_text(client, entity, text, typing=typing)
        if len(content) > 1:
            await asyncio.sleep(max(0.05, delay / 2))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def status(customer_id, job_id) -> dict | None:
    """Full job state, or None when the job does not exist for this customer.

    Returning None rather than an empty-but-decorated dict matters: callers test
    truthiness, and a dict that always carries 'counts' and 'accounts' keys would
    look like a real job that simply has nothing in it.
    """
    job = db.tgm_get_job(customer_id, job_id)
    if not job:
        return None
    job["counts"] = db.tgm_counts(customer_id, job_id)
    job["accounts"] = db.tgm_job_accounts(customer_id, job_id)
    return job


def progress_card(customer_id, job_id) -> str:
    job = status(customer_id, job_id)
    if not job:
        return cards.card("📨 ارسال چنداکانتی", ["جاب پیدا نشد."])
    total = int(job.get("total") or 0)
    done = int(job.get("sent_count") or 0) + int(job.get("failed_count") or 0) \
        + int(job.get("skipped_count") or 0)
    labels = {"queued": "در صف", "running": "در حال اجرا", "waiting": "در انتظار",
              "stop_requested": "در حال توقف", "stopped": "متوقف شد",
              "paused": "نیمه‌کاره", "done": "پایان", "failed": "خطا",
              "frozen": "ارسال موقتاً متوقف"}
    rows = [
        cards.kv("Job", job.get("job_id")),
        cards.kv("State", labels.get(job.get("state"), job.get("state"))),
        cards.kv("Progress", f"{cards.bar(done, max(1, total))}  {done}/{total}"),
        cards.kv("Sent", cards.num(job.get("sent_count") or 0)),
        cards.kv("Failed", cards.num(job.get("failed_count") or 0)),
        cards.kv("Skipped", cards.num(job.get("skipped_count") or 0)),
        cards.kv("Mutual first", cards.num(job.get("mutual_total") or 0)),
    ]
    if job.get("current_phone"):
        rows.append(cards.kv("Current", job["current_phone"]))
    rows.append(cards.LINE)
    for account in job.get("accounts") or []:
        mark = {"done": "✅", "running": "▶️", "failed": "🔴",
                "stopped": "⛔", "skipped": "⏭", "pending": "▫️"}.get(
                    account.get("state"), "▫️")
        rows.append(f"{mark} {account['phone']} → "
                    f"✉️{cards.num(account.get('sent_count') or 0)}"
                    f" / {cards.num(account.get('total') or 0)}")
    return cards.panel_card("📨 - #tg_multi_send", rows)


async def _report(customer_id, job_id, state: str) -> None:
    text = progress_card(customer_id, job_id)
    await logbus.customer_action(db.get_customer(customer_id),
                                "tg_multi_finished", [
        cards.kv("Job", job_id), cards.kv("State", state)], platform="Telegram")
    job = db.tgm_get_job(customer_id, job_id) or {}
    if not _bot:
        return
    try:
        from telethon import Button
        buttons = [[Button.inline("📊 وضعیت", f"tgjob_{job_id}".encode())]]
        if state in ("paused", "stopped"):
            buttons.insert(0, [Button.inline("▶️ ادامه",
                                             f"tgjres_{job_id}".encode())])
        buttons.append([Button.inline("🔙 تلگرام", b"tg")])
        if job.get("msg_id"):
            await _bot.edit_message(int(customer_id), int(job["msg_id"]), text,
                                    buttons=buttons)
        else:
            await _bot.send_message(int(customer_id), text, buttons=buttons)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
async def restore_pending() -> None:
    """Resume jobs that were mid-flight, and re-register their accounts.

    Re-registering matters as much as resuming: the busy registry is in memory,
    so after a restart the health engine would otherwise see a free account and
    connect on top of a job that is about to continue.
    """
    try:
        jobs = db.owner_tgm_unfinished()
    except Exception:
        return
    resumed = 0
    for job in jobs:
        customer_id = job.get("customer_id")
        job_id = job.get("job_id")
        if not (customer_id and job_id):
            continue
        for account in db.tgm_job_accounts(customer_id, job_id):
            if account.get("state") == "running":
                busy.adopt(_key(customer_id, account["phone"]), "multi",
                           customer_id=customer_id,
                           extra={"account_id": account["account_id"]})
        # Mark it paused rather than auto-restarting: the customer decides, and
        # an automatic restart after a crash loop would hammer the platform.
        db.tgm_update_job(customer_id, job_id, state="paused",
                          current_phone="")
        db.queue_notification(customer_id, cards.card("📨 ارسال نیمه‌کاره", [
            cards.kv("شماره جاب", job_id, width=10),
            "سرویس ری‌استارت شد و این ارسال نیمه‌کاره ماند.",
            "با «ادامه» از همان‌جا ادامه می‌دهد؛ کسانی که پیام گرفته‌اند",
            "دوباره پیام نمی‌گیرند.",
        ]))
        resumed += 1
    if resumed:
        await logbus.warn("tg_multi_restored", [
            cards.kv("Jobs", resumed),
            "جاب‌های نیمه‌کاره «نیمه‌کاره» علامت خوردند و اکانت‌های",
            "در حال اجرا دوباره در رجیستری ثبت شدند.",
        ])


def running_jobs() -> list:
    return [jid for jid, task in _tasks.items() if not task.done()]


def elapsed(job: dict) -> str:
    try:
        started = job.get("created_at") or ""
        if not started:
            return "—"
        return started[11:16]
    except Exception:
        return "—"


def now_ts() -> float:
    return time.time()
