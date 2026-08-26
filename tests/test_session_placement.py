"""
A session file must live on the server that runs the account's work.

WHAT THIS REPRODUCES
--------------------
Channel creation failed with INVALID_AUTH on master AND on every worker, for
hours, across several supposed fixes. It was never about channels. A Rubika
session is a FILE on one machine; accounts.worker_id says which machine, and the
job is routed there. When the file and the job are on different servers, rubpy
does not error — it connects UNAUTHENTICATED. Then:

    * get_contacts returns ZERO on an account with thousands, and
    * the first SIGNED call (addChannel) answers INVALID_AUTH.

Which is exactly the pair of symptoms that were chased as two separate bugs.

Three ways the file and the job ended up apart:

  1. db.add_account never UPDATED worker_id, so a re-login that landed on a
     different server kept pointing at the old one.
  2. Token login stored the five portable values in the database and wrote no
     session file anywhere, while reporting "Session Saved: YES".
  3. A rebuilt or freshly provisioned worker has an empty session store for
     accounts the master still believes live there.
"""
import asyncio
import os

import pytest

import db
import rubika_client as rb
import session_store
import worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


_VALUES = {"auth": "AUTH123", "private_key": "PK", "guid": "u-1",
           "user_agent": "UA", "phone": "989121110000"}


# --------------------------------------------------------------------------- #
# 1. Routing: a re-login must move the account to the server it logged in on
# --------------------------------------------------------------------------- #
def test_a_relogin_on_another_server_updates_the_route(alice):
    """THE ROUTING BUG. The session file is written where the login happened; if
    the account row still points elsewhere, every job runs on a server with no
    session and gets INVALID_AUTH."""
    aid = db.add_account(alice, "09121110000", name="a", worker_id=3)
    assert db.get_account(alice, aid)["worker_id"] == 3
    again = db.add_account(alice, "09121110000", name="a", worker_id=7)
    assert again == aid, "the same phone must stay one account"
    assert db.get_account(alice, aid)["worker_id"] == 7, \
        "a re-login on worker 7 must route the account to worker 7"


def test_omitting_the_worker_does_not_move_an_account(alice):
    """Passing no server must not silently drag an account off the worker that
    actually holds its session."""
    aid = db.add_account(alice, "09121110001", name="a", worker_id=5)
    db.add_account(alice, "09121110001", name="renamed")
    acc = db.get_account(alice, aid)
    assert acc["worker_id"] == 5, "worker_id must survive an update that omits it"
    assert acc["name"] == "renamed", "the rest of the update still applies"


# --------------------------------------------------------------------------- #
# 2. Writing a session from the five portable values
# --------------------------------------------------------------------------- #
class _RecordingSession:
    def __init__(self):
        self.inserted = None

    def insert(self, **kwargs):
        self.inserted = kwargs


class _RecordingClient:
    def __init__(self, name):
        self.name = name
        self.session = _RecordingSession()
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        return None


@pytest.fixture
def recording(monkeypatch):
    made = []

    def _make(name):
        c = _RecordingClient(name)
        made.append(c)
        return c
    monkeypatch.setattr(rb, "_make_client", _make)
    return made


def test_import_session_writes_the_five_values(recording):
    assert rb.import_session("09121110000", 42, _VALUES) is True
    client = recording[0]
    assert client.session.inserted is not None, "a session file must be written"
    got = client.session.inserted
    assert got["auth"] == "AUTH123"
    assert got["private_key"] == "PK", \
        "private_key is what signs addChannel; without it channels fail"
    assert got["guid"] == "u-1"
    assert got["user_agent"] == "UA"


def test_import_session_never_connects(recording):
    """Write-only on purpose: connecting here would be a SECOND live connection
    on the session, which is what provokes AUTH_FROM_ANOTHER."""
    rb.import_session("09121110000", 42, _VALUES)
    assert recording[0].connected is False, "importing must not open a connection"


def test_import_session_uses_the_customer_scoped_path(recording):
    rb.import_session("09121110000", 42, _VALUES)
    assert "c42" in recording[0].name, "sessions are namespaced per customer"
    assert "989121110000" in recording[0].name, "and keyed by the normalised phone"


def test_import_session_refuses_values_with_no_auth(recording):
    assert rb.import_session("09121110000", 42, {"private_key": "PK"}) is False
    assert rb.import_session("09121110000", 42, {}) is False
    assert rb.import_session("09121110000", 42, None) is False
    assert recording == [], "nothing may be written without an auth value"


# --------------------------------------------------------------------------- #
# 3. place(): put the stored session where the work will run
# --------------------------------------------------------------------------- #
def test_place_writes_the_session_locally(alice, recording, monkeypatch):
    aid = db.add_account(alice, "09121110000", name="a")
    db.set_session_blob(alice, aid, _VALUES)
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)
    assert asyncio.run(session_store.place(alice, acc)) is True
    assert recording[0].session.inserted["auth"] == "AUTH123"


def test_place_pushes_the_session_to_the_owning_worker(alice, monkeypatch):
    aid = db.add_account(alice, "09121110000", name="a", worker_id=2)
    db.set_session_blob(alice, aid, _VALUES)
    sent = {}

    async def _api(w, method, path, payload=None, timeout=None):
        sent["path"] = path
        sent["payload"] = payload
        return {"ok": True}
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "wk-1"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)
    monkeypatch.setattr(worker, "api_call", _api)

    acc = db.get_account(alice, aid)
    assert asyncio.run(session_store.place(alice, acc)) is True
    assert sent["path"] == "/session/import"
    assert sent["payload"]["auth"] == "AUTH123"
    assert sent["payload"]["private_key"] == "PK"
    assert sent["payload"]["customer_id"] == alice


def test_place_reports_failure_when_nothing_is_stored(alice, monkeypatch):
    """An account from before portable sessions has no blob; it can only be
    repaired by a fresh login, and place() must say so rather than pretend."""
    aid = db.add_account(alice, "09121110000", name="a")
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)
    assert asyncio.run(session_store.place(alice, acc)) is False


def test_place_never_raises_when_the_worker_is_unreachable(alice, monkeypatch):
    aid = db.add_account(alice, "09121110000", name="a", worker_id=2)
    db.set_session_blob(alice, aid, _VALUES)

    async def _boom(*a, **k):
        raise RuntimeError("worker down")
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "wk-1"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)
    monkeypatch.setattr(worker, "api_call", _boom)
    acc = db.get_account(alice, aid)
    assert asyncio.run(session_store.place(alice, acc)) is False


# --------------------------------------------------------------------------- #
# 4. run_with_repair(): the self-heal
# --------------------------------------------------------------------------- #
def test_an_auth_failure_is_repaired_and_retried_once(alice, recording,
                                                      monkeypatch):
    """The whole point: the customer's channel creation succeeds on the retry
    instead of showing them INVALID_AUTH."""
    aid = db.add_account(alice, "09121110000", name="a")
    db.set_session_blob(alice, aid, _VALUES)
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)
    calls = []

    async def _op():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("{'status_det': 'INVALID_AUTH'}")
        return "channel-guid"

    got = asyncio.run(session_store.run_with_repair(alice, acc, _op))
    assert got == "channel-guid"
    assert len(calls) == 2, "the operation must be retried exactly once"
    assert recording, "the session must have been placed before the retry"


def test_a_non_auth_failure_is_not_retried(alice, monkeypatch):
    """Rewriting a session because a group was muted would be a great way to
    break healthy accounts."""
    aid = db.add_account(alice, "09121110000", name="a")
    db.set_session_blob(alice, aid, _VALUES)
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)
    calls = []

    async def _op():
        calls.append(1)
        raise RuntimeError("CHANNEL_ACCESS_DENIED")

    with pytest.raises(RuntimeError):
        asyncio.run(session_store.run_with_repair(alice, acc, _op))
    assert len(calls) == 1, "a non-auth error must not trigger a retry"


def test_the_original_error_survives_when_there_is_nothing_to_repair(alice,
                                                                    monkeypatch):
    aid = db.add_account(alice, "09121110000", name="a")   # no blob stored
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)

    async def _op():
        raise RuntimeError("INVALID_AUTH")

    with pytest.raises(RuntimeError, match="INVALID_AUTH"):
        asyncio.run(session_store.run_with_repair(alice, acc, _op))


def test_a_still_failing_operation_does_not_loop(alice, recording, monkeypatch):
    aid = db.add_account(alice, "09121110000", name="a")
    db.set_session_blob(alice, aid, _VALUES)
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)
    calls = []

    async def _op():
        calls.append(1)
        raise RuntimeError("INVALID_AUTH")

    with pytest.raises(RuntimeError):
        asyncio.run(session_store.run_with_repair(alice, acc, _op))
    assert len(calls) == 2, "exactly one retry, then give up"


def test_a_successful_operation_is_run_once(alice, monkeypatch):
    aid = db.add_account(alice, "09121110000", name="a")
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    acc = db.get_account(alice, aid)
    calls = []

    async def _op():
        calls.append(1)
        return "ok"
    assert asyncio.run(session_store.run_with_repair(alice, acc, _op)) == "ok"
    assert len(calls) == 1, "the happy path must not pay for the repair path"


# --------------------------------------------------------------------------- #
# 5. The wiring
# --------------------------------------------------------------------------- #
def test_the_worker_exposes_a_write_only_session_import():
    body = _src("worker_api.py")
    start = body.index('"/session/import"')
    section = body[start:start + 1600]
    assert "import_session" in section
    assert "connect_ready" not in section, \
        "importing must never connect; that is what causes AUTH_FROM_ANOTHER"


def test_the_channel_flow_repairs_the_session_on_auth_failure():
    body = _src("rubika_panel.py")
    start = body.index("async def _channel_flow")
    section = body[start:start + 3500]
    assert "run_with_repair" in section, \
        "an INVALID_AUTH here is usually a misplaced session, not a dead account"


def _function_source(filename: str, header: str) -> str:
    """One whole function, sliced to where it actually ends.

    Both checks below used a fixed byte window — body[start:start + 1800] — and
    that is a trap this repo has already been bitten by: the window silently stops
    covering the function the moment the code grows, and then reports a fix that
    is plainly present as missing. It happened again when _step_token gained its
    verification step. Slice to the next top-level definition instead.
    """
    body = _src(filename)
    start = body.index(header)
    rest = body[start + len(header):]
    end = len(rest)
    for marker in ("\nasync def ", "\ndef ", "\nclass "):
        at = rest.find(marker)
        if at != -1:
            end = min(end, at)
    return header + rest[:end]


def test_collect_targets_repairs_the_session_too():
    section = _function_source("rubika_panel.py", "async def _collect_targets")
    assert "run_with_repair" in section, \
        "zero contacts is the same missing session, one symptom later"


def test_token_login_actually_writes_the_session_file():
    section = _function_source("rubika_panel.py", "async def _step_token")
    assert "session_store.place" in section, \
        "storing the values in the database is not a login"


def test_token_login_verifies_before_creating_the_account():
    """A token that cannot work must never become an account row."""
    section = _function_source("rubika_panel.py", "async def _step_token")
    verify_at = section.index("_verify_session_token")
    add_at = section.index("db.add_account")
    assert verify_at < add_at, \
        ("the old order created the account, wrote the blob and reported "
         "'Status: SUCCESS' without ever connecting — so a dead token produced a "
         "healthy-looking account that failed on its first campaign")
