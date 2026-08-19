"""
Multi-tenant isolation — the property the whole business depends on.

If any of these fail, one customer can see or destroy another customer's
accounts, which is the single worst thing this service could do.
"""
import pytest

import db


# --------------------------------------------------------------------------- #
# The golden rule: no customer id => loud error, never a silent full-table read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [None, "", 0])
def test_scoped_readers_refuse_missing_customer_id(bad):
    with pytest.raises(db.ScopeError):
        db.list_accounts(bad)
    with pytest.raises(db.ScopeError):
        db.tg_list_accounts(bad)
    with pytest.raises(db.ScopeError):
        db.get_setting(bad, "rb_marker")


def test_scope_error_on_non_numeric_customer_id():
    with pytest.raises(db.ScopeError):
        db.list_accounts("not-a-number")


def test_every_customer_scoped_function_requires_the_id():
    """Spot-check a broad sample so a new function cannot quietly skip the guard."""
    calls = [
        (db.list_accounts, ()),
        (db.count_accounts, ()),
        (db.tg_list_accounts, ()),
        (db.tg_count_accounts, ()),
        (db.tabchi_get, (5,)),
        (db.secretary_get, (5,)),
        (db.usage_today, ("send",)),
        (db.probe_budget_left, ()),
        (db.cjob_running, ()),
        (db.sent_targets, (5,)),
        (db.tg_content_list, ()),
        (db.queue_notification, ("hi",)),
    ]
    for fn, args in calls:
        with pytest.raises(db.ScopeError):
            fn(None, *args)


# --------------------------------------------------------------------------- #
# Accounts never cross between customers
# --------------------------------------------------------------------------- #
def test_accounts_are_invisible_across_customers(alice, bob):
    db.add_account(alice, "09120000001", name="A1")
    db.add_account(alice, "09120000002", name="A2")
    db.add_account(bob, "09130000003", name="B1")

    assert [a["phone"] for a in db.list_accounts(alice)] == \
           ["09120000001", "09120000002"]
    assert [a["phone"] for a in db.list_accounts(bob)] == ["09130000003"]


def test_get_account_refuses_another_customers_id(alice, bob):
    """The ownership check that stops a replayed callback from reaching across."""
    aid = db.add_account(alice, "09120000001")
    assert db.get_account(alice, aid) is not None
    assert db.get_account(bob, aid) is None


def test_delete_account_cannot_touch_another_customer(alice, bob):
    aid = db.add_account(alice, "09120000001")
    db.delete_account(bob, aid)                      # must be a no-op
    assert db.get_account(alice, aid) is not None
    db.delete_account(alice, aid)
    assert db.get_account(alice, aid) is None


def test_two_customers_may_own_the_same_phone(alice, bob):
    """SIMs get resold. Making the number globally unique would mean the second
    customer can never add it and cannot be told why."""
    a = db.add_account(alice, "09121110000", name="mine")
    b = db.add_account(bob, "09121110000", name="also mine")
    assert a and b and a != b
    assert db.get_account(alice, a)["name"] == "mine"
    assert db.get_account(bob, b)["name"] == "also mine"


def test_same_phone_twice_for_one_customer_is_the_same_row(alice):
    first = db.add_account(alice, "09121110000", name="v1")
    second = db.add_account(alice, "09121110000", name="v2")
    assert first == second
    assert len(db.list_accounts(alice)) == 1
    assert db.get_account(alice, first)["name"] == "v2"


def test_telegram_accounts_are_isolated_too(alice, bob):
    db.tg_add_account(alice, "09120000001", name="tg-a")
    db.tg_add_account(bob, "09120000001", name="tg-b")
    assert len(db.tg_list_accounts(alice)) == 1
    assert len(db.tg_list_accounts(bob)) == 1
    assert db.tg_list_accounts(alice)[0]["name"] == "tg-a"


# --------------------------------------------------------------------------- #
# Settings: the bug that made every customer share one send text
# --------------------------------------------------------------------------- #
def test_settings_do_not_bleed_between_customers(alice, bob):
    db.set_setting(alice, "rb_marker", "SHOES")
    db.set_setting(bob, "rb_marker", "TUTORING")
    assert db.get_marker(alice) == "SHOES"
    assert db.get_marker(bob) == "TUTORING"


def test_settings_fall_back_to_defaults_per_customer(alice):
    import config
    assert db.get_marker(alice) == config.FORWARD_MARKER
    assert db.get_delay(alice) == config.clamp_delay(config.DEFAULT_DELAY)


def test_send_delay_is_clamped(alice):
    db.set_setting(alice, "send_delay", 999)
    assert db.get_delay(alice) <= 10.0
    db.set_setting(alice, "send_delay", 0.0001)
    assert db.get_delay(alice) >= 0.2


# --------------------------------------------------------------------------- #
# Anti-duplicate ledgers
# --------------------------------------------------------------------------- #
def test_sent_ledger_is_per_customer_and_per_account(alice, bob):
    a1 = db.add_account(alice, "09120000001")
    b1 = db.add_account(bob, "09130000001")
    db.mark_sent(alice, a1, "u-123")
    assert db.was_sent(alice, a1, "u-123") is True
    assert db.was_sent(bob, b1, "u-123") is False


def test_reset_sent_only_clears_the_named_account(alice):
    a1 = db.add_account(alice, "09120000001")
    a2 = db.add_account(alice, "09120000002")
    db.mark_sent(alice, a1, "x")
    db.mark_sent(alice, a2, "x")
    db.reset_sent(alice, a1)
    assert db.was_sent(alice, a1, "x") is False
    assert db.was_sent(alice, a2, "x") is True


def test_rubika_and_telegram_ledgers_are_separate(alice):
    a1 = db.add_account(alice, "09120000001")
    db.mark_sent(alice, a1, "target", platform="rb")
    assert db.was_sent(alice, a1, "target", platform="rb") is True
    assert db.was_sent(alice, a1, "target", platform="tg") is False


# --------------------------------------------------------------------------- #
# Deleting a customer must leave nothing behind
# --------------------------------------------------------------------------- #
def test_deleting_a_customer_removes_everything_they_owned(alice, bob):
    aid = db.add_account(alice, "09120000001")
    db.tg_add_account(alice, "09120000009")
    db.set_setting(alice, "rb_marker", "X")
    db.mark_sent(alice, aid, "t1")
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    db.secretary_set(alice, aid, enabled=True, text="hi")
    db.queue_notification(alice, "note")
    db.usage_incr(alice, "send", 5)
    db.save_paused_send(alice, aid, "09120000001", {"rest": [1, 2]})

    bob_aid = db.add_account(bob, "09130000001")
    db.tabchi_add_text(bob, bob_aid, "bob text")

    db.delete_customer(alice)

    assert db.get_customer(alice) is None
    assert db.list_accounts(alice) == []
    assert db.tg_list_accounts(alice) == []
    assert db.tabchi_texts(alice, aid) == []
    assert db.tabchi_groups(alice, aid) == []
    assert db.usage_today(alice, "send") == 0
    assert db.get_paused_send(alice, aid) is None
    assert db.was_sent(alice, aid, "t1") is False

    # Bob is untouched — deletion is scoped, not a table wipe.
    assert len(db.list_accounts(bob)) == 1
    assert len(db.tabchi_texts(bob, bob_aid)) == 1
