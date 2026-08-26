"""
When a worker endpoint fails, the master must show the WORKER's reason.

Reported: channel creation on a remote worker failed with only

    HTTPStatusError: Client error '400 Bad Request' for url '.../channel/create'

The worker DID return a useful detail — HTTPException(400, detail="InvalidAuth:
...") — but api_call called resp.raise_for_status(), which throws away the body and
keeps only the status line. So the real cause never reached the log, and the same
"400 Bad Request" stood in for a dead session, a missing marker, or anything else.

Underneath was a second lesson the same report exposed: a worker runs its OWN copy
of the code inside its Docker image, so a master update does not fix it. The
login-disconnect fix was live on the master and absent on the worker, and channel
creation kept getting INVALID_AUTH there. The worker screen now shows the code
version and warns when it is behind.
"""
import asyncio
import os

import pytest

import worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, json=None, headers=None):
        return self._resp


@pytest.fixture
def patched(monkeypatch):
    async def _open_tunnel(w):
        return 40000
    async def _close_tunnel(wid):
        return None
    monkeypatch.setattr(worker, "open_tunnel", _open_tunnel)
    monkeypatch.setattr(worker, "close_tunnel", _close_tunnel)
    monkeypatch.setattr(worker.crypto_util, "decrypt", lambda v: "token")
    return monkeypatch


def _call_with(monkeypatch, resp):
    import sys
    import types as pytypes
    httpx = pytypes.ModuleType("httpx")
    httpx.AsyncClient = lambda **k: _Client(resp)
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    w = {"id": 2, "tag": "wk-0db9", "api_token_enc": "x"}
    return asyncio.run(worker.api_call(w, "POST", "/channel/create",
                                       {"title": "t"}, timeout=10))


def test_a_worker_error_carries_the_workers_own_detail(patched):
    resp = _Resp(400, {"detail": "InvalidAuth: session invalid on worker"})
    with pytest.raises(worker.WorkerAPIError) as exc:
        _call_with(patched, resp)
    text = str(exc.value)
    assert "InvalidAuth" in text, "the worker's real reason must be shown"
    assert "session invalid on worker" in text
    assert "wk-0db9" in text, "and which worker it was"


def test_the_status_code_is_included(patched):
    resp = _Resp(400, {"detail": "marker not found"})
    with pytest.raises(worker.WorkerAPIError) as exc:
        _call_with(patched, resp)
    assert "400" in str(exc.value)
    assert "marker not found" in str(exc.value)


def test_a_non_json_error_body_still_surfaces_text(patched):
    resp = _Resp(500, body=None, text="Internal Server Error: traceback...")
    with pytest.raises(worker.WorkerAPIError) as exc:
        _call_with(patched, resp)
    assert "Internal Server Error" in str(exc.value)


def test_a_successful_call_returns_the_json(patched):
    resp = _Resp(200, {"ok": True, "channel_guid": "c-1"})
    result = _call_with(patched, resp)
    assert result == {"ok": True, "channel_guid": "c-1"}


def test_a_failed_call_drops_the_tunnel(patched, monkeypatch):
    """A broken worker call should reset the tunnel so the next call reopens it."""
    closed = []

    async def _close(wid):
        closed.append(wid)
    monkeypatch.setattr(worker, "close_tunnel", _close)
    resp = _Resp(400, {"detail": "boom"})
    with pytest.raises(worker.WorkerAPIError):
        _call_with(patched, resp)
    assert closed == [2], "the tunnel must be dropped after an error"


# --------------------------------------------------------------------------- #
# The worker runs its own code — the master must surface a version mismatch
# --------------------------------------------------------------------------- #
def test_the_worker_screen_shows_the_code_version_and_warns_when_behind():
    body = _src("owner_bot.py")
    start = body.index("async def worker_detail_cb")
    section = body[start:start + 2500]
    assert "worker_code_version" in section, "the worker's version must be shown"
    assert "master_code_version" in section, "compared against the master"
    assert "عقب" in section, "a behind-version worker must be flagged"


def test_the_version_check_is_skipped_for_the_master():
    """The master has no separate image; comparing it to itself is noise."""
    body = _src("owner_bot.py")
    start = body.index("async def worker_detail_cb")
    section = body[start:start + 2500]
    # The version block must be guarded by a not-master check.
    version_at = section.index("worker_code_version")
    guard = section.rfind("is_master", 0, version_at)
    assert guard != -1, "the version check must be inside a not-master guard"
