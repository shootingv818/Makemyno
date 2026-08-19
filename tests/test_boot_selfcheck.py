"""
The boot-time database API check, and the worker build command.

Both come from the same lesson: a failure that only shows up after a customer has
already hit it costs several rounds to diagnose. Checking at startup, and not
breaking a working build to silence a warning, are the two cheap fixes.
"""
import os

import pytest

import customer_bot
import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# The db API check
# --------------------------------------------------------------------------- #
def test_a_healthy_module_passes():
    customer_bot._assert_db_api()          # must not raise


def test_a_missing_function_is_caught_at_boot(monkeypatch):
    """The exact production shape: db.add_account present but set to None, which
    gives "'NoneType' object is not callable" the moment a customer logs in."""
    monkeypatch.setattr(db, "add_account", None)
    with pytest.raises(SystemExit) as exc:
        customer_bot._assert_db_api()
    assert "add_account" in str(exc.value)


def test_the_message_names_every_broken_attribute(monkeypatch):
    monkeypatch.setattr(db, "add_account", None)
    monkeypatch.setattr(db, "get_marker", None)
    with pytest.raises(SystemExit) as exc:
        customer_bot._assert_db_api()
    text = str(exc.value)
    assert "add_account" in text and "get_marker" in text


def test_the_message_tells_you_how_to_fix_it(monkeypatch):
    """Stale bytecode was the cause both times, so the remedy is in the error."""
    monkeypatch.setattr(db, "add_account", None)
    with pytest.raises(SystemExit) as exc:
        customer_bot._assert_db_api()
    text = str(exc.value)
    assert "__pycache__" in text
    assert db.__file__ in text, "it must say which file was loaded"


def test_a_deleted_attribute_is_caught_too(monkeypatch):
    monkeypatch.delattr(db, "pool_lease_block")
    with pytest.raises(SystemExit) as exc:
        customer_bot._assert_db_api()
    assert "pool_lease_block" in str(exc.value)


def test_the_check_runs_before_any_handler_can_be_reached():
    """Order matters: after bot.start() a customer can already press a button."""
    body = _src("customer_bot.py")
    assert body.index("_assert_db_api()") < body.index("await bot.start(")


def test_every_checked_name_actually_exists_today():
    """Guards the guard: a typo in the list would fail every boot forever."""
    customer_bot._assert_db_api()
    for name in ("add_account", "pool_lease_block", "tgm_create_job"):
        assert callable(getattr(db, name, None))


# --------------------------------------------------------------------------- #
# The worker build command
# --------------------------------------------------------------------------- #
def test_the_worker_build_does_not_require_buildx():
    """Forcing DOCKER_BUILDKIT=1 to silence a deprecation banner broke
    provisioning outright: BuildKit needs the buildx plugin, which a plain
    `apt install docker.io` does not ship. The banner was only a warning."""
    # Code only. The comment above the fix explains the mistake by naming it, and
    # matching that sentence would fail the test for describing the bug it guards.
    code = "\n".join(line.split("#")[0] for line in _src("worker.py").splitlines())
    assert "DOCKER_BUILDKIT=1" not in code, (
        "BuildKit needs buildx, which bare servers do not have")
    assert code.count("DOCKER_BUILDKIT=0") >= 2, (
        "both provision and update must pin the legacy builder")


def test_the_build_failure_report_is_wide_enough_for_the_real_cause():
    """A deprecation banner used to push the actual apt error out of the tail."""
    body = _src("worker.py")
    assert "[-1500:]" in body
    assert "out + \"\\n\" + err" in body, "both streams must be reported"
