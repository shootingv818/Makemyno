"""
account_conn.py — ONE warm connection per account session, reused and serialised.
================================================================================

The connection shape that Rubika tolerates, and why each part matters:

  1) ONE persistent connection for the life of the work: opened once
     (open_client + connect_ready) and REUSED. We do not connect/disconnect per
     message — that churn is what makes Rubika treat the activity as suspicious
     and revoke the session.

  2) Lazy reconnect: there is a single client per session. If a call fails
     because the socket died we drop it and the NEXT call transparently
     reconnects, so we never get stuck on a dead socket.

  3) Timeouts: callers pass timeout= so one stuck request cannot hold the
     connection forever.

  4) A per-session asyncio.Lock serialises calls, so the same session is never
     used from two places at once — the number-one cause of INVALID_AUTH.

  + An idle janitor closes connections nobody has used for a while.
  + A suspected dead session is CONFIRMED on a fresh connection before the
     account is ever declared invalid, so a muted group or a transient network
     hiccup is not mistaken for a revoked session.

SESSION IDENTITY IS (customer, phone) — NOT phone
-------------------------------------------------
Every function here takes a customer id as well as the phone number. Two
customers may own the same number, and their sessions are genuinely different
sessions living in different folders. Keying this cache by phone alone would
hand customer B the warm client of customer A: the same account object, the same
socket, and an instant AUTH_FROM_ANOTHER for whoever connected first.

This module never imports the bots; the optional invalid-auth notifier is
injected with set_invalid_auth_handler().
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import config
import rubika_client as rb


class _Conn:
    __slots__ = ("customer_id", "phone", "lock", "client", "last_used", "invalid")

    def __init__(self, customer_id: int, phone: str):
        self.customer_id = customer_id
        self.phone = phone
        self.lock = asyncio.Lock()
        self.client = None          # the ONE persistent rubpy client (or None)
        self.last_used = 0.0
        self.invalid = False


_conns: dict = {}                   # "cid:phone" -> _Conn
_janitor_task = None
_invalid_auth_handler = None        # async def handler(customer_id, phone) -> None


def set_invalid_auth_handler(fn):
    global _invalid_auth_handler
    _invalid_auth_handler = fn


def _cid(customer_id) -> int:
    try:
        cid = int(customer_id)
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        raise ValueError("account_conn requires a customer id "
                         "(two customers may own the same phone number)")
    return cid


def _key(customer_id, phone: str) -> str:
    return f"{_cid(customer_id)}:{rb.normalize_phone(phone)}"


def _get_conn(customer_id, phone: str) -> _Conn:
    cid = _cid(customer_id)
    key = _key(cid, phone)
    c = _conns.get(key)
    if c is None:
        c = _Conn(cid, rb.normalize_phone(phone))
        _conns[key] = c
    return c


def is_auth_error(err: Exception) -> bool:
    """True ONLY for explicit Rubika 'session invalid' signals.

    Deliberately narrow: a banned or muted group, one failed send, a timeout or
    a transient network hiccup must NOT look like a dead session, because that
    is how healthy accounts get wrongly quarantined.
    """
    s = str(err).upper()
    return ("INVALID_AUTH" in s or "INVALIDAUTH" in s
            or "NOT_REGISTERED" in s or "AUTH_FROM_ANOTHER" in s)


class InvalidAuthError(RuntimeError):
    """Raised when the account session is invalid (needs a fresh login)."""


async def _disconnect_quietly(client):
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


async def _ensure_connected(c: _Conn):
    """Return the warm client, opening and connecting it once if needed."""
    if c.client is not None:
        return c.client
    client = rb.open_client(c.phone, c.customer_id)
    await rb.connect_ready(client)
    c.client = client
    return client


async def _drop(c: _Conn):
    """Close and forget the persistent client so the next call reconnects."""
    cl = c.client
    c.client = None
    await _disconnect_quietly(cl)


@asynccontextmanager
async def connection(customer_id, phone: str):
    """Hold the session's lock and yield its ONE warm client.

    The connection is NOT closed on normal exit — it stays warm and is reused.
    On any error inside the block the client is dropped so the next round
    reconnects. Auth errors propagate unchanged so the caller can confirm them.
    """
    c = _get_conn(customer_id, phone)
    async with c.lock:
        c.last_used = time.monotonic()
        try:
            client = await _ensure_connected(c)
            yield client
            c.last_used = time.monotonic()
        except Exception:
            await _drop(c)
            raise


@asynccontextmanager
async def fresh_connection(customer_id, phone: str):
    """Yield a brand-new, single-use client for ONE signed operation.

    This is the shape Rubika demands for the heavy signed calls — creating a
    channel, adding members, forwarding the marked post, joining a group. Run
    over a *reused* warm socket (the one an account has just been sending on),
    the platform answers those calls with INVALID_AUTH; run on a fresh
    connection they succeed. So we:

      1) close any warm connection for this session (there must be exactly one
         socket for the account — "ensure single connection"),
      2) open a dedicated client and connect it,
      3) hand it to the caller,
      4) always disconnect it on the way out.

    The fresh client is deliberately NOT stored in the warm-connection cache: it
    lives only for this operation. The next warm ``call`` transparently opens a
    new persistent connection. Callers must already hold the account's busy lock
    so a second connection can never coexist with this one.
    """
    await close(customer_id, phone)
    client = rb.open_client(rb.normalize_phone(phone), _cid(customer_id))
    try:
        await rb.connect_ready(client)
        yield client
    finally:
        await _disconnect_quietly(client)


async def fresh_call(customer_id, phone: str, fn, *args, timeout: float = None,
                     **kwargs):
    """Run one ``fn(client, ...)`` on a fresh single-use connection.

    Same auth-confirmation contract as ``call``: an auth-looking failure is
    verified on yet another fresh connection before InvalidAuthError is raised,
    so a transient hiccup never wrongly quarantines a healthy account.
    """
    try:
        async with fresh_connection(customer_id, phone) as client:
            if timeout:
                return await asyncio.wait_for(fn(client, *args, **kwargs),
                                              timeout=timeout)
            return await fn(client, *args, **kwargs)
    except InvalidAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        if is_auth_error(e) and await verify_session_dead(customer_id, phone):
            await notify_invalid(customer_id, phone)
            raise InvalidAuthError(
                f"{_key(customer_id, phone)}: session invalid") from e
        raise


async def call(customer_id, phone: str, fn, *args, timeout: float = None,
               **kwargs):
    """Run one ``fn(client, ...)`` on the warm connection.

    On an auth-looking error the session is CONFIRMED dead on a fresh connection
    before InvalidAuthError is raised, so a transient hiccup never kills a
    healthy account.
    """
    try:
        async with connection(customer_id, phone) as client:
            if timeout:
                return await asyncio.wait_for(fn(client, *args, **kwargs),
                                              timeout=timeout)
            return await fn(client, *args, **kwargs)
    except InvalidAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        if is_auth_error(e) and await verify_session_dead(customer_id, phone):
            await notify_invalid(customer_id, phone)
            raise InvalidAuthError(
                f"{_key(customer_id, phone)}: session invalid") from e
        raise


async def verify_session_dead(customer_id, phone: str) -> bool:
    """Confirm a suspected dead session on a FRESH connection.

    Opens a brand-new client and makes one cheap read-only call. If that works
    the session is alive and the earlier error was transient -> False. Only an
    explicit auth failure on the fresh connection means truly dead -> True.

    NOTE FOR CALLERS: never run this on an account that is mid-job. It opens a
    second connection, which is the very thing that revokes a session. The
    health engine consults busy.busy_account_ids() first and skips busy
    accounts — a busy account is provably alive anyway.
    """
    c = _get_conn(customer_id, phone)
    async with c.lock:
        await _drop(c)                        # ditch the suspect connection
        client = None
        try:
            client = rb.open_client(c.phone, c.customer_id)
            await rb.connect_ready(client)
            await asyncio.wait_for(rb.get_self_guid(client), timeout=30)
            # the fresh connection works -> keep it as the new warm client
            c.client = client
            c.last_used = time.monotonic()
            client = None                     # do not close it in finally
            return False
        except Exception as e:  # noqa: BLE001
            return is_auth_error(e)
        finally:
            await _disconnect_quietly(client)


async def notify_invalid(customer_id, phone: str):
    """Mark a session invalid and hand it to the injected notifier."""
    c = _get_conn(customer_id, phone)
    c.invalid = True
    await _drop(c)
    if _invalid_auth_handler is None:
        return
    try:
        await _invalid_auth_handler(c.customer_id, c.phone)
    except Exception:
        pass


async def close(customer_id, phone: str):
    """Force-close a session's warm connection.

    Call this before a fresh login, and before anything that opens its own
    client, so a second connection can never coexist for one session.
    """
    c = _conns.get(_key(customer_id, phone))
    if not c:
        return
    async with c.lock:
        await _drop(c)
        c.invalid = False


async def close_all():
    for c in list(_conns.values()):
        try:
            async with c.lock:
                await _drop(c)
        except Exception:
            pass


async def close_customer(customer_id):
    """Close every warm connection belonging to one customer."""
    prefix = f"{_cid(customer_id)}:"
    for key, c in list(_conns.items()):
        if not key.startswith(prefix):
            continue
        try:
            async with c.lock:
                await _drop(c)
                c.invalid = False
        except Exception:
            pass


def reset_invalid(customer_id, phone: str):
    c = _conns.get(_key(customer_id, phone))
    if c:
        c.invalid = False


def drop_connection(customer_id, phone: str):
    """Schedule a force-close WITHOUT awaiting.

    Safe to call from inside a loop after a stuck or timed-out call: the
    disconnect runs in the background and the next connection() reconnects.
    """
    c = _conns.get(_key(customer_id, phone))
    if not c:
        return
    cl = c.client
    c.client = None
    if cl is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop to schedule on. The reference is already cleared, so the
        # socket will be reclaimed; building the coroutine here would only leak
        # an un-awaited object.
        return
    loop.create_task(_disconnect_quietly(cl))


def is_invalid(customer_id, phone: str) -> bool:
    c = _conns.get(_key(customer_id, phone))
    return bool(c and c.invalid)


def open_count() -> int:
    """How many warm sockets are currently held (owner diagnostics)."""
    return sum(1 for c in _conns.values() if c.client is not None)


# --------------------------------------------------------------------------- #
# Idle janitor: close connections nobody has used for a while, so accounts
# whose engines were switched off do not keep a socket open forever.
# --------------------------------------------------------------------------- #
async def _janitor_loop():
    idle = max(60, int(config.CONN_IDLE_CLOSE_SEC))
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for c in list(_conns.values()):
            if c.client is None or c.lock.locked():
                continue
            if (now - c.last_used) < idle:
                continue
            try:
                await asyncio.wait_for(c.lock.acquire(), timeout=0.1)
            except Exception:
                continue
            try:
                if c.client is not None and (time.monotonic() - c.last_used) >= idle:
                    await _drop(c)
            finally:
                c.lock.release()


def start_janitor():
    global _janitor_task
    if _janitor_task is None or _janitor_task.done():
        try:
            _janitor_task = asyncio.create_task(_janitor_loop())
        except RuntimeError:
            _janitor_task = None
    return _janitor_task
