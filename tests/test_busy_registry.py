"""
The session busy registry — the fix for "accounts keep getting shot".

The failure being prevented: Rubika allows one live connection per session, so a
second connection revokes it. In the base project the periodic health checker
connected to EVERY account every three hours without asking whether the account
was mid-send, killed the ones that were busy, and then recorded them as dead.
"""
import asyncio
import time

import pytest

import busy
import config


def test_key_separates_customers_with_the_same_phone():
    """Two customers can own one number; their sessions are still distinct."""
    k1 = busy.key_for("0912 111 0000", customer_id=1)
    k2 = busy.key_for("09121110000", customer_id=2)
    assert k1 != k2
    # formatting of the same number must not create a different key
    assert busy.key_for("0912-111-0000", customer_id=1) == k1


def test_platform_is_part_of_the_key():
    assert busy.key_for("0912", customer_id=1, platform="rb") != \
           busy.key_for("0912", customer_id=1, platform="tg")


def test_second_claim_is_refused():
    key = busy.key_for("09120000001", customer_id=1)
    assert busy.acquire(key, "send", customer_id=1) is True
    assert busy.acquire(key, "pdf", customer_id=1) is False
    busy.release(key)
    assert busy.acquire(key, "pdf", customer_id=1) is True


def test_reason_names_the_blocking_operation():
    """A customer who taps a button must be told why nothing happened."""
    key = busy.key_for("09120000001", customer_id=1)
    busy.acquire(key, "contacts", customer_id=1)
    text = busy.reason(key)
    assert "افزودن مخاطب" in text
    assert text.strip() != ""


def test_reason_is_empty_when_free():
    assert busy.reason(busy.key_for("09120000009", customer_id=1)) == ""


def test_release_does_not_steal_another_holders_claim():
    key = busy.key_for("09120000001", customer_id=1)
    busy.acquire(key, "send", customer_id=1)
    busy.release(key, "pdf")          # wrong owner -> must not release
    assert busy.is_busy(key) is True
    busy.release(key, "send")
    assert busy.is_busy(key) is False


def test_stale_entries_are_reclaimed(monkeypatch):
    """A crashed task must not mark an account busy forever."""
    key = busy.key_for("09120000001", customer_id=1)
    busy.acquire(key, "send", customer_id=1)
    busy._held[key]["since"] = time.time() - 10_000
    monkeypatch.setattr(config, "BUSY_STALE_SEC", 60)
    assert busy.is_busy(key) is False
    assert busy.acquire(key, "send", customer_id=1) is True


# --------------------------------------------------------------------------- #
# hold(): the context manager every feature is supposed to use
# --------------------------------------------------------------------------- #
def test_hold_grants_then_frees():
    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        async with busy.hold(key, "send", customer_id=1) as held:
            assert held.ok is True
            assert bool(held) is True
            assert busy.is_busy(key) is True
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


def test_hold_refuses_when_already_held_and_explains_why():
    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        async with busy.hold(key, "send", customer_id=1) as first:
            assert first.ok
            async with busy.hold(key, "pdf", customer_id=1) as second:
                assert second.ok is False
                assert "ارسال" in second.reason
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


def test_hold_releases_even_when_the_body_raises():
    """An exception mid-send must not leave the account permanently locked."""
    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        with pytest.raises(RuntimeError):
            async with busy.hold(key, "send", customer_id=1) as held:
                assert held.ok
                raise RuntimeError("boom")
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


def test_hold_releases_on_cancellation():
    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)

        async def worker():
            async with busy.hold(key, "send", customer_id=1) as held:
                assert held.ok
                await asyncio.sleep(10)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)
        assert busy.is_busy(key) is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


def test_settle_delay_keeps_the_slot_a_little_longer(monkeypatch):
    """Even a purely sequential reconnect can be read as a conflict, so the slot
    is held past the end of the work."""
    monkeypatch.setattr(config, "SESSION_SETTLE_SEC", 0.3)

    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        started = time.monotonic()
        async with busy.hold(key, "send", customer_id=1) as held:
            assert held.ok
        elapsed = time.monotonic() - started
        assert elapsed >= 0.3
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


def test_settle_can_be_switched_off_for_fast_paths(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SETTLE_SEC", 5.0)

    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        started = time.monotonic()
        async with busy.hold(key, "verify", customer_id=1, settle=False) as held:
            assert held.ok
        assert time.monotonic() - started < 1.0

    asyncio.run(scenario())


def test_two_coroutines_racing_for_one_session_only_one_wins():
    async def scenario():
        key = busy.key_for("09120000001", customer_id=1)
        wins = []

        async def contender(tag):
            async with busy.hold(key, tag, customer_id=1) as held:
                if held.ok:
                    wins.append(tag)
                    await asyncio.sleep(0.05)

        await asyncio.gather(*[contender(f"send{i}") for i in range(8)])
        assert len(wins) >= 1
        assert busy.is_busy(key) is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# adopt(): the restart trap
# --------------------------------------------------------------------------- #
def test_adopt_reregisters_a_resumed_job():
    """The registry is in memory, so a restart empties it — but jobs do resume.
    Without adopt() the resumed job is invisible and the health engine will
    connect on top of it."""
    key = busy.key_for("09120000001", customer_id=1)
    busy.clear_all()                                  # simulate a restart
    assert busy.is_busy(key) is False
    busy.adopt(key, "contacts", customer_id=1, extra={"account_id": 7})
    assert busy.is_busy(key) is True
    assert 7 in busy.busy_account_ids(1)


def test_adopt_overrides_a_stale_claim():
    key = busy.key_for("09120000001", customer_id=1)
    busy.acquire(key, "send", customer_id=1)
    busy.adopt(key, "contacts", customer_id=1)
    assert busy.who(key)["what"] == "contacts"


# --------------------------------------------------------------------------- #
# busy_account_ids(): what the health engine consults
# --------------------------------------------------------------------------- #
def test_busy_account_ids_is_scoped_and_only_lists_known_accounts():
    busy.acquire(busy.key_for("1", customer_id=1), "send",
                 customer_id=1, extra={"account_id": 11})
    busy.acquire(busy.key_for("2", customer_id=2), "send",
                 customer_id=2, extra={"account_id": 22})
    busy.acquire(busy.key_for("3", customer_id=1), "login", customer_id=1)

    assert busy.busy_account_ids(1) == {11}
    assert busy.busy_account_ids(2) == {22}
    assert busy.busy_account_ids() == {11, 22}


def test_health_check_pattern_skips_busy_accounts():
    """The exact decision the health engine makes: a busy account is provably
    alive, so it is skipped rather than probed."""
    accounts = [{"id": 1}, {"id": 2}, {"id": 3}]
    busy.acquire(busy.key_for("2", customer_id=1), "send",
                 customer_id=1, extra={"account_id": 2})
    busy_ids = busy.busy_account_ids(1)

    probed, skipped = [], []
    for acc in accounts:
        if acc["id"] in busy_ids:
            skipped.append(acc["id"])
            continue
        probed.append(acc["id"])

    assert probed == [1, 3]
    assert skipped == [2]      # never probed, never marked dead


def test_snapshot_reports_what_is_held():
    busy.acquire(busy.key_for("1", customer_id=1), "pdf", customer_id=1)
    snap = busy.snapshot()
    assert len(snap) == 1
    assert snap[0]["what"] == "pdf"
    assert snap[0]["customer_id"] == 1
    assert snap[0]["held_for"] >= 0


# --------------------------------------------------------------------------- #
# Heavy-job slots: protect the SERVER, not the session
# --------------------------------------------------------------------------- #
def test_heavy_slots_cap_concurrency_and_release():
    assert busy.take_slot("pdf", 2) is True
    assert busy.take_slot("pdf", 2) is True
    assert busy.take_slot("pdf", 2) is False        # third one queues
    busy.free_slot("pdf")
    assert busy.take_slot("pdf", 2) is True
    busy.free_slot("pdf")
    busy.free_slot("pdf")
    assert busy.slot_used("pdf") == 0


def test_freeing_an_unused_slot_never_goes_negative():
    busy.free_slot("pdf")
    busy.free_slot("pdf")
    assert busy.slot_used("pdf") == 0
