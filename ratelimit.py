"""
ratelimit.py — per-customer anti-flood guard with automatic blocking.
====================================================================

A customer who performs more than config.RATE_LIMIT_MAX guarded actions inside
config.RATE_LIMIT_WINDOW seconds is blocked automatically and the event is
logged for the owner.

Two details that matter more than the threshold itself:

  * The counting window lives in the DATABASE, not in memory. An attacker who
    can crash or restart the process must not be able to reset their own
    counter.

  * A blocked customer is then IGNORED COMPLETELY — no reply, no error, no log
    spam. Answering a flooder is doing their work for them: every reply costs us
    an API call and gives them feedback. Silence costs one indexed lookup.

This guards *panel actions* (button taps, commands). Sending volume is metered
separately by the daily probe budget and by the emergency freeze.
"""
from __future__ import annotations

import cards
import config
import db
import logbus


async def guard(customer_id, name: str = "", action: str = "") -> bool:
    """Record one action. True = allowed, False = blocked (now or already).

    Callers must treat False as "stop, and say nothing further" — the reason has
    already been handled here.
    """
    try:
        if db.is_blocked(customer_id):
            return False
    except db.ScopeError:
        # No customer id at all: refuse rather than guess.
        return False

    if not config.RATE_LIMIT_AUTOBLOCK:
        return True

    allowed, count = db.rate_hit(customer_id)
    if allowed:
        return True

    # Over the limit: block once, log once.
    db.set_blocked(customer_id, True)
    await logbus.event("🚫 - #rate_limit_block", [
        cards.kv("Customer", name or customer_id),
        cards.kv("ID", customer_id),
        cards.kv("Actions", f"{count} in {config.RATE_LIMIT_WINDOW}s "
                            f"(limit {config.RATE_LIMIT_MAX})"),
        cards.kv("Last action", action or "—"),
        "⛔ Blocked automatically.",
    ])
    return False


async def unblock(customer_id, by: str = "owner") -> None:
    """Lift a block and clear the window, so the customer starts clean."""
    db.set_blocked(customer_id, False)
    db.rate_reset(customer_id)
    await logbus.event("✅ - #rate_limit_unblock", [
        cards.kv("ID", customer_id),
        cards.kv("By", by),
    ])
