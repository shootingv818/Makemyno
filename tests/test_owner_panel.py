"""
The owner panel: card rendering, the flows that change customer state, and the
guards that stop a mis-tap from doing damage.

Telethon is stubbed, so these tests drive the module's own logic (card text,
audit trail, notification queue, confirmations) rather than the transport.
"""
import asyncio

import pytest

import antispam
import backup
import cards
import central_db
import config
import db
import logbus
import owner_bot
import worker


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    monkeypatch.setattr(logbus, "to_group_file", noop)


def _worker_row(tag="wk-a", master=0, ip="1.2.3.4"):
    return db.add_worker(tag, ip, 22, "root", "enc", 8765, "tok", is_master=master)


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
class _Event:
    def __init__(self, sender_id):
        self.sender_id = sender_id


def test_only_the_owner_passes_the_guard(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", 555)
    assert owner_bot.is_owner(_Event(555)) is True
    assert owner_bot.is_owner(_Event(556)) is False


def test_nobody_passes_when_no_owner_is_configured(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", 0)
    assert owner_bot.is_owner(_Event(0)) is False
    assert owner_bot.is_owner(_Event(123)) is False


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def test_dashboard_counts_customers_accounts_and_fleet(alice, bob):
    wid = _worker_row("wk-a")
    db.add_account(alice, "09120000001", worker_id=wid)
    db.add_account(bob, "09130000001", worker_id=wid)
    db.tg_add_account(alice, "09120000009")
    db.update_worker_health(wid, "ok", 120, 1)

    text = owner_bot.dashboard_card()
    assert "#owner_panel" in text
    assert cards.LINE in text
    assert "Customers" in text and "Workers" in text
    assert "🟢 ONLINE" in text


def test_dashboard_shows_offline_and_frozen_states():
    db.set_bot_online(False, by="shield", note="flood")
    db.set_sends_frozen(True)
    text = owner_bot.dashboard_card()
    assert "🔴 OFFLINE" in text
    assert "FROZEN" in text


def test_dashboard_survives_an_empty_service():
    text = owner_bot.dashboard_card()
    assert "#owner_panel" in text


def test_main_menu_flags_open_tickets(alice):
    plain = owner_bot.main_menu()
    central_db.add_ticket(alice, "help")
    with_ticket = owner_bot.main_menu()
    flat_before = [b for row in plain for b in row]
    flat_after = [b for row in with_ticket for b in row]
    assert len(flat_after) == len(flat_before)
    assert any("(1)" in str(b) for b in flat_after)


# --------------------------------------------------------------------------- #
# Customer list + profile rendering
# --------------------------------------------------------------------------- #
def test_customer_mark_reflects_state(alice):
    db.add_days(alice, 30)
    assert owner_bot._cust_mark(db.get_customer(alice)) == "🟢"
    db.set_expiry(alice, db._iso_after_days(1))
    assert owner_bot._cust_mark(db.get_customer(alice)) == "🟡"
    db.set_expiry(alice, "2020-01-01 00:00:00")
    assert owner_bot._cust_mark(db.get_customer(alice)) == "🔴"
    db.set_blocked(alice, True)
    assert owner_bot._cust_mark(db.get_customer(alice)) == "⛔"


def test_customer_label_includes_account_count(alice):
    db.add_account(alice, "09120000001")
    db.tg_add_account(alice, "09120000009")
    label = owner_bot._cust_label(db.get_customer(alice))
    assert "📱2" in label
    assert "Alice" in label


def test_profile_card_shows_the_essentials(alice):
    wid = _worker_row("wk-a")
    db.add_account(alice, "09120000001", worker_id=wid)
    db.add_days(alice, 30)
    db.set_note(alice, "priority customer")

    text = owner_bot._profile_card(db.get_customer(alice))
    assert "#customer_profile" in text
    assert str(alice) in text
    assert "ACTIVE" in text
    assert "wk-a" in text
    assert "priority customer" in text


def test_profile_card_marks_blocked_and_expired(alice):
    db.set_expiry(alice, "2020-01-01 00:00:00")
    assert "EXPIRED" in owner_bot._profile_card(db.get_customer(alice))
    db.set_blocked(alice, True)
    assert "BLOCKED" in owner_bot._profile_card(db.get_customer(alice))


def test_profile_buttons_flip_with_block_state(alice):
    buttons = owner_bot._profile_buttons(db.get_customer(alice))
    assert any("مسدود کردن" in str(b) for row in buttons for b in row)
    db.set_blocked(alice, True)
    buttons = owner_bot._profile_buttons(db.get_customer(alice))
    assert any("رفع مسدودی" in str(b) for row in buttons for b in row)


# --------------------------------------------------------------------------- #
# Audience selection for broadcasts
# --------------------------------------------------------------------------- #
def test_audience_filters(alice, bob, monkeypatch):
    monkeypatch.setattr(config, "EXPIRY_WARN_DAYS", 2)
    db.add_days(alice, 30)
    db.set_expiry(bob, "2020-01-01 00:00:00")

    assert len(owner_bot._audience("all")) == 2
    assert [c["telegram_id"] for c in owner_bot._audience("active")] == [alice]
    assert [c["telegram_id"] for c in owner_bot._audience("expired")] == [bob]

    soon = db.ensure_customer(3003, "Soon")["telegram_id"]
    db.set_expiry(soon, db._iso_after_days(1))
    assert soon in [c["telegram_id"] for c in owner_bot._audience("soon")]


def test_blocked_customers_are_not_in_active_or_expired(alice):
    db.add_days(alice, 30)
    db.set_blocked(alice, True)
    assert owner_bot._audience("active") == []
    assert owner_bot._audience("expired") == []
    assert len(owner_bot._audience("all")) == 1


# --------------------------------------------------------------------------- #
# The owner cannot DM a customer, so everything goes through the outbox
# --------------------------------------------------------------------------- #
def test_granting_time_queues_a_notification_and_audits(alice):
    class _Ev:
        sender_id = 1
        async def answer(self, *a, **k):
            return None
        async def edit(self, *a, **k):
            return None

    asyncio.run(owner_bot._grant_days(_Ev(), alice, 30))

    assert db.days_left(alice) >= 29
    pending = db.fetch_unsent_notifications()
    assert len(pending) == 1
    assert pending[0]["customer_id"] == alice
    assert "اعتبار" in pending[0]["text"]
    assert central_db.list_audit()[0]["action"] == "time"


def test_broadcast_queues_one_notification_per_recipient(alice, bob):
    db.add_days(alice, 10)
    db.add_days(bob, 10)

    class _Ev:
        sender_id = 1
        raw_text = "سرویس فردا بروزرسانی می‌شود"
        async def respond(self, *a, **k):
            return None

    owner_bot.state[1] = {"step": "await_broadcast", "kind": "active"}
    asyncio.run(owner_bot._step_broadcast(_Ev(), owner_bot.state[1]))

    queued = db.fetch_unsent_notifications()
    assert len(queued) == 2
    assert {n["customer_id"] for n in queued} == {alice, bob}
    assert central_db.list_broadcasts()[0]["queued"] == 2


def test_empty_broadcast_text_queues_nothing(alice):
    db.add_days(alice, 10)

    class _Ev:
        sender_id = 1
        raw_text = "   "
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_broadcast(_Ev(), {"kind": "active"}))
    assert db.fetch_unsent_notifications() == []


def test_direct_message_is_queued(alice):
    class _Ev:
        sender_id = 1
        raw_text = "سلام، مشکل حل شد"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_dm(_Ev(), {"cid": alice}))
    queued = db.fetch_unsent_notifications()
    assert len(queued) == 1
    assert "مشکل حل شد" in queued[0]["text"]


# --------------------------------------------------------------------------- #
# Destructive actions need a typed confirmation
# --------------------------------------------------------------------------- #
def test_deleting_a_customer_requires_the_exact_id(alice):
    db.add_account(alice, "09120000001")

    class _Ev:
        sender_id = 1
        raw_text = "9999"
        async def respond(self, *a, **k):
            return None

    owner_bot.state[1] = {"step": "await_del_confirm", "cid": alice}
    asyncio.run(owner_bot._step_del_confirm(_Ev(), owner_bot.state[1]))
    assert db.get_customer(alice) is not None      # wrong id -> nothing happened


def test_deleting_a_customer_with_the_right_id_removes_everything(alice):
    db.add_account(alice, "09120000001")
    db.tg_add_account(alice, "09120000009")

    class _Ev:
        sender_id = 1
        raw_text = str(alice)
        async def respond(self, *a, **k):
            return None

    owner_bot.state[1] = {"step": "await_del_confirm", "cid": alice}
    asyncio.run(owner_bot._step_del_confirm(_Ev(), owner_bot.state[1]))

    assert db.get_customer(alice) is None
    assert central_db.list_audit()[0]["action"] == "delete_customer"


def test_deleting_a_worker_requires_the_exact_tag(alice):
    wid = _worker_row("wk-a3f1")
    db.add_account(alice, "09120000001", worker_id=wid)

    class _Ev:
        sender_id = 1
        raw_text = "wk-wrong"
        async def respond(self, *a, **k):
            return None

    owner_bot.state[1] = {"step": "await_wk_del", "wid": wid, "tag": "wk-a3f1"}
    asyncio.run(owner_bot._step_wk_del(_Ev(), owner_bot.state[1]))
    assert db.get_worker(wid) is not None


# --------------------------------------------------------------------------- #
# Adding a customer by hand
# --------------------------------------------------------------------------- #
def test_add_customer_parses_id_and_optional_days():
    class _Ev:
        sender_id = 1
        raw_text = "774119203 30"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_new_customer(_Ev(), {}))
    assert db.get_customer(774119203) is not None
    assert db.days_left(774119203) >= 29


def test_add_customer_without_days_uses_the_trial(monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 3)

    class _Ev:
        sender_id = 1
        raw_text = "888777"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_new_customer(_Ev(), {}))
    assert 2 <= db.days_left(888777) <= 3


def test_add_customer_rejects_a_non_numeric_id():
    class _Ev:
        sender_id = 1
        raw_text = "not-an-id"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_new_customer(_Ev(), {}))
    assert db.owner_count_customers()["total"] == 0


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def test_diagnose_reports_customer_worker_and_session(alice):
    wid = _worker_row("wk-a")
    db.update_worker_health(wid, "ok", 100, 1)
    aid = db.add_account(alice, "09121234567", worker_id=wid)
    db.incr_account_sent(alice, aid, 42)

    text = asyncio.run(owner_bot._diagnose("09121234567"))
    assert "#diagnose" in text
    assert "Alice" in text
    assert "wk-a" in text
    assert "🟢 active" in text
    assert "42" in text


def test_diagnose_explains_a_dead_session(alice):
    aid = db.add_account(alice, "09121234567")
    db.set_status(alice, aid, "quarantined")
    text = asyncio.run(owner_bot._diagnose("09121234567"))
    assert "quarantined" in text
    assert "لاگین" in text          # tells the owner what the fix is


def test_diagnose_reports_a_busy_account(alice):
    import busy
    db.add_account(alice, "09121234567")
    key = busy.key_for("09121234567", customer_id=alice)
    busy.acquire(key, "contacts", customer_id=alice)
    text = asyncio.run(owner_bot._diagnose("09121234567"))
    assert "افزودن مخاطب" in text


def test_diagnose_on_an_unknown_number():
    text = asyncio.run(owner_bot._diagnose("09000000000"))
    assert "پیدا نشد" in text


def test_diagnose_finds_both_platforms(alice):
    db.add_account(alice, "09121234567")
    db.tg_add_account(alice, "09121234567")
    text = asyncio.run(owner_bot._diagnose("09121234567"))
    assert text.count("#diagnose") == 2


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
def test_ticket_reply_answers_and_notifies(alice):
    tid = central_db.add_ticket(alice, "اکانتم کار نمی‌کند")

    class _Ev:
        sender_id = 1
        raw_text = "بررسی شد، دوباره لاگین کن"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_ticket_reply(_Ev(), {"tid": tid}))

    assert central_db.get_ticket(tid)["answered"] == 1
    queued = db.fetch_unsent_notifications()
    assert len(queued) == 1
    assert "دوباره لاگین" in queued[0]["text"]
    assert central_db.count_open_tickets() == 0


def test_ticket_reply_on_a_missing_ticket_is_safe():
    class _Ev:
        sender_id = 1
        raw_text = "hello"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_ticket_reply(_Ev(), {"tid": 9999}))
    assert db.fetch_unsent_notifications() == []


# --------------------------------------------------------------------------- #
# Warning the customers on one worker
# --------------------------------------------------------------------------- #
def test_worker_warning_reaches_exactly_the_affected_customers(alice, bob):
    """A throttled worker affects only the customers who have accounts on it."""
    w1 = _worker_row("wk-a")
    w2 = _worker_row("wk-b")
    db.add_account(alice, "09120000001", worker_id=w1)
    db.add_account(bob, "09130000001", worker_id=w2)

    class _Ev:
        sender_id = 1
        raw_text = "اختلال موقت روی یکی از سرورها"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_worker_warn(_Ev(), {"wid": w1}))
    queued = db.fetch_unsent_notifications()
    assert [n["customer_id"] for n in queued] == [alice]


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #
def test_note_can_be_set_and_cleared(alice):
    class _Ev:
        sender_id = 1
        raw_text = "دو بار اسپم کرد"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_note(_Ev(), {"cid": alice}))
    assert db.get_customer(alice)["note"] == "دو بار اسپم کرد"

    class _Clear(_Ev):
        raw_text = "-"

    asyncio.run(owner_bot._step_note(_Clear(), {"cid": alice}))
    assert db.get_customer(alice)["note"] == ""


# --------------------------------------------------------------------------- #
# Custom day amounts
# --------------------------------------------------------------------------- #
def test_custom_day_input_accepts_negative(alice):
    db.set_expiry(alice, db._iso_after_days(40))

    class _Ev:
        sender_id = 1
        raw_text = "-35"
        async def respond(self, *a, **k):
            return None

    owner_bot.state[1] = {"step": "await_days", "cid": alice}
    asyncio.run(owner_bot._step_days(_Ev(), owner_bot.state[1]))
    assert db.days_left(alice) <= 5


def test_custom_day_input_rejects_garbage(alice):
    before = db.get_customer(alice)["expires_at"]

    class _Ev:
        sender_id = 1
        raw_text = "many days please"
        async def respond(self, *a, **k):
            return None

    owner_bot.state[1] = {"step": "await_days", "cid": alice}
    asyncio.run(owner_bot._step_days(_Ev(), owner_bot.state[1]))
    assert db.get_customer(alice)["expires_at"] == before


# --------------------------------------------------------------------------- #
# Maintenance notice
# --------------------------------------------------------------------------- #
def test_maintenance_notice_is_written_into_the_flag_file():
    central_db.set_maintenance(True)

    class _Ev:
        sender_id = 1
        raw_text = "۱۰ دقیقه دیگر برگرد"
        async def respond(self, *a, **k):
            return None

    asyncio.run(owner_bot._step_maint_note(_Ev(), {}))
    with open(central_db.maintenance_flag_path(), encoding="utf-8") as fh:
        assert "۱۰ دقیقه" in fh.read()


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_search_step_handles_hits_and_misses(alice):
    db.add_account(alice, "09121234567")
    responses = []

    class _Ev:
        sender_id = 1
        raw_text = "1234567"
        async def respond(self, text, **k):
            responses.append(text)

    asyncio.run(owner_bot._step_search(_Ev(), {}))
    assert "Found" in responses[0]

    class _Miss(_Ev):
        raw_text = "zzzz"

    asyncio.run(owner_bot._step_search(_Miss(), {}))
    assert "پیدا نشد" in responses[1]


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
def test_backup_stats_counts_sessions(alice):
    db.tg_add_account(alice, "09120000009", session="stringsession")
    stats = backup.stats()
    assert stats["tg"] == 1
    assert "encrypted" in stats


def test_backup_refuses_to_write_plaintext(monkeypatch, tmp_path):
    """Sessions are account-equivalent secrets; an unencrypted archive must never
    be produced by accident."""
    import crypto_util
    monkeypatch.setattr(crypto_util, "is_configured", lambda: False)
    path = tmp_path / "x.zip"
    path.write_bytes(b"data")
    with pytest.raises(backup.BackupError):
        backup._encrypt(str(path))
    assert not path.exists()          # and the plaintext file is removed


def test_backup_reports_when_there_is_nothing_to_save():
    result = asyncio.run(backup.run_backup())
    assert result["ok"] is False
    assert result["error"] in ("no-sessions", "") or result["error"]


def test_backup_summary_rows_mention_partial_state():
    rows = backup.summary_rows({"rb_local": 1, "rb_workers": 2, "tg": 3,
                                "unreachable": ["wk-b"]})
    text = "\n".join(rows)
    assert "partial" in text
    rows_ok = backup.summary_rows({"rb_local": 1, "rb_workers": 0, "tg": 0,
                                   "unreachable": []})
    assert "complete" in "\n".join(rows_ok)


# --------------------------------------------------------------------------- #
# Fleet helpers used by the panel
# --------------------------------------------------------------------------- #
def test_status_emoji_tracks_ping_bands(monkeypatch):
    monkeypatch.setattr(config, "PING_GREEN_MS", 800)
    monkeypatch.setattr(config, "PING_YELLOW_MS", 2000)
    assert worker.status_emoji({"file_ok": 0, "ping_ms": 10}) == "🔴"
    assert worker.status_emoji({"file_ok": 1, "ping_ms": 100}) == "🟢"
    assert worker.status_emoji({"file_ok": 1, "ping_ms": 1500}) == "🟡"
    assert worker.status_emoji({"file_ok": 1, "ping_ms": 5000}) == "🔴"
    assert worker.status_emoji({"file_ok": 1, "ping_ms": -1}) == "🟡"


def test_ping_and_route_labels():
    assert worker.ping_text({"ping_ms": 340}) == "340ms"
    assert worker.ping_text({"ping_ms": -1}) == "—"
    assert worker.route_label({"file_ok": 1}) == "API ok"
    assert worker.route_label({"file_ok": 0}) == "No answer"


def test_master_row_is_created_once():
    first = worker.ensure_master_worker()
    second = worker.ensure_master_worker()
    assert first["id"] == second["id"]
    assert first["tag"] == "master"
    assert len([w for w in db.list_workers() if w["is_master"]]) == 1


def test_master_is_not_created_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    assert worker.ensure_master_worker() is None
    assert db.list_workers() == []


def test_worker_for_account_falls_back_to_master(alice):
    worker.ensure_master_worker()
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    assert worker.worker_for_account(acc)["is_master"] == 1


def test_worker_for_account_honours_affinity(alice):
    """A session lives on one worker's disk, so a job must go back to it."""
    worker.ensure_master_worker()
    wid = _worker_row("wk-a")
    aid = db.add_account(alice, "09120000001", worker_id=wid)
    acc = db.get_account(alice, aid)
    assert worker.worker_for_account(acc)["id"] == wid


def test_gen_tag_is_unique_and_master_is_named():
    assert worker.gen_tag(is_master=True) == "master"
    _worker_row("wk-a")
    tags = {worker.gen_tag() for _ in range(20)}
    assert "wk-a" not in tags


def test_is_local_only_for_master():
    assert worker.is_local({"is_master": 1}) is True
    assert worker.is_local({"is_master": 0}) is False
    assert worker.is_local(None) is False


# --------------------------------------------------------------------------- #
# Round-robin placement through the real selector
# --------------------------------------------------------------------------- #
def test_pick_worker_spreads_across_the_pool(monkeypatch):
    """Every panel used to start counting from worker #1; a persisted pointer is
    what actually spreads the load."""
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    ids = [_worker_row("wk-a"), _worker_row("wk-b"), _worker_row("wk-c")]
    for wid in ids:
        db.update_worker_health(wid, "ok", 100, 1)

    picks = [asyncio.run(worker.pick_worker_for_login(verify=False))["id"]
             for _ in range(6)]
    assert picks == ids + ids


def test_pick_worker_skips_unhealthy_remotes(monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    good = _worker_row("wk-good")
    bad = _worker_row("wk-bad")
    db.update_worker_health(good, "ok", 100, 1)
    db.update_worker_health(bad, "down", -1, 0)
    picks = {asyncio.run(worker.pick_worker_for_login(verify=False))["id"]
             for _ in range(6)}
    assert picks == {good}


def test_pick_worker_honours_exclude(monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    a = _worker_row("wk-a")
    b = _worker_row("wk-b")
    for wid in (a, b):
        db.update_worker_health(wid, "ok", 100, 1)
    picks = {asyncio.run(worker.pick_worker_for_login(verify=False,
                                                      exclude_id=a))["id"]
             for _ in range(4)}
    assert picks == {b}


def test_pick_worker_returns_none_when_nothing_is_usable(monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    wid = _worker_row("wk-a")
    db.set_worker_enabled(wid, False)
    assert asyncio.run(worker.pick_worker_for_login(verify=False)) is None


def test_disabled_worker_is_never_picked_even_if_healthy(monkeypatch):
    monkeypatch.setattr(config, "MASTER_AS_WORKER", False)
    a = _worker_row("wk-a")
    b = _worker_row("wk-b")
    for wid in (a, b):
        db.update_worker_health(wid, "ok", 100, 1)
    db.set_worker_enabled(a, False)
    picks = {asyncio.run(worker.pick_worker_for_login(verify=False))["id"]
             for _ in range(4)}
    assert picks == {b}


def test_local_master_is_usable_without_health_checks():
    worker.ensure_master_worker()
    picked = asyncio.run(worker.pick_worker_for_login(verify=False))
    assert picked is not None and picked["is_master"] == 1


# --------------------------------------------------------------------------- #
# Shield interaction from the panel
# --------------------------------------------------------------------------- #
def test_shield_status_feeds_the_panel(monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_MAX", 20)
    status = antispam.status()
    assert set(status) >= {"online", "recent_starts", "window", "limit"}
