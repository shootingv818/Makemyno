"""
Provisioning failures must say what failed and what to check.

A real report read, in full:

    ❌ - #provision_failed
    • Server : 95.182.95.126
    • Error  : TimeoutError:

That is every byte of diagnostic information the owner received. The cause was a
ten-second SSH connect budget — borrowed from the health tunnel, where ten seconds
is right — applied to a server that was still busy from the previous docker build.
Two things were wrong: the budget, and the fact that nothing said so.
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


# --------------------------------------------------------------------------- #
# Two budgets, because the two uses are nothing alike
# --------------------------------------------------------------------------- #
def test_admin_work_gets_a_bigger_budget_than_the_tunnel():
    """A server mid-build takes far longer than ten seconds to answer a login."""
    assert config.SSH_ADMIN_CONNECT_TIMEOUT > config.SSH_CONNECT_TIMEOUT
    assert config.SSH_ADMIN_CONNECT_TIMEOUT >= 30


def test_the_build_step_allows_many_minutes():
    """A docker build on a small VPS legitimately takes a long time; cutting it
    short looks like a broken build rather than a slow one."""
    assert config.SSH_BUILD_TIMEOUT >= 1200
    assert config.SSH_BUILD_TIMEOUT > config.SSH_STEP_TIMEOUT


def test_the_tunnel_budget_stays_tight():
    """The other direction matters too: a dead link must be noticed quickly."""
    assert config.SSH_CONNECT_TIMEOUT <= 15


# --------------------------------------------------------------------------- #
# A timeout says which step, and what to look at
# --------------------------------------------------------------------------- #
def test_a_connect_timeout_names_the_host_and_the_limit(monkeypatch):
    import sys
    import types as pytypes

    async def _never(*args, **kwargs):
        await asyncio.sleep(10)

    fake = pytypes.ModuleType("asyncssh")
    fake.connect = _never
    monkeypatch.setitem(sys.modules, "asyncssh", fake)
    monkeypatch.setattr(config, "SSH_ADMIN_CONNECT_TIMEOUT", 0.05)

    with pytest.raises(worker.SSHStepTimeout) as exc:
        asyncio.run(worker._ssh_connect("1.2.3.4", 22, "root", "pw",
                                        keepalive=False))
    text = str(exc.value)
    assert "1.2.3.4:22" in text, "the report must name the host"
    assert "SSH" in text
    assert "docker ps" in text, "it must say what to check on the server"


def test_a_step_timeout_names_the_step(monkeypatch):
    class _Conn:
        async def run(self, command, check=False):
            await asyncio.sleep(10)

    with pytest.raises(worker.SSHStepTimeout) as exc:
        asyncio.run(worker._run(_Conn(), "sleep 100", timeout=0.05,
                                label="ساخت ایمیج Docker"))
    text = str(exc.value)
    assert "ساخت ایمیج Docker" in text, "the failing step must be named"
    assert "df -h" in text, "it must suggest what to inspect"


def test_a_step_timeout_is_a_timeout_error():
    """So any existing `except TimeoutError` still catches it."""
    assert issubclass(worker.SSHStepTimeout, TimeoutError)


def test_a_command_without_a_timeout_still_works():
    class _Res:
        exit_status = 0
        stdout = "ok"
        stderr = ""

    class _Conn:
        async def run(self, command, check=False):
            return _Res()

    code, out, err = asyncio.run(worker._run(_Conn(), "echo ok"))
    assert (code, out, err) == (0, "ok", "")


# --------------------------------------------------------------------------- #
# An empty exception message never reaches the owner bare
# --------------------------------------------------------------------------- #
@pytest.fixture
def has_asyncssh(monkeypatch):
    """provision_worker checks for asyncssh up front and returns early without it.
    The sandbox has no network to install it, so a stub stands in — these tests are
    about the ERROR REPORTING, not about SSH."""
    import sys
    import types as pytypes
    monkeypatch.setitem(sys.modules, "asyncssh", pytypes.ModuleType("asyncssh"))


def test_a_messageless_exception_gets_a_description(has_asyncssh, monkeypatch):
    """`TimeoutError()` has an empty str(). The old code rendered that as
    "TimeoutError:" and stopped."""
    async def _boom(*args, **kwargs):
        raise TimeoutError()          # deliberately no message
    monkeypatch.setattr(worker, "_ssh_connect", _boom)

    result = asyncio.run(worker.provision_worker("1.2.3.4", 22, "root", "pw"))
    assert result["ok"] is False
    detail = result["error"]
    assert detail.strip() != "TimeoutError:"
    assert len(detail) > 40, f"too terse to act on: {detail!r}"
    assert "uptime" in detail or "docker" in detail


def test_a_labelled_timeout_survives_intact(has_asyncssh, monkeypatch):
    """Its message already explains the step; it must not be flattened to a type
    name."""
    async def _boom(*args, **kwargs):
        raise worker.SSHStepTimeout("مرحله‌ی «ساخت ایمیج Docker» تمام نشد.\nبررسی کن: df -h")
    monkeypatch.setattr(worker, "_ssh_connect", _boom)

    result = asyncio.run(worker.provision_worker("1.2.3.4", 22, "root", "pw"))
    assert "ساخت ایمیج Docker" in result["error"]
    assert "df -h" in result["error"]


def test_a_normal_exception_keeps_its_message(has_asyncssh, monkeypatch):
    async def _boom(*args, **kwargs):
        raise PermissionError("Auth failed for user root")
    monkeypatch.setattr(worker, "_ssh_connect", _boom)

    result = asyncio.run(worker.provision_worker("1.2.3.4", 22, "root", "pw"))
    assert "Auth failed" in result["error"]
    assert "PermissionError" in result["error"]


# --------------------------------------------------------------------------- #
# The owner's card keeps the lines
# --------------------------------------------------------------------------- #
def test_the_failure_card_does_not_collapse_a_multiline_error():
    """cards.kv() puts everything on one truncated line, which is how the
    "what to check" half of the message disappeared."""
    body = _src("owner_bot.py")
    start = body.index('if not result.get("ok"):')
    section = body[start:start + 1400]
    assert "splitlines()" in section, "the guidance lines must survive"
    assert 'cards.kv("Error"' not in section, (
        "a multi-line error must not be squeezed into a kv row")


def test_the_failure_card_offers_a_retry():
    body = _src("owner_bot.py")
    start = body.index('if not result.get("ok"):')
    section = body[start:start + 1400]
    assert b"wk_add" in section.encode(), "there must be a way to try again"


def test_every_long_provisioning_step_is_bounded():
    """One wedged command must not hang provisioning forever."""
    body = _src("worker.py")
    for step in ("نصب Docker", "دریافت سورس", "ساخت ایمیج Docker",
                 "اجرای کانتینر"):
        assert f'label="{step}"' in body, f"{step} has no labelled timeout"
