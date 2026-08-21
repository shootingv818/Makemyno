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
# job_id -> the task that keeps the customer's card up to date
_live: dict = {}

# Injected so a running job can refresh the customer's live card.
_bot = None

# A FloodWait no longer than this is simply waited out in place: parking the
# account and reopening its session later costs more than the wait itself.
# Anything longer parks the account so the rest of the job carries on without it.
FLOOD_INLINE_MAX = 90

# How many times one account may be throttled before the job gives up on it.
# Retrying a rate-limited account indefinitely only deepens the limit, and its
# remaining recipients are better handled by another account than by waiting all
# day for this one.
MAX_FLOODWAIT_ROUNDS = 3

# The only states from which an account may be (re)started by the round loop.
# 'running' is included because it means a turn was interrupted, not finished.
# Everything else — done, failed, stopped, skipped, floodwait — is either
# finished or waiting on a cooldown that the loop handles separately.
RUNNABLE_STATES = frozenset({"pending", "running", ""})


def bind(bot) -> None:
    global _bot
    _bot = bot


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="tg")


def _target_key(entity) -> str:
    """A stable identity for a recipient, used for dedup."""
    uid = getattr(entity, "id", None)
    return str(uid) if uid is not None else str(entity)


# Rolling record of how long the PLATFORM takes per message, so a slow send can
# be attributed to the network instead of to our configured delay.
_send_times: list = []


def note_send_time(seconds: float) -> None:
    _send_times.append(float(seconds))
    if len(_send_times) > 200:
        del _send_times[:-200]


def send_timing() -> dict:
    """Average/last seconds spent inside Telegram's API, over recent sends."""
    if not _send_times:
        return {"avg": 0.0, "last": 0.0, "n": 0}
    return {"avg": sum(_send_times) / len(_send_times),
            "last": _send_times[-1], "n": len(_send_times)}


def _payload(entity, kind: str) -> dict:
    """What we persist about a recipient.

    The access_hash is the important part. A bare numeric id forces Telethon to
    RESOLVE the peer before it can send, and when the entity is not in the session
    cache that resolution is an extra API round-trip PER RECIPIENT. On a link with
    real latency that alone can dominate the send rate — the customer sets a 0.2s
    gap and watches one message leave every few seconds, with the time going to
    lookups rather than to sending.

    Captured here at discovery time, while we already hold the full entity.
    """
    return {
        "kind": kind,
        "id": getattr(entity, "id", None),
        "access_hash": getattr(entity, "access_hash", None),
    }


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
                out.append((_target_key(user), _payload(user, "user"), True))
            for user in others:
                out.append((_target_key(user), _payload(user, "user"), False))
        if target_mode in ("both", "groups"):
            for group in await tg.get_group_entities(client):
                out.append((_target_key(group), _payload(group, "group"), False))
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
    # The live card belongs to the ENGINE, not to the screen that launched it.
    # It used to be started by tg_panel, which meant a RESUMED job and a job
    # revived by restart recovery had no live card at all — two ways to end up
    # watching a frozen number while work was actually happening.
    _live[job_id] = asyncio.create_task(_live_card(customer_id, job_id))
    return db.tgm_get_job(customer_id, job_id)


async def _live_card(customer_id, job_id) -> None:
    """Keep the job's card current until it reaches a terminal state.

    Edits the message whose id the job row remembers, so it survives a resume and
    a process restart. Skips the edit when nothing changed, because Telegram
    rejects an edit with identical content and that error is pure noise.
    """
    terminal = {"done", "stopped", "failed", "frozen", "paused"}
    last = ""
    try:
        while True:
            await asyncio.sleep(config.TG_STATS_REFRESH)
            job = db.tgm_get_job(customer_id, job_id)
            if not job:
                return
            text = progress_card(customer_id, job_id)
            if text != last and job.get("msg_id") and _bot:
                last = text
                try:
                    from telethon import Button
                    await _bot.edit_message(
                        int(customer_id), int(job["msg_id"]), text,
                        buttons=[[Button.inline("⛔ توقف",
                                                f"tgjstop_{job_id}".encode())],
                                 [Button.inline("📊 جزئیات",
                                                f"tgjob_{job_id}".encode())]])
                except Exception:      # noqa: BLE001 - a failed edit is not fatal
                    pass
            if job.get("state") in terminal:
                return
    except asyncio.CancelledError:
        return


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
    # The card refresher would otherwise outlive the job it was watching.
    live = _live.pop(job_id, None)
    if live and not live.done():
        live.cancel()
    return db.tgm_get_job(customer_id, job_id)


async def resume(customer_id, job_id) -> dict:
    """Continue a stopped job. Already-sent recipients stay skipped."""
    job = db.tgm_get_job(customer_id, job_id)
    if not job:
        raise ValueError("unknown job")
    db.tgm_update_job(customer_id, job_id, stop_requested=0, state="running",
                      last_error="")
    # Accounts parked by a stop, an error burst or a freeze go back in the queue.
    # The round loop only starts a runnable account, so without this a resumed
    # job would skip every account that had been stopped — which is all of them.
    # A 'failed' account is deliberately left out: its session is dead or it was
    # given up on, and retrying it only reproduces the same failure.
    db.tgm_requeue_stopped_accounts(customer_id, job_id)
    return await start(customer_id, job_id)


async def _run(customer_id, job_id) -> None:
    control = _controls.setdefault(job_id, {"stop": False})
    try:
        # Any recipient still marked inflight belongs to a turn that was killed
        # (restart, crash, forced stop). Nobody owns it, so without this it is
        # never attempted again and the job can never reach 100%.
        recovered = db.tgm_reset_inflight(customer_id, job_id)
        if recovered:
            await logbus.customer_action(
                db.get_customer(customer_id), "tg_multi_recovered", [
                    cards.kv("Job", job_id),
                    cards.kv("Requeued", cards.num(recovered)),
                ], platform="Telegram")

        # Accounts are walked in rounds, not once. A single pass could never
        # come back to an account parked on a FloodWait, so a throttled account
        # lost the rest of its recipients even though the limit had expired long
        # before the job ended.
        while not control["stop"] and not db.are_sends_frozen():
            worked = False
            for account in db.tgm_job_accounts(customer_id, job_id):
                if control["stop"] or db.are_sends_frozen():
                    break
                # An allow-list, deliberately. A skip-list let "stopped" through,
                # and because accounts are now walked in ROUNDS an account that
                # had already hit its consecutive-error ceiling was picked up
                # again on the next pass and burned straight through the ceiling
                # a second time. Only an account that is genuinely waiting for
                # its turn may run.
                if account["state"] not in RUNNABLE_STATES:
                    continue
                await _run_account(customer_id, job_id, account, control)
                worked = True
            if control["stop"] or db.are_sends_frozen():
                break
            if worked:
                continue                    # another pass: parked ones may be due

            # Nothing runnable left. If accounts are parked, wait for the first
            # cooldown to expire and carry on; the wait is interruptible so stop
            # still answers immediately.
            wait = db.tgm_next_cooldown(customer_id, job_id)
            if wait is None:
                break                       # genuinely finished
            db.tgm_update_job(customer_id, job_id, state="waiting",
                              current_phone="")
            await logbus.customer_action(
                db.get_customer(customer_id), "tg_multi_waiting", [
                    cards.kv("Job", job_id),
                    cards.kv("Resumes in", f"{int(wait)}s"),
                    "همهٔ اکانت‌های باقی‌مانده در محدودیت تلگرام هستند. کار "
                    "متوقف نشده — بعد از پایان محدودیت خودش ادامه می‌دهد.",
                ], platform="Telegram")
            if not await _sleep_unless_stopped(control, wait + 2):
                break
            db.tgm_wake_cooled(customer_id, job_id)
            db.tgm_update_job(customer_id, job_id, state="running")

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


async def _sleep_unless_stopped(control: dict, seconds: float,
                               step: float = 2.0) -> bool:
    """Sleep out a cooldown; return False if the job was stopped meanwhile.

    A flat sleep of a FloodWait can be hours long, which would leave the stop
    button dead for hours.
    """
    waited = 0.0
    while waited < seconds:
        if control.get("stop") or db.are_sends_frozen():
            return False
        chunk = min(step, seconds - waited)
        await asyncio.sleep(chunk)
        waited += chunk
    return not control.get("stop")


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

        # Upload media ONCE for this account, before the recipient loop. Per
        # account rather than per job, because a file reference belongs to the
        # account that uploaded it.
        try:
            plan = await prepare_content(client, content)
        except Exception:      # noqa: BLE001 - fall back to per-recipient upload
            plan = None
        if plan and any(s["kind"] == "media" and s.get("saved") is not None
                        for s in plan):
            await logbus.customer_action(db.get_customer(customer_id),
                                        "tg_media_preuploaded", [
                cards.kv("Phone", phone),
                cards.kv("Items", len(plan)),
                "فایل یک بار آپلود شد و برای بقیه کپی می‌شود.",
            ], platform="Telegram")

        sent = failed = skipped = 0
        consecutive = 0
        max_errors = db.get_max_errors(customer_id)
        stop_reason = ""
        flood_seconds = 0

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

                # Claim the row BEFORE the send. If the process dies mid-send the
                # row stays 'inflight', which is the honest state: we do not know
                # whether it arrived. _run requeues those on resume, and an
                # account given up on turns them into 'uncertain' rather than
                # quietly counting them as delivered.
                db.tgm_set_recipient(customer_id, job_id, row["idx"], "inflight")
                try:
                    await _deliver(client, target, content, delay, plan=plan)
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
                        #
                        # A SHORT wait is slept through here, because parking and
                        # reopening the session costs more than the wait itself.
                        # A LONG one must NOT be: this used to
                        # `await asyncio.sleep(wait)` unconditionally, inside the
                        # recipient loop, still holding the session claim. An
                        # 11-hour FloodWait therefore froze the whole job — every
                        # other account queued behind the throttled one and the
                        # customer saw a send that had simply stopped. The
                        # account is now PARKED and the job moves to the next
                        # one; the runner picks it back up when the cooldown
                        # expires and it continues from what is left.
                        if wait <= FLOOD_INLINE_MAX:
                            db.tgm_update_account(
                                customer_id, job_id, account_id,
                                last_error=f"floodwait {wait}s (waiting)")
                            await asyncio.sleep(wait)
                            continue
                        stop_reason = "floodwait"
                        flood_seconds = wait
                        break
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

        if stop_reason == "floodwait":
            # Park, or give up if this account has already been throttled too
            # many times. Giving up skips what is left for THIS account instead
            # of retrying a rate-limited account forever, which only deepens the
            # limit.
            reason = f"FloodWait {flood_seconds}s"
            rounds = db.tgm_park_account(customer_id, job_id, account_id,
                                         flood_seconds, reason)
            if rounds > MAX_FLOODWAIT_ROUNDS:
                gave_up, uncertain = db.tgm_give_up_account(
                    customer_id, job_id, account_id,
                    f"{reason} — throttled {rounds} times, giving up")
                db.tgm_bump_job(customer_id, job_id, skipped=gave_up,
                                uncertain=uncertain)
                await logbus.customer_action(
                    db.get_customer(customer_id), "tg_multi_account_gave_up", [
                        cards.kv("Job", job_id),
                        cards.kv("Phone", phone),
                        cards.kv("Reason", reason),
                        cards.kv("Rounds", rounds),
                        cards.kv("Skipped", cards.num(gave_up)),
                        cards.kv("Uncertain", cards.num(uncertain)),
                    ], platform="Telegram")
            else:
                await logbus.customer_action(
                    db.get_customer(customer_id), "tg_multi_account_parked", [
                        cards.kv("Job", job_id),
                        cards.kv("Phone", phone),
                        cards.kv("Reason", reason),
                        cards.kv("Round", f"{rounds}/{MAX_FLOODWAIT_ROUNDS}"),
                        cards.kv("Sent", cards.num(sent)),
                        "این اکانت موقتاً کنار گذاشته شد و بعد از پایان محدودیت "
                        "خودش ادامه می‌دهد. بقیهٔ اکانت‌ها متوقف نمی‌شوند.",
                    ], platform="Telegram")
        else:
            db.tgm_update_account(customer_id, job_id, account_id,
                                 sent_count=sent, failed_count=failed,
                                 consec_fail=consecutive,
                                 state={"auth_failed": "failed",
                                        "error_burst": "stopped",
                                        "stopped": "stopped"}.get(stop_reason,
                                                                  "done"),
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


def _peer(target: dict):
    """The cheapest thing Telethon can send to.

    With an access_hash we can hand Telethon a ready InputPeer and it sends
    immediately. Without one it must resolve the id first, which is an extra API
    round-trip per recipient whenever the entity is not cached — the difference
    between a 0.2s gap and one message every few seconds on a slow link.

    Falls back to the raw value, so a target stored before this existed (or an
    entity object handed in by the single-send path) still works.
    """
    raw = target.get("id")
    access_hash = target.get("access_hash")
    if access_hash is None or not isinstance(raw, int):
        return raw
    try:
        from telethon.tl import types
        if (target.get("kind") or "user") == "user":
            return types.InputPeerUser(user_id=raw, access_hash=access_hash)
        return types.InputPeerChannel(channel_id=raw, access_hash=access_hash)
    except Exception:      # noqa: BLE001 - resolution by id still works
        return raw


async def prepare_content(client, content: list) -> list:
    """Upload every media item ONCE, to the account's own Saved Messages.

    THIS IS THE SEND-SPEED FIX. `client.send_file(entity, path)` re-reads and
    re-UPLOADS the file for every single recipient, so a 3 MB image sent to a
    thousand contacts was a thousand uploads. Telegram lets you reuse the file
    reference of a message you already sent, so the file goes up once and every
    later send is a cheap copy — no upload, and no "forwarded from" tag.

    Text items cost nothing to prepare and pass through untouched. If an upload
    fails, that item keeps its path and falls back to per-recipient upload, which
    is slow but still correct.

    Returns a plan in the SAME ORDER as `content`, because the customer configured
    item 1, 2, 3 and expects them delivered in that order.
    """
    plan = []
    for item in content or []:
        kind = (item or {}).get("kind") or "text"
        text = (item or {}).get("text") or ""
        path = (item or {}).get("file_path") or ""
        if kind == "media" and path:
            saved = None
            try:
                saved = await tg.upload_to_saved(client, path, text)
            except Exception:      # noqa: BLE001 - fall back to per-send upload
                saved = None
            plan.append({"kind": "media", "saved": saved, "path": path,
                         "text": text})
        else:
            plan.append({"kind": "text", "text": text})
    return plan


async def _deliver(client, target: dict, content: list, delay: float,
                   plan: list = None) -> None:
    """Send every configured content item to one recipient.

    `plan` is the pre-uploaded version from prepare_content. It is optional so the
    single-account path and older callers keep working, but without it every media
    item is uploaded again for this recipient.
    """
    entity = _peer(target)
    if entity is None:
        raise ValueError("target has no id")

    if plan:
        typing = 0.0
        if config.TG_TYPING_MAX > 0:
            import random
            typing = random.uniform(config.TG_TYPING_MIN, config.TG_TYPING_MAX)
        for step in plan:
            started = time.monotonic()
            if step["kind"] == "media" and step.get("saved") is not None:
                # A copy of an already-uploaded file: no upload, no forward tag.
                await tg.send_saved_media(client, entity, step["saved"],
                                          step.get("text") or "")
            elif step["kind"] == "media":
                await tg.send_media(client, entity, step["path"],
                                    caption=step.get("text") or "")
            else:
                await tg.send_text(client, entity, step.get("text") or "",
                                   typing=typing)
            note_send_time(time.monotonic() - started)
            if len(plan) > 1:
                await asyncio.sleep(max(0.05, delay / 2))
        return
    typing = 0.0
    if config.TG_TYPING_MAX > 0:
        import random
        typing = random.uniform(config.TG_TYPING_MIN, config.TG_TYPING_MAX)
    for item in content:
        kind = (item or {}).get("kind") or "text"
        text = (item or {}).get("text") or ""
        path = (item or {}).get("file_path") or ""
        started = time.monotonic()
        if kind == "media" and path:
            await tg.send_media(client, entity, path, caption=text)
        else:
            await tg.send_text(client, entity, text, typing=typing)
        # Measure what the PLATFORM costs us, separately from our own delay.
        # "Sending feels slow" is unanswerable; "0.2s of delay and 2.8s waiting
        # for Telegram" tells you immediately that the gap is the network, not a
        # setting — and that no amount of lowering the speed will help.
        note_send_time(time.monotonic() - started)
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
    job["per_account"] = db.tgm_counts_per_account(customer_id, job_id)
    return job


def progress_card(customer_id, job_id) -> str:
    job = status(customer_id, job_id)
    if not job:
        return cards.card("📨 ارسال چنداکانتی", ["جاب پیدا نشد."])
    total = int(job.get("total") or 0)

    # LIVE counts, from the recipients table.
    #
    # This card used to read job["sent_count"], a column that _run_account only
    # writes when it FINISHES an account — so a job sending to two thousand people
    # displayed 0/2000 the entire time and looked frozen. The recipients table is
    # updated per message, so it is the only source that actually moves.
    live = job.get("counts") or {}
    sent = int(live.get("sent", job.get("sent_count") or 0))
    failed = int(live.get("failed", job.get("failed_count") or 0))
    skipped = int(live.get("skipped", job.get("skipped_count") or 0))
    uncertain = int(live.get("uncertain", job.get("uncertain_count") or 0))
    inflight = int(live.get("inflight", 0))
    done = sent + failed + skipped + uncertain

    labels = {"queued": "در صف", "running": "در حال اجرا",
              "waiting": "⏳ در انتظار پایان محدودیت",
              "stop_requested": "در حال توقف", "stopped": "متوقف شد",
              "paused": "نیمه‌کاره", "done": "پایان", "failed": "خطا",
              "frozen": "ارسال موقتاً متوقف"}
    rows = [
        cards.kv("Job", job.get("job_id")),
        cards.kv("State", labels.get(job.get("state"), job.get("state"))),
        cards.kv("Progress", f"{cards.bar(done, max(1, total))}  {done}/{total}"),
        cards.kv("Sent", cards.num(sent)),
        cards.kv("Failed", cards.num(failed)),
        cards.kv("Skipped", cards.num(skipped)),
        cards.kv("Mutual first", cards.num(job.get("mutual_total") or 0)),
    ]
    # Only shown when non-zero: an "Uncertain: 0" row on every card trains people
    # to ignore the one time it matters.
    if uncertain:
        rows.append(cards.kv("Uncertain", cards.num(uncertain)))
    if inflight:
        rows.append(cards.kv("In flight", cards.num(inflight)))

    # Parked accounts, and when the job picks itself back up. A job that is
    # waiting out a FloodWait is NOT stuck, and without this line it looks
    # identical to one that has died.
    parked = [a for a in (job.get("accounts") or [])
              if a.get("state") == "floodwait"]
    if parked:
        soonest = db.tgm_next_cooldown(customer_id, job_id)
        rows.append(cards.kv("Throttled", f"{len(parked)} اکانت"))
        if soonest is not None:
            minutes = int(soonest // 60)
            rows.append(cards.kv(
                "Resumes in",
                f"{minutes} دقیقه" if minutes >= 1 else f"{int(soonest)} ثانیه"))
    # Where the time actually goes. Without this, "sending is slow" is a guess:
    # the configured gap and the platform's own latency are indistinguishable from
    # the outside, and lowering the speed setting cannot fix a slow network.
    timing = send_timing()
    if timing["n"]:
        gap = float(job.get("delay") or config.TG_SEND_DELAY)
        per = timing["avg"] + gap
        rows.append(cards.kv("Speed", f"{gap:.2f}s تنظیم‌شده"))
        rows.append(cards.kv("Telegram", f"{timing['avg']:.2f}s در هر پیام"))
        if per > 0:
            rows.append(cards.kv("Rate", f"~{int(60 / per)} پیام در دقیقه"))
    if job.get("current_phone"):
        rows.append(cards.kv("Current", job["current_phone"]))
    rows.append(cards.LINE)
    # Per account, also from the live table rather than the account row, which is
    # written only when that account finishes.
    per_account = job.get("per_account") or {}
    for account in job.get("accounts") or []:
        mark = {"done": "✅", "running": "▶️", "failed": "🔴",
                "stopped": "⛔", "skipped": "⏭", "pending": "▫️",
                "floodwait": "⏳"}.get(account.get("state"), "▫️")
        seen = per_account.get(int(account["account_id"]), {})
        acc_sent = int(seen.get("sent", account.get("sent_count") or 0))
        acc_total = int(account.get("total") or 0) or sum(seen.values())
        line = (f"{mark} {account['phone']} → "
                f"✉️{cards.num(acc_sent)} / {cards.num(acc_total)}")
        if seen.get("failed"):
            line += f"  ⚠️{cards.num(seen['failed'])}"
        rows.append(line)
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
