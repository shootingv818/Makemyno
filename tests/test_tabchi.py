"""
Tabchi and the secretary: the always-on engines.

The recurring risk with an always-on feature is that it keeps doing the wrong
thing forever — posting to a group that ejected us, answering the same person on
every pass, or opening a second connection on top of a live send. Each test here
pins one of those down.
"""
import asyncio

import pytest

import busy
import cards
import config
import db
import logbus
import tabchi


@pytest.fixture(autouse=True)
def silent(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    monkeypatch.setattr(logbus, "to_group_file", noop)
    busy.clear_all()
    tabchi._tabchi_tasks.clear()
    tabchi._secretary_tasks.clear()
    yield
    busy.clear_all()
    tabchi._tabchi_tasks.clear()
    tabchi._secretary_tasks.clear()


def _account(cid, phone="09120000001"):
    return db.add_account(cid, phone, name="acc")


# --------------------------------------------------------------------------- #
# Text rotation
# --------------------------------------------------------------------------- #
def test_rotation_never_repeats_the_previous_text():
    """An identical message on a fixed schedule is the easiest bot signature to
    spot, so avoiding the immediate repeat is a safety property, not polish."""
    texts = ["a", "b", "c"]
    last = None
    for _ in range(200):
        idx, text = tabchi._pick_text(texts, last)
        assert idx != last, "the same text was picked twice in a row"
        assert text == texts[idx]
        last = idx


def test_rotation_with_one_text_is_stable():
    assert tabchi._pick_text(["only"], None) == (0, "only")
    assert tabchi._pick_text(["only"], 0) == (0, "only")


def test_rotation_with_no_texts_returns_nothing():
    assert tabchi._pick_text([], None) == (None, None)


# --------------------------------------------------------------------------- #
# A pass refuses to run without the things it needs
# --------------------------------------------------------------------------- #
def test_pass_without_texts_does_nothing(alice):
    aid = _account(alice)
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    acc = db.get_account(alice, aid)
    result = asyncio.run(tabchi._tabchi_pass(alice, acc))
    assert result["reason"] == "no_texts"
    assert result["sent"] == 0


def test_pass_without_joined_groups_does_nothing(alice):
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")   # not joined
    acc = db.get_account(alice, aid)
    result = asyncio.run(tabchi._tabchi_pass(alice, acc))
    assert result["reason"] == "no_groups"


def test_pass_yields_when_the_session_is_already_busy(alice):
    """THE CENTRAL GUARANTEE: a tabchi pass never opens a second connection on an
    account that is mid-send. That collision is what revoked sessions in the base
    project, and the whole busy registry exists to make it impossible."""
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    gid = db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    groups = db.tabchi_groups(alice, aid)
    db.tabchi_group_joined(alice, groups[0]["id"], "g0")
    acc = db.get_account(alice, aid)

    key = tabchi._key(alice, acc["phone"])
    assert busy.acquire(key, "send", customer_id=alice)
    try:
        result = asyncio.run(tabchi._tabchi_pass(alice, acc))
    finally:
        busy.release(key, "send")
    assert result["reason"] == "busy"
    assert result["sent"] == 0


# --------------------------------------------------------------------------- #
# Failing groups get muted automatically
# --------------------------------------------------------------------------- #
def test_a_group_that_keeps_failing_is_muted(alice):
    """Retrying a group that kicked us out, every interval, forever, is how an
    account gets flagged. After a few failures the group is dropped from the
    rotation and the customer can un-mute it deliberately."""
    aid = _account(alice)
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    group = db.tabchi_groups(alice, aid)[0]
    db.tabchi_group_joined(alice, group["id"], "g0")

    fails = 0
    for _ in range(config.TABCHI_GROUP_MAX_FAILS):
        fails = db.tabchi_group_fail(alice, group["id"])
    assert fails >= config.TABCHI_GROUP_MAX_FAILS

    assert db.tabchi_groups(alice, aid)[0]["muted"]
    # A muted group is out of the send rotation.
    assert db.tabchi_groups(alice, aid, joined_only=True) == []


def test_a_success_clears_the_failure_streak(alice):
    """The counter must measure a streak, not a lifetime total, or a group that
    fails once a month eventually mutes itself for no reason."""
    aid = _account(alice)
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    group = db.tabchi_groups(alice, aid)[0]
    db.tabchi_group_joined(alice, group["id"], "g0")

    db.tabchi_group_fail(alice, group["id"])
    db.tabchi_group_ok(alice, group["id"])
    for _ in range(config.TABCHI_GROUP_MAX_FAILS - 1):
        db.tabchi_group_fail(alice, group["id"])
    assert not db.tabchi_groups(alice, aid)[0]["muted"]


def test_unmute_restores_the_group(alice):
    aid = _account(alice)
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    group = db.tabchi_groups(alice, aid)[0]
    db.tabchi_group_joined(alice, group["id"], "g0")
    for _ in range(config.TABCHI_GROUP_MAX_FAILS):
        db.tabchi_group_fail(alice, group["id"])
    assert db.tabchi_groups(alice, aid)[0]["muted"]

    restored = db.tabchi_unmute_all(alice, aid)
    assert restored == 1
    assert not db.tabchi_groups(alice, aid)[0]["muted"]
    assert len(db.tabchi_groups(alice, aid, joined_only=True)) == 1


# --------------------------------------------------------------------------- #
# Interval clamping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("asked", [0, 1, -50, 5])
def test_interval_is_clamped_up_to_the_floor(alice, asked):
    """A customer typing "5 seconds" is asking to be rate-limited by the
    platform, so the floor is enforced in the database, not in the UI."""
    aid = _account(alice)
    db.tabchi_set(alice, aid, interval_sec=asked)
    assert db.tabchi_get(alice, aid)["interval_sec"] >= config.TABCHI_MIN_INTERVAL


def test_interval_is_clamped_down_to_the_ceiling(alice):
    aid = _account(alice)
    db.tabchi_set(alice, aid, interval_sec=99999999)
    assert db.tabchi_get(alice, aid)["interval_sec"] <= config.TABCHI_MAX_INTERVAL


# --------------------------------------------------------------------------- #
# Groups and texts are per account, per customer
# --------------------------------------------------------------------------- #
def test_group_lists_do_not_leak_between_customers(alice, bob):
    """The base project pooled every successfully-joined link and made every
    other account join it. In a multi-tenant service that hands one customer's
    groups to everyone else and gets the whole fleet banned together."""
    a_id = _account(alice, "09120000001")
    b_id = _account(bob, "09120000002")
    db.tabchi_add_group(alice, a_id, "https://rubika.ir/joing/ALICE")
    db.tabchi_add_text(alice, a_id, "alice text")

    assert db.tabchi_groups(bob, b_id) == []
    assert db.tabchi_texts(bob, b_id) == []


def test_one_customer_cannot_read_another_accounts_tabchi(alice, bob):
    a_id = _account(alice, "09120000001")
    db.tabchi_add_text(alice, a_id, "secret")
    # bob asking about alice's account id gets nothing, because get_account
    # proves ownership first.
    assert db.get_account(bob, a_id) is None


def test_duplicate_links_are_rejected(alice):
    aid = _account(alice)
    assert db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    assert not db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    assert len(db.tabchi_groups(alice, aid)) == 1


# --------------------------------------------------------------------------- #
# Apply to all
# --------------------------------------------------------------------------- #
def test_apply_to_all_copies_texts_but_never_group_links(alice):
    """Texts and the interval are the tedious part worth copying. Group links are
    not: a link list belongs to the account that is actually a member."""
    src = _account(alice, "09120000001")
    dst = _account(alice, "09120000002")
    db.tabchi_add_text(alice, src, "one")
    db.tabchi_add_text(alice, src, "two")
    db.tabchi_set(alice, src, interval_sec=3600)
    db.tabchi_add_group(alice, src, "https://rubika.ir/joing/SRC")

    copied = db.tabchi_apply_to_all(alice, src)
    assert copied == 1
    assert [t["text"] for t in db.tabchi_texts(alice, dst)] == ["one", "two"]
    assert db.tabchi_get(alice, dst)["interval_sec"] == 3600
    assert db.tabchi_groups(alice, dst) == [], "group links must not be copied"


def test_apply_to_all_never_reaches_another_customer(alice, bob):
    src = _account(alice, "09120000001")
    b_acc = _account(bob, "09120000002")
    db.tabchi_add_text(alice, src, "alice only")
    db.tabchi_apply_to_all(alice, src)
    assert db.tabchi_texts(bob, b_acc) == []


def test_secretary_apply_to_all_copies_the_settings(alice):
    src = _account(alice, "09120000001")
    dst = _account(alice, "09120000002")
    db.secretary_set(alice, src, text="hi there", mode="text", interval_sec=600)
    assert db.secretary_apply_to_all(alice, src) == 1
    got = db.secretary_get(alice, dst)
    assert got["text"] == "hi there"
    assert got["interval_sec"] == 600


# --------------------------------------------------------------------------- #
# The secretary answers each person exactly once
# --------------------------------------------------------------------------- #
def test_secretary_answers_a_person_only_once(alice):
    """Without this ledger the secretary re-answers every open chat on every
    pass, which reads as spam to the recipient and to the platform."""
    aid = _account(alice)
    assert not db.secretary_was_replied(alice, aid, "u-1")
    db.secretary_mark_replied(alice, aid, "u-1")
    assert db.secretary_was_replied(alice, aid, "u-1")
    # Marking twice must not raise or double-count.
    db.secretary_mark_replied(alice, aid, "u-1")
    assert db.secretary_replied_recent(alice, aid).count("u-1") == 1


def test_the_replied_ledger_is_per_account(alice):
    a1 = _account(alice, "09120000001")
    a2 = _account(alice, "09120000002")
    db.secretary_mark_replied(alice, a1, "u-1")
    assert not db.secretary_was_replied(alice, a2, "u-1"), (
        "two accounts of one customer are two different senders; each may greet "
        "the same person once")


def test_the_replied_ledger_is_per_customer(alice, bob):
    a = _account(alice, "09120000001")
    b = _account(bob, "09120000002")
    db.secretary_mark_replied(alice, a, "u-1")
    assert not db.secretary_was_replied(bob, b, "u-1")


def test_the_skip_list_sent_to_a_worker_is_capped(alice):
    """It crosses the tunnel on every pass, so it cannot grow without bound."""
    aid = _account(alice)
    for i in range(50):
        db.secretary_mark_replied(alice, aid, f"u-{i}")
    assert len(tabchi._replied_guids(alice, aid, limit=10)) == 10
    assert len(tabchi._replied_guids(alice, aid)) == 50


def test_secretary_pass_yields_when_busy(alice):
    aid = _account(alice)
    db.secretary_set(alice, aid, text="hello", mode="text")
    acc = db.get_account(alice, aid)
    key = tabchi._key(alice, acc["phone"])
    assert busy.acquire(key, "send", customer_id=alice)
    try:
        result = asyncio.run(tabchi._secretary_pass(alice, acc))
    finally:
        busy.release(key, "send")
    assert result["reason"] == "busy"


def test_secretary_in_text_mode_refuses_to_run_without_a_text(alice):
    aid = _account(alice)
    db.secretary_set(alice, aid, mode="text", text="")
    acc = db.get_account(alice, aid)
    result = asyncio.run(tabchi._secretary_pass(alice, acc))
    assert result["reason"] == "no_text"


# --------------------------------------------------------------------------- #
# Enable/disable bookkeeping and restart recovery
# --------------------------------------------------------------------------- #
def test_enabled_accounts_are_listed_for_recovery(alice, bob):
    """After a restart the service must know what to relaunch; an always-on
    feature that silently stops is indistinguishable from a broken one."""
    a = _account(alice, "09120000001")
    b = _account(bob, "09120000002")
    db.tabchi_set(alice, a, enabled=True)
    db.secretary_set(bob, b, enabled=True, text="hi")

    enabled = db.owner_tabchi_enabled()
    assert (alice, a) in [(r["customer_id"], r["account_id"]) for r in enabled]

    sec = db.owner_secretary_enabled()
    assert (bob, b) in [(r["customer_id"], r["account_id"]) for r in sec]


def test_per_customer_enabled_view_is_scoped(alice, bob):
    a = _account(alice, "09120000001")
    _account(bob, "09120000002")
    db.tabchi_set(alice, a, enabled=True)
    assert len(db.tabchi_enabled_accounts(alice)) == 1
    assert db.tabchi_enabled_accounts(bob) == []


def test_restore_engines_relaunches_enabled_accounts(alice, monkeypatch):
    a = _account(alice, "09120000001")
    db.tabchi_set(alice, a, enabled=True)
    db.secretary_set(alice, a, enabled=True, text="hi")

    started = {"tabchi": [], "secretary": []}
    monkeypatch.setattr(tabchi, "start_tabchi",
                        lambda cid, aid: started["tabchi"].append(aid))
    monkeypatch.setattr(tabchi, "start_secretary",
                        lambda cid, aid: started["secretary"].append(aid))
    asyncio.run(tabchi.restore_engines())
    assert started["tabchi"] == [a]
    assert started["secretary"] == [a]


def test_restore_engines_survives_a_broken_row(alice, monkeypatch):
    """Recovery must never be all-or-nothing at boot: one bad row cannot stop the
    service from coming up."""
    def boom():
        raise RuntimeError("db exploded")
    monkeypatch.setattr(db, "owner_tabchi_enabled", boom)
    asyncio.run(tabchi.restore_engines())      # must not raise


def test_counters_accumulate(alice):
    aid = _account(alice)
    db.tabchi_incr_sent(alice, aid, 5)
    db.tabchi_incr_sent(alice, aid, 3)
    assert db.tabchi_get(alice, aid)["sent_total"] == 8
    db.secretary_incr(alice, aid, 2)
    db.secretary_incr(alice, aid, 4)
    assert db.secretary_get(alice, aid)["replied_total"] == 6


def test_clearing_texts_and_groups_reports_what_it_removed(alice):
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "a")
    db.tabchi_add_text(alice, aid, "b")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    assert db.tabchi_clear_texts(alice, aid) == 2
    assert db.tabchi_clear_groups(alice, aid) == 1
    assert db.tabchi_texts(alice, aid) == []
    assert db.tabchi_groups(alice, aid) == []


# --------------------------------------------------------------------------- #
# Cards render without a live client
# --------------------------------------------------------------------------- #
def test_cards_render_and_keep_the_house_divider(alice):
    aid = _account(alice)
    db.tabchi_add_text(alice, aid, "hello")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    acc = db.get_account(alice, aid)

    for text in (tabchi.section_card(alice), tabchi.account_card(alice, acc),
                 tabchi.groups_card(alice, acc), tabchi.secretary_card(alice, acc)):
        assert isinstance(text, str) and text.strip()
    assert cards.LINE in tabchi.account_card(alice, acc)


def test_account_card_shows_muted_groups(alice):
    aid = _account(alice)
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/AAA")
    group = db.tabchi_groups(alice, aid)[0]
    db.tabchi_group_joined(alice, group["id"], "g0")
    for _ in range(config.TABCHI_GROUP_MAX_FAILS):
        db.tabchi_group_fail(alice, group["id"])
    acc = db.get_account(alice, aid)
    assert "muted" in tabchi.account_card(alice, acc)
    assert "🔇" in tabchi.groups_card(alice, acc)


def test_section_card_handles_a_customer_with_no_accounts(alice):
    assert isinstance(tabchi.section_card(alice), str)


# --------------------------------------------------------------------------- #
# Scope enforcement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fn,args", [
    ("tabchi_get", (1,)), ("tabchi_texts", (1,)), ("tabchi_groups", (1,)),
    ("secretary_get", (1,)), ("secretary_replied_recent", (1,)),
    ("tabchi_add_text", (1, "x")), ("tabchi_add_group", (1, "link")),
])
def test_tabchi_db_calls_refuse_a_missing_customer(fn, args):
    """The golden rule: a scoped function called without a customer raises rather
    than quietly operating across the whole service."""
    with pytest.raises(db.ScopeError):
        getattr(db, fn)(None, *args)
