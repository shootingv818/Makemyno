"""
Rollback scenarios for section 5: crashes, restarts, and half-finished work.

An always-on engine plus a periodic sweep is the combination most likely to leave
debris behind — a claimed session nobody released, an engine that never came back
after a restart, a loop still running against a deleted account. These tests kill
things at awkward moments and check what is left.
"""
import asyncio

import pytest

import account_conn
import busy
import config
import db
import health
import logbus
import tabchi
import worker


@pytest.fixture(autouse=True)
def silent(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    monkeypatch.setattr(logbus, "to_group_file", noop)
    monkeypatch.setattr(config, "HEALTH_ACCOUNT_GAP", 0.0)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)
    busy.clear_all()
    health._deaths.clear()
    tabchi._tabchi_tasks.clear()
    tabchi._secretary_tasks.clear()
    yield
    busy.clear_all()
    health._deaths.clear()
    tabchi._tabchi_tasks.clear()
    tabchi._secretary_tasks.clear()


def _account(cid, phone="09120000001"):
    return db.add_account(cid, phone, name="acc")


def _local_only(monkeypatch):
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)


# --------------------------------------------------------------------------- #
# A crash inside a pass must not leave the session claimed
# --------------------------------------------------------------------------- #
def test_a_crash_mid_pass_releases_the_session(alice, monkeypatch):
    """A leaked claim is worse than a crash: the account looks permanently busy,
    every later job skips it, and the customer sees an account that silently
    stopped working with no error anywhere."""
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_group_joined(alice, db.tabchi_groups(alice, aid)[0]["id"], "g0")
    acc = db.get_account(alice, aid)
    _local_only(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("network died mid-send")
    monkeypatch.setattr(account_conn, "call", _boom)

    result = asyncio.run(tabchi._tabchi_pass(alice, acc))
    key = tabchi._key(alice, acc["phone"])
    assert not busy.is_busy(key), "the session was left claimed after a crash"
    assert result["failed"] >= 1


def test_a_crash_mid_secretary_pass_releases_the_session(alice, monkeypatch):
    aid = _account(alice)
    db.secretary_set(alice, aid, mode="text", text="hi")
    acc = db.get_account(alice, aid)
    _local_only(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("gone")
    monkeypatch.setattr(account_conn, "call", _boom)

    with pytest.raises(RuntimeError):
        asyncio.run(tabchi._secretary_pass(alice, acc))
    assert not busy.is_busy(tabchi._key(alice, acc["phone"]))


def test_a_crash_in_the_health_check_releases_the_session(alice, monkeypatch):
    _local_only(monkeypatch)
    aid = _account(alice)
    acc = db.get_account(alice, aid)

    async def _verify(customer_id, phone):
        raise RuntimeError("boom")
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    asyncio.run(health.check_account(acc, set()))
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    assert not busy.is_busy(key), (
        "the health engine must never leave a claim behind — it would make the "
        "account invisible to every subsequent job")


def test_a_dying_pass_on_invalid_auth_still_releases(alice, monkeypatch):
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_group_joined(alice, db.tabchi_groups(alice, aid)[0]["id"], "g0")
    acc = db.get_account(alice, aid)
    _local_only(monkeypatch)

    async def _dead(*args, **kwargs):
        raise account_conn.InvalidAuthError("session revoked")
    monkeypatch.setattr(account_conn, "call", _dead)

    result = asyncio.run(tabchi._tabchi_pass(alice, acc))
    assert result["reason"] == "auth_failed"
    assert not busy.is_busy(tabchi._key(alice, acc["phone"]))


# --------------------------------------------------------------------------- #
# Stopping an engine
# --------------------------------------------------------------------------- #
def test_stopping_a_loop_that_was_never_started_is_harmless():
    asyncio.run(tabchi.stop_tabchi(999))
    asyncio.run(tabchi.stop_secretary(999))


def test_stop_all_clears_the_registry(alice, monkeypatch):
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)
    db.secretary_set(alice, aid, enabled=True, text="hi")

    async def _go():
        tabchi.start_tabchi(alice, aid)
        tabchi.start_secretary(alice, aid)
        await asyncio.sleep(0)
        await tabchi.stop_all()
        return tabchi.running()
    live = asyncio.run(_go())
    assert live["tabchi"] == [] and live["secretary"] == []


def test_starting_twice_does_not_create_two_loops(alice):
    """Two loops on one account means two connections on one session, which is
    the failure mode this whole design exists to prevent."""
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)

    async def _go():
        tabchi.start_tabchi(alice, aid)
        first = tabchi._tabchi_tasks[aid]["task"]
        tabchi.start_tabchi(alice, aid)
        second = tabchi._tabchi_tasks[aid]["task"]
        same = first is second
        await tabchi.stop_all()
        return same
    assert asyncio.run(_go()) is True


def test_a_loop_exits_when_the_feature_is_switched_off(alice):
    """The flag in the database is the authority, so switching off from any
    process (or a restart) stops the loop rather than orphaning it."""
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=False)

    async def _go():
        tabchi.start_tabchi(alice, aid)
        await asyncio.sleep(0.05)
        return tabchi._tabchi_tasks[aid]["task"].done()
    assert asyncio.run(_go()) is True


def test_a_loop_exits_and_disables_itself_when_the_account_dies(alice):
    """A quarantined account cannot connect, so looping on it forever would just
    fill the log with the same error every interval."""
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)
    db.set_status(alice, aid, "quarantined")

    async def _go():
        tabchi.start_tabchi(alice, aid)
        await asyncio.sleep(0.05)
        return tabchi._tabchi_tasks[aid]["task"].done()
    assert asyncio.run(_go()) is True
    assert not db.tabchi_get(alice, aid)["enabled"]


def test_a_loop_exits_when_the_account_is_deleted(alice):
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)
    db.delete_account(alice, aid)

    async def _go():
        tabchi.start_tabchi(alice, aid)
        await asyncio.sleep(0.05)
        return tabchi._tabchi_tasks[aid]["task"].done()
    assert asyncio.run(_go()) is True


# --------------------------------------------------------------------------- #
# The kill switch
# --------------------------------------------------------------------------- #
def test_the_kill_switch_stops_tabchi_from_sending(alice, monkeypatch):
    """The owner's freeze must reach the background engines too. A kill switch
    that only stops the buttons is not a kill switch."""
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_group_joined(alice, db.tabchi_groups(alice, aid)[0]["id"], "g0")
    db.tabchi_set(alice, aid, enabled=True)
    db.set_sends_frozen(True)
    _local_only(monkeypatch)

    passes = []

    async def _pass(cid, acc):
        passes.append(1)
        return {"sent": 0, "failed": 0, "muted": 0, "reason": ""}
    monkeypatch.setattr(tabchi, "_tabchi_pass", _pass)

    async def _go():
        tabchi.start_tabchi(alice, aid)
        await asyncio.sleep(0.05)
        await tabchi.stop_all()
    asyncio.run(_go())
    assert passes == [], "tabchi sent while the service was frozen"


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
def test_recovery_restores_exactly_what_was_enabled(alice, bob, monkeypatch):
    on_a = _account(alice, "09120000001")
    off_a = _account(alice, "09120000002")
    on_b = _account(bob, "09120000003")
    db.tabchi_set(alice, on_a, enabled=True)
    db.tabchi_set(alice, off_a, enabled=False)
    db.tabchi_set(bob, on_b, enabled=True)

    started = []
    monkeypatch.setattr(tabchi, "start_tabchi",
                        lambda cid, aid: started.append((cid, aid)))
    monkeypatch.setattr(tabchi, "start_secretary", lambda cid, aid: None)
    asyncio.run(tabchi.restore_engines())
    assert sorted(started) == sorted([(alice, on_a), (bob, on_b)])


def test_recovery_skips_engines_whose_account_died(alice, monkeypatch):
    """Otherwise every restart relaunches loops that immediately fail, and the
    log fills with errors nobody can act on."""
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)
    db.set_status(alice, aid, "quarantined")

    started = []
    monkeypatch.setattr(tabchi, "start_tabchi",
                        lambda cid, aid_: started.append(aid_))
    monkeypatch.setattr(tabchi, "start_secretary", lambda cid, aid_: None)
    asyncio.run(tabchi.restore_engines())
    assert started == []


def test_recovery_is_idempotent(alice, monkeypatch):
    """It runs at every boot, and a supervisor that restarts the process twice in
    a row must not end up with two loops per account."""
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)

    async def _go():
        await tabchi.restore_engines()
        await tabchi.restore_engines()
        count = len(tabchi.running()["tabchi"])
        await tabchi.stop_all()
        return count
    assert asyncio.run(_go()) == 1


def test_a_settings_change_survives_a_restart(alice):
    """Everything the engine needs lives in the database, not in memory, so a
    restart resumes with the same texts, interval and group list."""
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "one")
    db.tabchi_set(alice, aid, enabled=True, interval_sec=2400)
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_group_joined(alice, db.tabchi_groups(alice, aid)[0]["id"], "g0")

    # simulate a restart: nothing but the database survives
    tabchi._tabchi_tasks.clear()

    row = db.tabchi_get(alice, aid)
    assert row["enabled"] and row["interval_sec"] == 2400
    assert [t["text"] for t in db.tabchi_texts(alice, aid)] == ["one"]
    assert len(db.tabchi_groups(alice, aid, joined_only=True)) == 1


# --------------------------------------------------------------------------- #
# Deleting an account cleans up after itself
# --------------------------------------------------------------------------- #
def test_deleting_an_account_removes_its_tabchi_rows(alice):
    """Otherwise the next account to reuse that id inherits a stranger's texts
    and group list."""
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_set(alice, aid, enabled=True)
    db.secretary_set(alice, aid, enabled=True, text="hi")
    db.secretary_mark_replied(alice, aid, "u-1")

    db.delete_account(alice, aid)

    assert db.tabchi_texts(alice, aid) == []
    assert db.tabchi_groups(alice, aid) == []
    assert db.secretary_replied_recent(alice, aid) == []
    assert db.tabchi_enabled_accounts(alice) == []
    assert db.secretary_enabled_accounts(alice) == []


def test_deleting_a_customer_leaves_no_tabchi_debris(alice):
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.secretary_mark_replied(alice, aid, "u-1")
    db.delete_customer(alice)
    assert db.owner_tabchi_enabled() == []
    assert db.owner_secretary_enabled() == []
    assert db.owner_all_accounts(status=None) == []


# --------------------------------------------------------------------------- #
# The health engine and the engines interacting
# --------------------------------------------------------------------------- #
def test_a_running_tabchi_pass_protects_the_account_from_the_sweep(alice,
                                                                  monkeypatch):
    """THE INTEGRATION THAT MATTERS: tabchi holds the session, the sweep runs at
    the same moment, and the account must come out untouched. This is the exact
    overlap that destroyed sessions in the base project."""
    _local_only(monkeypatch)
    aid = _account(alice)
    acc = db.get_account(alice, aid)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_group_joined(alice, db.tabchi_groups(alice, aid)[0]["id"], "g0")

    probed = []

    async def _verify(customer_id, phone):
        probed.append(phone)
        return True                     # would quarantine, if ever reached
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    async def _slow_send(cid, phone, fn, timeout=None):
        await asyncio.sleep(0.05)
        return None
    monkeypatch.setattr(account_conn, "call", _slow_send)

    async def _go():
        pass_task = asyncio.create_task(tabchi._tabchi_pass(alice, acc))
        await asyncio.sleep(0.01)       # let the pass claim the session
        await health.sweep()
        await pass_task

    asyncio.run(_go())
    assert probed == [], "the sweep probed an account that tabchi was using"
    assert db.get_account(alice, aid)["status"] == "active"


def test_the_sweep_does_not_disturb_a_secretary_pass(alice, monkeypatch):
    _local_only(monkeypatch)
    aid = _account(alice)
    db.secretary_set(alice, aid, mode="text", text="hi")
    acc = db.get_account(alice, aid)

    probed = []

    async def _verify(customer_id, phone):
        probed.append(phone)
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    async def _slow(cid, phone, fn, timeout=None):
        await asyncio.sleep(0.05)
        return []
    monkeypatch.setattr(account_conn, "call", _slow)

    async def _go():
        task = asyncio.create_task(tabchi._secretary_pass(alice, acc))
        await asyncio.sleep(0.01)
        await health.sweep()
        await task
    asyncio.run(_go())
    assert probed == []
    assert db.get_account(alice, aid)["status"] == "active"


def test_a_quarantined_account_stops_its_engines_and_they_stay_stopped(alice,
                                                                      monkeypatch):
    """End to end: the sweep confirms a death, switches the engines off, and the
    recovery pass at the next restart does not bring them back."""
    _local_only(monkeypatch)
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)
    db.secretary_set(alice, aid, enabled=True, text="hi")

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)
    asyncio.run(health.sweep())

    assert db.get_account(alice, aid)["status"] == "quarantined"
    started = []
    monkeypatch.setattr(tabchi, "start_tabchi",
                        lambda cid, aid_: started.append(aid_))
    monkeypatch.setattr(tabchi, "start_secretary",
                        lambda cid, aid_: started.append(aid_))
    asyncio.run(tabchi.restore_engines())
    assert started == []


def test_re_login_brings_an_account_and_its_engines_back(alice):
    """The recovery story from the customer's side: re-login flips the status, and
    switching tabchi back on is one tap because nothing was deleted."""
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    db.tabchi_group_joined(alice, db.tabchi_groups(alice, aid)[0]["id"], "g0")
    db.set_status(alice, aid, "quarantined")
    db.tabchi_set(alice, aid, enabled=False)

    db.set_status(alice, aid, "active")          # what re-login does
    db.tabchi_set(alice, aid, enabled=True)

    assert len(db.tabchi_enabled_accounts(alice)) == 1
    assert [t["text"] for t in db.tabchi_texts(alice, aid)] == ["hello"]
    assert len(db.tabchi_groups(alice, aid, joined_only=True)) == 1
