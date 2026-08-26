"""
The health engine — the tests that exist because the old one killed accounts.

In the base project the engine connected to every account every three hours to
check it. Rubika permits one connection per session, so checking an account that
was mid-send revoked the session; the engine then saw the auth failure it had
just caused and reported the account dead.

The first group of tests below is that bug, pinned shut from several directions.
"""
import asyncio
import time

import pytest

import account_conn
import busy
import cards
import config
import db
import health
import logbus
import worker


@pytest.fixture(autouse=True)
def silent(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    monkeypatch.setattr(logbus, "to_group_file", noop)
    monkeypatch.setattr(config, "HEALTH_ACCOUNT_GAP", 0.0)
    busy.clear_all()
    health._deaths.clear()
    health._last_report.clear()
    health._burst_alerted_at = 0.0
    yield
    busy.clear_all()
    health._deaths.clear()
    health._last_report.clear()


@pytest.fixture
def no_probe(monkeypatch):
    """Fail loudly if the engine ever connects when it should not have."""
    calls = []

    async def _verify(customer_id, phone):
        calls.append((customer_id, phone))
        return False
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)
    return calls


def _account(cid, phone="09120000001"):
    return db.add_account(cid, phone, name="acc")


def _local_only(monkeypatch):
    """Force the master path so no HTTP is involved."""
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)


# =========================================================================== #
# RULE 1 — a busy account is never probed
# =========================================================================== #
def test_a_busy_account_is_reported_alive_without_connecting(alice, no_probe,
                                                             monkeypatch):
    """THE BUG THAT MOTIVATED THIS MODULE. The account is mid-send; the engine
    must not open a second connection, because that is what revokes the session.
    Being busy is itself proof the session works."""
    _local_only(monkeypatch)
    aid = _account(alice)
    acc = db.get_account(alice, aid)
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    assert busy.acquire(key, "send", customer_id=alice, extra={"account_id": aid})
    try:
        verdict = asyncio.run(health.check_account(acc, {aid}))
    finally:
        busy.release(key, "send")

    assert verdict == "busy"
    assert no_probe == [], "the engine connected to an account that was mid-job"
    assert db.get_account(alice, aid)["status"] == "active"


def test_busy_is_detected_from_the_registry_even_without_the_id_hint(alice,
                                                                    no_probe,
                                                                    monkeypatch):
    """Two independent checks: the caller's id set and the registry itself. The
    engine should not depend on the snapshot being perfectly fresh, because a job
    can start between building the snapshot and reaching this account."""
    _local_only(monkeypatch)
    aid = _account(alice)
    acc = db.get_account(alice, aid)
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    busy.acquire(key, "tabchi", customer_id=alice)
    try:
        verdict = asyncio.run(health.check_account(acc, set()))   # empty hint
    finally:
        busy.release(key, "tabchi")
    assert verdict == "busy"
    assert no_probe == []


def test_a_sweep_skips_the_busy_account_and_checks_the_rest(alice, monkeypatch):
    _local_only(monkeypatch)
    busy_id = _account(alice, "09120000001")
    free_id = _account(alice, "09120000002")
    probed = []

    async def _verify(customer_id, phone):
        probed.append(phone)
        return False
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    acc = db.get_account(alice, busy_id)
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    busy.acquire(key, "send", customer_id=alice, extra={"account_id": busy_id})
    try:
        totals = asyncio.run(health.sweep())
    finally:
        busy.release(key, "send")

    assert totals["busy"] == 1
    assert totals["alive"] == 1
    assert probed == [db.get_account(alice, free_id)["phone"]]


def test_a_busy_remote_account_is_not_even_asked_about(alice, monkeypatch):
    """THE GAP THE LOCAL TESTS MISS, AND THE REASON THE GUARD IS NOT REDUNDANT.

    On the master path `busy.hold` is a second line of defence: it cannot acquire
    a held session, so the probe never happens even if the explicit check is
    removed. The REMOTE path has no such backstop — it makes an HTTP call — so
    this check is the only thing standing between a mid-job account and a probe.

    The worker's own registry is not a substitute. A master-side hold can span
    several worker calls (joining twenty groups is twenty requests under one
    hold), and between them the worker's registry is momentarily free while the
    master's is not.
    """
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "w2", "kind": "remote"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)

    calls = []

    async def _api(w, method, path, payload=None, timeout=None):
        calls.append(path)
        return {"dead": True, "skipped": False}      # would quarantine
    monkeypatch.setattr(worker, "api_call", _api)

    aid = _account(alice)
    acc = db.get_account(alice, aid)
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    busy.acquire(key, "join", customer_id=alice, extra={"account_id": aid})
    try:
        verdict = asyncio.run(health.check_account(acc, {aid}))
    finally:
        busy.release(key, "join")

    assert verdict == "busy"
    assert calls == [], "the engine asked a worker about an account that was mid-job"
    assert db.get_account(alice, aid)["status"] == "active"


def test_a_busy_remote_account_is_protected_without_the_id_hint(alice, monkeypatch):
    """Same gap, reached through the registry rather than the caller's snapshot."""
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "w2", "kind": "remote"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)
    calls = []

    async def _api(w, method, path, payload=None, timeout=None):
        calls.append(path)
        return {"dead": True, "skipped": False}
    monkeypatch.setattr(worker, "api_call", _api)

    aid = _account(alice)
    acc = db.get_account(alice, aid)
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    busy.acquire(key, "send", customer_id=alice)
    try:
        assert asyncio.run(health.check_account(acc, set())) == "busy"
    finally:
        busy.release(key, "send")
    assert calls == []


def test_a_remote_busy_answer_is_not_treated_as_death(alice, monkeypatch):
    """The worker answers "busy, therefore alive". The master must believe it."""
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "w2", "kind": "remote"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)

    async def _api(w, method, path, payload=None, timeout=None):
        return {"dead": False, "skipped": True, "reason": "busy"}
    monkeypatch.setattr(worker, "api_call", _api)

    aid = _account(alice)
    acc = db.get_account(alice, aid)
    assert asyncio.run(health.check_account(acc, set())) == "busy"
    assert db.get_account(alice, aid)["status"] == "active"


# =========================================================================== #
# RULE 2 — one error is not proof of death
# =========================================================================== #
def test_an_unreachable_worker_leaves_the_account_alone(alice, monkeypatch):
    """A network problem between master and worker says nothing about the
    session. Quarantining on it would mark a whole server's accounts dead every
    time the tunnel hiccups."""
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "w2", "kind": "remote"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)

    async def _api(*args, **kwargs):
        raise OSError("connection refused")
    monkeypatch.setattr(worker, "api_call", _api)

    aid = _account(alice)
    acc = db.get_account(alice, aid)
    assert asyncio.run(health.check_account(acc, set())) == "unknown"
    assert db.get_account(alice, aid)["status"] == "active"


def test_a_check_that_raises_locally_leaves_the_account_alone(alice, monkeypatch):
    _local_only(monkeypatch)

    async def _verify(customer_id, phone):
        raise asyncio.TimeoutError("timed out")
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    aid = _account(alice)
    acc = db.get_account(alice, aid)
    assert asyncio.run(health.check_account(acc, set())) == "unknown"
    assert db.get_account(alice, aid)["status"] == "active"


def test_a_worker_reporting_check_failed_is_unknown_not_dead(alice, monkeypatch):
    monkeypatch.setattr(worker, "worker_for_account",
                        lambda acc: {"id": 2, "tag": "w2", "kind": "remote"})
    monkeypatch.setattr(worker, "is_local", lambda w: False)

    async def _api(*args, **kwargs):
        return {"dead": False, "skipped": True, "reason": "check failed: OSError"}
    monkeypatch.setattr(worker, "api_call", _api)

    aid = _account(alice)
    acc = db.get_account(alice, aid)
    assert asyncio.run(health.check_account(acc, set())) == "unknown"
    assert db.get_account(alice, aid)["status"] == "active"


def test_a_sweep_with_every_check_failing_changes_nothing(alice, monkeypatch):
    """The safety property in aggregate: a bad afternoon on the network must not
    quarantine the fleet."""
    _local_only(monkeypatch)
    for i in range(5):
        _account(alice, f"0912000000{i}")

    async def _verify(customer_id, phone):
        raise OSError("down")
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    totals = asyncio.run(health.sweep())
    assert totals["unknown"] == 5
    assert totals["dead"] == 0
    assert all(a["status"] == "active" for a in db.list_accounts(alice))


# =========================================================================== #
# RULE 3 — a confirmed death is quarantined, not deleted
# =========================================================================== #
def test_a_confirmed_dead_account_is_quarantined_and_keeps_its_data(alice,
                                                                   monkeypatch):
    """Quarantine, never delete: the customer paid for those contacts and that
    send history, and none of it should vanish over a status flag."""
    _local_only(monkeypatch)
    aid = _account(alice)
    db.set_account_contacts(alice, aid, 4321)
    db.incr_account_sent(alice, aid, 99)

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    acc = db.get_account(alice, aid)
    assert asyncio.run(health.check_account(acc, set())) == "dead"

    after = db.get_account(alice, aid)
    assert after is not None, "the row must survive"
    assert after["status"] == "quarantined"
    assert after["contacts"] == 4321
    assert after["sent_total"] == 99


def test_a_dead_account_has_its_engines_switched_off(alice, monkeypatch):
    """Otherwise tabchi keeps looping on a session that cannot connect, retrying
    forever and filling the log with the same error."""
    _local_only(monkeypatch)
    aid = _account(alice)
    db.tabchi_set(alice, aid, enabled=True)
    db.secretary_set(alice, aid, enabled=True, text="hi")

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    asyncio.run(health.check_account(db.get_account(alice, aid), set()))
    assert not db.tabchi_get(alice, aid)["enabled"]
    assert not db.secretary_get(alice, aid)["enabled"]


def test_autodisable_can_be_turned_off(alice, monkeypatch):
    _local_only(monkeypatch)
    monkeypatch.setattr(config, "HEALTH_ENGINE_AUTODISABLE_DEAD", False)
    aid = _account(alice)

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    assert asyncio.run(health.check_account(db.get_account(alice, aid),
                                            set())) == "dead"
    assert db.get_account(alice, aid)["status"] == "active"


def test_the_customer_is_notified_about_their_dead_account(alice, monkeypatch):
    """The base project told only the owner, so from the customer's side the
    account silently stopped working and the service looked broken."""
    _local_only(monkeypatch)
    aid = _account(alice)

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    told = []

    async def _notify(cid, phone):
        told.append((cid, phone))
    asyncio.run(health.sweep(notify=_notify))
    assert told == [(alice, db.get_account(alice, aid)["phone"])]


def test_a_failing_notifier_does_not_break_the_sweep(alice, monkeypatch):
    """Telling the customer is best-effort: a customer who blocked the bot must
    not stop the rest of the fleet from being checked."""
    _local_only(monkeypatch)
    _account(alice, "09120000001")
    _account(alice, "09120000002")

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    async def _notify(cid, phone):
        raise RuntimeError("blocked the bot")

    totals = asyncio.run(health.sweep(notify=_notify))
    assert totals["dead"] == 2


# =========================================================================== #
# The sweep only looks at accounts it should
# =========================================================================== #
def test_already_quarantined_accounts_are_not_re_checked(alice, monkeypatch):
    """They are already flagged and waiting on the customer to re-login;
    connecting to them again buys nothing and costs a connection."""
    _local_only(monkeypatch)
    alive = _account(alice, "09120000001")
    dead = _account(alice, "09120000002")
    db.set_status(alice, dead, "quarantined")

    probed = []

    async def _verify(customer_id, phone):
        probed.append(phone)
        return False
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    totals = asyncio.run(health.sweep())
    assert totals["checked"] == 1
    assert probed == [db.get_account(alice, alive)["phone"]]


def test_the_sweep_covers_every_customer(alice, bob, monkeypatch):
    """It is a service-wide sweep — the one legitimately unscoped reader."""
    _local_only(monkeypatch)
    _account(alice, "09120000001")
    _account(bob, "09120000002")

    async def _verify(customer_id, phone):
        return False
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)

    totals = asyncio.run(health.sweep())
    assert totals["checked"] == 2


def test_a_sweep_with_no_accounts_is_harmless(monkeypatch):
    _local_only(monkeypatch)
    totals = asyncio.run(health.sweep())
    assert totals["checked"] == 0 and totals["dead"] == 0


def test_owner_all_accounts_is_the_only_unscoped_reader(alice, bob):
    _account(alice, "09120000001")
    _account(bob, "09120000002")
    rows = db.owner_all_accounts(status="active", platform="rb")
    assert {r["customer_id"] for r in rows} == {alice, bob}
    # and it can be asked for everything, regardless of status
    db.set_status(alice, db.list_accounts(alice)[0]["id"], "quarantined")
    assert len(db.owner_all_accounts(status=None)) == 2
    assert len(db.owner_all_accounts(status="active")) == 1


# =========================================================================== #
# Dead-burst alert
# =========================================================================== #
def test_deaths_below_the_threshold_do_not_alert(monkeypatch):
    monkeypatch.setattr(config, "DEAD_BURST_MAX", 5)
    for i in range(4):
        health._note_death(1001, f"0912000000{i}")
    asyncio.run(health._maybe_burst_alert())
    assert health._burst_alerted_at == 0.0


def test_a_burst_of_deaths_alerts_the_owner(monkeypatch):
    """One dead account is a customer's problem. Twenty in an hour is a platform
    event or a bug in this service, and the owner needs to know before the
    support messages arrive."""
    monkeypatch.setattr(config, "DEAD_BURST_MAX", 5)
    sent = []

    async def _event(title, rows, **kwargs):
        sent.append(title)
    monkeypatch.setattr(logbus, "event", _event)

    for i in range(6):
        health._note_death(1001, f"0912000000{i}")
    asyncio.run(health._maybe_burst_alert())
    assert any("dead_burst" in t for t in sent)


def test_the_same_burst_alerts_only_once(monkeypatch):
    """Re-alerting on every pass turns a useful signal into noise the owner
    learns to ignore."""
    monkeypatch.setattr(config, "DEAD_BURST_MAX", 3)
    sent = []

    async def _event(title, rows, **kwargs):
        sent.append(title)
    monkeypatch.setattr(logbus, "event", _event)

    for i in range(5):
        health._note_death(1001, f"0912000000{i}")
    asyncio.run(health._maybe_burst_alert())
    asyncio.run(health._maybe_burst_alert())
    asyncio.run(health._maybe_burst_alert())
    assert len([t for t in sent if "dead_burst" in t]) == 1


def test_old_deaths_fall_out_of_the_window(monkeypatch):
    """The detector measures a rate, not a lifetime total; otherwise a healthy
    service eventually trips it just by existing."""
    monkeypatch.setattr(config, "DEAD_BURST_WINDOW", 60)
    monkeypatch.setattr(config, "DEAD_BURST_MAX", 3)
    health._deaths.append((time.time() - 600, 1001, "old-1"))
    health._deaths.append((time.time() - 600, 1001, "old-2"))
    count = health._note_death(1001, "new-1")
    assert count == 1, "stale deaths should have been dropped"


def test_the_burst_alert_breaks_down_by_customer(monkeypatch):
    """Whether ten deaths are one customer or ten changes the diagnosis
    completely, so the alert has to say which."""
    monkeypatch.setattr(config, "DEAD_BURST_MAX", 4)
    captured = {}

    async def _event(title, rows, **kwargs):
        captured["rows"] = rows
    monkeypatch.setattr(logbus, "event", _event)

    for i in range(4):
        health._note_death(1001, f"a{i}")
    health._note_death(2002, "b0")
    asyncio.run(health._maybe_burst_alert())
    body = "\n".join(str(r) for r in captured["rows"])
    assert "1001" in body and "2002" in body


# =========================================================================== #
# The loop, and the boot-order hazard
# =========================================================================== #
def test_the_engine_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_ENGINE_ENABLED", False)

    async def _go():
        return health.start()
    assert asyncio.run(_go()) is None


def test_the_engine_waits_before_its_first_sweep(monkeypatch):
    """BOOT-ORDER HAZARD: restore_pending() is still re-registering resumed jobs
    in the busy registry. A sweep that beats it there sees a mid-send account as
    idle and probes it — reintroducing the exact bug this module fixes."""
    monkeypatch.setattr(config, "HEALTH_ENGINE_WARMUP", 3600)
    swept = []

    async def _sweep(notify=None):
        swept.append(1)
        return {}
    monkeypatch.setattr(health, "sweep", _sweep)

    async def _go():
        task = health.start()
        await asyncio.sleep(0.05)
        await health.stop()
    asyncio.run(_go())
    assert swept == [], "the engine swept before the warm-up elapsed"


def test_the_loop_sweeps_after_the_warmup_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_ENGINE_WARMUP", 0)
    monkeypatch.setattr(config, "HEALTH_ENGINE_INTERVAL", 3600)
    swept = []

    async def _sweep(notify=None):
        swept.append(1)
        return {}
    monkeypatch.setattr(health, "sweep", _sweep)

    async def _go():
        health.start()
        await asyncio.sleep(0.05)
        await health.stop()
    asyncio.run(_go())
    assert swept == [1]
    assert health._task is None


def test_the_loop_does_not_sweep_while_the_service_is_offline(monkeypatch):
    """If the owner took the service offline (or the anti-spam shield did), the
    engine should be quiet too rather than working a fleet nobody is using."""
    monkeypatch.setattr(config, "HEALTH_ENGINE_WARMUP", 0)
    monkeypatch.setattr(db, "is_bot_online", lambda: False)
    swept = []

    async def _sweep(notify=None):
        swept.append(1)
        return {}
    monkeypatch.setattr(health, "sweep", _sweep)

    async def _go():
        health.start()
        await asyncio.sleep(0.05)
        await health.stop()
    asyncio.run(_go())
    assert swept == []


def test_a_crashing_sweep_does_not_kill_the_loop(monkeypatch):
    """The engine has to survive its own bugs; a dead engine is a fleet nobody is
    watching."""
    monkeypatch.setattr(config, "HEALTH_ENGINE_WARMUP", 0)
    monkeypatch.setattr(config, "HEALTH_ENGINE_INTERVAL", 0)
    calls = []

    async def _sweep(notify=None):
        calls.append(1)
        raise RuntimeError("boom")
    monkeypatch.setattr(health, "sweep", _sweep)

    async def _go():
        health.start()
        await asyncio.sleep(0.05)
        alive = health._task and not health._task.done()
        await health.stop()
        return alive
    assert asyncio.run(_go()) is True
    assert len(calls) > 1, "the loop should have retried after the crash"


def test_start_is_idempotent(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_ENGINE_WARMUP", 3600)

    async def _go():
        first = health.start()
        second = health.start()
        same = first is second
        await health.stop()
        return same
    assert asyncio.run(_go()) is True


# =========================================================================== #
# Reporting
# =========================================================================== #
def test_the_report_card_renders_before_any_sweep():
    text = health.report_card()
    assert isinstance(text, str) and text.strip()


def test_the_report_card_shows_the_skipped_count(alice, monkeypatch):
    """The owner needs "busy, skipped" to be visible and labelled as healthy,
    otherwise a big skip count reads like a failure."""
    _local_only(monkeypatch)
    aid = _account(alice)
    acc = db.get_account(alice, aid)
    key = busy.key_for(acc["phone"], customer_id=alice, platform="rb")
    busy.acquire(key, "send", customer_id=alice, extra={"account_id": aid})
    try:
        asyncio.run(health.sweep())
    finally:
        busy.release(key, "send")

    report = health.last_report()
    assert report["busy"] == 1
    card = health.report_card()
    assert cards.LINE in card
    assert "Busy (skipped)" in card



# =========================================================================== #
# The report has to cross a process boundary
# =========================================================================== #
def test_the_report_is_persisted_for_the_owner_process(alice, monkeypatch):
    """The engine runs in the customer process because the busy registry is in
    memory there. The owner's panel is a DIFFERENT process, so an in-memory
    report would be invisible to the only person who wants to read it."""
    _local_only(monkeypatch)
    aid = _account(alice)

    async def _verify(customer_id, phone):
        return True
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)
    asyncio.run(health.sweep())

    stored = db.get_health_report()
    assert stored["dead"] == 1
    assert stored["checked"] == 1
    assert stored.get("at")


def test_the_card_renders_from_the_database_alone(alice, monkeypatch):
    """Exactly what the owner bot does: it never runs a sweep, it only reads."""
    _local_only(monkeypatch)
    _account(alice)

    async def _verify(customer_id, phone):
        return False
    monkeypatch.setattr(account_conn, "verify_session_dead", _verify)
    asyncio.run(health.sweep())

    health._last_report.clear()          # simulate the other process
    card = health.report_card()
    assert "Checked" in card and "Busy (skipped)" in card


def test_a_corrupt_stored_report_does_not_break_the_screen():
    """A truncated write must not take the owner's panel down."""
    conn = db._conn()
    conn.execute("UPDATE bot_state SET health_report = ? WHERE id = 1",
                 ("{not json",))
    conn.commit()
    conn.close()
    assert db.get_health_report() == {}
    assert isinstance(health.report_card(), str)


def test_the_schema_gains_missing_columns_on_an_existing_database(tmp_path,
                                                                 monkeypatch):
    """CREATE TABLE IF NOT EXISTS skips a table that already exists, so a column
    added in a later build never appears on an older database and the first read
    of it crashes at startup. This is that migration."""
    path = str(tmp_path / "old.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()

    # Roll the table back to the pre-health shape.
    conn = db._conn()
    conn.execute("ALTER TABLE bot_state RENAME TO bot_state_old")
    conn.execute("CREATE TABLE bot_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
                 "online INTEGER DEFAULT 1, offline_by TEXT DEFAULT '', "
                 "offline_at TEXT DEFAULT '', offline_note TEXT DEFAULT '', "
                 "sends_frozen INTEGER DEFAULT 0, frozen_at TEXT DEFAULT '')")
    conn.execute("INSERT INTO bot_state (id, online) VALUES (1, 1)")
    conn.execute("DROP TABLE bot_state_old")
    conn.commit()
    conn.close()

    db.init()                             # the upgrade
    db.set_health_report({"checked": 3, "dead": 0})
    assert db.get_health_report()["checked"] == 3


def test_the_migration_is_safe_to_run_repeatedly(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "again.db"))
    for _ in range(3):
        db.init()
    db.set_health_report({"checked": 1})
    assert db.get_health_report()["checked"] == 1
