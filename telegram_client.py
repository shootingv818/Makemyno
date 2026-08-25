"""
telegram_client.py — the Telegram userbot layer (Telethon).
==========================================================

The Telegram counterpart of rubika_client.py. Each account is a Telethon
userbot whose session is stored encrypted in the database as a StringSession, so
accounts survive restarts.

Login reuses the panel's own API_ID / API_HASH — no extra credentials.

Shape mirrors account_conn.py: ONE warm client per account, reused across rounds,
serialised by a per-account lock, with lazy reconnect. Telegram is aggressive
about flood limits, so every public call is FloodWait-aware.

CLIENT IDENTITY IS (customer, phone) — NOT phone
------------------------------------------------
Every function takes a customer id. Two customers may own the same number, and
those are two different sessions; keying the warm-client cache by phone alone
would hand customer B the live client of customer A — the same socket, and an
immediate session revocation for whoever connected first.
"""
from __future__ import annotations

import asyncio
import re

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    ChatWriteForbiddenError,
    UserNotParticipantError,
)

import config
import crypto_util
import db


# --------------------------------------------------------------------------- #
# Warm client registry (one persistent client per account, like account_conn).
# --------------------------------------------------------------------------- #
_clients: dict = {}        # "cid:phone" -> TelegramClient (connected, authorized)
_locks: dict = {}          # "cid:phone" -> asyncio.Lock


def _cid(customer_id) -> int:
    try:
        cid = int(customer_id)
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        raise ValueError("telegram_client requires a customer id "
                         "(two customers may own the same phone number)")
    return cid


def _key(customer_id, phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"{_cid(customer_id)}:{digits}"


def _lock(customer_id, phone: str) -> asyncio.Lock:
    key = _key(customer_id, phone)
    lk = _locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _locks[key] = lk
    return lk


def _new_client(session_str: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session_str or None),
                          config.API_ID, config.API_HASH)


async def save_session(customer_id, account_id, client: TelegramClient):
    """Persist the encrypted StringSession for an account."""
    try:
        raw = client.session.save()
        db.tg_set_session(customer_id, account_id, crypto_util.encrypt(raw))
    except Exception:
        pass


async def get_client(customer_id, account_id) -> TelegramClient:
    """A connected, authorised warm client for one account.

    Opened lazily from the stored session. Raises when the account has no usable
    session, which the caller turns into "log in again" rather than a crash.
    """
    acc = db.tg_get_account(customer_id, account_id)
    if not acc:
        raise RuntimeError("no_account")
    key = _key(customer_id, acc["phone"])
    existing = _clients.get(key)
    if existing is not None and existing.is_connected():
        return existing
    if not acc.get("session"):
        raise RuntimeError("no_session")
    try:
        session_str = crypto_util.decrypt(acc["session"])
    except Exception:
        session_str = acc["session"]
    client = _new_client(session_str)
    await client.connect()
    if not await client.is_user_authorized():
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError("unauthorized")
    _clients[key] = client
    return client


async def drop_client(customer_id, phone: str):
    client = _clients.pop(_key(customer_id, phone), None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


async def close_customer(customer_id):
    """Close every warm client belonging to one customer."""
    prefix = f"{_cid(customer_id)}:"
    for key in [k for k in _clients if k.startswith(prefix)]:
        client = _clients.pop(key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def close_all():
    for key in list(_clients.keys()):
        client = _clients.pop(key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def open_count() -> int:
    """How many warm Telegram clients are held (owner diagnostics)."""
    return sum(1 for c in _clients.values() if c is not None)


# --------------------------------------------------------------------------- #
# FloodWait-aware call wrapper.
# --------------------------------------------------------------------------- #
async def safe_call(coro_factory, *, retries: int = 1):
    """Run an awaitable; if Telegram answers FloodWaitError, wait the requested
    seconds (capped) and retry once. `coro_factory` is a zero-arg function
    returning a fresh awaitable each attempt."""
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", 5)) + 1, config.TG_FLOOD_MAX_WAIT)
            if attempt >= retries:
                raise
            attempt += 1
            await asyncio.sleep(wait)


# --------------------------------------------------------------------------- #
# Login flow (phone -> code -> optional 2FA password).
# --------------------------------------------------------------------------- #
async def start_login(phone: str) -> dict:
    """Begin login: connect a fresh client and request the SMS/app code.
    Returns a ctx dict carried through the conversation."""
    client = _new_client("")
    await client.connect()
    sent = await client.send_code_request(phone)
    return {"client": client, "phone": phone,
            "phone_code_hash": getattr(sent, "phone_code_hash", None)}


async def finish_login(ctx: dict, code: str) -> dict:
    """Sign in with the code. May raise SessionPasswordNeededError (-> 2FA)."""
    client = ctx["client"]
    await client.sign_in(ctx["phone"], code,
                         phone_code_hash=ctx.get("phone_code_hash"))
    return ctx


async def finish_password(ctx: dict, password: str) -> dict:
    client = ctx["client"]
    await client.sign_in(password=password)
    return ctx


async def commit_login(customer_id, ctx: dict) -> dict:
    """Finish a login: read the account info, persist the encrypted session under
    this customer, and keep the client warm. Returns the info dict plus the new
    account id."""
    client = ctx["client"]
    phone = ctx["phone"]
    info = await account_info(client)
    account_id = db.tg_add_account(
        customer_id, phone,
        name=info.get("name", ""), username=info.get("username", ""),
        session=crypto_util.encrypt(client.session.save()),
        contacts=info.get("contacts", 0),
        mutuals=info.get("mutuals", 0),
        groups=info.get("groups", 0),
    )
    _clients[_key(customer_id, phone)] = client
    info["account_id"] = account_id
    return info


# --------------------------------------------------------------------------- #
# Account info.
# --------------------------------------------------------------------------- #
def _full_name(me) -> str:
    parts = [getattr(me, "first_name", "") or "", getattr(me, "last_name", "") or ""]
    return " ".join(p for p in parts if p).strip() or "—"


async def account_info(client: TelegramClient) -> dict:
    me = await client.get_me()
    users = []
    try:
        res = await client(functions.contacts.GetContactsRequest(hash=0))
        users = list(getattr(res, "users", []) or [])
    except Exception:
        users = []
    mutuals = len([u for u in users if getattr(u, "mutual_contact", False)])
    groups = 0
    try:
        async for d in client.iter_dialogs():
            if getattr(d, "is_group", False):
                groups += 1
    except Exception:
        groups = 0
    return {
        "user_id": getattr(me, "id", None),
        "name": _full_name(me),
        "username": getattr(me, "username", "") or "",
        "phone": getattr(me, "phone", "") or "",
        "contacts": len(users),
        "mutuals": mutuals,
        "groups": groups,
    }


async def get_contacts(client: TelegramClient) -> list:
    res = await client(functions.contacts.GetContactsRequest(hash=0))
    return list(getattr(res, "users", []) or [])


async def get_mutual_contacts(client: TelegramClient) -> list:
    """Only contacts who added the account back (mutual). Safest to message."""
    users = await get_contacts(client)
    return [u for u in users if getattr(u, "mutual_contact", False)]


async def get_contacts_ordered(client: TelegramClient) -> tuple:
    """Return (mutuals, others) — TWO LISTS, mutuals first.

    Both callers unpack this as `mutuals, others = ...` and iterate each, marking
    the mutual ones so a send reaches them first (they added the account back, so
    they are the least likely to report it).

    This used to return (ordered_list, mutual_COUNT), and every caller unpacked
    the count into `others` and then ran `for user in others` — a plain
    `'int' object is not iterable` on the first real Telegram send. Two lists is
    what the callers always wanted; the count is just len(mutuals).
    """
    users = await get_contacts(client)
    mutuals = [u for u in users if getattr(u, "mutual_contact", False)]
    others = [u for u in users if not getattr(u, "mutual_contact", False)]
    return mutuals, others


# --------------------------------------------------------------------------- #
# Groups the account is a member of (tabchi targets).
# --------------------------------------------------------------------------- #
async def get_group_entities(client: TelegramClient) -> list:
    out = []
    async for d in client.iter_dialogs():
        try:
            if d.is_group:
                out.append(d.entity)
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# Sending (text + media with caption) — FloodWait-aware. Optional human typing.
# --------------------------------------------------------------------------- #
async def _typing(client: TelegramClient, entity, seconds: float):
    """Show a human-like typing indicator for `seconds` before sending."""
    if seconds <= 0:
        return
    try:
        async with client.action(entity, "typing"):
            await asyncio.sleep(seconds)
    except Exception:
        await asyncio.sleep(seconds)


async def send_text(client: TelegramClient, entity, text: str, typing: float = 0.0):
    if typing > 0:
        await _typing(client, entity, typing)
    return await safe_call(lambda: client.send_message(entity, text))


def _name_attributes(file_path: str, file_name: str = ""):
    """[DocumentAttributeFilename] for the name the RECIPIENT should see, or None.

    Telethon derives a document's name from os.path.basename() of whatever path it
    is handed (telethon/utils.py, get_attributes). Our stored path is
    "<uid>_<random>_<name>", so every file arrived at the recipient carrying that
    prefix — the customer's "قرارداد نهایی.pdf" was delivered as
    "7658493021_9f3c1a44_قراردادنهایی.pdf". Passing the attribute explicitly
    overrides the guess, and it also means a future change to the storage layout
    can never leak into what the recipient sees.

    Returns None when there is nothing to override with, so the caller falls back
    to Telethon's own behaviour rather than sending a nameless document.
    """
    import os

    import rubika_client as rb      # safe_file_name lives there; one definition

    if not rb._keep_file_name():
        return None
    name = rb.safe_file_name(file_name or "", fallback="") \
        or rb.safe_file_name(os.path.basename(file_path or ""), fallback="")
    if not name:
        return None
    try:
        from telethon.tl.types import DocumentAttributeFilename
        return [DocumentAttributeFilename(file_name=name)]
    except Exception:      # noqa: BLE001 - never fail a send over a nicer name
        return None


async def _send_file_named(client: TelegramClient, entity, file, caption: str,
                           file_path: str = "", file_name: str = ""):
    """send_file with an explicit filename, falling back to the plain call.

    A photo sent as a photo has no filename at all on Telegram's side, and some
    builds reject `attributes` for non-document media. Losing the name is a
    cosmetic problem; failing the send is a campaign problem — so any failure here
    retries exactly the call this code made before.
    """
    attributes = _name_attributes(file_path or (file if isinstance(file, str)
                                                else ""), file_name)
    if attributes:
        try:
            return await safe_call(lambda: client.send_file(
                entity, file, caption=caption or None, attributes=attributes))
        except (TypeError, ValueError):
            pass
    return await safe_call(
        lambda: client.send_file(entity, file, caption=caption or None))


async def send_media(client: TelegramClient, entity, file_path: str,
                     caption: str = "", typing: float = 0.0,
                     file_name: str = ""):
    if typing > 0:
        await _typing(client, entity, typing)
    return await _send_file_named(client, entity, file_path, caption,
                                  file_path=file_path, file_name=file_name)


async def send_content(client: TelegramClient, entity, text: str = "",
                       file_path: str = "", caption: str = "", typing: float = 0.0,
                       file_name: str = ""):
    """Send configured content: a media file with caption if file_path is set,
    otherwise a plain text message."""
    if file_path:
        return await send_media(client, entity, file_path, caption or text, typing,
                                file_name=file_name)
    return await send_text(client, entity, text, typing)


async def upload_to_saved(client: TelegramClient, file_path: str,
                          caption: str = "", file_name: str = ""):
    """Upload a media file ONCE to the account's own Saved Messages and return
    the resulting Message. The file is then FORWARDED to every recipient, so it
    is uploaded a single time instead of re-uploaded per chat (much faster).

    This is THE place the recipient-visible name is decided for a whole campaign:
    every later send reuses this upload's media object, so the name recorded here
    is the name a thousand recipients get.
    """
    return await _send_file_named(client, "me", file_path, caption,
                                  file_path=file_path, file_name=file_name)


async def forward_to(client: TelegramClient, entity, message):
    """Forward an already-sent (Saved-Messages) message to a chat — no re-upload."""
    return await safe_call(lambda: client.forward_messages(entity, message))


async def send_saved_media(client: TelegramClient, entity, saved_msg, caption: str = ""):
    """Re-send the media of an already-uploaded Saved-Messages message WITHOUT a
    'Forwarded from' header and WITHOUT re-uploading the file (reuses the file
    reference). Falls back to a plain forward if the build can't reuse media."""
    media = getattr(saved_msg, "media", None)
    if media is not None:
        try:
            return await safe_call(
                lambda: client.send_file(entity, media, caption=caption or None))
        except Exception:
            pass
    return await safe_call(lambda: client.forward_messages(entity, saved_msg))


# --------------------------------------------------------------------------- #
# Group / channel join + comment / forced-membership helpers (phases 3-5).
# --------------------------------------------------------------------------- #
_INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w\-]+)",
                        re.IGNORECASE)
_PUBLIC_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z][\w\d_]{3,})",
                        re.IGNORECASE)
_TG_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)?[\w\-]+",
    re.IGNORECASE)


def extract_tg_links(text: str) -> list:
    if not text:
        return []
    out, seen = [], set()
    for m in _TG_LINK_RE.findall(text):
        lk = m.rstrip("/")
        if lk not in seen:
            seen.add(lk)
            out.append(lk)
    return out


async def join_link(client: TelegramClient, link: str):
    """Join via private invite (t.me/+hash) or public username. Returns the
    joined entity (or None)."""
    link = (link or "").strip()
    m = _INVITE_RE.search(link)
    if m:
        res = await safe_call(
            lambda: client(functions.messages.ImportChatInviteRequest(m.group(1))))
        chats = getattr(res, "chats", None)
        return chats[0] if chats else None
    m = _PUBLIC_RE.search(link)
    uname = m.group(1) if m else link.lstrip("@")
    ent = await client.get_entity(uname)
    await safe_call(lambda: client(functions.channels.JoinChannelRequest(ent)))
    return ent


async def get_linked_discussion(client: TelegramClient, channel):
    """Return the linked discussion-group entity of a channel (the place where
    comments live), or None if the channel has comments disabled."""
    try:
        full = await client(functions.channels.GetFullChannelRequest(channel))
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if linked_id:
            return await client.get_entity(linked_id)
    except Exception:
        return None
    return None


async def ensure_can_write(client: TelegramClient, entity) -> bool:
    """Try to guarantee the account can write in `entity`; join it if needed
    (handles the common 'must join to send' / forced-membership case at the
    API level). Returns True if writing should now be possible."""
    try:
        await client(functions.channels.JoinChannelRequest(entity))
        return True
    except UserNotParticipantError:
        try:
            await client(functions.channels.JoinChannelRequest(entity))
            return True
        except Exception:
            return False
    except ChatWriteForbiddenError:
        return False
    except Exception:
        # not a channel (basic group) or already a member — assume writable
        return True



# --------------------------------------------------------------------------- #
# Phase 3/4 helpers: read a channel's recent messages + comment under a post.
# --------------------------------------------------------------------------- #
async def get_recent_messages(client: TelegramClient, entity, limit: int = 100) -> list:
    out = []
    try:
        async for m in client.iter_messages(entity, limit=limit):
            out.append(m)
    except Exception:
        pass
    return out


async def get_recent_post_ids(client: TelegramClient, channel, limit: int = 5) -> list:
    """IDs of the most recent posts of a channel (newest first)."""
    ids = []
    try:
        async for m in client.iter_messages(channel, limit=limit):
            if getattr(m, "id", None):
                ids.append(m.id)
    except Exception:
        pass
    return ids


async def comment_to_post(client: TelegramClient, channel, post_id: int, text: str,
                          typing: float = 0.0):
    """Post a comment under a channel post (Telethon routes it to the linked
    discussion group via comment_to). If the account must first join the
    discussion group, join it and retry once."""
    async def _send():
        return await client.send_message(channel, text, comment_to=post_id)
    if typing > 0:
        try:
            await asyncio.sleep(typing)
        except Exception:
            pass
    try:
        return await safe_call(_send)
    except Exception:
        # forced membership: join the linked discussion group, then retry once.
        disc = await get_linked_discussion(client, channel)
        if disc is not None:
            try:
                await client(functions.channels.JoinChannelRequest(disc))
            except Exception:
                pass
        return await safe_call(_send)


async def entity_id(entity) -> int:
    return getattr(entity, "id", None)
