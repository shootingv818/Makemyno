"""
The customer bot shell: the access gate, the start card, support, help.

The gate is the single place every screen passes through, so its ordering is the
security property worth testing hardest. If a new screen ever skips it, the
architecture test in test_architecture.py catches that separately.
"""
import asyncio
import os

import pytest

import antispam
import cards
import config
import customer_bot
import db
import help_text
import logbus


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)


@pytest.fixture(autouse=True)
def flag_in_tmp(tmp_path, monkeypatch):
    """Point the maintenance flag at the temp dir, like the real data folder."""
    monkeypatch.setattr(customer_bot, "DATA_DIR", str(tmp_path))
    yield


class _Ev:
    """Minimal event double: records what the panel said back."""

    def __init__(self, sender_id=1001, text=""):
        self.sender_id = sender_id
        self.raw_text = text
        self.replies = []
        self.file = None

    async def respond(self, text, **kwargs):
        self.replies.append(text)
        return None

    async def edit(self, text, **kwargs):
        self.replies.append(text)
        return None

    async def answer(self, *args, **kwargs):
        return None

    async def get_sender(self):
        class _S:
            first_name = "Alice"
            username = "alice"
        return _S()

    @property
    def said(self):
        return "\n".join(self.replies)


def _gate(event, **kwargs):
    return asyncio.run(customer_bot._gate(event, **kwargs))


# --------------------------------------------------------------------------- #
# The gate, check by check
# --------------------------------------------------------------------------- #
def test_active_customer_passes(alice):
    db.add_days(alice, 30)
    assert _gate(_Ev(alice)) is True


def test_blocked_customer_gets_absolute_silence(alice):
    """Answering a flooder costs us an API call and gives them feedback."""
    db.add_days(alice, 30)
    db.set_blocked(alice, True)
    event = _Ev(alice)
    assert _gate(event) is False
    assert event.replies == []


def test_expired_customer_is_told_and_offered_support(alice):
    db.set_expiry(alice, "2020-01-01 00:00:00")
    event = _Ev(alice)
    assert _gate(event) is False
    assert "دسترسی غیرفعال" in event.said


def test_expired_customer_may_still_reach_support(alice):
    db.set_expiry(alice, "2020-01-01 00:00:00")
    assert _gate(_Ev(alice), need_active=False) is True


def test_maintenance_blocks_everyone_and_shows_the_notice(alice, tmp_path):
    db.add_days(alice, 30)
    with open(os.path.join(str(tmp_path), "maintenance.flag"), "w",
              encoding="utf-8") as fh:
        fh.write("۱۰ دقیقه دیگر برگرد")
    event = _Ev(alice)
    assert _gate(event) is False
    assert "۱۰ دقیقه" in event.said


def test_maintenance_flag_without_a_notice_uses_a_default(alice, tmp_path):
    db.add_days(alice, 30)
    with open(os.path.join(str(tmp_path), "maintenance.flag"), "w",
              encoding="utf-8") as fh:
        fh.write("1")
    event = _Ev(alice)
    assert _gate(event) is False
    assert "بروزرسانی" in event.said


def test_offline_bot_refuses_known_customers_politely(alice):
    db.add_days(alice, 30)
    db.set_bot_online(False, by="shield", note="flood")
    event = _Ev(alice)
    assert _gate(event) is False
    assert "موقتاً" in event.said


def test_offline_bot_says_nothing_to_a_stranger():
    db.set_bot_online(False, by="shield")
    event = _Ev(99999)
    assert _gate(event) is False
    assert event.replies == []


def test_rate_limit_blocks_through_the_gate(alice, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 3)
    db.add_days(alice, 30)
    results = [_gate(_Ev(alice)) for _ in range(5)]
    assert results[:3] == [True, True, True]
    assert results[3] is False
    assert db.is_blocked(alice) is True


def test_count_action_false_does_not_consume_the_budget(alice, monkeypatch):
    """Pagination taps must not burn a customer's action budget."""
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 3)
    db.add_days(alice, 30)
    for _ in range(10):
        assert _gate(_Ev(alice), count_action=False) is True
    assert db.is_blocked(alice) is False


def test_gate_updates_last_seen(alice):
    db.add_days(alice, 30)
    db.set_expiry(alice, db._iso_after_days(30))
    before = db.get_customer(alice)["last_seen"]
    _gate(_Ev(alice))
    assert db.get_customer(alice)["last_seen"] >= before


def test_gate_order_blocked_beats_maintenance(alice, tmp_path):
    """A blocked customer must stay silent even during maintenance — the cheap
    check has to come first."""
    db.set_blocked(alice, True)
    with open(os.path.join(str(tmp_path), "maintenance.flag"), "w") as fh:
        fh.write("1")
    event = _Ev(alice)
    assert _gate(event) is False
    assert event.replies == []


# --------------------------------------------------------------------------- #
# Start card
# --------------------------------------------------------------------------- #
def test_start_card_has_both_sections_and_a_total(alice):
    a1 = db.add_account(alice, "09120000001")
    db.add_account(alice, "09120000002")
    t1 = db.tg_add_account(alice, "09120000009")
    db.incr_account_sent(alice, a1, 2093)
    db.tg_incr_sent(alice, t1, 4887)

    text = customer_bot.start_card(alice)
    assert "🤖 Bot Panel" in text
    assert cards.LINE in text
    assert "🟣 Rubika" in text and "✈️ Telegram" in text
    assert "2,093" in text and "4,887" in text
    assert "Total Sent: 6,980" in text           # the grand total
    assert "Which section do you want to open?" in text


def test_start_card_counts_healthy_separately(alice):
    a1 = db.add_account(alice, "09120000001")
    db.add_account(alice, "09120000002")
    db.set_status(alice, a1, "quarantined")
    text = customer_bot.start_card(alice)
    assert "Accounts: 2  (1 healthy)" in text


def test_start_card_on_an_empty_account(alice):
    text = customer_bot.start_card(alice)
    assert "Accounts: 0" in text
    assert "Total Sent: 0" in text


def test_start_card_shows_days_left(alice):
    db.set_expiry(alice, db._iso_after_days(12))
    assert "روز باقی" in customer_bot.start_card(alice)


def test_start_menu_offers_both_sections():
    flat = [str(b) for row in customer_bot.start_menu() for b in row]
    assert any("Rubika" in b for b in flat)
    assert any("Telegram" in b for b in flat)
    assert any("راهنما" in b for b in flat)


# --------------------------------------------------------------------------- #
# /start behaviour
# --------------------------------------------------------------------------- #
def test_start_creates_the_customer_and_grants_the_trial(monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 3)
    event = _Ev(7777)
    asyncio.run(customer_bot.start_handler(event))
    assert db.get_customer(7777) is not None
    assert db.days_left(7777) >= 2
    assert "Bot Panel" in event.said


def test_start_is_ignored_for_a_blocked_customer(alice):
    db.set_blocked(alice, True)
    event = _Ev(alice)
    asyncio.run(customer_bot.start_handler(event))
    assert event.replies == []


def test_start_during_maintenance_shows_the_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRIAL_DAYS", 3)
    with open(os.path.join(str(tmp_path), "maintenance.flag"), "w") as fh:
        fh.write("1")
    event = _Ev(8888)
    asyncio.run(customer_bot.start_handler(event))
    assert "تعمیر" in event.said


def test_start_without_a_trial_explains_and_shows_the_id(monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 0)
    event = _Ev(9999)
    asyncio.run(customer_bot.start_handler(event))
    assert "دسترسی غیرفعال" in event.said
    assert "9999" in event.said


def test_a_flood_of_new_starts_takes_the_bot_offline(monkeypatch):
    """The shield, exercised through the real handler."""
    monkeypatch.setattr(config, "TRIAL_DAYS", 3)
    monkeypatch.setattr(config, "START_FLOOD_MAX", 4)

    async def scenario():
        for uid in range(50000, 50012):
            await customer_bot.start_handler(_Ev(uid))

    asyncio.run(scenario())
    assert db.is_bot_online() is False


def test_returning_customer_never_trips_the_shield(alice, monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_MAX", 2)
    db.add_days(alice, 30)

    async def scenario():
        for _ in range(15):
            await customer_bot.start_handler(_Ev(alice))

    asyncio.run(scenario())
    assert db.is_bot_online() is True


# --------------------------------------------------------------------------- #
# Support tickets
# --------------------------------------------------------------------------- #
def test_filing_a_ticket_records_it_with_context(alice):
    db.add_days(alice, 30)
    db.add_account(alice, "09120000001")
    event = _Ev(alice, "اکانتم کار نمی‌کند، کد خطا E-1A2B3C")
    asyncio.run(customer_bot._step_ticket(event, {}))
    tickets = db.customer_tickets(alice)
    assert len(tickets) == 1
    assert "E-1A2B3C" in tickets[0]["text"]
    assert "ثبت شد" in event.said


def test_a_too_short_ticket_is_rejected(alice):
    db.add_days(alice, 30)
    event = _Ev(alice, "hi")
    asyncio.run(customer_bot._step_ticket(event, {}))
    assert db.customer_tickets(alice) == []
    assert "کوتاه" in event.said


def test_ticket_text_is_capped(alice):
    """Capped, and the customer is TOLD when it happened.

    The cap used to be a silent 2000, and the owner's card then showed only the
    first 400 characters with no marker at all — so a customer who wrote three
    paragraphs had two of them vanish and neither side knew. Both numbers moved,
    but the point of the test is the notice, not the number.
    """
    db.add_days(alice, 30)
    event = _Ev(alice, "x" * 9000)
    asyncio.run(customer_bot._step_ticket(event, {}))
    stored = db.customer_tickets(alice)[0]["text"]
    assert len(stored) <= config.TICKET_MAX
    assert "کاراکتر" in event.said, \
        "a trimmed message must say so; silence makes the customer think it arrived"


def test_a_normal_ticket_is_stored_whole(alice):
    db.add_days(alice, 30)
    body = "مشکل " * 200          # ~1000 chars, well under the cap
    event = _Ev(alice, body)
    asyncio.run(customer_bot._step_ticket(event, {}))
    assert db.customer_tickets(alice)[0]["text"] == body.strip()
    assert "کاراکتر" not in event.said


# --------------------------------------------------------------------------- #
# Notification delivery
# --------------------------------------------------------------------------- #
def test_notification_loop_delivers_and_marks_done(alice, monkeypatch):
    """The owner cannot DM a customer, so this loop is the only delivery path."""
    db.queue_notification(alice, "hello from the owner")
    sent = []

    async def fake_send(uid, text, **kwargs):
        sent.append((uid, text))

    monkeypatch.setattr(customer_bot.bot, "send_message", fake_send)

    async def scenario():
        task = asyncio.create_task(customer_bot.notification_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert sent and sent[0][0] == alice
    assert db.fetch_unsent_notifications() == []


def test_a_customer_who_blocked_the_bot_does_not_wedge_the_queue(alice, bob,
                                                                monkeypatch):
    db.queue_notification(alice, "first")
    db.queue_notification(bob, "second")
    delivered = []

    async def flaky_send(uid, text, **kwargs):
        if uid == alice:
            raise RuntimeError("bot was blocked by the user")
        delivered.append(uid)

    monkeypatch.setattr(customer_bot.bot, "send_message", flaky_send)

    async def scenario():
        task = asyncio.create_task(customer_bot.notification_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert delivered == [bob]
    assert db.fetch_unsent_notifications() == []      # both marked done


# --------------------------------------------------------------------------- #
# Dead-account notice
# --------------------------------------------------------------------------- #
def test_a_dead_session_notifies_the_customer_and_quarantines(alice, monkeypatch):
    """The base project only told the owner, so from the customer's side an
    account silently stopped working."""
    aid = db.add_account(alice, "09120000001")
    sent = []

    async def fake_send(uid, text, **kwargs):
        sent.append((uid, text))

    monkeypatch.setattr(customer_bot.bot, "send_message", fake_send)
    monkeypatch.setattr(config, "NOTIFY_CUSTOMER_ON_DEAD", True)

    asyncio.run(customer_bot._on_invalid_auth(alice, "09120000001"))

    assert db.get_account(alice, aid)["status"] == "quarantined"
    assert sent and sent[0][0] == alice
    assert "از کار افتاد" in sent[0][1]


def test_dead_account_notice_can_be_disabled(alice, monkeypatch):
    db.add_account(alice, "09120000001")
    sent = []

    async def fake_send(uid, text, **kwargs):
        sent.append(uid)

    monkeypatch.setattr(customer_bot.bot, "send_message", fake_send)
    monkeypatch.setattr(config, "NOTIFY_CUSTOMER_ON_DEAD", False)
    asyncio.run(customer_bot._on_invalid_auth(alice, "09120000001"))
    assert sent == []


def test_dead_account_notice_survives_an_unknown_phone(alice, monkeypatch):
    async def fake_send(uid, text, **kwargs):
        return None
    monkeypatch.setattr(customer_bot.bot, "send_message", fake_send)
    asyncio.run(customer_bot._on_invalid_auth(alice, "09990000000"))


# --------------------------------------------------------------------------- #
# Help
# --------------------------------------------------------------------------- #
def test_every_help_topic_renders():
    for name in help_text.topics():
        text, buttons = help_text.topic(name)
        assert cards.LINE in text
        assert buttons


def test_unknown_help_topic_degrades_gracefully():
    text, buttons = help_text.topic("nonsense")
    assert "پیدا نشد" in text
    assert buttons


def test_help_index_lists_the_topics():
    text = help_text.index_card()
    for word in ("ارسال", "اکانت", "محتوا", "مخاطبین", "خطاها"):
        assert word in text


def test_help_never_mentions_the_log_group():
    """A customer must never learn it exists."""
    blob = help_text.index_card()
    for name in help_text.topics():
        blob += help_text.topic(name)[0]
    for phrase in ("گروه لاگ", "log group", "LOG_GROUP"):
        assert phrase not in blob


def test_help_explains_the_error_code_flow():
    text, _ = help_text.topic("errors")
    assert "کد خطا" in text
    assert "پشتیبانی" in text


def test_help_explains_why_an_account_can_be_busy():
    text, _ = help_text.topic("accounts")
    assert "مشغول" in text


def test_help_states_the_probe_budget():
    text, _ = help_text.topic("discovery")
    assert cards.num(config.PROBE_DAILY_CAP) in text
