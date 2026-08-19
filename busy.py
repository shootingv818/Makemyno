"""
busy.py — the ONE registry that decides who may open an account's session.
==========================================================================

WHY THIS FILE EXISTS
--------------------
Rubika (and Telegram) allow exactly ONE live connection per session. A second
connection is answered with AUTH_FROM_ANOTHER and the session is REVOKED — from
the user's point of view "the account got shot" and has to be logged in again.

In the base project this rule was only half-enforced. Sending checked a busy
flag, but the photo export, the contact import, the discovery engine, the brain,
the pool AND — worst of all — the periodic health checker did not. The health
engine walked every account every three hours and opened a connection to test
whether it was alive; if the account happened to be mid-send that second
connection revoked the session, and the engine then recorded the account as
"dead". The watchdog was manufacturing the very failures it reported.

THE RULE THIS MODULE ENFORCES
-----------------------------
No code opens a session without doing BOTH of these:

    1. ASK   — is anybody already working on this account?
    2. JOIN  — register itself, so everybody else can see it is working.

Half of the old bugs came from features that did (1) but not (2): they were
invisible, so a send started right on top of them.

Use it as a context manager and both halves are automatic:

    async with busy.hold(key, "send", customer_id=cid) as held:
        if not held.ok:
            await answer(held.reason)      # a human-readable reason, not silence
            return
        ...open the session and work...

Notes
-----
* The key is the SESSION identity, i.e. platform + customer + phone. Two
  customers may own the same phone number (different sessions, namespaced
  storage), so the customer id is part of the key.
* Entries carry a timestamp and are reclaimed after BUSY_STALE_SEC, so a task
  that dies without releasing cannot mark an account busy forever.
* The registry is in-process, which is exactly right here: the whole service
  runs as ONE shared customer bot, so one process sees every customer's work.
  Anything that resumes a job after a restart MUST re-register it (see adopt()),
  otherwise the resumed job is invisible again and the health engine will
  happily connect on top of it.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import config

# key -> {"what": str, "since": float, "customer_id": int|None, "extra": dict}
_held: dict[str, dict] = {}

# Per-key async locks so two coroutines cannot both win the same slot.
_locks: dict[str, asyncio.Lock] = {}

# Human wording for each operation, used to build the "why can't I" message.
_LABELS = {
    "send": "ارسال",
    "channel": "ارسال کانالی",
    "multi": "ارسال چنداکانتی",
    "contacts": "افزودن مخاطب",
    "discovery": "کشف مخاطب",
    "brain": "مغز",
    "pool": "مغز استخری",
    "export": "گرفتن مخاطبین",
    "pdf": "آرشیو عکس",
    "tabchi": "تبچی",
    "secretary": "منشی",
    "login": "ورود",
    "verify": "بررسی سلامت",
    "precheck": "پیش‌بررسی",
    "join": "عضو شدن در گروه",
}


def label(what: str) -> str:
    return _LABELS.get(what, what or "یک عملیات")


def key_for(phone: str, customer_id=None, platform: str = "rb") -> str:
    """Build the session key. Same phone + different customer = different key,
    because the two are genuinely different sessions in different folders."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    cid = int(customer_id) if customer_id else 0
    return f"{platform}:{cid}:{digits}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _prune(key: str) -> None:
    """Drop the entry if it has outlived BUSY_STALE_SEC (crashed holder)."""
    entry = _held.get(key)
    if not entry:
        return
    age = time.time() - float(entry.get("since") or 0)
    if age > float(config.BUSY_STALE_SEC):
        _held.pop(key, None)


def who(key: str) -> dict | None:
    """What is currently holding this session, if anything."""
    _prune(key)
    return _held.get(key)


def is_busy(key: str) -> bool:
    return who(key) is not None


def reason(key: str) -> str:
    """A message the customer can act on, instead of a button that does nothing."""
    entry = who(key)
    if not entry:
        return ""
    mins = int((time.time() - float(entry.get("since") or 0)) // 60)
    when = f" (از {mins} دقیقه پیش)" if mins >= 1 else ""
    return (f"⚠️ روی این اکانت الان «{label(entry.get('what'))}» در حال اجراست"
            f"{when}.\nبعد از اتمام دوباره امتحان کن.")


def acquire(key: str, what: str, customer_id=None, extra: dict = None) -> bool:
    """Claim the session. False when somebody else already holds it."""
    _prune(key)
    if key in _held:
        return False
    _held[key] = {
        "what": what,
        "since": time.time(),
        "customer_id": int(customer_id) if customer_id else None,
        "extra": dict(extra or {}),
    }
    return True


def release(key: str, what: str = None) -> None:
    """Free the session. Passing `what` avoids releasing somebody else's claim."""
    entry = _held.get(key)
    if not entry:
        return
    if what and entry.get("what") != what:
        return
    _held.pop(key, None)


def adopt(key: str, what: str, customer_id=None, extra: dict = None) -> None:
    """Re-register a job that was resumed after a restart.

    The registry lives in memory, so a restart empties it — but jobs are
    restart-safe and DO come back. Without this call the resumed job is
    invisible and the next health pass will open a second connection on top of
    it and kill the account. Every recovery routine must call adopt().
    """
    _held[key] = {
        "what": what,
        "since": time.time(),
        "customer_id": int(customer_id) if customer_id else None,
        "extra": dict(extra or {}),
    }


def snapshot() -> list:
    """Everything currently held — for the owner's diagnostics screen."""
    out = []
    for key, entry in list(_held.items()):
        _prune(key)
        if key not in _held:
            continue
        out.append({
            "key": key,
            "what": entry.get("what"),
            "customer_id": entry.get("customer_id"),
            "held_for": int(time.time() - float(entry.get("since") or 0)),
        })
    return out


def busy_account_ids(customer_id=None) -> set:
    """Account ids currently working, when the holder recorded one in `extra`.

    The health engine uses this to SKIP busy accounts: an account that is
    mid-send is provably alive, so probing it gains nothing and risks the
    session.
    """
    out = set()
    for key, entry in list(_held.items()):
        _prune(key)
        if key not in _held:
            continue
        if customer_id and entry.get("customer_id") != int(customer_id):
            continue
        aid = (entry.get("extra") or {}).get("account_id")
        if aid:
            out.add(int(aid))
    return out


def clear_all() -> None:
    """Drop every claim. Only for tests and for a deliberate owner-side reset."""
    _held.clear()


class _Held:
    """Result object yielded by hold()."""

    __slots__ = ("ok", "reason", "key", "what")

    def __init__(self, ok: bool, reason_text: str, key: str, what: str):
        self.ok = ok
        self.reason = reason_text
        self.key = key
        self.what = what

    def __bool__(self) -> bool:
        return self.ok


@contextlib.asynccontextmanager
async def hold(key: str, what: str, customer_id=None, extra: dict = None,
               settle: bool = True):
    """Claim a session for the duration of the block, then always release it.

    `settle` adds the post-release pause described in config.SESSION_SETTLE_SEC:
    even a purely SEQUENTIAL reconnect can be read as a conflict if it happens
    immediately after a disconnect, so we hold the slot a little longer than the
    work itself and let the platform settle before anyone else connects.
    """
    got = acquire(key, what, customer_id=customer_id, extra=extra)
    if not got:
        yield _Held(False, reason(key), key, what)
        return
    try:
        yield _Held(True, "", key, what)
    finally:
        if settle and config.SESSION_SETTLE_SEC > 0:
            try:
                await asyncio.sleep(float(config.SESSION_SETTLE_SEC))
            except asyncio.CancelledError:
                # Still release on cancellation — never leak the claim.
                release(key, what)
                raise
        release(key, what)


# --------------------------------------------------------------------------- #
# Concurrency caps for heavy features (memory, not sessions)
# --------------------------------------------------------------------------- #
_slots: dict[str, int] = {}


def take_slot(name: str, limit: int) -> bool:
    """Global cap on how many of one heavy job may run at once.

    Photo export decodes images in memory; a handful of customers starting one
    simultaneously is enough to exhaust a small VPS. This is separate from the
    session registry: it protects the SERVER, not the account.
    """
    used = int(_slots.get(name, 0))
    if used >= max(1, int(limit)):
        return False
    _slots[name] = used + 1
    return True


def free_slot(name: str) -> None:
    used = int(_slots.get(name, 0))
    _slots[name] = max(0, used - 1)


def slot_used(name: str) -> int:
    return int(_slots.get(name, 0))
