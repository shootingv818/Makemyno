"""
Channel creation, member-adding and prepare must run on a FRESH connection.

WHAT THIS REPRODUCES
--------------------
On a live server every attempt to create a channel — on the master AND on a
worker — died with:

    rubpy.exceptions.InvalidAuth: {'status_det': 'INVALID_AUTH'}

even though the very same account had just sent hundreds of messages in a pool.
Sends went over the warm, reused connection and worked; the signed channel
calls (addChannel, addChannelMembers) went over that SAME warm socket and the
platform rejected them.

The reference project never hits this because it does not reuse the warm socket
for those calls. It closes any warm connection, opens ONE dedicated client,
does the signed work, and disconnects it. That is exactly what
account_conn.fresh_connection / fresh_call now provide, and what the channel
and prepare code paths now use.

A SECOND bug rode along on the same screens: a plain-text send on a worker
reported "no contacts" on an account with hundreds of them, because /prepare
refused to return the recipient list unless a marker post existed. Text mode
needs no marker, so the list must come back regardless.
"""
import asyncio
import os

import pytest

import account_conn
import rubika_client as rb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class _TrackClient:
    """Records connect/disconnect so a test can prove the lifecycle."""

    def __init__(self, tag):
        self.tag = tag
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fresh_rb(monkeypatch):
    """Make open_client hand out tracked clients and connect_ready a no-op."""
    made = []

    def _open(phone, customer_id):
        c = _TrackClient(f"{customer_id}:{phone}")
        made.append(c)
        return c

    async def _ready(client):
        client.connected = True

    monkeypatch.setattr(rb, "normalize_phone", lambda p: p)
    monkeypatch.setattr(rb, "open_client", _open)
    monkeypatch.setattr(rb, "connect_ready", _ready)
    return made


# --------------------------------------------------------------------------- #
# fresh_connection: the single-connection lifecycle
# --------------------------------------------------------------------------- #
def test_fresh_connection_opens_connects_and_disconnects(fresh_rb):
    async def _go():
        async with account_conn.fresh_connection(7, "0912") as client:
            assert client.connected, "the fresh client must be connected"
            assert not client.disconnected
            return client
    client = asyncio.run(_go())
    assert client.disconnected, "the fresh client must be disconnected on exit"
    assert len(fresh_rb) == 1, "exactly one dedicated client for the operation"


def test_fresh_connection_disconnects_even_when_the_work_raises(fresh_rb):
    async def _go():
        async with account_conn.fresh_connection(7, "0912") as client:
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        asyncio.run(_go())
    assert fresh_rb[0].disconnected, "a failing operation must still disconnect"


def test_fresh_connection_closes_the_warm_socket_first(fresh_rb):
    """The whole point: there must be exactly ONE socket. Any warm connection
    the account was sending on has to be torn down before the fresh one opens —
    otherwise two connections coexist and the platform revokes the session."""
    warm = _TrackClient("warm")
    c = account_conn._get_conn(7, "0912")
    c.client = warm

    async def _go():
        async with account_conn.fresh_connection(7, "0912"):
            pass
    asyncio.run(_go())
    assert warm.disconnected, "the warm connection must be closed first"


def test_the_fresh_client_is_not_left_in_the_warm_cache(fresh_rb):
    """The dedicated client lives only for the operation; the next warm call
    must open its own, not inherit this disconnected one."""
    async def _go():
        async with account_conn.fresh_connection(7, "0912"):
            pass
    asyncio.run(_go())
    assert account_conn._get_conn(7, "0912").client is None


def test_fresh_call_runs_the_function_and_returns_its_value(fresh_rb):
    async def _work(client):
        return f"did work on {client.tag}"
    got = asyncio.run(account_conn.fresh_call(7, "0912", _work))
    assert got == "did work on 7:0912"
    assert fresh_rb[0].disconnected


def test_fresh_call_disconnects_after_a_failure(fresh_rb):
    async def _work(client):
        raise ValueError("nope")
    with pytest.raises(ValueError):
        asyncio.run(account_conn.fresh_call(7, "0912", _work))
    assert fresh_rb[0].disconnected


# --------------------------------------------------------------------------- #
# The source contract: signed operations must NOT reuse the warm socket
# --------------------------------------------------------------------------- #
def _endpoint(body, name):
    """The body of one FastAPI endpoint, up to the next @app decorator."""
    start = body.index(f'"/{name}"')
    tail = body[start:]
    end = tail.find("@app.", 10)
    return tail[:end if end != -1 else len(tail)]


# CORRECTION, after channel creation kept failing with INVALID_AUTH on every
# server even with the fresh-connection rule in place.
#
# The three tests below used to demand `fresh_call`, and that requirement was
# built on half of the reference. The reference has TWO shapes: its older
# /channel/create closes the warm socket and opens a dedicated client, while its
# actively used /gen/create and /broadcast/run — the ones group_panel drives —
# call rb.create_channel straight over the WARM connection. Only the first was
# read, and the conclusion "signed calls need a fresh socket" was generalised
# from it.
#
# What actually broke it was the CHURN, not which socket. fresh_connection closed
# the warm socket and reopened within milliseconds, and config.py has warned all
# along that "even a fast SEQUENTIAL reconnect on the same session can be treated
# as a conflict". Rubika saw the conflict and refused the first signed call.
#
# So the contract is now: go over the warm connection first, like the reference's
# live paths, fall back to a dedicated one, and NEVER reopen without settling.
def test_worker_channel_create_uses_the_signed_call_path():
    body = _src("worker_api.py")
    section = _endpoint(body, "channel/create")
    assert "signed_call" in section, \
        ("channel creation must go through account_conn.signed_call, which tries "
         "the warm connection first like the reference's /gen/create")
    assert "fresh_call(" not in section, \
        ("fresh_call closes and instantly reopens the session; that churn is what "
         "answered addChannel with INVALID_AUTH")


def test_worker_channel_add_uses_the_signed_call_path():
    body = _src("worker_api.py")
    section = _endpoint(body, "channel/add")
    assert "signed_call" in section
    assert "fresh_call(" not in section


def test_signed_call_tries_warm_then_fresh():
    """The order matters: warm first is the reference's live behaviour."""
    src = _src("account_conn.py")
    start = src.index("async def signed_call")
    body = src[start:]
    nxt = body.find("\nasync def ", 10)
    body = _code_only(body[:nxt if nxt != -1 else len(body)])
    warm_at = body.index("call(customer_id, phone, fn")
    fresh_at = body.index("fresh_connection(customer_id, phone)")
    assert warm_at < fresh_at, \
        "signed_call must attempt the warm connection BEFORE the dedicated one"
    assert "is_auth_error" in body, \
        "only an auth-shaped failure may trigger the second attempt"


# --------------------------------------------------------------------------- #
# The real defect: reopening a session without waiting out the settle delay
# --------------------------------------------------------------------------- #
def _code_only(text: str) -> str:
    """Strip comments and docstrings.

    Needed because these checks name the very symbols they guard, in the comment
    that explains the fix. Removing the real `_settle_after_close(...)` call left
    the words "See _settle_after_close" behind in a comment and this test stayed
    green — the same trap test_reference_audit documents.
    """
    out = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"""', "'''")):
            ticks = stripped[:3]
            if not in_doc:
                in_doc = not (stripped.endswith(ticks) and len(stripped) > 3)
                continue
            in_doc = False
            continue
        if in_doc or stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


def test_fresh_connection_settles_before_reopening():
    src = _src("account_conn.py")
    start = src.index("async def fresh_connection")
    body = src[start:]
    nxt = body.find("\nasync def ", 10)
    body = _code_only(body[:nxt if nxt != -1 else len(body)])
    close_at = body.index("close(customer_id, phone)")
    settle_at = body.index("_settle_after_close")
    open_at = body.index("rb.open_client")
    assert close_at < settle_at < open_at, \
        ("fresh_connection must wait AFTER closing the warm socket and BEFORE "
         "opening a new one — reopening in the same breath is the conflict that "
         "made addChannel return INVALID_AUTH")


def test_verify_session_dead_settles_before_probing():
    src = _src("account_conn.py")
    start = src.index("async def verify_session_dead")
    body = src[start:]
    nxt = body.find("\nasync def ", 10)
    body = _code_only(body[:nxt if nxt != -1 else len(body)])
    assert "_settle_after_close" in body, \
        ("probing on a socket opened straight after a close is itself a conflict, "
         "so the probe would report a healthy session as dead")


def test_settle_is_skipped_when_nothing_was_open():
    """An account with no warm socket must not pay the delay for nothing."""
    import asyncio as _asyncio
    import time as _time

    started = _time.monotonic()
    _asyncio.run(account_conn._settle_after_close(False))
    assert (_time.monotonic() - started) < 0.5


def test_settle_actually_waits_when_a_socket_was_closed(monkeypatch):
    import asyncio as _asyncio

    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(account_conn.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(account_conn.config, "SESSION_SETTLE_SEC", 5.0)
    _asyncio.run(account_conn._settle_after_close(True))
    assert slept == [5.0], \
        "a real close must be followed by the full settle delay"


def test_close_reports_whether_a_live_socket_was_closed():
    """The settle decision depends on this being truthful."""
    import asyncio as _asyncio

    async def _go():
        # nothing cached for this session -> nothing was closed
        assert await account_conn.close(4242, "989120000099") is False

    _asyncio.run(_go())


def test_worker_prepare_uses_a_fresh_connection():
    body = _src("worker_api.py")
    section = _endpoint(body, "prepare")
    assert "fresh_call" in section
    assert "account_conn.call(" not in section


def _function_body(body: str, header: str) -> str:
    """One whole top-level function. A fixed byte window stopped covering the
    function as soon as the code grew, which reports a present fix as missing."""
    start = body.index(header)
    rest = body[start + len(header):]
    end = len(rest)
    for marker in ("\nasync def ", "\ndef ", "\nclass "):
        at = rest.find(marker)
        if at != -1:
            end = min(end, at)
    return header + rest[:end]


def test_the_panel_channel_flow_uses_the_signed_call_path():
    body = _src("rubika_panel.py")
    section = _function_body(body, "async def _channel_flow")
    assert "signed_call" in section, \
        "the local channel path must use the same warm-first shape"
    assert "fresh_call(" not in section, \
        "the local path must not churn the session either"


# --------------------------------------------------------------------------- #
# prepare: recipients come back regardless of the marker
# --------------------------------------------------------------------------- #
def test_prepare_returns_recipients_without_a_marker_in_text_mode():
    """The 'no contacts' bug: text mode has no marker, so the source must read
    recipients whether or not a marked post exists, and must not short-circuit
    on 'marker not found'."""
    body = _src("worker_api.py")
    section = _endpoint(body, "prepare")
    assert 'return {"ok": False, "error": "marker not found"}' not in section, \
        "prepare must no longer refuse the recipient list when the marker is absent"
    assert "text_mode" in section, "text mode must skip the marker requirement"
    assert "get_ordered_recipients" in section, "recipients are always read"


def test_prepare_model_accepts_a_mode():
    body = _src("worker_api.py")
    start = body.index("class Prepare(")
    section = body[start:start + 200]
    assert "mode" in section, "the master tells prepare which mode it is"


def test_collect_targets_passes_the_mode_to_the_worker():
    body = _src("rubika_panel.py")
    start = body.index("async def _collect_targets")
    section = body[start:start + 900]
    assert '"mode": mode' in section, \
        "the worker cannot skip the marker for text mode unless it is told the mode"



# --------------------------------------------------------------------------- #
# _collect_targets: end to end, the recipient list survives a missing marker
# --------------------------------------------------------------------------- #
def test_collect_targets_returns_contacts_for_a_text_send_with_no_marker(
        monkeypatch):
    """The exact live symptom: a plain-text send on a worker said 'no contacts'
    on an account that had hundreds. _collect_targets must return them."""
    import rubika_panel
    import worker
    import db

    seen = {}

    async def _api_call(w, method, path, payload=None, timeout=None):
        seen["path"] = path
        seen["payload"] = payload
        # The worker, with the fix, ships the recipient list even though this
        # account has no marked post (message_id None, marker_found False).
        return {"ok": True, "from_guid": "me", "message_id": None,
                "marker_found": False,
                "targets": ["c-1", "c-2", "c-3"]}

    monkeypatch.setattr(worker, "worker_for_account", lambda acc: {"id": 9})
    monkeypatch.setattr(worker, "is_local", lambda w: False)
    monkeypatch.setattr(worker, "api_call", _api_call)
    monkeypatch.setattr(db, "get_marker", lambda cid: "")

    acc = {"id": 1, "phone": "0912"}
    got = asyncio.run(rubika_panel._collect_targets(55, acc, "text"))
    assert got == ["c-1", "c-2", "c-3"], "the recipient list must survive"
    assert seen["payload"]["mode"] == "text", "the mode must reach the worker"
