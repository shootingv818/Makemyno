"""
antispam.py — the shield that keeps a mass-/start attack from taking the bot down.
=================================================================================

THREAT
------
A competitor points a hundred (or a thousand) throwaway Telegram accounts at the
customer bot and has them all press /start. Each one would otherwise create a
customer row, get a trial, receive a welcome card and appear in the owner's
customer list. The database fills with junk, real customers queue behind the
flood, and Telegram starts rate-limiting our replies.

RESPONSE
--------
Count DISTINCT users who send /start inside a rolling window. If more than
config.START_FLOOD_MAX distinct users start within config.START_FLOOD_WINDOW
seconds, the bot puts itself OFFLINE: it stops answering strangers entirely and
tells the owner. Existing customers are unaffected in their ongoing jobs — the
running send loops keep going, because the shield closes the front door, it does
not stop the machine.

Only the owner can lift the shield, from the owner panel. That is deliberate: an
automatic timer would simply be waited out by the attacker.

WHY DISTINCT USERS
------------------
One curious person tapping /start ten times is not an attack. A hundred fresh
accounts tapping once each is. Counting distinct users is what tells the two
apart, and it is also why the per-customer rate limiter (ratelimit.py) cannot do
this job: at the moment of the flood these are not customers yet.
"""
from __future__ import annotations

import cards
import config
import db
import logbus

# Remember that we already announced the shield, so a continuing flood does not
# post one alert per attacker.
_announced = False


def shield_enabled() -> bool:
    return bool(config.START_FLOOD_SHIELD)


async def note_start(user_id, is_new: bool) -> bool:
    """Record a /start and decide whether the bot should stay open.

    Returns True when the caller may continue serving this user, False when the
    shield is (or has just become) active.

    Only genuinely NEW users feed the flood counter: a returning customer
    pressing /start is normal traffic and must never be able to trip the shield.
    """
    global _announced

    if not db.is_bot_online():
        return False

    if not shield_enabled() or not is_new:
        return True

    count = db.record_start(user_id)
    if count <= int(config.START_FLOOD_MAX):
        return True

    # Flood detected -> go offline and tell the owner.
    db.set_bot_online(False, by="shield",
                      note=f"{count} new /start in {config.START_FLOOD_WINDOW}s")
    if not _announced:
        _announced = True
        await logbus.event("🛡 - #antispam_shield", [
            cards.kv("Status", "BOT IS NOW OFFLINE"),
            cards.kv("Trigger", f"{count} distinct new /start in "
                                f"{config.START_FLOOD_WINDOW}s"),
            cards.kv("Limit", config.START_FLOOD_MAX),
            cards.LINE,
            "Looks like a mass-/start attack. Strangers are no longer served.",
            "Running jobs are untouched.",
            "Lift it from the owner panel when the flood passes.",
        ])
    return False


async def lift(by: str = "owner", clear_window: bool = True) -> None:
    """Bring the bot back online (owner action)."""
    global _announced
    _announced = False
    if clear_window:
        # Drop the recorded burst, otherwise the stale window trips the shield
        # again on the very next /start.
        db.clear_start_events()
    db.set_bot_online(True, by=by)
    await logbus.event("✅ - #antispam_lift", [
        cards.kv("Status", "BOT IS ONLINE"),
        cards.kv("By", by),
    ])


async def lower(by: str = "owner", note: str = "") -> None:
    """Take the bot offline by hand (owner action)."""
    db.set_bot_online(False, by=by, note=note or "manual")
    await logbus.event("🛡 - #antispam_offline", [
        cards.kv("Status", "BOT IS OFFLINE"),
        cards.kv("By", by),
        cards.kv("Note", note or "manual"),
    ])


def status() -> dict:
    """Shield state for the owner dashboard."""
    state = db.get_bot_state()
    return {
        "online": bool(state.get("online", 1)),
        "by": state.get("offline_by") or "",
        "at": state.get("offline_at") or "",
        "note": state.get("offline_note") or "",
        "recent_starts": db.recent_start_count(),
        "window": config.START_FLOOD_WINDOW,
        "limit": config.START_FLOOD_MAX,
    }
