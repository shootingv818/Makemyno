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

import os

import cards
import config
import db
import telegram_multi_send as multi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_the_progress_card_moves_while_an_account_is_still_sending(alice):
    """THE BUG THE CUSTOMER SAW. The card read job["sent_count"], a column that
    _run_account only writes when it FINISHES an account — so a job sending to
    hundreds of people displayed 0/N the whole time and looked frozen. The
    recipients table is updated per message, so it is the only live source."""
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    aid = db.add_tg_account(alice, "09120000001") if hasattr(db, "add_tg_account") \
        else None
    account_id = aid or 1
    db.tgm_add_account(alice, job_id, account_id, "09120000001", 3)
    db.tgm_add_recipients(alice, job_id, account_id, [
        ("u1", {"kind": "user", "id": 1}, True),
        ("u2", {"kind": "user", "id": 2}, False),
        ("u3", {"kind": "user", "id": 3}, False)])
    db.tgm_update_job(alice, job_id, total=3)

    assert "0/3" in multi.progress_card(alice, job_id)

    # Two delivered. The job row is deliberately NOT bumped — that only happens
    # when the account finishes, which is exactly the situation being tested.
    db.tgm_set_recipient(alice, job_id, 0, "sent")
    db.tgm_set_recipient(alice, job_id, 1, "sent")

    card = multi.progress_card(alice, job_id)
    assert "2/3" in card, "the card did not move while the account was mid-run"
    assert db.tgm_get_job(alice, job_id)["sent_count"] == 0, (
        "the stale column is still zero — proving the card no longer reads it")


def test_the_per_account_line_is_live_too(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    db.tgm_add_account(alice, job_id, 1, "09120000001", 2)
    db.tgm_add_recipients(alice, job_id, 1, [
        ("u1", {"kind": "user", "id": 1}, True),
        ("u2", {"kind": "user", "id": 2}, False)])
    db.tgm_set_recipient(alice, job_id, 0, "sent")
    card = multi.progress_card(alice, job_id)
    assert "09120000001" in card
    assert "1 / 2" in card or "1 /2" in card, f"per-account count stale: {card}"


def test_failures_show_on_the_account_line(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    db.tgm_add_account(alice, job_id, 1, "09120000001", 2)
    db.tgm_add_recipients(alice, job_id, 1, [
        ("u1", {"kind": "user", "id": 1}, True),
        ("u2", {"kind": "user", "id": 2}, False)])
    db.tgm_set_recipient(alice, job_id, 0, "sent")
    db.tgm_set_recipient(alice, job_id, 1, "failed", "PeerFlood")
    assert "⚠️" in multi.progress_card(alice, job_id)


def test_the_live_card_belongs_to_the_engine_not_the_panel():
    """It used to be started by the panel, so a RESUMED job and one revived by
    restart recovery had no live card — two ways to watch a frozen number while
    work was actually happening."""
    assert hasattr(multi, "_live_card")
    body = open(os.path.join(ROOT, "telegram_multi_send.py"), encoding="utf-8").read()
    start = body.index("async def start(")
    assert "_live_card" in body[start:start + 900], (
        "start() must launch the live card so resume and recovery get one too")


def test_the_live_card_stops_at_a_terminal_state(monkeypatch, alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    db.tgm_update_job(alice, job_id, msg_id=42, state="running")

    edits = []

    class _Bot:
        async def edit_message(self, cid, mid, text, buttons=None):
            edits.append(text)
            # Flip to a terminal state after the first edit.
            db.tgm_update_job(alice, job_id, state="done")

    monkeypatch.setattr(multi, "_bot", _Bot())
    monkeypatch.setattr(multi.config, "TG_STATS_REFRESH", 0.0)
    asyncio.run(multi._live_card(alice, job_id))
    # It terminates rather than looping forever, and the LAST edit shows the
    # finished state — the state is read before the edit, so a final pass is
    # exactly what leaves the customer looking at a correct card.
    assert 1 <= len(edits) <= 3, f"should settle quickly, got {len(edits)}"
    assert "پایان" in edits[-1], "the final card must show the finished state"


def test_the_live_card_survives_a_failed_edit(monkeypatch, alice):
    """A customer who deleted the message makes the edit fail; the loop must still
    exit cleanly rather than raise into the job."""
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")
    db.tgm_update_job(alice, job_id, msg_id=42, state="done")

    class _Bot:
        async def edit_message(self, *a, **k):
            raise RuntimeError("message to edit not found")

    monkeypatch.setattr(multi, "_bot", _Bot())
    monkeypatch.setattr(multi.config, "TG_STATS_REFRESH", 0.0)
    asyncio.run(multi._live_card(alice, job_id))      # must not raise


def test_the_live_card_stops_if_the_job_is_deleted(monkeypatch, alice):
    monkeypatch.setattr(multi.config, "TG_STATS_REFRESH", 0.0)
    asyncio.run(multi._live_card(alice, "no-such-job"))


def test_stopping_a_job_cancels_its_card_refresher(alice):
    """Otherwise the refresher outlives the job it was watching."""
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.2, "both")

    async def _go():
        async def _forever():
            await asyncio.sleep(3600)
        multi._live[job_id] = asyncio.create_task(_forever())
        await multi.stop(alice, job_id, grace=0.01)
        return job_id in multi._live
    assert asyncio.run(_go()) is False



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
