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
    was_live = await close(customer_id, phone)
    # Never reopen in the same breath as the close. See _settle_after_close.
    await _settle_after_close(was_live)
    client = rb.open_client(rb.normalize_phone(phone), _cid(customer_id))
    try:
        await rb.connect_ready(client)
        yield client
    finally:
        await _disconnect_quietly(client)


async def signed_call(customer_id, phone: str, fn, *args, timeout: float = None,
                      **kwargs):
    """Run one signed operation the way the reference's LIVE paths run it.

    The reference has two shapes for creating a channel, and this repo copied the
    wrong one. Its older /channel/create closes the warm socket and opens a
    dedicated client; its actively used /gen/create and /broadcast/run — the ones
    group_panel drives — call rb.create_channel straight over the WARM,
    persistent connection through account_conn.call. Its own module docstring
    says why: "we do NOT connect/disconnect per message — that rapid churn is
    what makes Rubika treat the activity as suspicious and revoke the session."

    So: try the warm connection first, exactly like /gen/create. Only if that
    fails in an auth-shaped way do we settle and try once on a dedicated
    connection, which keeps the old behaviour available instead of betting
    everything on one theory. Whichever wins is visible in the logs.
    """
    try:
        return await call(customer_id, phone, fn, *args, timeout=timeout,
                          **kwargs)
    except InvalidAuthError:
        raise
    except Exception as warm_error:      # noqa: BLE001
        if not is_auth_error(warm_error):
            raise
        try:
            async with fresh_connection(customer_id, phone) as client:
                if timeout:
                    return await asyncio.wait_for(fn(client, *args, **kwargs),
                                                  timeout=timeout)
                return await fn(client, *args, **kwargs)
        except Exception as fresh_error:      # noqa: BLE001
            # Both shapes failed. Report the pair, because "which connection
            # shape did we use" is the single most useful fact for this class of
            # bug and it must not be lost.
            raise RuntimeError(
                f"signed call failed on both connections; "
                f"warm={type(warm_error).__name__}: {str(warm_error)[:120]} | "
                f"fresh={type(fresh_error).__name__}: {str(fresh_error)[:120]}"
            ) from fresh_error


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
        was_live = c.client is not None
        await _drop(c)                        # ditch the suspect connection
        # Same trap as fresh_connection: probing on a socket opened immediately
        # after a close is itself a conflict, so the probe would report a healthy
        # session as dead and quarantine the account.
        await _settle_after_close(was_live)
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


async def close(customer_id, phone: str) -> bool:
    """Force-close a session's warm connection.

    Call this before a fresh login, and before anything that opens its own
    client, so a second connection can never coexist for one session.

    Returns True when a LIVE socket was actually closed. Callers that are about
    to reopen need to know, because reopening straight after a real close is what
    Rubika treats as a conflict — see _settle_after_close.
    """
    c = _conns.get(_key(customer_id, phone))
    if not c:
        return False
    async with c.lock:
        was_live = c.client is not None
        await _drop(c)
        c.invalid = False
        return was_live


async def _settle_after_close(was_live: bool) -> None:
    """Wait out the settle delay before reopening a session we just closed.

    This missing wait is what broke channel creation, member-adding and prepare
    on every server at once.

    config.py has said it all along: "even a fast SEQUENTIAL reconnect on the same
    session can be treated as a conflict, so we always wait after closing before
    opening again." fresh_connection closed the warm socket and reopened within
    milliseconds — zero wait — so Rubika saw a conflict and answered the first
    signed call (addChannel) with INVALID_AUTH. Reads still worked, which is why
    it looked like a channel bug rather than a connection bug.

    It compounded: the failure sent fresh_call into verify_session_dead, another
    instant reconnect, and session_store.run_with_repair then retried the whole
    thing. One "create channel" tap opened four connections to one session inside
    a few seconds.

    Only waited when something was really closed, so an account with no warm
    socket does not pay five seconds for nothing.
    """
    if not was_live:
        return
    delay = float(getattr(config, "SESSION_SETTLE_SEC", 0) or 0)
    if delay > 0:
        await asyncio.sleep(delay)


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
