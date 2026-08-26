"""
Send ORDER and the brake logic, both ported from the reference project.

WHO GETS THE MESSAGE FIRST
--------------------------
Rubika: the reference orders an account's own contacts as
  1) whoever is ONLINE right now (chat-first inside that tier),
  2) then people we have a chat with but who are offline,
  3) then everyone else by LAST SEEN, most recent first.
This repo had drifted to chat-first / online-second, which buries the people most
likely to read the message right now behind an old conversation list.

Telegram: mutual (two-way) contacts first, then one-way. They added the account
back, so they are the least likely to report it — reaching them first is what
makes an account survive further into a run.

WHEN A SEND MUST BRAKE
----------------------
The reference is deliberate about which failure means what, and ours was not:
  * only a platform RESTRICTION (PeerFlood / too many requests) counts toward the
    consecutive-error ceiling — ours counted every hiccup, so five scattered
    network errors across a thousand recipients stopped a healthy account,
  * a lost CONNECTION abandons the turn immediately instead of spending five
    recipients' worth of budget proving the network is down,
  * a locked DATABASE is our own contention: pause the job, never burn the
    account and never fail the whole job,
  * every FloodWait parks the account, so the wait is interruptible by a stop and
    always counts toward the give-up ceiling.
"""
import asyncio
import os

import pytest

import config
import db
import rubika_client as rb
import telegram_multi_send as multi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Rubika: online first, then last seen
# --------------------------------------------------------------------------- #
class _RubikaAccount:
    """Contacts and chats shaped the way rubpy returns them."""

    def __init__(self, contacts, chats=()):
        self.contacts = contacts
        self.chats = list(chats)

    async def get_me(self):
        return {"user": {"user_guid": "u_self"}}

    async def get_contacts(self, start_id=None):
        return {"users": self.contacts, "next_start_id": None}

    async def get_chats(self, start_id=None):
        return {"chats": [{"object_guid": g, "abs_object": {"type": "User"}}
                          for g in self.chats], "next_start_id": None}


def _contact(guid, *, online=False, last=0):
    """Shaped as rubpy really returns a contact.

    Presence is the string field `status` ("Online" / "Offline"), NOT a boolean —
    a fake that invented `is_online` would have every contact read as offline and
    these ordering assertions would pass against any implementation at all.
    """
    return {"user_guid": guid, "first_name": guid,
            "status": "Online" if online else "Offline",
            "last_online": last}


def test_the_presence_field_is_the_one_the_platform_sends():
    """Guards the fake above: if this drifts, every ordering test goes blind."""
    assert rb._is_online(_contact("x", online=True)) is True
    assert rb._is_online(_contact("x")) is False
    assert rb._last_online_of(_contact("x", last=42)) == 42


def _order(client):
    return [item["guid"] for item in
            asyncio.run(rb.get_ordered_recipients(client))]


def test_online_contacts_come_first():
    """The account MUST have a chat here, or this test proves nothing.

    With no chats at all, the old chat-first implementation also happened to put
    online contacts first — so a fixture without a conversation passes against
    both orders. The discriminating case is the real one: an online stranger
    versus someone we have an old chat with.
    """
    client = _RubikaAccount([
        _contact("offline_we_chat_with", last=900),
        _contact("online_stranger", online=True, last=10),
        _contact("offline_old", last=100),
    ], chats=["offline_we_chat_with"])
    assert _order(client)[0] == "online_stranger", \
        "an online contact reads the message now; that is the whole point"


def test_after_the_online_ones_the_order_is_last_seen():
    client = _RubikaAccount([
        _contact("seen_long_ago", last=100),
        _contact("seen_recently", last=900),
        _contact("seen_yesterday", last=500),
    ])
    assert _order(client) == ["seen_recently", "seen_yesterday",
                              "seen_long_ago"]


def test_a_chat_we_have_beats_a_stranger_but_not_an_online_stranger():
    """Tier 2 sits BETWEEN online and the rest, exactly as the reference has it."""
    client = _RubikaAccount([
        _contact("stranger_online", online=True, last=1),
        _contact("we_chat", last=50),
        _contact("stranger_offline", last=999),
    ], chats=["we_chat"])
    assert _order(client) == ["stranger_online", "we_chat", "stranger_offline"]


def test_inside_the_online_tier_the_ones_we_chat_with_come_first():
    client = _RubikaAccount([
        _contact("online_stranger", online=True, last=999),
        _contact("online_friend", online=True, last=1),
    ], chats=["online_friend"])
    assert _order(client) == ["online_friend", "online_stranger"]


def test_every_contact_is_still_included_exactly_once():
    """An ordering bug that silently DROPS people is the expensive kind."""
    client = _RubikaAccount([
        _contact("a", online=True), _contact("b"), _contact("c", last=5),
        _contact("d", online=True, last=7),
    ], chats=["b", "zz_not_a_contact"])
    got = _order(client)
    assert sorted(got) == ["a", "b", "c", "d"]
    assert len(got) == len(set(got))


# --------------------------------------------------------------------------- #
# Telegram: two-way (mutual) first, then one-way
# --------------------------------------------------------------------------- #
def test_mutual_contacts_are_queued_and_served_first(alice):
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    # Deliberately inserted one-way FIRST, so passing cannot be an accident of
    # insertion order.
    db.tgm_add_recipients(alice, job_id, aid, [
        ("one_way_a", {"kind": "user", "id": 1}, False),
        ("two_way_a", {"kind": "user", "id": 2}, True),
        ("one_way_b", {"kind": "user", "id": 3}, False),
        ("two_way_b", {"kind": "user", "id": 4}, True),
    ])
    order = [row["target_key"] for row in
             db.tgm_pending_recipients(alice, job_id, aid)]
    assert order[:2] == ["two_way_a", "two_way_b"], \
        "two-way contacts added the account back and must be reached first"
    assert order[2:] == ["one_way_a", "one_way_b"]


def test_the_discovery_step_marks_two_way_contacts():
    """The flag has to be SET at discovery, or the ordering above has nothing
    to sort by."""
    body = _src("telegram_multi_send.py")
    start = body.index("async def _discover_targets")
    end = body.index("\nasync def ", start + 10)
    section = body[start:end]
    assert "get_contacts_ordered" in section
    assert "True" in section and "False" in section, \
        "mutuals and others must be marked differently"


# --------------------------------------------------------------------------- #
# The brakes
# --------------------------------------------------------------------------- #
def test_every_floodwait_parks_by_default():
    """0 = park always, which is the reference behaviour.

    An inline sleep had two faults the reference does not have: it ignored the
    stop button, and it never counted toward the give-up ceiling — so an account
    could absorb short FloodWaits for ever and never be parked.
    """
    assert config.TG_FLOOD_INLINE_MAX == 0
    assert multi.FLOOD_INLINE_MAX == config.TG_FLOOD_INLINE_MAX, \
        "the module constant must follow the setting, not shadow it"


def test_an_inline_floodwait_wait_is_interruptible(monkeypatch):
    """If an inline wait is ever configured, a stop must still be answered."""
    code = _func_code("_run_account")
    idx = code.index("TG_FLOOD_INLINE_MAX")
    window = code[idx:idx + 600]
    assert "_sleep_unless_stopped" in window, \
        "an inline FloodWait sleep that ignores the stop flag is a dead button"
    assert "await asyncio.sleep(wait)" not in window


def test_the_send_timeout_can_never_undercut_a_legitimate_flood_sleep():
    """The compounding trap, asserted rather than trusted.

    safe_call legitimately sleeps out a FloodWait of up to TG_FLOOD_MAX_WAIT
    inside ONE send. A per-send timeout below that would fire on a send that is
    behaving correctly, and since a timeout is treated as a lost connection the
    account would be abandoned for being throttled. The reference has exactly
    that mismatch (120s timeout over a 300s sleep); config refuses to.
    """
    assert config.TG_SEND_TIMEOUT > config.TG_FLOOD_MAX_WAIT


def test_a_hung_send_cannot_hold_the_turn_for_ever():
    code = _func_code("_run_account")
    assert "asyncio.wait_for(" in code and "TG_SEND_TIMEOUT" in code, \
        "one stuck socket would hold the session claim and the whole turn"


def _func_code(name: str) -> str:
    """One function, sliced by indentation — never a fixed byte window."""
    import re
    lines = _src("telegram_multi_send.py").splitlines()
    start = indent = None
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:async\s+)?def\s+" + re.escape(name) + r"\b",
                         line)
        if match:
            start, indent = i, len(match.group(1))
            break
    assert start is not None, f"{name} not found"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if not lines[j].strip():
            continue
        current = len(lines[j]) - len(lines[j].lstrip())
        if current <= indent and not lines[j].lstrip().startswith(
                ("#", ")", "]", "}", "@")):
            end = j
            break
    return "\n".join(lines[start:end])


def test_a_stop_written_by_another_process_is_obeyed(alice, monkeypatch):
    """Ours read only the in-memory flag.

    So a stop pressed while a different process owned the job — after a restart,
    or from the owner service — reported success and the job kept sending. The
    reference polls the durable stop_requested column, which is the only source
    both processes share.
    """
    delivered = []

    class _Client:
        pass

    async def _get_client(customer_id, account_id):
        return _Client()

    async def _deliver(client, target, content, delay, plan=None):
        delivered.append(target.get("id"))
        # Simulate the other process pressing stop after the first recipient.
        db.tgm_update_job(alice, job_id, stop_requested=1,
                          state="stop_requested")

    import telegram_client as tgc
    monkeypatch.setattr(tgc, "get_client", _get_client)
    monkeypatch.setattr(multi, "_deliver", _deliver)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [(str(i), {"kind": "user", "id": i}, False)
                           for i in range(1, 6)])

    asyncio.run(multi._run(alice, job_id))

    assert delivered == [1], \
        "the job must notice a stop it did not write itself"


class _PeerFlood(Exception):
    pass


class _FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"wait {seconds}s")
        self.seconds = seconds


def test_a_parked_account_keeps_the_restriction_count_it_had(alice, monkeypatch):
    """The counter has to be DURABLE, as it is in the reference.

    It lived only in a local variable, so an account parked on a FloodWait after
    four restriction errors came back with a clean slate — and could keep
    collecting restrictions for ever without ever reaching the ceiling. The
    ceiling exists precisely because a restricted account must be set aside.

    Here the account collects two restrictions, is parked by a FloodWait, and
    must reach its ceiling of three on the very first restriction after waking.
    """
    delivered = []
    seen = []

    class _Client:
        pass

    async def _get_client(customer_id, account_id):
        return _Client()

    async def _deliver(client, target, content, delay, plan=None):
        uid = target.get("id")
        seen.append(uid)
        if uid in (1, 2):
            raise _PeerFlood("too many requests")
        if uid == 3 and seen.count(3) == 1:
            # One second, so the test exercises the REAL park-and-wake path
            # (cooldown written, cooldown expired, account revived) instead of
            # patching the wait away and never proving the wake works.
            raise _FloodWaitError(1)
        if uid == 3:
            raise _PeerFlood("too many requests")
        delivered.append(uid)

    import telegram_client as tgc
    monkeypatch.setattr(tgc, "get_client", _get_client)
    monkeypatch.setattr(multi, "_deliver", _deliver)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)
    db.set_setting(alice, "max_errors", 3)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [(str(i), {"kind": "user", "id": i}, False)
                           for i in range(1, 6)])

    asyncio.run(multi._run(alice, job_id))

    assert delivered == [], \
        "the third restriction must hit the ceiling; the count survived the park"
    account = db.tgm_job_accounts(alice, job_id)[0]
    assert account["state"] == "stopped"
    assert int(account["consec_fail"] or 0) >= 3


def test_the_waiting_loop_cannot_spin(alice, monkeypatch):
    """If the cooldown wait ever returns early, the loop must not hammer sqlite.

    Simulated by making the wait return immediately while a parked account still
    has a minute to go — the condition the reference's guard exists for. Without
    the guard this is a hot loop that burns a core and writes to the database
    thousands of times a second, while the job reads a calm "waiting" to anyone
    looking at the card. Found for real: an earlier version of this very test hung
    for 62 seconds and that was the only reason anyone noticed.
    """
    async def _returns_immediately(control, seconds, step=2.0):
        return not control.get("stop")

    # COUNT THE PASSES, do not try to time-limit them. asyncio.wait_for cannot
    # cancel a loop whose only awaits complete without yielding, so a timeout
    # here would sit there for a minute and then report success — which is
    # exactly what the first version of this test did.
    passes = []
    real_wake = db.tgm_wake_cooled

    def _counting_wake(customer_id, job_id):
        passes.append(1)
        if len(passes) > 3:
            # BaseException on purpose: _run's `except Exception` would swallow
            # an AssertionError and mark the job failed, hiding the spin.
            raise KeyboardInterrupt("the waiting loop is spinning")
        return real_wake(customer_id, job_id)

    monkeypatch.setattr(multi, "_sleep_unless_stopped", _returns_immediately)
    monkeypatch.setattr(db, "tgm_wake_cooled", _counting_wake)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [("1", {"kind": "user", "id": 1}, False)])
    # Parked with a minute still to run, so the wake finds nothing.
    db.tgm_park_account(alice, job_id, aid, 60, "FloodWait 60s")

    asyncio.run(multi._run(alice, job_id))

    assert len(passes) == 1, \
        "one look, then give up — anything more is a spin on the database"

    job = db.tgm_get_job(alice, job_id)
    assert job["state"] in ("paused", "waiting"), \
        "the work is unfinished, so it must be resumable rather than 'done'"
    assert db.tgm_counts(alice, job_id).get("pending") == 1, \
        "nothing may be thrown away when the loop gives up"


def test_a_locked_database_pauses_the_job_instead_of_burning_the_account(
        alice, monkeypatch):
    import sqlite3

    delivered = []

    class _Client:
        pass

    async def _get_client(customer_id, account_id):
        return _Client()

    async def _deliver(client, target, content, delay, plan=None):
        if target.get("id") == 2:
            raise sqlite3.OperationalError("database is locked")
        delivered.append(target.get("id"))

    import telegram_client as tgc
    monkeypatch.setattr(tgc, "get_client", _get_client)
    monkeypatch.setattr(multi, "_deliver", _deliver)
    monkeypatch.setattr(config, "TABCHI_ACCOUNT_STAGGER", 0)

    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.0)
    aid = db.tg_add_account(alice, "09120000001")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [(str(i), {"kind": "user", "id": i}, False)
                           for i in range(1, 5)])

    asyncio.run(multi._run(alice, job_id))

    assert delivered == [1]
    job = db.tgm_get_job(alice, job_id)
    assert job["state"] == "paused", "a lock must not fail the whole job"
    account = db.tgm_job_accounts(alice, job_id)[0]
    assert account["state"] == "stopped", \
        "the account did nothing wrong and must be requeued by resume"
    counts = db.tgm_counts(alice, job_id)
    assert counts.get("pending") == 3, \
        "the recipient we could not record must stay in the queue"
    assert db.tgm_requeue_stopped_accounts(alice, job_id) == 1
