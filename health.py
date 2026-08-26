"""
health.py — the health engine, rewritten around the mistake it used to make.
============================================================================

WHAT WENT WRONG IN THE BASE PROJECT
-----------------------------------
The old health engine woke every three hours and connected to every account to
see whether it still worked. Rubika allows one connection per session, so
connecting to an account that was mid-send made the platform revoke the session.
The engine then observed the auth failure it had just caused, labelled the
account dead, and the customer was told their account had "burned".

The engine was the murderer investigating the murder.

THE FIX, IN THREE RULES
-----------------------
1. A BUSY ACCOUNT IS NEVER PROBED. It is reported alive without connecting,
   because an account currently doing work has already proved it works. This is
   the whole reason `busy.py` exists, and it only holds if the engine runs in the
   same process as the jobs — which is why this module is started by
   customer_bot, not owner_bot.

2. ONE ERROR IS NOT A DEATH SENTENCE. A suspicion is confirmed on a fresh
   connection (`account_conn.verify_session_dead`) and only an explicit auth
   failure counts. A timeout, a network blip, or an unreachable worker leaves the
   account exactly as it was.

3. QUARANTINE, NOT DELETION. A dead account keeps its row, its stats and its
   contact count, and gets a re-login button. Nothing the customer paid for is
   thrown away over a status flag.

WHY THE OWNER STILL GETS AN ALERT
---------------------------------
One dead account is a customer problem. Twenty dead accounts inside an hour is a
platform-wide event or a bug in this service, and the owner needs to know before
the support messages arrive. That is the dead-burst alert.
"""
from __future__ import annotations

import asyncio
import time

import account_conn
import busy
import cards
import config
import db
import logbus
import worker

# (timestamp, customer_id, phone) of confirmed deaths, for the burst detector
_deaths: list = []
_burst_alerted_at = 0.0
_task = None
_last_report: dict = {}


def _note_death(customer_id, phone: str) -> int:
    """Record a death and return how many happened inside the alert window."""
    now = time.time()
    _deaths.append((now, customer_id, phone))
    cutoff = now - config.DEAD_BURST_WINDOW
    while _deaths and _deaths[0][0] < cutoff:
        _deaths.pop(0)
    return len(_deaths)


async def _maybe_burst_alert() -> None:
    """Tell the owner when deaths cluster — one dead account is noise, twenty is
    a signal, and the difference matters at 3am."""
    global _burst_alerted_at
    if len(_deaths) < config.DEAD_BURST_MAX:
        return
    # Do not re-alert for the same burst on every pass.
    if time.time() - _burst_alerted_at < config.DEAD_BURST_WINDOW:
        return
    _burst_alerted_at = time.time()

    per_customer: dict = {}
    for _, cid, _phone in _deaths:
        per_customer[cid] = per_customer.get(cid, 0) + 1
    top = sorted(per_customer.items(), key=lambda kv: -kv[1])[:6]

    await logbus.event("🚨 - #dead_burst", [
        cards.kv("Dead accounts", len(_deaths)),
        cards.kv("Window", f"{config.DEAD_BURST_WINDOW // 60} minutes"),
        cards.kv("Customers hit", len(per_customer)),
        cards.LINE,
        *[cards.kv(f"Customer {cid}", f"{n} accounts") for cid, n in top],
        cards.LINE,
        "Many sessions died at once. Check whether the platform changed",
        "something before answering support.",
    ])


async def check_account(acc: dict, busy_ids: set) -> str:
    """Verify one account. Returns 'alive' | 'busy' | 'dead' | 'unknown'.

    Never raises: a health sweep that dies halfway leaves half the fleet
    unchecked and reports nothing.
    """
    customer_id, phone = acc["customer_id"], acc["phone"]

    # RULE 1 — busy means alive, and it means DO NOT TOUCH.
    if acc["id"] in busy_ids:
        return "busy"
    key = busy.key_for(phone, customer_id=customer_id, platform="rb")
    if busy.is_busy(key):
        return "busy"

    w = worker.worker_for_account(acc)
    try:
        if w and not worker.is_local(w):
            res = await worker.api_call(w, "POST", "/account/verify", {
                "customer_id": customer_id, "phone": phone},
                timeout=config.HEALTH_TIMEOUT + 45)
            if res.get("skipped"):
                return "busy" if "busy" in str(res.get("reason", "")) else "unknown"
            dead = bool(res.get("dead"))
        else:
            # Claim the session first, so a job that starts mid-check cannot
            # collide with the probe we are about to open.
            async with busy.hold(key, "verify", customer_id=customer_id,
                                 extra={"account_id": acc["id"]}) as held:
                if not held.ok:
                    return "busy"
                dead = await account_conn.verify_session_dead(customer_id, phone)
    except Exception:      # noqa: BLE001
        # RULE 2 — an unreachable worker or a timeout is not proof of death.
        return "unknown"

    if not dead:
        return "alive"

    # RULE 3 — quarantine, keep everything, and tell the customer.
    if config.HEALTH_ENGINE_AUTODISABLE_DEAD:
        try:
            db.set_status(customer_id, acc["id"], "quarantined")
            db.tabchi_set(customer_id, acc["id"], enabled=False)
            db.secretary_set(customer_id, acc["id"], enabled=False)
        except Exception:  # noqa: BLE001
            pass
    return "dead"


async def sweep(notify=None) -> dict:
    """One pass over every active account. `notify(customer_id, phone)` is the
    customer-facing announcement, injected so this module never imports the bot.
    """
    accounts = db.owner_all_accounts(status="active", platform="rb")
    totals = {"checked": 0, "alive": 0, "busy": 0, "dead": 0, "unknown": 0}
    dead_list = []

    for acc in accounts:
        busy_ids = busy.busy_account_ids(acc["customer_id"])
        verdict = await check_account(acc, busy_ids)
        totals["checked"] += 1
        totals[verdict] = totals.get(verdict, 0) + 1
        if verdict == "dead":
            dead_list.append(acc)
            _note_death(acc["customer_id"], acc["phone"])
            if notify:
                try:
                    await notify(acc["customer_id"], acc["phone"])
                except Exception as exc:  # noqa: BLE001
                    await logbus.error(exc, context="health notify",
                                       customer=acc["customer_id"], notify=False)
        # Space the passes out: a health sweep should be invisible, not a spike.
        await asyncio.sleep(config.HEALTH_ACCOUNT_GAP)

    totals["dead_accounts"] = [f"{a['customer_id']}:{a['phone']}" for a in dead_list]
    _last_report.clear()
    _last_report.update(totals)
    _last_report["at"] = cards.now()
    # Persist it: the engine lives in the customer process but the owner's panel
    # is a different process, so an in-memory report would never reach the one
    # person who wants to read it.
    try:
        db.set_health_report(_last_report)
    except Exception:      # noqa: BLE001
        pass

    if totals["dead"] or totals["unknown"]:
        await logbus.event("🩺 - #health_sweep", [
            cards.kv("Checked", totals["checked"]),
            cards.kv("Alive", totals["alive"]),
            cards.kv("Busy (skipped)", totals["busy"]),
            cards.kv("Dead", totals["dead"]),
            cards.kv("Unknown", totals["unknown"]),
            *([cards.LINE] + [cards.kv("Dead", d)
                              for d in totals["dead_accounts"][:10]]
              if dead_list else []),
        ])
    await _maybe_burst_alert()
    return totals


async def _loop(notify=None) -> None:
    # Never sweep immediately on boot: restore_pending() is still re-registering
    # resumed jobs in the busy registry, and a sweep that runs before that lands
    # is a sweep that probes accounts which are actually mid-job.
    await asyncio.sleep(config.HEALTH_ENGINE_WARMUP)
    while True:
        try:
            if db.is_bot_online():
                await sweep(notify=notify)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="health engine", notify=False)
        await asyncio.sleep(config.HEALTH_ENGINE_INTERVAL)


def start(notify=None):
    """Launch the engine. Safe to call twice."""
    global _task
    if not config.HEALTH_ENGINE_ENABLED:
        return None
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(notify=notify))
    return _task


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


def last_report() -> dict:
    return dict(_last_report)


def report_card() -> str:
    """The owner's view of the last sweep.

    Reads the persisted row rather than the in-memory copy, so the same function
    renders correctly from the owner bot — a process that never runs a sweep.
    """
    r = _last_report or db.get_health_report()
    if not r:
        return cards.panel_card("🩺 - #health_engine", [
            "هنوز دوره‌ای اجرا نشده.",
            cards.kv("Interval",
                     f"{config.HEALTH_ENGINE_INTERVAL // 60} minutes"),
        ])
    return cards.panel_card("🩺 - #health_engine", [
        cards.kv("Last run", str(r.get("at", "—"))[:16]),
        cards.kv("Checked", r.get("checked", 0)),
        cards.kv("Alive", r.get("alive", 0)),
        cards.kv("Busy (skipped)", r.get("busy", 0)),
        cards.kv("Dead", r.get("dead", 0)),
        cards.kv("Unknown", r.get("unknown", 0)),
        cards.LINE,
        "اکانت مشغول اصلاً تست نمی‌شود؛ کاری که در حال انجام است",
        "خودش ثابت می‌کند سشن سالم است.",
    ])
