"""
Rollback and resilience.

Every state change in this project must be reversible, and every failure must
leave the database usable. These tests drive each switch to its bad state and
then back, kill things half-way through, and feed the parsers garbage.
"""
import asyncio
import json
import os
import sqlite3
import threading
import time

import pytest

import antispam
import busy
import central_db
import config
import db
import logbus


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)


# --------------------------------------------------------------------------- #
# Schema is idempotent — re-running init() must never destroy data
# --------------------------------------------------------------------------- #
def test_init_is_idempotent_and_preserves_data(alice):
    aid = db.add_account(alice, "09120000001", name="keep me")
    db.set_setting(alice, "rb_marker", "KEEP")
    for _ in range(3):
        db.init()
    assert db.get_account(alice, aid)["name"] == "keep me"
    assert db.get_marker(alice) == "KEEP"
    assert db.schema_version() == db.SCHEMA_VERSION


def test_central_init_is_idempotent():
    central_db.audit("x", "y")
    central_db.init()
    central_db.init()
    assert len(central_db.list_audit()) == 1


# --------------------------------------------------------------------------- #
# Every toggle goes there and back
# --------------------------------------------------------------------------- #
def test_block_then_unblock_restores_service(alice):
    db.add_days(alice, 10)
    assert db.is_active(alice) is True
    db.set_blocked(alice, True)
    assert db.is_active(alice) is False
    db.set_blocked(alice, False)
    assert db.is_active(alice) is True


def test_offline_then_online_restores_service():
    assert db.is_bot_online() is True
    asyncio.run(antispam.lower(by="test", note="rollback"))
    assert db.is_bot_online() is False
    asyncio.run(antispam.lift(by="test"))
    assert db.is_bot_online() is True


def test_freeze_then_thaw_restores_sending():
    db.set_sends_frozen(True)
    assert db.are_sends_frozen() is True
    db.set_sends_frozen(False)
    assert db.are_sends_frozen() is False


def test_maintenance_on_off_leaves_no_trace():
    path = central_db.maintenance_flag_path()
    central_db.set_maintenance(True)
    assert os.path.exists(path)
    central_db.set_maintenance(False)
    assert not os.path.exists(path)
    assert central_db.get_maintenance() is False


def test_maintenance_flag_removal_is_safe_when_file_is_already_gone():
    central_db.set_maintenance(True)
    os.remove(central_db.maintenance_flag_path())
    central_db.set_maintenance(False)          # must not raise
    assert central_db.get_maintenance() is False


def test_worker_disable_then_enable_keeps_accounts_attached(alice):
    wid = db.add_worker("wk-a", "1.2.3.4", 22, "root", "p", 8765, "t")
    aid = db.add_account(alice, "09120000001", worker_id=wid)
    db.set_worker_enabled(wid, False)
    assert db.get_account(alice, aid)["worker_id"] == wid   # disabled != detached
    db.set_worker_enabled(wid, True)
    assert db.list_enabled_workers()[0]["id"] == wid


def test_account_quarantine_then_restore(alice):
    aid = db.add_account(alice, "09120000001")
    db.set_status(alice, aid, "quarantined")
    assert db.count_accounts(alice) == {"total": 1, "healthy": 0, "dead": 1}
    db.set_status(alice, aid, "active")
    assert db.count_accounts(alice) == {"total": 1, "healthy": 1, "dead": 0}


def test_tabchi_mute_then_unmute(alice):
    aid = db.add_account(alice, "09120000001")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    gid = db.tabchi_groups(alice, aid)[0]["id"]
    db.tabchi_group_joined(alice, gid, "g-1")
    for _ in range(config.TABCHI_GROUP_MAX_FAILS):
        db.tabchi_group_fail(alice, gid)
    assert db.tabchi_groups(alice, aid)[0]["muted"] == 1
    assert db.tabchi_groups(alice, aid, joined_only=True) == []
    db.tabchi_unmute_all(alice, aid)
    assert len(db.tabchi_groups(alice, aid, joined_only=True)) == 1


def test_group_failure_counter_resets_on_success(alice):
    aid = db.add_account(alice, "09120000001")
    db.tabchi_add_group(alice, aid, "L")
    gid = db.tabchi_groups(alice, aid)[0]["id"]
    db.tabchi_group_fail(alice, gid)
    db.tabchi_group_fail(alice, gid)
    db.tabchi_group_ok(alice, gid)
    assert db.tabchi_groups(alice, aid)[0]["fails"] == 0
    assert db.tabchi_groups(alice, aid)[0]["muted"] == 0


# --------------------------------------------------------------------------- #
# Crashing half-way through
# --------------------------------------------------------------------------- #
def test_a_crash_mid_job_does_not_lock_the_account_forever():
    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        try:
            async with busy.hold(key, "send", customer_id=1):
                raise KeyboardInterrupt("operator pulled the plug")
        except KeyboardInterrupt:
            pass
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


def test_restart_loses_the_registry_and_recovery_restores_it(alice):
    """The exact sequence that used to kill accounts after a restart:
    the job resumes, but nothing re-registers it, so the next health pass
    connects on top of it."""
    aid = db.add_account(alice, "09120000001")
    job = db.cjob_create(alice, aid, "09120000001", "import",
                         {"pairs": [["0912", "n"]]})
    key = busy.key_for("09120000001", customer_id=alice)
    busy.acquire(key, "contacts", customer_id=alice, extra={"account_id": aid})

    busy.clear_all()                      # <- the restart
    assert busy.is_busy(key) is False

    # boot-time recovery: re-adopt every unfinished job
    for row in db.owner_cjobs_running():
        rkey = busy.key_for(row["phone"], customer_id=row["customer_id"])
        busy.adopt(rkey, "contacts", customer_id=row["customer_id"],
                   extra={"account_id": row["account_id"]})

    assert busy.is_busy(key) is True
    assert aid in busy.busy_account_ids(alice)
    assert db.cjob_get(alice, job)["status"] == "running"


def test_finished_jobs_are_not_re_adopted(alice):
    aid = db.add_account(alice, "09120000001")
    job = db.cjob_create(alice, aid, "09120000001", "import", {})
    db.cjob_update(alice, job, status="done")
    assert db.owner_cjobs_running() == []


def test_job_progress_survives_and_can_be_resumed(alice):
    aid = db.add_account(alice, "09120000001")
    job = db.cjob_create(alice, aid, "09120000001", "import",
                         {"pairs": [[str(i), ""] for i in range(100)]})
    db.cjob_update(alice, job, cursor=40, added=38, failed=2)
    row = db.cjob_get(alice, job)
    assert row["cursor"] == 40 and row["added"] == 38 and row["failed"] == 2
    assert len(row["payload"]["pairs"]) == 100
    # resuming continues from the cursor instead of re-messaging the first 40
    remaining = row["payload"]["pairs"][row["cursor"]:]
    assert len(remaining) == 60


def test_stale_busy_entry_is_reclaimed_rather_than_blocking_forever(monkeypatch):
    key = busy.key_for("09120000001", customer_id=1)
    busy.acquire(key, "pdf", customer_id=1)
    busy._held[key]["since"] = time.time() - 99_999
    monkeypatch.setattr(config, "BUSY_STALE_SEC", 10)
    assert busy.is_busy(key) is False


def test_heavy_slot_is_released_even_if_the_job_explodes():
    assert busy.take_slot("pdf", 1) is True
    try:
        raise RuntimeError("decode failed")
    except RuntimeError:
        busy.free_slot("pdf")
    assert busy.take_slot("pdf", 1) is True
    busy.free_slot("pdf")


# --------------------------------------------------------------------------- #
# Garbage in must not crash anything
# --------------------------------------------------------------------------- #
def test_corrupt_paused_payload_yields_an_empty_dict(alice):
    aid = db.add_account(alice, "09120000001")
    db.save_paused_send(alice, aid, "09120000001", {"ok": 1})
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("UPDATE paused_sends SET payload = '{not json' WHERE account_id = ?",
                 (aid,))
    conn.commit()
    conn.close()
    assert db.get_paused_send(alice, aid)["payload"] == {}


def test_corrupt_job_payload_yields_an_empty_dict(alice):
    aid = db.add_account(alice, "09120000001")
    job = db.cjob_create(alice, aid, "p", "import", {"pairs": []})
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("UPDATE contact_jobs SET payload = 'xx' WHERE id = ?", (job,))
    conn.commit()
    conn.close()
    assert db.cjob_get(alice, job)["payload"] == {}
    assert db.owner_cjobs_running()[0]["payload"] == {}


@pytest.mark.parametrize("token", [
    "", "   ", "garbage", "MMSESS:not-base64!!", "YDSESS:", None,
])
def test_bad_session_tokens_return_none(token):
    assert db.session_unpack(token) is None


def test_session_pack_round_trip():
    values = {"auth": "a", "private_key": "k", "guid": "g",
              "phone": "0912", "user_agent": "ua"}
    token = db.session_pack(values)
    assert token.startswith("MMSESS:")
    assert db.session_unpack(token) == values


def test_base_project_session_prefix_is_still_accepted():
    """Sessions exported by the previous project must keep importing."""
    import base64
    values = {"auth": "a", "phone": "0912"}
    raw = base64.urlsafe_b64encode(
        json.dumps(values).encode("utf-8")).decode("ascii")
    assert db.session_unpack("YDSESS:" + raw) == values


def test_session_blob_survives_without_the_crypto_library(alice):
    """crypto_util may be unavailable; storage falls back to the packed token
    rather than losing the session."""
    aid = db.add_account(alice, "09120000001")
    values = {"auth": "a", "private_key": "k", "guid": "g",
              "phone": "09120000001", "user_agent": "ua"}
    db.set_session_blob(alice, aid, values)
    assert db.get_session_blob(alice, aid) == values


def test_empty_session_blob_reads_as_none(alice):
    aid = db.add_account(alice, "09120000001")
    assert db.get_session_blob(alice, aid) is None


def test_set_session_blob_ignores_empty_values(alice):
    aid = db.add_account(alice, "09120000001")
    db.set_session_blob(alice, aid, {})
    assert db.get_session_blob(alice, aid) is None


def test_settings_survive_non_numeric_values(alice):
    db.set_setting(alice, "send_delay", "abc")
    assert db.get_delay(alice) == config.clamp_delay(config.DEFAULT_DELAY)
    db.set_setting(alice, "max_errors", "")
    assert db.get_max_errors(alice) == config.MAX_ERRORS


def test_malformed_expiry_is_treated_as_expired(alice):
    db.set_expiry(alice, "not-a-date")
    assert db.seconds_left(alice) == 0
    assert db.is_active(alice) is False


def test_date_only_expiry_is_parsed(alice):
    db.set_expiry(alice, "2099-01-01")
    assert db.is_active(alice) is True


def test_missing_customer_reads_as_inactive():
    assert db.is_active(999999) is False
    assert db.seconds_left(999999) == 0
    assert db.is_blocked(999999) is False


def test_operations_on_a_missing_account_are_no_ops(alice):
    db.set_status(alice, 424242, "quarantined")
    db.incr_account_sent(alice, 424242, 5)
    db.delete_account(alice, 424242)
    assert db.list_accounts(alice) == []


# --------------------------------------------------------------------------- #
# Two processes on one file (the owner bot and the customer bot)
# --------------------------------------------------------------------------- #
def test_two_connections_can_write_concurrently(alice):
    """WAL + busy_timeout are what stop 'database is locked' when the owner panel
    writes while the customer bot is busy."""
    errors = []

    def writer(tag):
        try:
            for i in range(30):
                db.usage_incr(alice, f"kind-{tag}", 1)
        except Exception as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    for t in range(4):
        assert db.usage_today(alice, f"kind-{t}") == 30


def test_owner_writes_while_customer_reads(alice):
    """The owner grants time; the customer bot sees it without a restart."""
    db.set_expiry(alice, "2020-01-01 00:00:00")
    assert db.is_active(alice) is False
    db.add_days(alice, 30)                     # "owner process"
    assert db.is_active(alice) is True         # "customer process"


def test_counters_are_atomic_under_concurrency(alice):
    aid = db.add_account(alice, "09120000001")
    errors = []

    def bump():
        try:
            for _ in range(25):
                db.incr_account_sent(alice, aid, 1)
        except Exception as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert db.get_account(alice, aid)["sent_total"] == 100


# --------------------------------------------------------------------------- #
# Applying settings to every account (and undoing it)
# --------------------------------------------------------------------------- #
def test_apply_tabchi_to_all_accounts(alice):
    """A customer with twenty accounts should not retype one text twenty times."""
    src = db.add_account(alice, "09120000001")
    others = [db.add_account(alice, f"0912000000{i}") for i in range(2, 6)]
    db.tabchi_add_text(alice, src, "hello")
    db.tabchi_add_text(alice, src, "world")
    db.tabchi_set(alice, src, interval_sec=900)

    copied = db.tabchi_apply_to_all(alice, src)
    assert copied == len(others)
    for aid in others:
        assert [t["text"] for t in db.tabchi_texts(alice, aid)] == ["hello", "world"]
        assert db.tabchi_get(alice, aid)["interval_sec"] == 900


def test_apply_to_all_replaces_rather_than_appends(alice):
    src = db.add_account(alice, "09120000001")
    other = db.add_account(alice, "09120000002")
    db.tabchi_add_text(alice, other, "stale")
    db.tabchi_add_text(alice, src, "fresh")
    db.tabchi_apply_to_all(alice, src)
    assert [t["text"] for t in db.tabchi_texts(alice, other)] == ["fresh"]


def test_apply_to_all_never_crosses_to_another_customer(alice, bob):
    src = db.add_account(alice, "09120000001")
    victim = db.add_account(bob, "09130000001")
    db.tabchi_add_text(alice, src, "alice text")
    db.tabchi_apply_to_all(alice, src)
    assert db.tabchi_texts(bob, victim) == []


def test_apply_to_all_does_not_copy_group_links(alice):
    """Links belong to the account that is actually a member of those groups."""
    src = db.add_account(alice, "09120000001")
    other = db.add_account(alice, "09120000002")
    db.tabchi_add_group(alice, src, "https://rubika.ir/joing/AAA")
    db.tabchi_apply_to_all(alice, src)
    assert db.tabchi_groups(alice, other) == []


def test_apply_secretary_to_all(alice):
    src = db.add_account(alice, "09120000001")
    other = db.add_account(alice, "09120000002")
    db.secretary_set(alice, src, mode="marker", text="hi there", interval_sec=300)
    assert db.secretary_apply_to_all(alice, src) == 1
    got = db.secretary_get(alice, other)
    assert got["mode"] == "marker" and got["text"] == "hi there"
    assert got["interval_sec"] == 300


def test_apply_to_all_with_a_single_account_is_a_no_op(alice):
    src = db.add_account(alice, "09120000001")
    assert db.tabchi_apply_to_all(alice, src) == 0


# --------------------------------------------------------------------------- #
# Secretary reply ledger
# --------------------------------------------------------------------------- #
def test_secretary_never_answers_the_same_person_twice(alice):
    aid = db.add_account(alice, "09120000001")
    assert db.secretary_was_replied(alice, aid, "u-1") is False
    db.secretary_mark_replied(alice, aid, "u-1")
    assert db.secretary_was_replied(alice, aid, "u-1") is True
    db.secretary_mark_replied(alice, aid, "u-1")      # idempotent
    assert db.secretary_was_replied(alice, aid, "u-1") is True


def test_secretary_ledger_is_scoped(alice, bob):
    a = db.add_account(alice, "09120000001")
    b = db.add_account(bob, "09130000001")
    db.secretary_mark_replied(alice, a, "shared-user")
    assert db.secretary_was_replied(bob, b, "shared-user") is False


# --------------------------------------------------------------------------- #
# Interval clamps hold at both extremes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expect_min", [
    (-5, True), (0, True), (1, True), (99999999, False),
])
def test_tabchi_interval_is_clamped(alice, value, expect_min):
    aid = db.add_account(alice, "09120000001")
    db.tabchi_set(alice, aid, interval_sec=value)
    got = db.tabchi_get(alice, aid)["interval_sec"]
    assert got == (config.TABCHI_MIN_INTERVAL if expect_min
                   else config.TABCHI_MAX_INTERVAL)


def test_secretary_interval_is_clamped(alice):
    aid = db.add_account(alice, "09120000001")
    db.secretary_set(alice, aid, interval_sec=1)
    assert db.secretary_get(alice, aid)["interval_sec"] == \
        config.SECRETARY_MIN_INTERVAL
    db.secretary_set(alice, aid, interval_sec=10 ** 9)
    assert db.secretary_get(alice, aid)["interval_sec"] == \
        config.SECRETARY_MAX_INTERVAL


def test_unknown_fields_are_ignored_by_setters(alice):
    aid = db.add_account(alice, "09120000001")
    db.tabchi_set(alice, aid, enabled=True, nonsense="x", sent_total=999)
    row = db.tabchi_get(alice, aid)
    assert row["enabled"] == 1
    assert row["sent_total"] == 0          # not writable through tabchi_set


def test_tabchi_get_creates_the_row_on_demand(alice):
    aid = db.add_account(alice, "09120000001")
    row = db.tabchi_get(alice, aid)
    assert row["enabled"] == 0
    assert row["interval_sec"] == config.TABCHI_DEFAULT_INTERVAL


def test_enabled_engines_exclude_dead_accounts(alice):
    aid = db.add_account(alice, "09120000001")
    db.tabchi_set(alice, aid, enabled=True)
    assert len(db.tabchi_enabled_accounts(alice)) == 1
    db.set_status(alice, aid, "quarantined")
    assert db.tabchi_enabled_accounts(alice) == []
    assert db.owner_tabchi_enabled() == []


def test_duplicate_group_link_is_not_added_twice(alice):
    aid = db.add_account(alice, "09120000001")
    assert db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA") is True
    assert db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA") is False
    assert len(db.tabchi_groups(alice, aid)) == 1


def test_same_link_for_two_accounts_is_allowed(alice):
    a1 = db.add_account(alice, "09120000001")
    a2 = db.add_account(alice, "09120000002")
    assert db.tabchi_add_group(alice, a1, "L") is True
    assert db.tabchi_add_group(alice, a2, "L") is True
