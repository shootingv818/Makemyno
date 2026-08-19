"""
The shared worker fleet, the owner-only database, and the owner's read paths.
"""
import os

import cards
import central_db
import db


def _worker(tag="wk-a", master=0, ip="1.2.3.4"):
    return db.add_worker(tag, ip, 22, "root", "enc-pass", 8765, "enc-token",
                         is_master=master)


# --------------------------------------------------------------------------- #
# Fleet basics
# --------------------------------------------------------------------------- #
def test_workers_are_global_and_ordered_master_first():
    _worker("wk-remote-1")
    _worker("wk-master", master=1)
    tags = [w["tag"] for w in db.list_workers()]
    assert tags[0] == "wk-master"


def test_duplicate_tag_does_not_create_a_second_worker():
    first = _worker("wk-a")
    second = _worker("wk-a")
    assert first == second
    assert len(db.list_workers()) == 1


def test_enabled_filter_and_toggle():
    wid = _worker("wk-a")
    assert len(db.list_enabled_workers()) == 1
    db.set_worker_enabled(wid, False)
    assert db.list_enabled_workers() == []
    db.set_worker_enabled(wid, True)
    assert len(db.list_enabled_workers()) == 1


def test_health_update_is_stored():
    wid = _worker("wk-a")
    db.update_worker_health(wid, "ok", 340, 1)
    w = db.get_worker(wid)
    assert w["status"] == "ok" and w["ping_ms"] == 340 and w["file_ok"] == 1
    assert w["last_checked"]


def test_deleting_a_worker_detaches_its_accounts(alice):
    wid = _worker("wk-a")
    aid = db.add_account(alice, "09120000001", worker_id=wid)
    db.incr_worker_sent(wid, 5)
    db.delete_worker(wid)
    assert db.get_worker(wid) is None
    assert db.get_account(alice, aid)["worker_id"] is None
    assert db.worker_sent_today(wid) == 0


# --------------------------------------------------------------------------- #
# Worker statistics — the card the owner asked for
# --------------------------------------------------------------------------- #
def test_worker_stats_split_healthy_and_dead(alice, bob):
    wid = _worker("wk-a")
    a1 = db.add_account(alice, "09120000001", worker_id=wid)
    a2 = db.add_account(alice, "09120000002", worker_id=wid)
    b1 = db.add_account(bob, "09130000001", worker_id=wid)
    db.set_status(alice, a2, "quarantined")

    stats = db.worker_account_stats(wid)
    assert stats["total"] == 3
    assert stats["healthy"] == 2
    assert stats["dead"] == 1
    assert stats["customers"] == 2      # accounts from two different customers
    assert a1 and b1


def test_worker_customers_lists_who_is_affected(alice, bob):
    """When a worker's IP gets throttled the owner needs to know whom to warn."""
    wid = _worker("wk-a")
    db.add_account(alice, "09120000001", worker_id=wid)
    db.add_account(alice, "09120000002", worker_id=wid)
    db.add_account(bob, "09130000001", worker_id=wid)

    rows = db.worker_customers(wid)
    assert [r["telegram_id"] for r in rows] == [alice, bob]   # busiest first
    assert rows[0]["accounts"] == 2


def test_accounts_per_worker_overview(alice):
    w1 = _worker("wk-a")
    w2 = _worker("wk-b")
    db.add_account(alice, "09120000001", worker_id=w1)
    db.incr_worker_sent(w1, 12)

    overview = {row["tag"]: row for row in db.accounts_per_worker()}
    assert overview["wk-a"]["total"] == 1
    assert overview["wk-a"]["sent_today"] == 12
    assert overview["wk-b"]["total"] == 0


def test_sent_today_accumulates_and_is_per_worker():
    w1, w2 = _worker("wk-a"), _worker("wk-b")
    db.incr_worker_sent(w1, 3)
    db.incr_worker_sent(w1, 4)
    db.incr_worker_sent(w2, 1)
    assert db.worker_sent_today(w1) == 7
    assert db.worker_sent_today(w2) == 1


def test_incr_worker_sent_tolerates_no_worker():
    db.incr_worker_sent(None, 5)        # local/unassigned account: must not raise


# --------------------------------------------------------------------------- #
# Round-robin placement pointer
# --------------------------------------------------------------------------- #
def test_round_robin_cycles_and_persists():
    """A per-process pointer resets on restart and sends everyone to worker #1;
    this one lives in the database."""
    seen = [db.fleet_rr_next(3) for _ in range(7)]
    assert seen == [0, 1, 2, 0, 1, 2, 0]


def test_round_robin_handles_a_shrinking_pool():
    db.fleet_rr_next(5)
    db.fleet_rr_next(5)
    assert db.fleet_rr_next(1) == 0     # pool of one -> always index 0


def test_round_robin_never_returns_out_of_range():
    for size in (1, 2, 3, 7):
        for _ in range(10):
            assert 0 <= db.fleet_rr_next(size) < size


# --------------------------------------------------------------------------- #
# Owner-side aggregate reads
# --------------------------------------------------------------------------- #
def test_owner_account_totals_covers_both_platforms(alice, bob):
    a1 = db.add_account(alice, "09120000001")
    db.add_account(bob, "09130000001")
    db.tg_add_account(alice, "09120000009")
    db.incr_account_sent(alice, a1, 25)

    totals = db.owner_account_totals()
    assert totals["rubika"]["total"] == 2
    assert totals["rubika"]["sent"] == 25
    assert totals["telegram"]["total"] == 1


def test_owner_customer_counts(alice, bob):
    db.add_days(alice, 30)
    db.set_expiry(bob, "2020-01-01 00:00:00")
    counts = db.owner_count_customers()
    assert counts["total"] == 2
    assert counts["active"] == 1
    assert counts["expired"] == 1
    db.set_blocked(bob, True)
    assert db.owner_count_customers()["blocked"] == 1


def test_owner_search_by_id_username_and_phone(alice):
    db.add_account(alice, "09121234567")
    assert [c["telegram_id"] for c in db.owner_search_customers("alice")] == [alice]
    assert [c["telegram_id"] for c in db.owner_search_customers(str(alice))] == [alice]
    assert [c["telegram_id"] for c in db.owner_search_customers("1234567")] == [alice]
    assert db.owner_search_customers("nobody-here") == []


def test_owner_search_finds_by_telegram_account_phone(alice):
    db.tg_add_account(alice, "09129998877")
    assert [c["telegram_id"] for c in db.owner_search_customers("9998877")] == [alice]


def test_locate_phone_reports_owner_and_platform(alice):
    """The one-tap diagnosis: which customer, which platform, which worker."""
    wid = _worker("wk-a")
    db.add_account(alice, "09121234567", worker_id=wid)
    db.tg_add_account(alice, "09121234567")

    found = db.owner_locate_phone("09121234567")
    assert len(found) == 2
    platforms = {row["platform"] for row in found}
    assert platforms == {"rubika", "telegram"}
    rb = next(r for r in found if r["platform"] == "rubika")
    assert rb["customer_id"] == alice
    assert rb["customer_name"] == "Alice"
    assert rb["worker_id"] == wid


def test_locate_phone_with_no_digits_returns_nothing():
    assert db.owner_locate_phone("hello") == []


def test_usage_chart_data_is_oldest_first(alice):
    db.usage_incr(alice, "send", 5)
    series = db.owner_usage_last_days(7, "send")
    assert series and series[-1][1] == 5


# --------------------------------------------------------------------------- #
# Notification outbox (owner -> customer, delivered by the customer bot)
# --------------------------------------------------------------------------- #
def test_notifications_queue_and_drain(alice, bob):
    db.queue_notification(alice, "hello alice")
    db.queue_notification(bob, "hello bob")
    pending = db.fetch_unsent_notifications()
    assert len(pending) == 2
    for row in pending:
        db.mark_notification_sent(row["id"])
    assert db.fetch_unsent_notifications() == []


def test_notifications_are_delivered_in_order(alice):
    for i in range(5):
        db.queue_notification(alice, f"msg {i}")
    texts = [n["text"] for n in db.fetch_unsent_notifications()]
    assert texts == [f"msg {i}" for i in range(5)]


# --------------------------------------------------------------------------- #
# Paused sends — "continue" instead of re-messaging everyone
# --------------------------------------------------------------------------- #
def test_paused_send_round_trip(alice):
    aid = db.add_account(alice, "09120000001")
    db.save_paused_send(alice, aid, "09120000001", {"rest": ["a", "b"], "done": 40})
    rec = db.get_paused_send(alice, aid)
    assert rec["payload"]["done"] == 40
    assert rec["payload"]["rest"] == ["a", "b"]
    db.delete_paused_send(alice, aid)
    assert db.get_paused_send(alice, aid) is None


def test_paused_send_is_not_readable_by_another_customer(alice, bob):
    aid = db.add_account(alice, "09120000001")
    db.save_paused_send(alice, aid, "09120000001", {"rest": []})
    assert db.get_paused_send(bob, aid) is None


def test_saving_twice_overwrites_the_same_account(alice):
    aid = db.add_account(alice, "09120000001")
    db.save_paused_send(alice, aid, "p", {"done": 1})
    db.save_paused_send(alice, aid, "p", {"done": 2})
    assert db.get_paused_send(alice, aid)["payload"]["done"] == 2


# --------------------------------------------------------------------------- #
# Number-status cache (global on purpose)
# --------------------------------------------------------------------------- #
def test_number_cache_avoids_reprobing():
    db.number_record("09120000001", True)
    db.number_record("09120000002", False)
    assert db.number_seen("09120000001")["on_rubika"] == 1
    assert db.number_seen("09120000002")["on_rubika"] == 0
    assert db.number_seen("09120000003") is None
    known = db.numbers_known(["09120000001", "09120000002", "09120000003"])
    assert known == {"09120000001", "09120000002"}


def test_numbers_known_handles_large_batches():
    phones = [f"0912{i:07d}" for i in range(1000)]
    for p in phones[:600]:
        db.number_record(p, True)
    known = db.numbers_known(phones)
    assert len(known) == 600


def test_numbers_known_with_empty_input():
    assert db.numbers_known([]) == set()


# --------------------------------------------------------------------------- #
# central_db — owner only, and mirrored to a flag file
# --------------------------------------------------------------------------- #
def test_maintenance_mirrors_to_a_flag_file():
    """The customer bot must honour maintenance WITHOUT opening this database."""
    path = central_db.maintenance_flag_path()
    assert central_db.get_maintenance() is False
    assert not os.path.exists(path)

    central_db.set_notice("back in 10 minutes")
    central_db.set_maintenance(True)
    assert central_db.get_maintenance() is True
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        assert "10 minutes" in fh.read()

    central_db.set_maintenance(False)
    assert not os.path.exists(path)


def test_audit_log_records_and_reads_back():
    central_db.audit("add_time", "Alice +30d")
    central_db.audit("block", "Bob")
    rows = central_db.list_audit()
    assert [r["action"] for r in rows] == ["block", "add_time"]   # newest first
    assert rows[1]["detail"] == "Alice +30d"


def test_broadcast_history():
    central_db.record_broadcast("hello", "active", 28)
    row = central_db.list_broadcasts()[0]
    assert row["queued"] == 28 and row["audience"] == "active"


def test_tickets_open_then_answered(alice):
    tid = central_db.add_ticket(alice, "my accounts stopped working")
    assert central_db.count_open_tickets() == 1
    central_db.answer_ticket(tid, "checked, please re-login")
    assert central_db.count_open_tickets() == 0
    ticket = central_db.get_ticket(tid)
    assert ticket["answered"] == 1 and ticket["answer"].startswith("checked")


def test_backup_bookkeeping():
    assert central_db.get_last_backup() == ""
    central_db.set_last_backup()
    assert central_db.get_last_backup() != ""


# --------------------------------------------------------------------------- #
# Card style must not drift
# --------------------------------------------------------------------------- #
def test_divider_is_exactly_31_dashes():
    assert cards.LINE == "-" * 31


def test_card_shape():
    out = cards.card("🤖 Bot Panel", ["a", "b"])
    assert out.splitlines() == ["🤖 Bot Panel", cards.LINE, "a", "b"]


def test_panel_card_wraps_with_dividers_and_footer():
    out = cards.panel_card("✅ - #login", ["x"], footer="--| worker")
    lines = out.splitlines()
    assert lines[0] == "| ✅ - #login"
    assert lines[1] == cards.LINE
    assert lines[-2] == cards.LINE
    assert lines[-1] == "--| worker"


def test_kv_aligns_and_num_formats():
    assert cards.kv("Status", "OK", width=8) == "• Status  : OK"
    assert cards.num(1234567) == "1,234,567"
    assert cards.num("n/a") == "n/a"


def test_bar_and_dot():
    assert cards.bar(0, 10) == "░" * 10
    assert cards.bar(10, 10) == "█" * 10
    assert cards.bar(5, 10).count("█") == 5
    assert cards.bar(1, 0) == "█" * 10          # zero total must not divide by zero
    assert cards.dot(True) == "🟢"
    assert cards.dot(False) == "🔴"
    assert cards.dot(None) == "⚪️"


def test_paginate_matches_the_requested_page_size():
    class _B:
        @staticmethod
        def inline(text, data):
            return (text, data)

    items = list(range(23))
    page0, nav0, idx0, total = cards.paginate(items, 0, "p_", _B, per_page=10)
    assert len(page0) == 10 and idx0 == 0 and total == 3
    assert len(nav0) == 1                       # first page: only "next"

    page1, nav1, _, _ = cards.paginate(items, 1, "p_", _B, per_page=10)
    assert page1[0] == 10 and len(nav1) == 2    # middle: both

    page2, nav2, idx2, _ = cards.paginate(items, 99, "p_", _B, per_page=10)
    assert idx2 == 2 and len(page2) == 3 and len(nav2) == 1


def test_paginate_with_no_items():
    class _B:
        @staticmethod
        def inline(text, data):
            return (text, data)

    page, nav, idx, total = cards.paginate([], 0, "p_", _B)
    assert page == [] and nav == [] and idx == 0 and total == 1


def test_schema_version_is_recorded():
    assert db.schema_version() == db.SCHEMA_VERSION
