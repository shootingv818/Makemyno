"""
Two post-launch complaints, pinned so they cannot come back:

  1. "I set the speed to 0.2s but a message goes out every ~3 seconds."
     A typing-simulation delay of 0.4–2.0s was ADDED ON TOP of the send delay.

  2. "The multi-account live card never updates."
     Multi-send had no progress refresher; the card only moved on a button tap.
"""
import asyncio
import types

import pytest

import cards
import config
import db
import telegram_multi_send as multi


# --------------------------------------------------------------------------- #
# 1. Typing simulation must not silently inflate the send gap
# --------------------------------------------------------------------------- #
def test_typing_simulation_is_off_by_default():
    """The speed the customer sets is the gap they should get. Typing on top of
    it turned 0.2s into ~2s and read as a broken bot."""
    assert config.TG_TYPING_MIN == 0.0
    assert config.TG_TYPING_MAX == 0.0


def test_deliver_adds_no_typing_delay_by_default(monkeypatch):
    """Drive _deliver and prove it does not sleep for typing when typing is off."""
    sends = []

    async def _send_text(client, entity, text, typing=0.0):
        sends.append(("text", typing))

    monkeypatch.setattr(multi.tg, "send_text", _send_text)

    slept = []

    async def _sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(multi.asyncio, "sleep", _sleep)

    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 5},
                               [{"kind": "text", "text": "hi"}], delay=0.2))
    assert sends == [("text", 0.0)], "a typing delay was passed despite typing off"
    # One content item -> no inter-item sleep inside _deliver.
    assert slept == []


def test_typing_can_still_be_enabled_deliberately(monkeypatch):
    monkeypatch.setattr(config, "TG_TYPING_MIN", 1.0)
    monkeypatch.setattr(config, "TG_TYPING_MAX", 1.0)
    captured = []

    async def _send_text(client, entity, text, typing=0.0):
        captured.append(typing)
    monkeypatch.setattr(multi.tg, "send_text", _send_text)

    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 5},
                               [{"kind": "text", "text": "hi"}], delay=0.2))
    assert captured == [1.0], "opt-in typing should still reach the sender"


# --------------------------------------------------------------------------- #
# 2. The multi-send card refreshes on its own
# --------------------------------------------------------------------------- #
class _FakeMsg:
    def __init__(self):
        self.edits = []
        self.id = 1

    async def edit(self, text, buttons=None):
        self.edits.append(text)


def test_the_live_loop_edits_until_the_job_is_terminal(monkeypatch, alice):
    import tg_panel

    # A job that is running for two polls, then done.
    states = iter([
        {"state": "running", "sent_count": 1},
        {"state": "running", "sent_count": 2},
        {"state": "done", "sent_count": 3},
    ])
    cards_seq = iter(["card-1", "card-2", "card-3"])

    monkeypatch.setattr(multi, "status", lambda c, j: next(states))
    monkeypatch.setattr(multi, "progress_card", lambda c, j: next(cards_seq))
    monkeypatch.setattr(tg_panel.config, "TG_STATS_REFRESH", 0.0)

    msg = _FakeMsg()
    asyncio.run(tg_panel._multi_progress_loop(alice, "job1", msg))
    # It kept editing while running and stopped once the job was done.
    assert msg.edits == ["card-1", "card-2", "card-3"]


def test_the_live_loop_stops_when_the_job_vanishes(monkeypatch, alice):
    import tg_panel
    monkeypatch.setattr(multi, "status", lambda c, j: None)
    monkeypatch.setattr(tg_panel.config, "TG_STATS_REFRESH", 0.0)
    msg = _FakeMsg()
    asyncio.run(tg_panel._multi_progress_loop(alice, "gone", msg))
    assert msg.edits == []


def test_a_failed_edit_does_not_kill_the_loop(monkeypatch, alice):
    """A customer who scrolled away makes the edit fail; the job must keep being
    tracked to its terminal state anyway."""
    import tg_panel
    states = iter([{"state": "running"}, {"state": "done"}])
    monkeypatch.setattr(multi, "status", lambda c, j: next(states))
    monkeypatch.setattr(multi, "progress_card",
                        lambda c, j: f"card-{id(object())}")
    monkeypatch.setattr(tg_panel.config, "TG_STATS_REFRESH", 0.0)

    class _BadMsg:
        id = 1
        async def edit(self, *a, **k):
            raise RuntimeError("message not found")

    # Must not raise.
    asyncio.run(tg_panel._multi_progress_loop(alice, "job", _BadMsg()))


def test_the_loop_does_not_spin_forever_without_delay(monkeypatch, alice):
    """Safety: a status that never turns terminal must still be bounded by the
    poll sleep, not become a busy-loop. We assert it honours the sleep call."""
    import tg_panel
    monkeypatch.setattr(multi, "status", lambda c, j: {"state": "done"})
    monkeypatch.setattr(multi, "progress_card", lambda c, j: "x")
    slept = []

    async def _sleep(s):
        slept.append(s)
    monkeypatch.setattr(tg_panel.asyncio, "sleep", _sleep)
    asyncio.run(tg_panel._multi_progress_loop(alice, "job", _FakeMsg()))
    assert slept, "the loop must sleep between polls, never busy-spin"



# --------------------------------------------------------------------------- #
# 3. One fewer round-trip per recipient
# --------------------------------------------------------------------------- #
def test_discovery_captures_the_access_hash():
    """A bare numeric id forces Telethon to RESOLVE the peer before sending, which
    is an extra API round-trip per recipient whenever it is not cached. On a slow
    link that dominates the send rate: the customer sets 0.2s and watches one
    message leave every few seconds, with the time going to lookups."""
    entity = types.SimpleNamespace(id=555, access_hash=999)
    payload = multi._payload(entity, "user")
    assert payload == {"kind": "user", "id": 555, "access_hash": 999}


def test_a_peer_with_an_access_hash_needs_no_resolution():
    peer = multi._peer({"kind": "user", "id": 555, "access_hash": 999})
    assert peer.__class__.__name__ == "InputPeerUser"
    assert peer.user_id == 555 and peer.access_hash == 999


def test_a_group_peer_is_built_as_a_channel():
    peer = multi._peer({"kind": "group", "id": 777, "access_hash": 888})
    assert peer.__class__.__name__ == "InputPeerChannel"


def test_a_target_without_an_access_hash_still_works():
    """Jobs created before this existed must keep running, and the single-send
    path hands in a real entity object rather than an id."""
    assert multi._peer({"kind": "user", "id": 555}) == 555
    entity = types.SimpleNamespace(id=1)
    assert multi._peer({"kind": "user", "id": entity}) is entity


def test_a_target_with_no_id_is_rejected_not_silently_skipped():
    with pytest.raises(ValueError):
        asyncio.run(multi._deliver(object(), {"kind": "user", "id": None},
                                   [{"kind": "text", "text": "hi"}], 0.1))


# --------------------------------------------------------------------------- #
# 4. The send rate is measurable, so "slow" stops being a guess
# --------------------------------------------------------------------------- #
def test_send_timing_starts_empty(monkeypatch):
    monkeypatch.setattr(multi, "_send_times", [])
    assert multi.send_timing() == {"avg": 0.0, "last": 0.0, "n": 0}


def test_send_timing_averages_recent_sends(monkeypatch):
    monkeypatch.setattr(multi, "_send_times", [])
    for value in (1.0, 2.0, 3.0):
        multi.note_send_time(value)
    timing = multi.send_timing()
    assert timing["n"] == 3
    assert timing["avg"] == 2.0
    assert timing["last"] == 3.0


def test_the_timing_window_is_bounded(monkeypatch):
    """A long job must not accumulate an unbounded list."""
    monkeypatch.setattr(multi, "_send_times", [])
    for i in range(500):
        multi.note_send_time(float(i))
    assert len(multi._send_times) <= 200


def test_deliver_records_how_long_telegram_took(monkeypatch):
    monkeypatch.setattr(multi, "_send_times", [])

    async def _send_text(client, entity, text, typing=0.0):
        return None
    monkeypatch.setattr(multi.tg, "send_text", _send_text)

    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 5},
                               [{"kind": "text", "text": "hi"}], delay=0.2))
    assert multi.send_timing()["n"] == 1


def test_the_progress_card_separates_our_delay_from_the_network(monkeypatch,
                                                               alice):
    """The point of the whole thing: the card must show that a slow send is
    Telegram's latency, not a setting the customer can lower."""
    monkeypatch.setattr(multi, "_send_times", [])
    multi.note_send_time(2.8)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    card = multi.progress_card(alice, job_id)
    assert "0.20s" in card, "the configured gap must be shown"
    assert "2.80s" in card, "the platform's own cost must be shown"
    assert "پیام در دقیقه" in card, "an actual rate makes it concrete"


def test_the_card_omits_timing_before_any_send(monkeypatch, alice):
    monkeypatch.setattr(multi, "_send_times", [])
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    card = multi.progress_card(alice, job_id)
    assert "پیام در دقیقه" not in card
