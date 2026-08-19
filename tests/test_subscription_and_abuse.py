"""
Subscription, clock tampering, rate limiting and the anti-spam shield.
"""
import asyncio
import time

import pytest

import antispam
import config
import db
import logbus
import ratelimit


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    """No telethon client in tests: swallow deliveries but keep the code paths."""
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)


# --------------------------------------------------------------------------- #
# Trial + expiry
# --------------------------------------------------------------------------- #
def test_new_customer_gets_the_trial(monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 3)
    cid = db.ensure_customer(555, "New", "new")["telegram_id"]
    assert db.is_active(cid) is True
    assert 2 <= db.days_left(cid) <= 3


def test_trial_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 0)
    cid = db.ensure_customer(556, "NoTrial")["telegram_id"]
    assert db.is_active(cid) is False
    assert db.seconds_left(cid) == 0


def test_ensure_customer_is_idempotent_and_keeps_the_expiry(monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 3)
    cid = db.ensure_customer(557, "A")["telegram_id"]
    db.add_days(cid, 30)
    before = db.get_customer(cid)["expires_at"]
    db.ensure_customer(557, "A renamed", "handle")
    after = db.get_customer(cid)
    assert after["expires_at"] == before       # re-/start must not reset time
    assert after["name"] == "A renamed"


def test_expired_customer_is_not_active(alice):
    db.set_expiry(alice, "2020-01-01 00:00:00")
    assert db.is_active(alice) is False
    assert db.seconds_left(alice) == 0


def test_add_days_extends_from_now_when_lapsed(alice):
    db.set_expiry(alice, "2020-01-01 00:00:00")
    db.add_days(alice, 7)
    assert 6 <= db.days_left(alice) <= 7


def test_add_days_extends_from_existing_expiry_when_still_valid(alice):
    """Topping up early must not throw away the remaining days."""
    db.set_expiry(alice, "2020-01-01 00:00:00")
    db.add_days(alice, 10)
    first = db.days_left(alice)
    db.add_days(alice, 10)
    assert db.days_left(alice) >= first + 9


def test_negative_days_reduce_access(alice):
    """The owner can take time back as well as give it."""
    db.set_expiry(alice, db._iso_after_days(30))
    assert db.days_left(alice) >= 29
    db.add_days(alice, -29)
    assert db.days_left(alice) <= 1
    assert db.is_active(alice) is True          # reduced, not revoked


def test_negative_days_can_expire_an_account(alice):
    db.set_expiry(alice, db._iso_after_days(5))
    db.add_days(alice, -10)
    assert db.is_active(alice) is False


def test_adding_time_clears_the_warned_flag(alice):
    db.set_warned(alice, True)
    db.add_days(alice, 5)
    assert db.get_customer(alice)["warned"] == 0


def test_blocked_customer_is_never_active(alice):
    db.add_days(alice, 30)
    db.set_blocked(alice, True)
    assert db.is_active(alice) is False
    db.set_blocked(alice, False)
    assert db.is_active(alice) is True


def test_expiring_soon_list_ignores_warned_and_blocked(alice, bob):
    db.set_expiry(alice, db._iso_after_days(1))
    db.set_expiry(bob, db._iso_after_days(1))
    db.set_warned(bob, True)
    ids = [c["telegram_id"] for c in db.owner_customers_expiring(2)]
    assert alice in ids and bob not in ids


# --------------------------------------------------------------------------- #
# Clock anti-tamper
# --------------------------------------------------------------------------- #
def test_monotonic_now_never_goes_backwards(monkeypatch):
    first = db.monotonic_now()
    monkeypatch.setattr(time, "time", lambda: first - 100_000)
    assert db.monotonic_now() >= first
    assert db.clock_tampered() is True


def test_clock_not_flagged_under_normal_drift(monkeypatch):
    base = db.monotonic_now()
    monkeypatch.setattr(time, "time", lambda: base - 5)
    assert db.clock_tampered() is False       # inside the tolerance


# --------------------------------------------------------------------------- #
# Rate limit
# --------------------------------------------------------------------------- #
def test_rate_limit_allows_up_to_the_ceiling(alice, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 5)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW", 60)
    for _ in range(5):
        assert asyncio.run(ratelimit.guard(alice)) is True


def test_rate_limit_blocks_past_the_ceiling(alice, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 3)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW", 60)

    async def scenario():
        results = [await ratelimit.guard(alice, "Alice") for _ in range(5)]
        return results

    results = asyncio.run(scenario())
    assert results[:3] == [True, True, True]
    assert results[3] is False
    assert db.is_blocked(alice) is True


def test_blocked_customer_is_refused_without_further_counting(alice):
    db.set_blocked(alice, True)
    assert asyncio.run(ratelimit.guard(alice)) is False


def test_rate_window_survives_a_restart(alice, monkeypatch):
    """The counter lives in the DB precisely so a crash cannot reset it."""
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 10)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW", 60)
    for _ in range(4):
        db.rate_hit(alice)
    # a "restart" is just a new connection here — the row is still there
    allowed, count = db.rate_hit(alice)
    assert allowed is True
    assert count == 5


def test_rate_window_rolls_over(alice, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW", 1)
    db.rate_hit(alice)
    db.rate_hit(alice)
    time.sleep(1.1)
    allowed, count = db.rate_hit(alice)
    assert allowed is True and count == 1


def test_unblock_clears_the_window(alice, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 2)
    asyncio.run(ratelimit.guard(alice))
    asyncio.run(ratelimit.guard(alice))
    asyncio.run(ratelimit.guard(alice))
    assert db.is_blocked(alice) is True
    asyncio.run(ratelimit.unblock(alice))
    assert db.is_blocked(alice) is False
    assert asyncio.run(ratelimit.guard(alice)) is True


def test_guard_without_a_customer_id_refuses():
    assert asyncio.run(ratelimit.guard(None)) is False


# --------------------------------------------------------------------------- #
# Anti-spam shield
# --------------------------------------------------------------------------- #
def test_shield_trips_on_a_flood_of_new_users(monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_MAX", 5)
    monkeypatch.setattr(config, "START_FLOOD_WINDOW", 120)

    async def scenario():
        allowed = []
        for uid in range(9000, 9010):
            allowed.append(await antispam.note_start(uid, is_new=True))
        return allowed

    allowed = asyncio.run(scenario())
    assert all(allowed[:5])            # first five are served
    assert allowed[-1] is False        # the flood is refused
    assert db.is_bot_online() is False


def test_one_person_tapping_start_repeatedly_does_not_trip_it(monkeypatch):
    """Distinct users, not raw taps — a curious customer is not an attack."""
    monkeypatch.setattr(config, "START_FLOOD_MAX", 3)

    async def scenario():
        return [await antispam.note_start(777, is_new=True) for _ in range(20)]

    assert all(asyncio.run(scenario()))
    assert db.is_bot_online() is True


def test_returning_customers_never_feed_the_counter(monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_MAX", 2)

    async def scenario():
        return [await antispam.note_start(uid, is_new=False)
                for uid in range(100, 130)]

    assert all(asyncio.run(scenario()))
    assert db.is_bot_online() is True


def test_owner_can_lift_the_shield(monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_MAX", 2)

    async def scenario():
        for uid in range(1, 8):
            await antispam.note_start(uid, is_new=True)
        assert db.is_bot_online() is False
        await antispam.lift(by="owner")
        assert db.is_bot_online() is True
        # and the stale burst must not immediately re-trip it
        return await antispam.note_start(999, is_new=True)

    assert asyncio.run(scenario()) is True
    assert db.is_bot_online() is True


def test_offline_bot_refuses_everyone(monkeypatch):
    asyncio.run(antispam.lower(by="owner", note="maintenance"))
    assert asyncio.run(antispam.note_start(4242, is_new=True)) is False


def test_shield_can_be_disabled_entirely(monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_SHIELD", False)
    monkeypatch.setattr(config, "START_FLOOD_MAX", 1)

    async def scenario():
        return [await antispam.note_start(uid, is_new=True) for uid in range(10)]

    assert all(asyncio.run(scenario()))
    assert db.is_bot_online() is True


def test_shield_status_reports_the_numbers(monkeypatch):
    monkeypatch.setattr(config, "START_FLOOD_MAX", 20)
    asyncio.run(antispam.note_start(1, is_new=True))
    status = antispam.status()
    assert status["online"] is True
    assert status["limit"] == 20
    assert status["recent_starts"] >= 1


# --------------------------------------------------------------------------- #
# Emergency freeze (the owner's kill switch)
# --------------------------------------------------------------------------- #
def test_sends_can_be_frozen_and_thawed():
    assert db.are_sends_frozen() is False
    db.set_sends_frozen(True)
    assert db.are_sends_frozen() is True
    db.set_sends_frozen(False)
    assert db.are_sends_frozen() is False


# --------------------------------------------------------------------------- #
# Daily probe budget — the throttle on number building
# --------------------------------------------------------------------------- #
def test_probe_budget_starts_full_and_drains(alice, monkeypatch):
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 2000)
    assert db.probe_budget_left(alice) == 2000
    db.probe_spend(alice, 500)
    assert db.probe_budget_left(alice) == 1500
    db.probe_spend(alice, 1500)
    assert db.probe_budget_left(alice) == 0


def test_probe_budget_never_goes_negative(alice, monkeypatch):
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 100)
    db.probe_spend(alice, 5000)
    assert db.probe_budget_left(alice) == 0


def test_probe_budget_is_per_customer(alice, bob, monkeypatch):
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 100)
    db.probe_spend(alice, 100)
    assert db.probe_budget_left(alice) == 0
    assert db.probe_budget_left(bob) == 100


def test_usage_counters_are_per_kind(alice):
    db.usage_incr(alice, "send", 10)
    db.usage_incr(alice, "probe", 3)
    assert db.usage_today(alice, "send") == 10
    assert db.usage_today(alice, "probe") == 3
    assert db.usage_today(alice, "pdf") == 0
