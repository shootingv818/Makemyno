"""
session_store.py — put an account's session WHERE THE WORK RUNS, and repair it.
==============================================================================

THE BUG THIS MODULE EXISTS FOR
------------------------------
A Rubika session is a file on ONE machine's disk. The account row says which
server that is (accounts.worker_id), and every job is routed there. When those
two disagree — the file is on server X, the job runs on server Y — rubpy does
NOT fail loudly. It connects UNAUTHENTICATED, and then:

  * reading contacts returns ZERO on an account with thousands, and
  * the first signed call (addChannel) answers INVALID_AUTH.

Which is why "channel creation is broken" and "contacts come back empty" were
never really two bugs, and never really about channels or contacts at all.

Three things put the file and the job on different servers:

  1. A re-login landing on a different server while accounts.worker_id kept the
     old one (fixed in db.add_account).
  2. Logging in with a portable session TOKEN, which stored the five values in
     the database and wrote no session file anywhere at all.
  3. A worker rebuilt, replaced, or provisioned fresh, so its session store is
     empty for accounts the master still believes live there.

HOW THIS FIXES IT
-----------------
The reference project solved this with portable sessions: a session is five
values (auth, private_key, guid, phone, user_agent), and given those it can be
rebuilt on any server WITHOUT a login code. We already store them encrypted in
accounts.session_blob at login. This module is what finally uses them.

  place()            - make sure the session file exists on the account's server
  run_with_repair()  - run an operation; if it fails the way a missing session
                       fails, place the session and try exactly once more

Placing is WRITE-ONLY (session.insert, never connect), so it can never open a
second live connection and can never provoke AUTH_FROM_ANOTHER.
"""
from __future__ import annotations

import db
import rubika_client as rb
import worker


def _auth_shaped(err: Exception) -> bool:
    """Does this failure look like "there is no valid session here"?

    Kept in step with account_conn.is_auth_error, and deliberately narrow: a
    muted group or a network hiccup must not trigger a session rewrite.
    """
    # A verdict that the session is FINE but the operation is refused must never
    # trigger a session rewrite. ChannelNotPermitted quotes the platform's
    # original INVALID_AUTH inside its own message, so a plain text match saw it
    # as a dead session and pointlessly re-placed a perfectly good session, then
    # retried an operation that cannot succeed.
    if type(err).__name__ == "ChannelNotPermitted":
        return False
    text = str(err).upper()
    if "NOT PERMITTED TO CREATE A CHANNEL" in text:
        return False
    return ("INVALID_AUTH" in text or "INVALIDAUTH" in text
            or "NOT_REGISTERED" in text or "AUTH_FROM_ANOTHER" in text)


async def place(customer_id, acc: dict) -> bool:
    """Ensure the account's session file exists on the server that runs its work.

    Returns True when a session was written. False means we had nothing to write
    (no stored blob) or the target server refused — never raises, because this is
    always an attempt to IMPROVE a situation that is already broken.
    """
    if not acc:
        return False
    values = None
    try:
        values = db.get_session_blob(customer_id, acc["id"])
    except Exception:      # noqa: BLE001
        values = None
    if not values or not values.get("auth"):
        # Nothing portable stored (an old login, before session_blob existed).
        # The account can only be repaired by logging in again.
        return False

    phone = acc["phone"]
    try:
        w = worker.worker_for_account(acc)
    except Exception:      # noqa: BLE001
        w = None

    if w and not worker.is_local(w):
        return await worker.push_session(w, customer_id, phone, values)

    # Local: write the file here, closing any warm connection first so nothing
    # is holding the session file open while we replace it.
    try:
        import account_conn
        try:
            await account_conn.close(customer_id, phone)
        except Exception:      # noqa: BLE001
            pass
        return bool(rb.import_session(phone, customer_id, values))
    except Exception:      # noqa: BLE001
        return False


async def run_with_repair(customer_id, acc: dict, operation):
    """Run ``operation()``; on a session-shaped failure, repair and retry ONCE.

    ``operation`` is a zero-argument coroutine FACTORY, not a coroutine, because
    a coroutine cannot be awaited twice and the whole point here is the retry.

    Only one retry, and only for auth-shaped errors: if placing the stored
    session does not fix it, the session really is dead and the caller's error
    handling should see the original failure rather than a loop.
    """
    try:
        return await operation()
    except Exception as first:      # noqa: BLE001
        if not _auth_shaped(first):
            raise
        if not await place(customer_id, acc):
            raise
        # The stored session is now on the right server. One more attempt.
        return await operation()


async def run_resilient(customer_id, acc: dict, operation):
    """run_with_repair, plus ONE settled retry when the PLATFORM said "try again".

    Two different failures, two different cures, deliberately not merged into one
    handler:

      * auth-shaped  -> the session is in the wrong place. place() and retry.
                        That is run_with_repair, unchanged.
      * transient    -> Rubika answered ERROR_TRY_AGAIN / SERVER_ERROR. Nothing is
                        wrong with the session and re-placing it would be pointless
                        work on a healthy account; the cure is to wait and ask again.

    The wait is SESSION_SETTLE_SEC and it is NOT optional. A remote prepare runs on
    a fresh connection, so retrying immediately would close and reopen the same
    session within milliseconds — the exact churn that Rubika answers with
    INVALID_AUTH. Retrying without settling would turn a transient hiccup into a
    quarantined account, which is strictly worse than the bug being fixed.

    Set RB_RETRY_TRIES=1 to make this behave exactly like run_with_repair.
    """
    try:
        return await run_with_repair(customer_id, acc, operation)
    except Exception as first:      # noqa: BLE001
        # Text-based on purpose: for a remote account this failure arrives as a
        # WorkerAPIError carrying the worker's detail string, not as the original
        # rubpy exception, so type checks would never match across the HTTP hop.
        if not rb.is_transient_failure(first):
            raise
        tries, _base, _jitter = rb._retry_settings()
        if tries <= 1:
            raise
        import asyncio

        import config
        settle = float(getattr(config, "SESSION_SETTLE_SEC", 0) or 0)
        if settle > 0:
            await asyncio.sleep(settle)
        return await run_with_repair(customer_id, acc, operation)
