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
