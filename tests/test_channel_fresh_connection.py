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


def test_worker_channel_create_uses_a_fresh_connection():
    body = _src("worker_api.py")
    section = _endpoint(body, "channel/create")
    assert "fresh_call" in section, \
        "channel creation must run on a fresh connection, not the warm one"
    assert "account_conn.call(" not in section, \
        "the warm-socket call is exactly what returned INVALID_AUTH"


def test_worker_channel_add_uses_a_fresh_connection():
    body = _src("worker_api.py")
    section = _endpoint(body, "channel/add")
    assert "fresh_call" in section
    assert "account_conn.call(" not in section


def test_worker_prepare_uses_a_fresh_connection():
    body = _src("worker_api.py")
    section = _endpoint(body, "prepare")
    assert "fresh_call" in section
    assert "account_conn.call(" not in section


def test_the_panel_channel_flow_uses_a_fresh_connection():
    body = _src("rubika_panel.py")
    start = body.index("async def _channel_flow")
    section = body[start:start + 2500]
    assert "fresh_call" in section, \
        "the local channel path must use a fresh connection too"


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
