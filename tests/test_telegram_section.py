"""
The Telegram section: the multi-account job engine, error classification, content
handling and the client scoping.

Most of these guard a specific failure mode: duplicate messages, a healthy
account abandoned over a FloodWait, or two customers sharing one warm client.
"""
import asyncio

import pytest

import busy
import config
import db
import logbus
import telegram_client as tgc
import telegram_multi_send as multi
import tg_panel


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    busy.clear_all()
    multi._tasks.clear()
    multi._controls.clear()
    tg_panel._jobs.clear()
    tg_panel._state.clear()
    yield
    busy.clear_all()
    multi._tasks.clear()
    multi._controls.clear()


class _User:
    def __init__(self, uid, mutual=False):
        self.id = uid
        self.mutual_contact = mutual
        self.phone = f"098100000{uid:02d}"


# --------------------------------------------------------------------------- #
# Client scoping
# --------------------------------------------------------------------------- #
def test_telegram_client_key_separates_customers():
    """Sharing one warm client between customers would hand B the socket A is
    using, and Telegram revokes the session of whoever connected first."""
    assert tgc._key(1, "+989121110000") != tgc._key(2, "+989121110000")
    assert tgc._key(1, "+98 912 111 0000") == tgc._key(1, "989121110000")


def test_telegram_client_requires_a_customer_id():
    for bad in (None, 0, "", "abc"):
        with pytest.raises(ValueError):
            tgc._key(bad, "0912")


def test_telegram_client_lock_is_per_session():
    a = tgc._lock(1, "0912")
    b = tgc._lock(2, "0912")
    assert a is not b
    assert tgc._lock(1, "0912") is a


def test_panel_key_matches_the_registry(alice):
    assert tg_panel._key(alice, "0912") == \
        busy.key_for("0912", customer_id=alice, platform="tg")


def test_telegram_and_rubika_keys_never_collide(alice):
    """The same number on both platforms is two independent sessions."""
    import rubika_panel
    assert tg_panel._key(alice, "09120000001") != \
        rubika_panel._key(alice, "09120000001")


# --------------------------------------------------------------------------- #
# Error classification — the difference between skipping one contact and
# abandoning a healthy account
# --------------------------------------------------------------------------- #
class _Flood(Exception):
    def __init__(self, seconds):
        super().__init__(f"FloodWait {seconds}")
        self.seconds = seconds


_Flood.__name__ = "FloodWaitError"


def test_floodwait_is_recognised_and_capped(monkeypatch):
    monkeypatch.setattr(config, "TG_FLOOD_MAX_WAIT", 300)
    assert multi._is_flood(_Flood(30)) == 31
    assert multi._is_flood(_Flood(100000)) == 300
    assert multi._is_flood(RuntimeError("nope")) == 0


def test_floodwait_is_not_a_failure():
    """Counting a slow-down request as an error is how the old code eventually
    gave up on perfectly healthy accounts."""
    exc = _Flood(10)
    assert multi._is_flood(exc) > 0
    assert multi._is_fatal_account_error(exc) is False
    assert multi._is_permanent_recipient_error(exc) is False


@pytest.mark.parametrize("message", [
    "AUTH_KEY_UNREGISTERED", "USER_DEACTIVATED_BAN", "SESSION_REVOKED",
])
def test_fatal_account_errors_stop_that_account(message):
    assert multi._is_fatal_account_error(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "USER_PRIVACY_RESTRICTED", "USER_IS_BLOCKED", "PEER_ID_INVALID",
    "USER_IS_BOT",
])
def test_permanent_recipient_errors_skip_only_that_person(message):
    assert multi._is_permanent_recipient_error(Exception(message)) is True
    assert multi._is_fatal_account_error(Exception(message)) is False


def test_a_transient_error_is_neither():
    exc = TimeoutError("read timeout")
    assert multi._is_flood(exc) == 0
    assert multi._is_fatal_account_error(exc) is False
    assert multi._is_permanent_recipient_error(exc) is False


# --------------------------------------------------------------------------- #
# Job persistence
# --------------------------------------------------------------------------- #
def test_job_stores_content_and_scoping(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.3,
                               "contacts")
    job = db.tgm_get_job(alice, job_id)
    assert job["customer_id"] == alice
    assert job["content"] == [{"kind": "text", "text": "hi"}]
    assert job["delay"] == 0.3
    assert job["target_mode"] == "contacts"
    assert job["state"] == "queued"


def test_a_job_is_invisible_to_another_customer(alice, bob):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2)
    assert db.tgm_get_job(bob, job_id) is None
    assert db.tgm_list_jobs(bob) == []


def test_recipients_are_queued_per_account(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    a1 = db.tg_add_account(alice, "09120000001")
    a2 = db.tg_add_account(alice, "09120000002")
    idx = db.tgm_add_recipients(alice, job_id, a1, [
        ("u1", {"id": 1}, True), ("u2", {"id": 2}, False)])
    db.tgm_add_recipients(alice, job_id, a2, [("u3", {"id": 3}, False)], idx)

    first = db.tgm_pending_recipients(alice, job_id, a1)
    assert [r["target_key"] for r in first] == ["u1", "u2"]   # mutual first
    assert first[0]["mutual"] == 1
    second = db.tgm_pending_recipients(alice, job_id, a2)
    assert [r["target_key"] for r in second] == ["u3"]


def test_mutuals_come_first(alice):
    """Mutual contacts added the account back, so they are the least likely to
    report a message — reaching them first keeps the account alive longer."""
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_recipients(alice, job_id, aid, [
        ("plain-1", {"id": 1}, False),
        ("mutual-1", {"id": 2}, True),
        ("plain-2", {"id": 3}, False),
        ("mutual-2", {"id": 4}, True),
    ])
    keys = [r["target_key"] for r in db.tgm_pending_recipients(alice, job_id, aid)]
    assert keys[:2] == ["mutual-1", "mutual-2"]


def test_recipient_state_transitions(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_recipients(alice, job_id, aid, [("u1", {"id": 1}, False)])
    db.tgm_set_recipient(alice, job_id, 0, "sent")
    assert db.tgm_pending_recipients(alice, job_id, aid) == []
    assert db.tgm_counts(alice, job_id) == {"sent": 1}


def test_cross_account_dedup_within_a_job(alice):
    """A customer with five accounts usually has overlapping contacts; without
    this every shared contact receives the same message five times."""
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    assert db.tgm_uid_already_sent(alice, job_id, "555") is False
    db.tgm_mark_uid_sent(alice, job_id, "555")
    assert db.tgm_uid_already_sent(alice, job_id, "555") is True


def test_dedup_does_not_leak_between_jobs(alice):
    job_a = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    job_b = db.tgm_create_job(alice, [{"kind": "text", "text": "y"}], 0.2)
    db.tgm_mark_uid_sent(alice, job_a, "555")
    assert db.tgm_uid_already_sent(alice, job_b, "555") is False


def test_job_counters_accumulate(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    db.tgm_bump_job(alice, job_id, sent=5, failed=2, skipped=1)
    db.tgm_bump_job(alice, job_id, sent=3)
    job = db.tgm_get_job(alice, job_id)
    assert (job["sent_count"], job["failed_count"], job["skipped_count"]) == (8, 2, 1)


def test_deleting_a_job_removes_everything(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid, [("u1", {"id": 1}, False)])
    db.tgm_mark_uid_sent(alice, job_id, "u1")
    db.tgm_delete_job(alice, job_id)
    assert db.tgm_get_job(alice, job_id) is None
    assert db.tgm_job_accounts(alice, job_id) == []
    assert db.tgm_counts(alice, job_id) == {}


def test_deleting_a_customer_removes_their_jobs(alice):
    db.tgm_create_job(alice, [{"kind": "text", "text": "x"}], 0.2)
    db.delete_customer(alice)
    assert db.owner_tgm_unfinished() == []


# --------------------------------------------------------------------------- #
# Running a job
# --------------------------------------------------------------------------- #
def _fake_client_layer(monkeypatch, delivered, fail_on=None, raise_exc=None):
    class _Client:
        pass

    async def fake_get_client(customer_id, account_id):
        return _Client()

    async def fake_deliver(client, target, content, delay, plan=None):
        # `plan` is the pre-uploaded media plan. Accepting it here keeps this fake
        # honest about the real signature: media is uploaded once per account now,
        # not re-uploaded for every recipient.
        uid = target.get("id")
        if fail_on is not None and uid in fail_on:
            raise (raise_exc or RuntimeError("send failed"))
        delivered.append(uid)

    monkeypatch.setattr(tgc, "get_client", fake_get_client)
    monkeypatch.setattr(multi, "_deliver", fake_deliver)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)


def test_a_job_sends_each_accounts_own_recipients(alice, monkeypatch):
    delivered = []
    _fake_client_layer(monkeypatch, delivered)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    a1 = db.tg_add_account(alice, "09120000001")
    a2 = db.tg_add_account(alice, "09120000002")
    db.tgm_add_account(alice, job_id, a1, "09120000001", 0)
    db.tgm_add_account(alice, job_id, a2, "09120000002", 1)
    idx = db.tgm_add_recipients(alice, job_id, a1, [
        ("1", {"kind": "user", "id": 1}, True),
        ("2", {"kind": "user", "id": 2}, False)])
    db.tgm_add_recipients(alice, job_id, a2, [
        ("3", {"kind": "user", "id": 3}, False)], idx)
    db.tgm_update_job(alice, job_id, total=3)

    asyncio.run(multi._run(alice, job_id))

    assert sorted(delivered) == [1, 2, 3]
    job = db.tgm_get_job(alice, job_id)
    assert job["state"] == "done"
    assert job["sent_count"] == 3
    assert db.get_customer(alice)["total_sends"] == 3


def test_a_shared_contact_is_messaged_once_per_job(alice, monkeypatch):
    delivered = []
    _fake_client_layer(monkeypatch, delivered)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    a1 = db.tg_add_account(alice, "09120000001")
    a2 = db.tg_add_account(alice, "09120000002")
    db.tgm_add_account(alice, job_id, a1, "09120000001", 0)
    db.tgm_add_account(alice, job_id, a2, "09120000002", 1)
    # both accounts have contact 42
    idx = db.tgm_add_recipients(alice, job_id, a1, [
        ("42", {"kind": "user", "id": 42}, False)])
    db.tgm_add_recipients(alice, job_id, a2, [
        ("42", {"kind": "user", "id": 42}, False)], idx)
    db.tgm_update_job(alice, job_id, total=2)

    asyncio.run(multi._run(alice, job_id))

    assert delivered == [42]                     # exactly once
    job = db.tgm_get_job(alice, job_id)
    assert job["sent_count"] == 1
    assert job["skipped_count"] == 1


def test_a_fatal_account_error_stops_only_that_account(alice, monkeypatch):
    delivered = []

    class _Client:
        pass

    async def fake_get_client(customer_id, account_id):
        return _Client()

    async def fake_deliver(client, target, content, delay, plan=None):
        # `plan` is the pre-uploaded media plan. Accepting it here keeps this fake
        # honest about the real signature: media is uploaded once per account now,
        # not re-uploaded for every recipient.
        uid = target.get("id")
        if uid == 1:
            raise Exception("AUTH_KEY_UNREGISTERED")
        delivered.append(uid)

    monkeypatch.setattr(tgc, "get_client", fake_get_client)
    monkeypatch.setattr(multi, "_deliver", fake_deliver)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    a1 = db.tg_add_account(alice, "09120000001")
    a2 = db.tg_add_account(alice, "09120000002")
    db.tgm_add_account(alice, job_id, a1, "09120000001", 0)
    db.tgm_add_account(alice, job_id, a2, "09120000002", 1)
    idx = db.tgm_add_recipients(alice, job_id, a1, [
        ("1", {"kind": "user", "id": 1}, False)])
    db.tgm_add_recipients(alice, job_id, a2, [
        ("9", {"kind": "user", "id": 9}, False)], idx)

    asyncio.run(multi._run(alice, job_id))

    assert delivered == [9]                                  # account 2 kept going
    assert db.tg_get_account(alice, a1)["status"] == "dead"
    assert db.tg_get_account(alice, a2)["status"] == "active"


def test_a_privacy_error_skips_just_that_recipient(alice, monkeypatch):
    delivered = []
    _fake_client_layer(monkeypatch, delivered, fail_on={2},
                       raise_exc=Exception("USER_PRIVACY_RESTRICTED"))

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid, [
        ("1", {"kind": "user", "id": 1}, False),
        ("2", {"kind": "user", "id": 2}, False),
        ("3", {"kind": "user", "id": 3}, False)])

    asyncio.run(multi._run(alice, job_id))

    assert delivered == [1, 3]
    job = db.tgm_get_job(alice, job_id)
    assert job["sent_count"] == 2 and job["skipped_count"] == 1
    assert job["state"] == "done"


def test_consecutive_failures_stop_the_account(alice, monkeypatch):
    _fake_client_layer(monkeypatch, [], fail_on={1, 2, 3, 4, 5, 6},
                       raise_exc=TimeoutError("flaky"))
    db.set_setting(alice, "max_errors", 3)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [(str(i), {"kind": "user", "id": i}, False)
                           for i in range(1, 7)])

    asyncio.run(multi._run(alice, job_id))

    job = db.tgm_get_job(alice, job_id)
    assert job["failed_count"] == 3          # stopped at the ceiling
    assert job["state"] == "paused"          # work remains
    account = db.tgm_job_accounts(alice, job_id)[0]
    assert account["state"] == "stopped"


def test_a_frozen_service_halts_the_job(alice, monkeypatch):
    """The owner's emergency stop has to reach a job already in flight."""
    delivered = []
    _fake_client_layer(monkeypatch, delivered)
    db.set_sends_frozen(True)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid, [
        ("1", {"kind": "user", "id": 1}, False)])

    asyncio.run(multi._run(alice, job_id))
    assert delivered == []
    assert db.tgm_get_job(alice, job_id)["state"] == "frozen"


def test_a_busy_account_is_skipped_not_failed(alice, monkeypatch):
    delivered = []
    _fake_client_layer(monkeypatch, delivered)
    aid = db.tg_add_account(alice, "09120000001")
    busy.acquire(tg_panel._key(alice, "09120000001"), "export", customer_id=alice)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid, [
        ("1", {"kind": "user", "id": 1}, False)])

    asyncio.run(multi._run(alice, job_id))
    assert delivered == []
    assert db.tgm_job_accounts(alice, job_id)[0]["state"] == "skipped"


def test_the_session_is_released_after_an_account_turn(alice, monkeypatch):
    _fake_client_layer(monkeypatch, [])
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid, [
        ("1", {"kind": "user", "id": 1}, False)])

    asyncio.run(multi._run(alice, job_id))
    assert busy.is_busy(tg_panel._key(alice, "09120000001")) is False


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #
def test_restore_marks_jobs_paused_and_readopts_running_accounts(alice):
    """Paused rather than auto-restarted: an automatic restart inside a crash
    loop would hammer the platform, and the customer should decide."""
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_update_job(alice, job_id, state="running")
    db.tgm_update_account(alice, job_id, aid, state="running")
    busy.clear_all()

    asyncio.run(multi.restore_pending())

    assert db.tgm_get_job(alice, job_id)["state"] == "paused"
    assert busy.is_busy(tg_panel._key(alice, "09120000001")) is True
    notes = db.fetch_unsent_notifications()
    assert notes and "نیمه‌کاره" in notes[0]["text"]


def test_restore_ignores_finished_jobs(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    db.tgm_update_job(alice, job_id, state="done")
    asyncio.run(multi.restore_pending())
    assert db.tgm_get_job(alice, job_id)["state"] == "done"
    assert db.fetch_unsent_notifications() == []


def test_restore_on_an_empty_database():
    asyncio.run(multi.restore_pending())


# --------------------------------------------------------------------------- #
# Stop is cooperative
# --------------------------------------------------------------------------- #
def test_stop_sets_the_flag_and_the_loop_notices(alice, monkeypatch):
    delivered = []

    class _Client:
        pass

    async def fake_get_client(customer_id, account_id):
        return _Client()

    async def slow_deliver(client, target, content, delay, plan=None):
        delivered.append(target.get("id"))
        await asyncio.sleep(0.05)

    monkeypatch.setattr(tgc, "get_client", fake_get_client)
    monkeypatch.setattr(multi, "_deliver", slow_deliver)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [(str(i), {"kind": "user", "id": i}, False)
                           for i in range(30)])

    async def scenario():
        await multi.start(alice, job_id)
        await asyncio.sleep(0.12)
        await multi.stop(alice, job_id, grace=1.0)

    asyncio.run(scenario())
    job = db.tgm_get_job(alice, job_id)
    assert job["state"] in ("stopped", "paused")
    assert 0 < len(delivered) < 30                # stopped part-way
    assert busy.is_busy(tg_panel._key(alice, "09120000001")) is False


def test_stopping_an_unknown_job_is_safe(alice):
    asyncio.run(multi.stop(alice, "deadbeef", grace=0.1))


def test_resume_requires_a_real_job(alice):
    with pytest.raises(ValueError):
        asyncio.run(multi.resume(alice, "nope"))


# --------------------------------------------------------------------------- #
# Progress rendering
# --------------------------------------------------------------------------- #
def test_progress_card_lists_accounts_and_totals(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_update_account(alice, job_id, aid, total=10, sent_count=4,
                          state="running")
    db.tgm_update_job(alice, job_id, total=10, sent_count=4, state="running")

    text = multi.progress_card(alice, job_id)
    assert "#tg_multi_send" in text
    assert "09120000001" in text
    assert "در حال اجرا" in text
    assert "█" in text


def test_progress_card_on_a_missing_job(alice):
    assert "پیدا نشد" in multi.progress_card(alice, "nope")


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #
def test_content_is_ordered_and_scoped(alice, bob):
    db.tg_content_add(alice, "text", text="first")
    db.tg_content_add(alice, "media", text="cap", file_path="/tmp/x.jpg",
                      file_name="x.jpg")
    db.tg_content_add(bob, "text", text="theirs")

    mine = db.tg_content_list(alice)
    assert [c["kind"] for c in mine] == ["text", "media"]
    assert mine[1]["file_name"] == "x.jpg"
    assert [c["text"] for c in db.tg_content_list(bob)] == ["theirs"]


def test_content_summary_describes_the_mix():
    assert tg_panel._content_summary([]) == "خالی"
    summary = tg_panel._content_summary([
        {"kind": "text"}, {"kind": "text"}, {"kind": "media"}])
    assert "2" in summary and "1" in summary


def test_clearing_content_removes_the_files(alice, tmp_path):
    path = tmp_path / "pic.jpg"
    path.write_bytes(b"data")
    db.tg_content_add(alice, "media", file_path=str(path), file_name="pic.jpg")
    removed = db.tg_content_clear(alice)
    assert removed == 1
    assert not path.exists()
    assert db.tg_content_list(alice) == []


def test_clearing_content_tolerates_a_missing_file(alice):
    db.tg_content_add(alice, "media", file_path="/nonexistent/x.jpg")
    assert db.tg_content_clear(alice) == 1


# --------------------------------------------------------------------------- #
# Panel rendering
# --------------------------------------------------------------------------- #
def test_section_card_reports_accounts_content_and_target(alice):
    db.tg_add_account(alice, "09120000001")
    db.tg_content_add(alice, "text", text="hello")
    text = tg_panel.menu_card(alice)
    assert "Telegram" in text
    assert "Content" in text
    assert "Target" in text


def test_target_mode_defaults_to_both_and_persists(alice):
    assert tg_panel._target_mode(alice) == "both"
    db.set_setting(alice, "tg_target", "groups")
    assert tg_panel._target_mode(alice) == "groups"


def test_speed_is_clamped_to_the_telegram_range(alice):
    db.set_setting(alice, "tg_send_delay", 99)
    assert tg_panel._speed(alice) <= config.TG_SEND_DELAY_MAX
    db.set_setting(alice, "tg_send_delay", 0.0001)
    assert tg_panel._speed(alice) >= config.TG_SEND_DELAY_MIN


def test_account_card_shows_mutual_and_busy_state(alice):
    aid = db.tg_add_account(alice, "09120000001", contacts=100, mutuals=40)
    acc = db.tg_get_account(alice, aid)
    text = tg_panel._account_card(alice, acc)
    assert "Mutual" in text and "40" in text
    busy.acquire(tg_panel._key(alice, "09120000001"), "export", customer_id=alice)
    assert "گرفتن مخاطبین" in tg_panel._account_card(alice, acc)


def test_dead_tg_account_offers_relogin(alice):
    aid = db.tg_add_account(alice, "09120000001")
    db.tg_set_status(alice, aid, "dead")
    acc = db.tg_get_account(alice, aid)
    flat = " ".join(str(b) for row in tg_panel._account_buttons(acc) for b in row)
    assert "ورود مجدد" in flat


def test_phone_normalisation_for_telegram():
    assert tg_panel._normalize_phone("09123456789") == "+989123456789"
    assert tg_panel._normalize_phone("+98 912 345 6789") == "+989123456789"
    assert tg_panel._normalize_phone("") == ""


def test_section_menu_has_no_owner_only_buttons():
    flat = " ".join(str(b) for row in tg_panel.menu_buttons() for b in row).lower()
    for forbidden in ("ورکر", "worker", "بکاپ", "backup"):
        assert forbidden not in flat


# --------------------------------------------------------------------------- #
# Session storage
# --------------------------------------------------------------------------- #
def test_tg_session_round_trip_is_scoped(alice, bob):
    a = db.tg_add_account(alice, "09120000001")
    b = db.tg_add_account(bob, "09120000001")
    db.tg_set_session(alice, a, "SESSION-A")
    db.tg_set_session(bob, b, "SESSION-B")
    assert db.tg_get_account(alice, a)["session"] == "SESSION-A"
    assert db.tg_get_account(bob, b)["session"] == "SESSION-B"


def test_tg_stats_update(alice):
    aid = db.tg_add_account(alice, "09120000001")
    db.tg_set_stats(alice, aid, contacts=500, mutuals=200, groups=12)
    acc = db.tg_get_account(alice, aid)
    assert (acc["contacts"], acc["mutuals"], acc["groups"]) == (500, 200, 12)


def test_tg_set_stats_ignores_unknown_fields(alice):
    aid = db.tg_add_account(alice, "09120000001")
    db.tg_set_stats(alice, aid, nonsense=5)
    assert db.tg_get_account(alice, aid) is not None
