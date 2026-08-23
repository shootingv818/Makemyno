"""
logbus.py — every log line in the project goes through here.
============================================================

Two destinations, and the difference between them is a product decision:

  * THE LOG GROUP (config.LOG_GROUP_ID) — private, owner only. Full detail:
    stack traces, error codes, which customer did what, which account, which
    worker, what content. This is the forensic trail.

  * THE CUSTOMER'S OWN PV — friendly, short, actionable. Their own events only.

Two rules that are deliberately absolute:

  1. THE LOG GROUP IS NEVER MENTIONED TO A CUSTOMER. Not its id, not its name,
     not "the error was reported to the log group". As far as a customer is
     concerned it does not exist.

  2. A CUSTOMER NEVER SEES A RAW ERROR. They get "a problem occurred" plus a
     short error CODE. The code is the join key: the customer quotes it, the
     owner searches the log group for it and lands on the full trace. That way
     internal detail (library names, worker tags, file paths) never leaks, and
     support still has everything.

Everything is wrapped so that logging can never raise into the caller — a
failure to log must not break a send.
"""
from __future__ import annotations

import asyncio
import os
import re
import traceback
import uuid

import cards
import config

# The telethon client used to deliver messages, injected at startup.
_client = None
# "owner" | "customer" | "worker" — stamped on every card so the log group shows
# which process produced the line.
_role = "?"

# Serialise deliveries: a burst of parallel jobs would otherwise interleave
# multi-line cards into an unreadable mess and hit Telegram's flood limits.
_send_lock: asyncio.Lock | None = None


def bind(client, role: str = None) -> None:
    global _client, _role, _send_lock
    _client = client
    if role:
        _role = role
    else:
        _role = config.MODE or "?"
    _send_lock = asyncio.Lock()


def now() -> str:
    return cards.now()


def card(title: str, rows: list) -> str:
    return cards.card(title, rows)


def panel_card(tag: str, rows: list, footer: str = None) -> str:
    return cards.panel_card(tag, rows, footer)


def new_code() -> str:
    """A short, quotable error code: E-1A2B3C."""
    return "E-" + uuid.uuid4().hex[:6].upper()


async def _deliver(chat_id, text: str, **kwargs):
    if not _client or not chat_id:
        return None
    try:
        if _send_lock is not None:
            async with _send_lock:
                return await _client.send_message(int(chat_id), text, **kwargs)
        return await _client.send_message(int(chat_id), text, **kwargs)
    except Exception:
        # Logging must never raise into the caller.
        return None


async def to_group(text: str, **kwargs):
    """Send raw text to the private log group."""
    return await _deliver(config.LOG_GROUP_ID, text, **kwargs)


async def to_group_file(path: str, caption: str = "", **kwargs):
    if not _client or not config.LOG_GROUP_ID:
        return None
    try:
        return await _client.send_file(int(config.LOG_GROUP_ID), path,
                                       caption=caption, force_document=True,
                                       **kwargs)
    except Exception:
        return None


async def to_pv(user_id, text: str, **kwargs):
    """Send to one customer's private chat."""
    if not user_id:
        return None
    return await _deliver(user_id, text, **kwargs)


# --------------------------------------------------------------------------- #
# The main entry point
# --------------------------------------------------------------------------- #
async def event(title: str, rows: list, pv_user=None, pv_text: str = None,
                footer: str = None):
    """Log one event.

    Always posts the full card to the log group. When `pv_user` is given the
    customer also gets a copy — `pv_text` overrides it with a friendlier
    version, which is how internal fields stay internal.
    """
    body = list(rows or [])
    body.append(f"🕒 {now()}")
    full = cards.card(title, body) if not footer else cards.panel_card(
        title, body, footer)
    await to_group(full)
    if pv_user:
        await to_pv(pv_user, pv_text if pv_text is not None else full)
    return full


async def customer_action(customer, action: str, rows: list = None,
                          platform: str = None, mirror: bool = False):
    """Record something a customer did.

    The owner asked to see everything: who pressed send, what content, which
    platform, which account. This is that trail. `customer` may be an id or the
    customer row (so the card can show a name without a second query).
    """
    if isinstance(customer, dict):
        cid = customer.get("telegram_id")
        name = customer.get("name") or ""
        username = customer.get("username") or ""
    else:
        cid, name, username = customer, "", ""
    who = f"{name}".strip() or "—"
    if username:
        who += f" (@{username})"
    head = [
        cards.kv("Customer", who),
        cards.kv("ID", cid),
    ]
    if platform:
        head.append(cards.kv("Platform", platform))
    body = head + list(rows or [])
    return await event(f"👤 - #{action}", body,
                       pv_user=cid if mirror else None)


# A short, actionable Persian sentence per situation. Ported from the reference.
#
# The customer used to get "⚠️ مشکلی پیش آمد" plus an error code for EVERY failure,
# including the ones that are entirely their own doing and instantly fixable — a
# mistyped login code, a wrong 2FA password, a phone number with a digit missing.
# An error code for those is worse than useless: it tells them to contact support
# about something they could have fixed in five seconds.
_KIND_MSG = {
    "code": "کد تأیید اشتباه یا منقضی شده بود. دوباره شروع کن و کد تازه را سریع "
            "وارد کن.",
    "password": "رمز دومرحله‌ای درست نیست. دوباره امتحان کن.",
    "login": "ورود انجام نشد. چند لحظه بعد دوباره امتحان کن؛ اگر ادامه داشت با "
             "پشتیبانی تماس بگیر.",
    "prepare": "آماده‌سازی ارسال انجام نشد. چند لحظه بعد دوباره امتحان کن.",
    "export": "خروجی گرفتن انجام نشد. چند لحظه بعد دوباره امتحان کن.",
    "generic": "مشکلی پیش آمد. چند لحظه بعد دوباره امتحان کن؛ اگر ادامه داشت با "
               "پشتیبانی تماس بگیر.",
}


def platform_message(err) -> str:
    """Rubika's OWN Persian sentence, when it sent one.

    Rubika answers some refusals with a client_show_message carrying text written
    for the end user — for example that a new account cannot be created yet
    because the previous one was deleted too recently. We were throwing that away
    and showing a generic apology instead, which is absurd: the platform had
    already explained itself better than we could.
    """
    text = str(err or "")
    if "client_show_message" not in text:
        return ""
    match = re.search(r"'message'\s*:\s*'([^']{4,400})'", text)
    if not match:
        match = re.search(r'"message"\s*:\s*"([^"]{4,400})"', text)
    return match.group(1).strip() if match else ""


def humanize_error(err, kind: str = "generic") -> str:
    """One clean Persian sentence for a failure. Never a repr, never a traceback."""
    platform = platform_message(err)
    if platform:
        return platform

    try:
        text = f"{type(err).__name__} {err!r}".lower()
    except Exception:      # noqa: BLE001
        text = ""

    if any(k in text for k in ("codeisinvalid", "code_is_invalid", "invalid_code",
                               "wrong_code", "phone_code_invalid",
                               "phone_code_expired", "codeisexpired")):
        return _KIND_MSG["code"]
    if any(k in text for k in ("password_hash_invalid", "passwordhashinvalid",
                               "wrong_pass", "invalid_pass", "password_invalid")):
        return _KIND_MSG["password"]
    # NOT_REGISTERED is not a broken session: Rubika is saying the account does
    # not exist any more. Telling the customer to "log in again" would send them
    # round a loop they cannot win.
    if any(k in text for k in ("not_registered", "notregistered")):
        return ("این شماره روی روبیکا حساب فعالی ندارد (حساب حذف یا غیرفعال "
                "شده). با شمارهٔ دیگری وارد شو.")
    if any(k in text for k in ("phone_number_invalid", "phonenumberinvalid",
                               "phone_invalid", "invalid_number")):
        return "شماره درست نیست. با کد کشور و کامل بفرست، مثل 09123456789."
    if any(k in text for k in ("auth_from_another", "session_revoked",
                               "authkeyunregistered", "userdeactivated",
                               "invalid_auth", "invalidauth")):
        return ("نشست این اکانت باطل شده — از دستگاه دیگری خارج شده یا پلتفرم "
                "قطعش کرده. یک‌بار دیگر وارد شو.")
    if any(k in text for k in ("too_requests", "toorequests", "too_many",
                               "flood", "slowmode", "slow_mode",
                               "many_requests")):
        return ("پلتفرم موقتاً محدودیت گذاشته. کمی بعد دوباره امتحان کن — "
                "چیزی خراب نشده.")
    if "'busy': true" in text or "busy" in text and "held_for" in text:
        return ("روی این اکانت همین حالا کار دیگری در جریان است. تا تمام شدنش "
                "صبر کن و دوباره بزن.")
    return _KIND_MSG.get(kind, _KIND_MSG["generic"])


async def error(exc: BaseException = None, *, context: str = "",
                customer=None, rows: list = None, notify: bool = True,
                pv_extra: str = "", kind: str = "generic") -> str:
    """Log a failure and return the error CODE to show the customer.

    The log group gets the exception type, the message and the traceback. The
    customer gets a short apology plus the code — never the internals.
    """
    code = new_code()
    cid = customer.get("telegram_id") if isinstance(customer, dict) else customer
    detail = []
    detail.append(cards.kv("Code", code))
    if context:
        detail.append(cards.kv("Where", context))
    if cid:
        detail.append(cards.kv("Customer", cid))
    detail.append(cards.kv("Role", _role))
    if exc is not None:
        detail.append(cards.kv("Error", type(exc).__name__))
        message = str(exc)
        if message:
            detail.append(cards.kv("Message", message[:300]))
    detail.extend(rows or [])
    if exc is not None:
        # The forensics are a convenience. If inspecting a frame goes wrong, the
        # REPORT must still go out: losing the error card is far worse than losing
        # the local variables.
        try:
            frame = _blame(exc)
        except Exception:      # noqa: BLE001
            frame = None
        if frame:
            detail.append(cards.LINE)
            detail.append(cards.kv("File", frame["where"]))
            detail.append(cards.kv("Line", frame["code"]))
            for name, value in frame["locals"]:
                detail.append(f"   {name} = {value}")
        tb = "".join(traceback.format_exception(type(exc), exc,
                                                exc.__traceback__))[-1200:]
        detail.append(cards.LINE)
        detail.append(f"```\n{tb}\n```")
    await to_group(cards.card("⚠️ - #error", detail + [f"🕒 {now()}"]),
                   parse_mode="md")
    if notify and cid:
        # Say WHAT went wrong, in a sentence the customer can act on. The error
        # code stays for the ones nobody can act on — but a mistyped login code
        # does not need a support ticket, and telling somebody to contact support
        # about their own typo is how a working product feels broken.
        explanation = humanize_error(exc, kind=kind) if exc is not None else ""
        rows = [explanation or "درخواست شما کامل نشد."]
        if not _self_inflicted(exc):
            rows.append(cards.kv("کد خطا", code, width=8))
            rows.append(pv_extra
                        or "این کد را برای پشتیبانی بفرست تا بررسی شود.")
        elif pv_extra:
            rows.append(pv_extra)
        rows.append(f"🕒 {now()}")
        await to_pv(cid, cards.card("⚠️ انجام نشد", rows))
    return code


# Failures the customer caused and can fix immediately. These get the explanation
# WITHOUT an error code: a code invites a support ticket, and there is nothing for
# support to do about a mistyped password.
_SELF_INFLICTED = (
    "codeisinvalid", "code_is_invalid", "invalid_code", "phone_code_invalid",
    "phone_code_expired", "codeisexpired",
    "password_hash_invalid", "passwordhashinvalid", "password_invalid",
    "phone_number_invalid", "phonenumberinvalid", "invalid_number",
    "not_registered", "notregistered",
)


def _self_inflicted(exc) -> bool:
    if exc is None:
        return False
    try:
        text = f"{type(exc).__name__} {exc!r}".lower()
    except Exception:      # noqa: BLE001
        return False
    return any(k in text for k in _SELF_INFLICTED)


# Anything whose NAME looks like a credential is redacted: this card goes to the
# log group, and a traceback is not a reason to print an auth token there.
_SECRET_HINTS = ("auth", "key", "token", "pass", "secret", "session", "private")


def _blame(exc: BaseException) -> dict | None:
    """The deepest frame in OUR code, with its local variables.

    WHY THIS EXISTS
    ---------------
    A production login failed with "'NoneType' object is not callable" pointing at

        aid = db.add_account(uid, phone, name=info.get("name") or "",

    Three callables live in that one statement, so the traceback named the wrong
    suspect: `db.add_account` was fine and `info.get` was the None. It cost three
    rounds of debugging, and printing the locals would have ended it in one —
    `info` would have shown up as a rubpy object instead of a dict.

    Only OUR frames are inspected: a traceback through telethon or rubpy has
    locals that are enormous and none of our business. Values are truncated, and
    anything that looks like a credential is redacted, because this card goes to
    the log group.
    """
    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        return None
    root = os.path.dirname(os.path.abspath(__file__))
    chosen = None
    while tb is not None:
        path = os.path.abspath(tb.tb_frame.f_code.co_filename)
        # Anywhere under the project root, including subdirectories, but never a
        # dependency that happens to live inside the virtualenv beneath it.
        inside = path == root or path.startswith(root + os.sep)
        vendored = "site-packages" in path or f"{os.sep}.venv{os.sep}" in path
        if inside and not vendored:
            chosen = tb
        tb = tb.tb_next
    if chosen is None:
        return None

    frame = chosen.tb_frame
    lineno = chosen.tb_lineno
    source = ""
    try:
        import linecache
        source = (linecache.getline(frame.f_code.co_filename, lineno) or "").strip()
    except Exception:      # noqa: BLE001
        pass

    shown = []
    for name, value in list(frame.f_locals.items()):
        if name.startswith("__"):
            continue
        if any(hint in name.lower() for hint in _SECRET_HINTS):
            shown.append((name, "<redacted>"))
            continue
        try:
            text = repr(value)
        except Exception:  # noqa: BLE001 - a broken __repr__ must not hide the bug
            text = f"<unreprable {type(value).__name__}>"
        if len(text) > 160:
            text = text[:160] + "…"
        # The type matters as much as the value: "a dict or a rubpy object?" was
        # the entire question in the bug that motivated this.
        shown.append((name, f"({type(value).__name__}) {text}"))
        if len(shown) >= 12:
            break

    return {
        "where": f"{os.path.basename(frame.f_code.co_filename)}:{lineno}"
                 f" in {frame.f_code.co_name}",
        "code": source[:160] or "—",
        "locals": shown,
    }


async def warn(title: str, rows: list) -> None:
    """Owner-only warning (fleet trouble, dead-account bursts, shield events)."""
    await to_group(cards.card(f"🟡 - #{title}", list(rows or []) + [f"🕒 {now()}"]))


def safe(coro):
    """Fire-and-forget a logging coroutine from a sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(_swallow(coro))


async def _swallow(coro):
    try:
        await coro
    except Exception:
        pass
