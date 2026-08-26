"""
forcedjoin.py — the sponsor-channel lock.
=========================================

The owner lists channels in the owner panel; a customer cannot use the bot until
they have joined every enabled one. Ported from the reference project's
forcedjoin.py, including the one rule that matters most:

    IF MEMBERSHIP CANNOT BE VERIFIED, DO NOT BLOCK.

Telegram only lets a bot read a user's membership when the bot is an ADMIN in the
channel. So the failure modes are: the bot is not admin yet, the username is
wrong, the channel was deleted, Telegram is rate-limiting us. Treating any of
those as "not a member" would lock out every customer at once over a
configuration mistake the customer cannot see or fix — and they would all arrive
at support simultaneously. Uncertainty therefore lets the user through, and the
owner panel says plainly that the bot must be an admin.

The owner is never gated. Locking yourself out of your own panel with a bad
channel entry is a trap worth spending three lines to avoid.
"""
from __future__ import annotations

import config
import db

# The verdict cache. get_permissions is a network call, and _gate runs on EVERY
# button press: a customer walking through five menus would otherwise pay five
# round-trips per channel. Only a POSITIVE result is cached — a user who has not
# joined must be re-checked, because they are about to join and press the button.
_passed: dict = {}


def _ttl() -> float:
    return float(getattr(config, "FORCED_JOIN_CACHE_SEC", 300) or 0)


def clear_cache(uid=None) -> None:
    """Forget cached verdicts. Called when the channel list changes.

    Without this, disabling a channel would leave customers still blocked, and
    adding one would let everyone through until the cache expired.
    """
    if uid is None:
        _passed.clear()
    else:
        _passed.pop(int(uid), None)


def is_active() -> bool:
    """Is any channel configured and enabled?"""
    return bool(db.list_forced_channels(only_enabled=True))


async def missing_for(client, uid: int) -> list:
    """The enabled channels this user is NOT in. Unverifiable ones are skipped."""
    if int(uid) == int(config.OWNER_ID):
        return []
    channels = db.list_forced_channels(only_enabled=True)
    if not channels:
        return []

    import time
    cached = _passed.get(int(uid))
    if cached and (time.time() - cached[0]) < _ttl() and cached[1] == len(channels):
        return []

    from telethon.errors import UserNotParticipantError

    missing = []
    for channel in channels:
        target = (channel.get("chat") or "").strip()
        if not target:
            continue
        try:
            await client.get_permissions(target, int(uid))
        except UserNotParticipantError:
            missing.append(channel)
        except Exception:      # noqa: BLE001
            # Bot not an admin, channel unresolved, or Telegram refused. We do not
            # know, so we do not block — see the module docstring.
            continue
    if not missing:
        _passed[int(uid)] = (time.time(), len(channels))
    return missing


def _link(channel: dict) -> str:
    link = (channel.get("link") or "").strip()
    if link:
        return link
    chat = (channel.get("chat") or "").lstrip("@")
    return f"https://t.me/{chat}" if chat else ""


def prompt(missing: list, Button, check_data: bytes = b"fj_check"):
    """(text, buttons) for the join prompt."""
    import cards
    rows = []
    for channel in missing:
        url = _link(channel)
        if url:
            rows.append([Button.url(
                f"📢 {channel.get('title') or channel.get('chat')}", url)])
    rows.append([Button.inline("✅ عضو شدم، بررسی کن", check_data)])
    text = cards.card("🔒 عضویت لازم است", [
        "برای استفاده از ربات، اول عضو کانال‌های زیر شو و بعد دکمهٔ "
        "«✅ عضو شدم» را بزن.",
    ])
    return text, rows


async def enforce(client, event, respond=None) -> bool:
    """True when the user may proceed; otherwise show the prompt and return False.

    `respond` lets the caller pass its own reply helper so the prompt goes out the
    same way every other card does.
    """
    missing = await missing_for(client, event.sender_id)
    if not missing:
        return True
    from telethon import Button
    text, buttons = prompt(missing, Button)
    try:
        if respond is not None:
            await respond(event, text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)
    except Exception:      # noqa: BLE001 - never let the prompt break the gate
        try:
            await client.send_message(event.sender_id, text, buttons=buttons)
        except Exception:
            pass
    return False
