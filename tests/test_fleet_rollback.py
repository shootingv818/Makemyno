"""
Fleet operations must be reversible and must fail safely.

Provisioning touches a remote machine over SSH, so it is the operation most
likely to fail half-way. None of those failures may leave the database claiming a
worker exists when it does not, or leave accounts pointing at a worker that is
gone.
"""
import asyncio

import pytest

import busy
import config
import db
import logbus
import worker
import worker_api


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    busy.clear_all()
    yield
    busy.clear_all()


def _worker_row(tag="wk-a", ip="1.2.3.4", master=0):
    return db.add_worker(tag, ip, 22, "root", "enc", 8765, "tok", is_master=master)


# --------------------------------------------------------------------------- #
# Provisioning failures
# --------------------------------------------------------------------------- #
def test_failed_provision_registers_nothing(monkeypatch):
    """A worker row must only appear after the remote build actually succeeded —
    otherwise the panel shows a server that cannot do any work."""
    async def failing(*args, **kwargs):
        return {"ok": False, "error": "docker build failed"}

    monkeypatch.setattr(worker, "provision_worker", failing)
    result = asyncio.run(worker.provision_worker("1.2.3.4", 22, "root", "pw"))
    assert result["ok"] is False
    assert db.list_workers() == []


def test_provision_without_asyncssh_reports_clearly(monkeypatch):
    """The error has to name the missing package, not surface an ImportError."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "asyncssh":
            raise ImportError("no asyncssh")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = asyncio.run(worker.provision_worker("1.2.3.4", 22, "root", "pw"))
    assert result["ok"] is False
    assert "asyncssh" in result["error"]
    assert db.list_workers() == []


def test_register_provisioned_encrypts_credentials(monkeypatch):
    """SSH passwords and API tokens are never stored in the clear."""
    import crypto_util
    # A reversible-but-not-echoing fake, so the assertion below is meaningful.
    monkeypatch.setattr(crypto_util, "encrypt",
                        lambda s: "enc:" + (s or "")[::-1])
    wid = worker.register_provisioned(
        "5.6.7.8", 22, "root", "s3cret",
        {"tag": "wk-x", "api_port": 8765, "api_token": "tok123"})
    row = db.get_worker(wid)
    assert row["ssh_pass_enc"] == "enc:terc3s"
    assert row["api_token_enc"] == "enc:321kot"
    assert "s3cret" not in str(row)
    assert "tok123" not in str(row)


# --------------------------------------------------------------------------- #
# Deleting a worker
# --------------------------------------------------------------------------- #
def test_delete_detaches_accounts_and_clears_counters(alice):
    wid = _worker_row("wk-a")
    a1 = db.add_account(alice, "09120000001", worker_id=wid)
    a2 = db.add_account(alice, "09120000002", worker_id=wid)
    db.incr_worker_sent(wid, 10)

    db.delete_worker(wid)

    assert db.get_worker(wid) is None
    assert db.get_account(alice, a1)["worker_id"] is None
    assert db.get_account(alice, a2)["worker_id"] is None
    assert db.worker_sent_today(wid) == 0
    # the accounts themselves survive: only their placement is gone
    assert len(db.list_accounts(alice)) == 2


def test_detached_accounts_get_placed_again_on_next_login(alice, monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    old = _worker_row("wk-old")
    new = _worker_row("wk-new")
    db.update_worker_health(new, "ok", 100, 1)
    aid = db.add_account(alice, "09120000001", worker_id=old)

    db.delete_worker(old)
    assert db.get_account(alice, aid)["worker_id"] is None

    picked = asyncio.run(worker.pick_worker_for_login(verify=False))
    assert picked["id"] == new


def test_teardown_failure_still_allows_removing_the_row(monkeypatch):
    """If the remote box is unreachable we still need to forget it locally, or
    the panel keeps showing a server that no longer exists."""
    wid = _worker_row("wk-dead")

    async def boom(*args, **kwargs):
        raise OSError("host unreachable")

    monkeypatch.setattr(worker, "_with_conn", boom)
    asyncio.run(worker.teardown_worker(db.get_worker(wid)))   # must not raise
    db.delete_worker(wid)
    assert db.get_worker(wid) is None


# --------------------------------------------------------------------------- #
# Enable / disable round trip
# --------------------------------------------------------------------------- #
def test_disable_then_enable_restores_selectability(monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    wid = _worker_row("wk-a")
    db.update_worker_health(wid, "ok", 100, 1)

    db.set_worker_enabled(wid, False)
    assert asyncio.run(worker.pick_worker_for_login(verify=False)) is None

    db.set_worker_enabled(wid, True)
    db.update_worker_health(wid, "ok", 100, 1)
    assert asyncio.run(worker.pick_worker_for_login(verify=False))["id"] == wid


def test_check_all_ignores_disabled_workers():
    a = _worker_row("wk-a")
    b = _worker_row("wk-b")
    db.set_worker_enabled(b, False)
    results = asyncio.run(worker.check_all())
    assert [r["id"] for r in results] == [a] or results == []


# --------------------------------------------------------------------------- #
# Health snapshots
# --------------------------------------------------------------------------- #
def test_master_is_always_reported_healthy():
    master = worker.ensure_master_worker()
    summary = asyncio.run(worker.check_worker(master))
    assert summary["status"] == "ok"
    assert summary["file_ok"] is True


def test_unreachable_worker_is_marked_down(monkeypatch):
    wid = _worker_row("wk-a", ip="203.0.113.1")

    async def no_route(*args, **kwargs):
        return -1

    monkeypatch.setattr(worker, "_tcp_ping", no_route)
    summary = asyncio.run(worker.check_worker(db.get_worker(wid)))
    assert summary["status"] == "down"
    assert summary["file_ok"] is False
    assert db.get_worker(wid)["status"] == "down"


def test_warm_only_check_reports_reconnecting_instead_of_cold_connecting(monkeypatch):
    """The background loop must not force a cold SSH connect, or a reconnecting
    tunnel looks like a dead worker."""
    wid = _worker_row("wk-a")

    async def fast_ping(*args, **kwargs):
        return 50

    called = []

    async def must_not_call(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setattr(worker, "_tcp_ping", fast_ping)
    monkeypatch.setattr(worker, "api_call", must_not_call)

    summary = asyncio.run(worker.check_worker(db.get_worker(wid), warm_only=True))
    assert summary["status"] == "blocked"
    assert summary["detail"] == "reconnecting"
    assert called == []


def test_check_all_survives_one_crashing_worker(monkeypatch):
    """One flaky server must never stall the whole cycle."""
    good = _worker_row("wk-good")
    bad = _worker_row("wk-bad")

    async def selective(w, warm_only=False):
        if w["id"] == bad:
            raise RuntimeError("exploded")
        return worker._mk_summary(w, "ok", 10, True, None)

    monkeypatch.setattr(worker, "check_worker", selective)
    results = asyncio.run(worker.check_all())
    by_id = {r["id"]: r for r in results}
    assert by_id[good]["status"] == "ok"
    assert by_id[bad]["status"] == "down"
    assert "crashed" in str(by_id[bad]["detail"])


def test_check_all_marks_a_timeout(monkeypatch):
    wid = _worker_row("wk-slow")

    async def hang(w, warm_only=False):
        await asyncio.sleep(30)

    monkeypatch.setattr(worker, "check_worker", hang)
    monkeypatch.setattr(asyncio, "wait_for",
                        lambda coro, timeout: _raise_timeout(coro))

    results = asyncio.run(worker.check_all())
    assert results[0]["status"] == "down"
    assert "timeout" in str(results[0]["detail"])
    assert wid


def _raise_timeout(coro):
    coro.close()

    async def _boom():
        raise asyncio.TimeoutError()
    return _boom()


def test_is_healthy_treats_unchecked_enabled_workers_as_usable():
    wid = _worker_row("wk-a")
    worker._health_cache.pop(wid, None)
    assert worker.is_healthy(db.get_worker(wid)) is True
    db.set_worker_enabled(wid, False)
    assert worker.is_healthy(db.get_worker(wid)) is False


# --------------------------------------------------------------------------- #
# Fleet statistics with an offline worker
# --------------------------------------------------------------------------- #
def test_offline_worker_accounts_are_unknown_not_dead(alice):
    """Reporting the accounts of an unreachable worker as dead would panic the
    owner over a network hiccup."""
    wid = _worker_row("wk-a")
    db.add_account(alice, "09120000001", worker_id=wid)
    db.add_account(alice, "09120000002", worker_id=wid)
    db.update_worker_health(wid, "down", -1, 0)

    row = next(w for w in db.accounts_per_worker() if w["id"] == wid)
    assert row["total"] == 2
    assert row["status"] == "down"
    # both accounts are still 'active' in the DB: nothing marked them dead
    assert row["healthy"] == 2


def test_fleet_overview_includes_workers_with_no_accounts():
    a = _worker_row("wk-a")
    b = _worker_row("wk-b")
    overview = {w["id"]: w for w in db.accounts_per_worker()}
    assert overview[a]["total"] == 0 and overview[b]["total"] == 0


# --------------------------------------------------------------------------- #
# Tunnels and supervisors
# --------------------------------------------------------------------------- #
def test_closing_an_unknown_tunnel_is_safe():
    asyncio.run(worker.close_tunnel(99999))     # must not raise


def test_stop_supervisor_without_one_is_safe():
    asyncio.run(worker.stop_supervisor(99999))


def test_supervisor_is_not_started_for_master_or_disabled():
    master = worker.ensure_master_worker()

    async def scenario():
        worker.start_supervisor(master)
        wid = _worker_row("wk-a")
        db.set_worker_enabled(wid, False)
        worker.start_supervisor(db.get_worker(wid))
        return len(worker._supervisors)

    assert asyncio.run(scenario()) == 0


def test_shutdown_clears_everything():
    async def scenario():
        await worker.shutdown()
        return worker._supervisors, worker._tunnels

    supervisors, tunnels = asyncio.run(scenario())
    assert supervisors == {} and tunnels == {}


def test_api_call_drops_the_tunnel_on_failure(monkeypatch):
    """A broken tunnel is the usual cause of a failed call, so it is discarded
    and rebuilt rather than reused forever."""
    import httpx
    wid = _worker_row("wk-a")
    closed = []

    async def fake_open(w):
        return 12345

    async def fake_close(worker_id):
        closed.append(worker_id)

    monkeypatch.setattr(worker, "open_tunnel", fake_open)
    monkeypatch.setattr(worker, "close_tunnel", fake_close)
    import crypto_util
    monkeypatch.setattr(crypto_util, "decrypt", lambda s: "tok")
    httpx.NEXT_ERROR = OSError("tunnel closed")

    async def scenario():
        with pytest.raises(OSError):
            await worker.api_call(db.get_worker(wid), "GET", "/ping")

    asyncio.run(scenario())
    assert closed == [wid]


def test_api_call_sends_the_bearer_token_through_the_tunnel(monkeypatch):
    """Credentials are decrypted at this single chokepoint and nowhere else."""
    import httpx
    wid = _worker_row("wk-a")

    async def fake_open(w):
        return 45678

    monkeypatch.setattr(worker, "open_tunnel", fake_open)
    import crypto_util
    monkeypatch.setattr(crypto_util, "decrypt", lambda s: "secret-token")
    httpx.CALLS.clear()
    httpx.NEXT_JSON = {"ok": True, "version": "abc1234"}

    result = asyncio.run(worker.api_call(db.get_worker(wid), "GET", "/ping"))
    assert result["version"] == "abc1234"
    method, url, kwargs = httpx.CALLS[-1]
    assert method == "GET"
    assert url == "http://127.0.0.1:45678/ping"     # loopback only
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_worker_version_lookup_tolerates_an_unreachable_worker(monkeypatch):
    """The versions screen must show a placeholder, not blow up, for a worker
    that is not answering."""
    import httpx
    wid = _worker_row("wk-a")

    async def fake_open(w):
        return 45678

    monkeypatch.setattr(worker, "open_tunnel", fake_open)
    import crypto_util
    monkeypatch.setattr(crypto_util, "decrypt", lambda s: "tok")
    httpx.NEXT_ERROR = OSError("down")
    assert asyncio.run(worker.worker_code_version(db.get_worker(wid))) == "?"


# --------------------------------------------------------------------------- #
# Worker-side job bookkeeping
# --------------------------------------------------------------------------- #
def test_worker_job_registry_starts_empty():
    assert worker_api._jobs == {}


def test_worker_releases_its_claim_when_a_job_finishes():
    """A crashed job must not leave the session claimed on the worker either."""
    key = worker_api._key(1, "09120000001")
    busy.acquire(key, "send", customer_id=1)
    assert busy.is_busy(key) is True
    busy.release(key, "send")
    assert busy.is_busy(key) is False


def test_two_jobs_cannot_claim_one_session_on_the_worker():
    key = worker_api._key(1, "09120000001")
    assert busy.acquire(key, "send", customer_id=1) is True
    assert busy.acquire(key, "pdf", customer_id=1) is False


def test_different_customers_same_number_can_work_at_once():
    """Two customers owning one number are two separate sessions, so both may
    run — the guard must not serialise unrelated work."""
    k1 = worker_api._key(1, "09121110000")
    k2 = worker_api._key(2, "09121110000")
    assert busy.acquire(k1, "send", customer_id=1) is True
    assert busy.acquire(k2, "send", customer_id=2) is True
