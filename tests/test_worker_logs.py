"""
Reading a worker's own log from the panel.

The report was:

    • State  : 🔴 BLOCKED
    • Ping   : 1ms
    • Route  : No answer
    • Detail : api error: RemoteProtocolError: Server disconnected without
               sending a response.

Every one of those lines is true and none of them says why. The ping proves the
server is up; "disconnected without a response" means the SSH tunnel opened and
nothing was listening on the far side. The reason is always in the container's log,
and reaching it meant opening an SSH session by hand — so the panel could report a
problem it could not explain.
"""
import asyncio
import os

import pytest

import config
import worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _code(name):
    return "\n".join(line.split("#")[0] for line in _src(name).splitlines()
                     if not line.strip().startswith("#"))


# --------------------------------------------------------------------------- #
# The log command gathers what is needed to tell the cases apart
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_ssh(monkeypatch):
    captured = {}

    class _Conn:
        def close(self):
            return None

    async def _connect(*args, **kwargs):
        return _Conn()

    async def _run(conn, command, check=False, timeout=None, label=""):
        captured["command"] = command
        return 0, captured.get("output", "log output here"), ""

    monkeypatch.setattr(worker, "_with_conn", _connect)
    monkeypatch.setattr(worker, "_run", _run)
    return captured


def test_it_reports_whether_the_container_is_even_running(fake_ssh):
    """A crash-looping container and a wedged one look identical from the master."""
    asyncio.run(worker.worker_logs({"id": 1, "tag": "wk-1"}))
    command = fake_ssh["command"]
    assert "docker inspect" in command
    assert ".State.Running" in command
    assert ".RestartCount" in command, "a restart loop must be visible"


def test_it_checks_whether_anything_listens_on_the_api_port(fake_ssh):
    """This is the exact question "disconnected without a response" raises."""
    asyncio.run(worker.worker_logs({"id": 1, "tag": "wk-1"}))
    command = fake_ssh["command"]
    assert str(config.WORKER_API_PORT) in command
    assert "ss -lntp" in command or "netstat" in command


def test_it_falls_back_when_ss_is_missing(fake_ssh):
    """A minimal image may have netstat and not ss, or the reverse."""
    asyncio.run(worker.worker_logs({"id": 1, "tag": "wk-1"}))
    assert "netstat" in fake_ssh["command"], "there must be a fallback"


def test_it_asks_for_the_container_log(fake_ssh):
    asyncio.run(worker.worker_logs({"id": 1, "tag": "wk-1"}, tail=25))
    assert "docker logs --tail 25" in fake_ssh["command"]
    assert "2>&1" in fake_ssh["command"], "a crash writes to stderr"


def test_a_broken_ssh_returns_a_message_not_an_exception():
    """A diagnostic tool that itself explodes is worse than none."""
    async def _boom(*args, **kwargs):
        raise OSError("connection refused")
    original = worker._with_conn
    worker._with_conn = _boom
    try:
        out = asyncio.run(worker.worker_logs({"id": 1, "tag": "wk-1"}))
    finally:
        worker._with_conn = original
    assert "OSError" in out
    assert "connection refused" in out


def test_an_empty_log_says_so(monkeypatch):
    class _Conn:
        def close(self):
            return None

    async def _connect(*args, **kwargs):
        return _Conn()

    async def _run(conn, command, check=False, timeout=None, label=""):
        return 0, "   ", ""
    monkeypatch.setattr(worker, "_with_conn", _connect)
    monkeypatch.setattr(worker, "_run", _run)
    out = asyncio.run(worker.worker_logs({"id": 1, "tag": "wk-1"}))
    assert out.strip(), "an empty result must still say something"


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
def test_a_missing_module_is_recognised():
    """httpx was missing from requirements once already; this is what that looks
    like from the outside."""
    verdict = worker.explain_worker_log(
        "ModuleNotFoundError: No module named 'fastapi'")
    assert "کتابخانه" in verdict
    assert "آپدیت" in verdict, "it must name the action that fixes it"


def test_the_asgi_mismatch_is_recognised():
    """The reference project's requirements carry a comment about exactly this:
    an incompatible fastapi/starlette/uvicorn mix makes a worker start cleanly and
    then fail every request."""
    verdict = worker.explain_worker_log(
        "TypeError: 'NoneType' object is not callable  [asgi2]")
    assert "fastapi" in verdict


def test_a_stopped_container_is_recognised():
    verdict = worker.explain_worker_log(
        "running=false exit=1 restarts=12 err=")
    assert "اجرا نمی‌شود" in verdict


def test_nothing_listening_is_recognised():
    verdict = worker.explain_worker_log("nothing on 8765")
    assert "گوش نمی‌دهد" in verdict


def test_a_port_clash_is_recognised():
    verdict = worker.explain_worker_log(
        "OSError: [Errno 98] Address already in use")
    assert "اشغال" in verdict
    assert "ری‌استارت" in verdict


def test_missing_worker_settings_are_recognised():
    verdict = worker.explain_worker_log("worker settings missing: WORKER_API_TOKEN")
    assert ".env" in verdict


def test_a_bare_traceback_still_gets_a_verdict():
    verdict = worker.explain_worker_log(
        "Traceback (most recent call last):\n  File ...\nValueError: nope")
    assert verdict, "even an unrecognised crash should be labelled a crash"


def test_a_healthy_log_gets_no_verdict():
    """Not everything needs a diagnosis; inventing one would be noise."""
    assert worker.explain_worker_log(
        "running=true exit=0\nWorker API listening on 127.0.0.1:8765") == ""


def test_the_verdict_never_raises_on_junk():
    for blob in (None, "", "   ", "\x00\x01"):
        assert isinstance(worker.explain_worker_log(blob), str)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_the_panel_has_a_log_button_and_a_handler():
    body = _code("owner_bot.py")
    assert 'CallbackQuery(pattern=rb"wklog_(\\d+)")' in body, "a handler is needed"
    # The button must be on the WORKER DETAIL screen, not merely somewhere in the
    # file: mutation testing removed the row and this passed, because the handler
    # builds its own wklog_ button for the refresh action.
    detail = body[body.index("async def worker_detail_cb"):]
    detail = detail[:detail.index("@bot.on", 10)]
    assert "wklog_" in detail, "the worker screen must offer the log button"


def test_the_log_screen_offers_the_two_actions_that_fix_things():
    """Reading a log and then having to navigate back to act on it is friction."""
    body = _code("owner_bot.py")
    start = body.index('async def worker_log_cb')
    section = body[start:start + 2500]
    assert "wkrst_" in section, "restart should be one tap away"
    assert "wkupd_" in section, "so should rebuilding the image"


def test_the_log_output_is_truncated_for_telegram():
    """Telegram caps a message near 4096 characters; a 60-line log plus a card can
    exceed that."""
    body = _code("owner_bot.py")
    start = body.index('async def worker_log_cb')
    section = body[start:start + 2500]
    assert "[-2400:]" in section


def test_the_master_has_no_container_log():
    """The master runs under systemd, not docker, so the button is hidden there —
    and the handler refuses rather than reporting a confusing empty log."""
    body = _code("owner_bot.py")
    start = body.index('async def worker_log_cb')
    section = body[start:start + 1200]
    assert 'is_master' in section
