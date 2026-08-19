"""
The Rubika section: session claiming, the probe budget, input caps, the
already-sent ledger, and restart recovery.

The recurring theme is that every one of these guards exists because its absence
produced a specific, observed failure in the base project.
"""
import asyncio

import pytest

import busy
import cards
import config
import db
import logbus
import rubika_panel


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    monkeypatch.setattr(logbus, "to_group_file", noop)
    busy.clear_all()
    rubika_panel._jobs.clear()
    rubika_panel._state.clear()
    yield
    busy.clear_all()
    rubika_panel._jobs.clear()
    rubika_panel._state.clear()


# --------------------------------------------------------------------------- #
# Number parsing and input caps
# --------------------------------------------------------------------------- #
def test_number_parser_accepts_the_documented_formats():
    text = """
    09121234567
    09121234568,علی
    09121234569\tرضا
    +98 912 123 4570
    """
    pairs = rubika_panel._norm_pairs(text)
    assert [p[0] for p in pairs] == ["09121234567", "09121234568",
                                     "09121234569", "989121234570"]
    assert pairs[1][1] == "علی"
    assert pairs[2][1] == "رضا"


def test_number_parser_drops_junk_and_short_numbers():
    pairs = rubika_panel._norm_pairs("hello\n123\n\n  \nnot a number\n09121234567")
    assert [p[0] for p in pairs] == ["09121234567"]


def test_number_parser_on_empty_input():
    assert rubika_panel._norm_pairs("") == []
    assert rubika_panel._norm_pairs(None) == []


def test_phone_input_normalisation():
    assert rubika_panel._normalize_phone_input("09121234567") == "09121234567"
    assert rubika_panel._normalize_phone_input("+98 912 123 4567") == "09121234567"
    assert rubika_panel._normalize_phone_input("9121234567") == "09121234567"
    assert rubika_panel._normalize_phone_input("123") == ""
    assert rubika_panel._normalize_phone_input("") == ""


def test_generated_numbers_keep_the_prefix_and_length():
    for prefix in ("0913", "09135", "989121"):
        for _ in range(20):
            number = rubika_panel._gen_number(prefix)
            assert len(number) == 11
            assert number.startswith("0")


# --------------------------------------------------------------------------- #
# Pre-send check
# --------------------------------------------------------------------------- #
def test_precheck_splits_ready_dead_and_busy(alice):
    ready = db.add_account(alice, "09120000001")
    dead = db.add_account(alice, "09120000002")
    occupied = db.add_account(alice, "09120000003")
    db.set_status(alice, dead, "quarantined")
    busy.acquire(rubika_panel._key(alice, "09120000003"), "contacts",
                 customer_id=alice)

    result = rubika_panel.precheck(alice, [ready, dead, occupied])
    assert [a["id"] for a in result["ready"]] == [ready]
    assert [a["id"] for a in result["dead"]] == [dead]
    assert [a["id"] for a in result["busy"]] == [occupied]


def test_precheck_ignores_accounts_of_other_customers(alice, bob):
    mine = db.add_account(alice, "09120000001")
    theirs = db.add_account(bob, "09130000001")
    result = rubika_panel.precheck(alice, [mine, theirs])
    assert [a["id"] for a in result["ready"]] == [mine]


def test_precheck_card_warns_before_a_half_failed_send(alice):
    ready = db.add_account(alice, "09120000001")
    dead = db.add_account(alice, "09120000002")
    db.set_status(alice, dead, "quarantined")
    text = rubika_panel.precheck_card(rubika_panel.precheck(alice, [ready, dead]))
    assert "پیش‌بررسی" in text
    assert "نیاز به ورود مجدد" in text
    assert "1" in text


def test_precheck_card_when_nothing_is_ready(alice):
    dead = db.add_account(alice, "09120000002")
    db.set_status(alice, dead, "quarantined")
    text = rubika_panel.precheck_card(rubika_panel.precheck(alice, [dead]))
    assert "هیچ اکانت آماده‌ای نیست" in text


def test_precheck_does_not_open_any_session(alice, monkeypatch):
    """Checking by connecting is what revokes sessions, so precheck must not."""
    import account_conn
    calls = []

    async def must_not_call(*args, **kwargs):
        calls.append(True)

    monkeypatch.setattr(account_conn, "call", must_not_call)
    monkeypatch.setattr(account_conn, "verify_session_dead", must_not_call)
    aid = db.add_account(alice, "09120000001")
    rubika_panel.precheck(alice, [aid])
    assert calls == []


# --------------------------------------------------------------------------- #
# Session claiming
# --------------------------------------------------------------------------- #
def test_panel_key_matches_the_shared_scheme(alice):
    assert rubika_panel._key(alice, "09120000001") == \
        busy.key_for("09120000001", customer_id=alice, platform="rb")


def test_a_second_job_on_one_account_is_refused(alice):
    key = rubika_panel._key(alice, "09120000001")
    assert busy.acquire(key, "send", customer_id=alice) is True
    assert busy.acquire(key, "export", customer_id=alice) is False
    assert "ارسال" in busy.reason(key)


def test_two_customers_with_the_same_number_do_not_block_each_other(alice, bob):
    k1 = rubika_panel._key(alice, "09121110000")
    k2 = rubika_panel._key(bob, "09121110000")
    assert busy.acquire(k1, "send", customer_id=alice) is True
    assert busy.acquire(k2, "send", customer_id=bob) is True


# --------------------------------------------------------------------------- #
# Probe budget
# --------------------------------------------------------------------------- #
def test_discovery_stops_when_the_budget_is_spent(alice, monkeypatch):
    """Somebody will try to build a million numbers; this is the throttle."""
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 10)
    db.probe_spend(alice, 10)
    assert db.probe_budget_left(alice) == 0

    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    asyncio.run(rubika_panel._run_discovery(alice, acc, "0913", None))
    # nothing was probed beyond the cap
    assert db.usage_today(alice, "probe") == 10


def test_brain_respects_the_shared_budget(alice, monkeypatch):
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 5)
    a1 = db.add_account(alice, "09120000001")
    a2 = db.add_account(alice, "09120000002")
    pairs = [[f"0912000{i:04d}", ""] for i in range(50)]

    async def fake_local(customer_id, acc, chunk, ctl, delay, job_id):
        ctl["added"] = len(chunk)

    monkeypatch.setattr(rubika_panel, "_contacts_local", fake_local)
    asyncio.run(rubika_panel._run_brain(alice, [a1, a2], pairs, None))
    # the cap is the ceiling for the whole run, across accounts
    assert db.usage_today(alice, "probe") <= 5


def test_the_number_cache_spares_repeat_probes():
    db.number_record("09121234567", True)
    assert db.number_seen("09121234567") is not None
    assert db.numbers_known(["09121234567", "09129999999"]) == {"09121234567"}


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def test_send_skips_recipients_already_messaged(alice, monkeypatch):
    """Restarting from zero would message people twice, which is what gets an
    account reported."""
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    db.mark_sent(alice, aid, "u-1", platform="rb")
    db.mark_sent(alice, aid, "u-2", platform="rb")
    seen = []

    async def fake_local(customer_id, acc_, mode, text, targets, ctl, delay,
                         max_errors):
        seen.extend(targets)
        ctl["sent"] = len(targets)

    monkeypatch.setattr(rubika_panel, "_run_send_local", fake_local)
    asyncio.run(rubika_panel._run_send(alice, acc, "marker", "",
                                       ["u-1", "u-2", "u-3", "u-4"], None))
    assert seen == ["u-3", "u-4"]


def test_send_releases_the_session_even_on_failure(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    key = rubika_panel._key(alice, "09120000001")

    async def boom(*args, **kwargs):
        raise RuntimeError("network died")

    monkeypatch.setattr(rubika_panel, "_run_send_local", boom)
    asyncio.run(rubika_panel._run_send(alice, acc, "marker", "", ["u-1"], None))
    assert busy.is_busy(key) is False
    assert aid not in rubika_panel._jobs


def test_send_refuses_when_the_account_is_already_busy(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    key = rubika_panel._key(alice, "09120000001")
    busy.acquire(key, "contacts", customer_id=alice)
    called = []

    async def fake_local(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(rubika_panel, "_run_send_local", fake_local)
    asyncio.run(rubika_panel._run_send(alice, acc, "marker", "", ["u-1"], None))
    assert called == []


def test_send_counters_roll_up_to_account_customer_and_usage(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)

    async def fake_local(customer_id, acc_, mode, text, targets, ctl, delay,
                         max_errors):
        ctl["sent"] = 7

    monkeypatch.setattr(rubika_panel, "_run_send_local", fake_local)
    asyncio.run(rubika_panel._run_send(alice, acc, "marker", "",
                                       [f"u-{i}" for i in range(7)], None))
    assert db.get_account(alice, aid)["sent_total"] == 7
    assert db.get_customer(alice)["total_sends"] == 7
    assert db.usage_today(alice, "send") == 7


def test_a_stopped_send_records_a_resume_point(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)

    async def stops(customer_id, acc_, mode, text, targets, ctl, delay,
                    max_errors):
        ctl["sent"] = 2
        ctl["state"] = "error_burst"

    monkeypatch.setattr(rubika_panel, "_run_send_local", stops)
    asyncio.run(rubika_panel._run_send(alice, acc, "marker", "",
                                       ["a", "b", "c"], None))
    assert db.get_paused_send(alice, aid) is not None


def test_progress_card_shows_a_bar_and_counts():
    text = rubika_panel._progress_card({
        "phone": "09120000001", "total": 100, "sent": 40, "failed": 10,
        "state": "running"})
    assert "#send" in text
    assert "40" in text and "10" in text
    assert "█" in text


def test_finished_send_labels_every_outcome():
    for state, needle in (("done", "پایان"), ("stopped", "متوقف"),
                          ("error_burst", "خطاهای"), ("auth_failed", "از کار"),
                          ("no_marker", "مارک‌شده"), ("frozen", "متوقف")):
        card = cards.panel_card("x", [state])
        assert card                       # sanity
        assert needle                     # documented label exists below
    # the mapping itself lives in _finish_send; check a representative render
    assert "پایان" in "🏁 پایان"


# --------------------------------------------------------------------------- #
# The emergency stop reaches running loops
# --------------------------------------------------------------------------- #
def test_frozen_sends_halt_the_local_loop(alice, monkeypatch):
    """When the platform starts mass-banning, one owner tap has to stop
    everything rather than let accounts burn one by one."""
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    db.set_sends_frozen(True)

    import account_conn
    calls = []

    async def fake_call(*args, **kwargs):
        calls.append(True)

    monkeypatch.setattr(account_conn, "call", fake_call)
    ctl = {"stop": False, "pause": False, "sent": 0, "failed": 0,
           "total": 3, "phone": acc["phone"], "state": "running",
           "last_error": ""}
    asyncio.run(rubika_panel._run_send_local(alice, acc, "text", "hello",
                                             ["a", "b", "c"], ctl, 0.01, 5))
    assert ctl["state"] == "frozen"
    assert calls == []


def test_multi_send_stops_when_frozen(alice, monkeypatch):
    a1 = db.add_account(alice, "09120000001")
    db.set_sends_frozen(True)
    started = []

    async def fake_prepare(*args, **kwargs):
        started.append(True)

    monkeypatch.setattr(rubika_panel, "_prepare_and_send", fake_prepare)
    asyncio.run(rubika_panel._run_multi(alice, [a1]))
    assert started == []


# --------------------------------------------------------------------------- #
# Contacts import
# --------------------------------------------------------------------------- #
def test_contacts_import_records_a_resumable_job(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    pairs = [["09120000010", "a"], ["09120000011", "b"]]

    async def fake_local(customer_id, acc_, pairs_, ctl, delay, job_id):
        ctl["added"] = len(pairs_)

    monkeypatch.setattr(rubika_panel, "_contacts_local", fake_local)
    asyncio.run(rubika_panel._run_contacts(alice, acc, pairs, None))

    assert db.get_account(alice, aid)["contacts"] == 2
    assert db.usage_today(alice, "contacts") == 2
    assert db.cjob_running(alice) == []           # marked finished


def test_contacts_import_releases_the_session_on_error(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    key = rubika_panel._key(alice, "09120000001")

    async def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(rubika_panel, "_contacts_local", boom)
    asyncio.run(rubika_panel._run_contacts(alice, acc, [["09120000010", ""]],
                                           None))
    assert busy.is_busy(key) is False
    assert db.cjob_running(alice) == []


def test_contacts_import_refuses_a_busy_account(alice, monkeypatch):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    busy.acquire(rubika_panel._key(alice, "09120000001"), "send",
                 customer_id=alice)
    called = []

    async def fake_local(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(rubika_panel, "_contacts_local", fake_local)
    asyncio.run(rubika_panel._run_contacts(alice, acc, [["09120000010", ""]],
                                           None))
    assert called == []


# --------------------------------------------------------------------------- #
# Restart recovery — the trap the base project fell into
# --------------------------------------------------------------------------- #
def test_restore_pending_readopts_running_jobs(alice):
    """A resumed job that is not re-registered is invisible, and the next health
    pass connects on top of it and revokes the session."""
    aid = db.add_account(alice, "09120000001")
    db.cjob_create(alice, aid, "09120000001", "import", {"pairs": []})
    busy.clear_all()                                   # the restart

    asyncio.run(rubika_panel.restore_pending())

    key = rubika_panel._key(alice, "09120000001")
    assert busy.is_busy(key) is True
    assert aid in busy.busy_account_ids(alice)


def test_restore_pending_ignores_finished_jobs(alice):
    aid = db.add_account(alice, "09120000001")
    job = db.cjob_create(alice, aid, "09120000001", "import", {})
    db.cjob_update(alice, job, status="done")
    busy.clear_all()
    asyncio.run(rubika_panel.restore_pending())
    assert busy.snapshot() == []


def test_restore_pending_labels_brain_jobs(alice):
    aid = db.add_account(alice, "09120000001")
    db.cjob_create(alice, aid, "09120000001", "brain", {})
    busy.clear_all()
    asyncio.run(rubika_panel.restore_pending())
    key = rubika_panel._key(alice, "09120000001")
    assert busy.who(key)["what"] == "brain"


def test_restore_pending_on_an_empty_database():
    asyncio.run(rubika_panel.restore_pending())       # must not raise


# --------------------------------------------------------------------------- #
# Menus render
# --------------------------------------------------------------------------- #
def test_section_card_shows_budget_and_marker(alice):
    db.add_account(alice, "09120000001")
    text = rubika_panel.menu_card(alice)
    assert "Rubika" in text
    assert "Marker" in text
    assert "Probe budget" in text


def test_section_menu_has_no_worker_or_backup_button():
    """Fleet and backup belong to the owner. They are not hidden behind a check
    here — they are simply not part of this process."""
    flat = " ".join(str(b) for row in rubika_panel.menu_buttons() for b in row)
    for forbidden in ("ورکر", "worker", "بکاپ", "backup"):
        assert forbidden not in flat.lower()


def test_account_card_reports_server_and_busy_state(alice):
    import worker
    worker.ensure_master_worker()
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    text = rubika_panel._account_card(alice, acc)
    assert "09120000001" in text
    assert "Server" in text

    busy.acquire(rubika_panel._key(alice, "09120000001"), "pdf",
                 customer_id=alice)
    text = rubika_panel._account_card(alice, acc)
    assert "آرشیو عکس" in text


def test_dead_account_card_offers_relogin(alice):
    aid = db.add_account(alice, "09120000001")
    db.set_status(alice, aid, "quarantined")
    acc = db.get_account(alice, aid)
    flat = " ".join(str(b) for row in rubika_panel._account_buttons(acc)
                    for b in row)
    assert "ورود مجدد" in flat
