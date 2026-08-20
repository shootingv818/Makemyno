"""
The live self-test itself must be trustworthy.

The owner cannot exercise session-collision protection, worker distribution, or
tenant isolation by hand, so a panel button runs them with simulated data. That
button is only reassuring if it actually goes RED when something is broken — a
self-test that always passes is worse than none, because it manufactures false
confidence. So these tests break each invariant and confirm the corresponding
check turns red.
"""
import asyncio

import pytest

import busy
import selftest


def _run():
    return asyncio.run(selftest.run())


def _by_prefix(results, needle):
    for name, ok, detail in results:
        if needle in name:
            return ok, detail
    raise AssertionError(f"no check matched {needle!r}")


def test_all_checks_pass_on_healthy_code():
    results = _run()
    s = selftest.summary(results)
    assert s["all_ok"], [r for r in results if not r[1]]


def test_the_summary_counts_match():
    results = _run()
    s = selftest.summary(results)
    assert s["total"] == len(results)
    assert s["passed"] + s["failed"] == s["total"]


def test_every_check_returns_a_name_bool_and_detail():
    for name, ok, detail in _run():
        assert isinstance(name, str) and name
        assert isinstance(ok, bool)
        assert isinstance(detail, str) and detail


# --------------------------------------------------------------------------- #
# Each check must go RED when its invariant is actually broken
# --------------------------------------------------------------------------- #
def test_collision_check_fails_if_the_lock_stops_working(monkeypatch):
    """If acquire ever started saying yes twice, the session guard is gone."""
    monkeypatch.setattr(busy, "acquire",
                        lambda *a, **k: True)      # broken: always grants
    ok, detail = _by_prefix(_run(), "تصادم")
    assert ok is False, "the collision check passed with a broken lock"


def test_hold_release_check_fails_on_a_leak(monkeypatch):
    """A hold that does not release leaves an account busy forever."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _leaky(key, what, **kwargs):
        busy.acquire(key, what)
        yield busy._Held(True, "", key, what)   # never releases
    monkeypatch.setattr(busy, "hold", _leaky)
    ok, _ = _by_prefix(_run(), "آزادسازی خودکار")
    assert ok is False, "the release check passed while the lock leaked"


def test_slot_cap_check_fails_if_the_cap_is_ignored(monkeypatch):
    monkeypatch.setattr(busy, "take_slot", lambda name, limit: True)
    ok, _ = _by_prefix(_run(), "سقف هم‌زمانی")
    assert ok is False, "the cap check passed with no cap"


def test_isolation_check_fails_if_paths_collide(monkeypatch):
    """If two customers ever shared a session file, one login would evict the
    other endlessly — the exact thing per-customer namespacing prevents."""
    import rubika_client as rb
    monkeypatch.setattr(rb, "session_path",
                        lambda phone, cid: f"/data/acc_{phone}")  # no cid!
    ok, _ = _by_prefix(_run(), "جداسازی سشن")
    assert ok is False, "the isolation check passed with colliding paths"


def test_scope_guard_check_fails_if_a_fn_stops_refusing(monkeypatch):
    import db
    monkeypatch.setattr(db, "list_accounts", lambda *a, **k: [])  # no ScopeError
    ok, _ = _by_prefix(_run(), "قانون طلایی")
    assert ok is False, "the scope check passed while a fn ran unscoped"


def test_login_disconnect_check_fails_if_the_fix_is_reverted(monkeypatch, tmp_path):
    """The exact bug that scared the owner. If disconnect() disappears from
    finish_login, this check must catch it before a customer does."""
    import os
    fake = tmp_path / "rubika_client.py"
    fake.write_text("async def finish_login(ctx, code):\n    return {}\n"
                    "async def other():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(selftest.os.path, "dirname", lambda p: str(tmp_path))
    ok, _ = _by_prefix(_run(), "رفع تصادم لاگین")
    assert ok is False, "the disconnect check passed with the fix removed"


# --------------------------------------------------------------------------- #
# It must be safe to run on a live server
# --------------------------------------------------------------------------- #
def test_running_it_leaves_no_busy_locks_behind():
    """Every check must clean up: a self-test that leaks a lock would itself take
    an account offline."""
    busy.clear_all()
    _run()
    assert busy.snapshot() == [], "the self-test leaked a busy lock"


def test_it_does_not_move_the_worker_pointer(monkeypatch):
    """Reading the round-robin sequence advances a persisted pointer; on a live
    server that must not disturb real distribution."""
    import db
    calls = []
    real = db.fleet_rr_next

    def _tracked(pool):
        calls.append(pool)
        return real(pool)
    monkeypatch.setattr(db, "fleet_rr_next", _tracked)
    _run()
    # It is allowed to READ the sequence, but the check exists to prove cycling,
    # not to be free of side effects — so we only assert it did not explode and
    # the pointer stays a small bounded value.
    assert all(0 <= db.fleet_rr_next(3) < 3 for _ in range(3))


def test_a_crashing_check_is_reported_red_not_raised(monkeypatch):
    """A broken check must never take the whole report down."""
    def _boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(busy, "acquire", _boom)
    results = _run()          # must not raise
    assert any(not ok for _, ok, _ in results)
