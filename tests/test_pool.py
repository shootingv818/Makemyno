"""
The pool brain: parallel leeching without paying for the same number twice.

The single property everything else rests on is that two accounts working the
same prefix never probe the same number. Probes are metered against a daily cap,
so a duplicate probe is money the customer spent for nothing — and with five
accounts and a random generator, duplicates are not an edge case, they are the
common case.
"""
import asyncio

import pytest

import account_conn
import busy
import cards
import config
import db
import logbus
import pool
import worker


@pytest.fixture(autouse=True)
def silent(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    monkeypatch.setattr(logbus, "to_group_file", noop)
    monkeypatch.setattr(config, "CONTACT_PROGRESS_EVERY", 0.01)
    monkeypatch.setattr(worker, "worker_for_account", lambda acc: None)
    # The real inter-probe delay is there to look human; in a test it is just
    # dead time, and it made the suite take minutes.
    monkeypatch.setattr(config, "clamp_discovery_delay", lambda v: 0.0)
    monkeypatch.setattr(config, "clamp_delay", lambda v: 0.0)
    busy.clear_all()
    pool._jobs.clear()
    pool._state.clear()
    yield
    busy.clear_all()
    pool._jobs.clear()
    pool._state.clear()


def _flat(rows) -> str:
    return " ".join(str(b) for row in rows for b in row)


_next_phone = [0]


def _accounts(cid, n=3):
    """Fresh phone numbers every call, so two jobs in one test do not collide."""
    ids = []
    for _ in range(n):
        _next_phone[0] += 1
        ids.append(db.add_account(cid, "0912%07d" % _next_phone[0], name="a"))
    return ids


def _job(cid, prefix="0912", target=10, mode="text", content="hi", n=3):
    """Returns (job_id, [account_id, ...])."""
    ids = _accounts(cid, n)
    accs = [db.get_account(cid, i) for i in ids]
    width, a, offset = pool.affine_params(prefix)
    job_id = db.pool_create_job(cid, prefix, target, width, a, offset, mode,
                                content, accs)
    return job_id, ids


# =========================================================================== #
# The number space: a bijection, so disjoint blocks give disjoint numbers
# =========================================================================== #
def test_the_affine_multiplier_is_coprime_to_the_space():
    """If A shares a factor with 10^k the sequence cycles early and revisits
    numbers, which is exactly the duplicate probing this design exists to
    avoid."""
    import math
    for prefix in ("0912", "09123", "091234", "0912345"):
        k, a, _ = pool.affine_params(prefix)
        assert math.gcd(a, 10 ** k) == 1, f"A is not coprime for {prefix}"


def test_walking_the_whole_space_visits_every_number_exactly_once():
    """The bijection property, checked exhaustively on a small space."""
    prefix = "0912345"                    # 4 free digits -> 10_000 values
    k, a, offset = pool.affine_params(prefix)
    job = {"prefix": prefix, "suffix_width": k, "affine_a": a,
           "affine_offset": offset}
    space = 10 ** k
    seen = {pool.number_at(job, i) for i in range(space)}
    assert len(seen) == space, "the permutation repeated a number"


def test_every_generated_number_is_eleven_digits():
    prefix = "0912"
    k, a, offset = pool.affine_params(prefix)
    job = {"prefix": prefix, "suffix_width": k, "affine_a": a,
           "affine_offset": offset}
    for i in (0, 1, 7, 999, 123456):
        number = pool.number_at(job, i)
        assert len(number) == 11 and number.startswith("0912")


def test_consecutive_indices_do_not_produce_consecutive_numbers():
    """Numbers that march upward look like a scan. The golden-ratio multiplier is
    there to scatter them."""
    prefix = "0912"
    k, a, offset = pool.affine_params(prefix)
    job = {"prefix": prefix, "suffix_width": k, "affine_a": a,
           "affine_offset": offset}
    gaps = {int(pool.number_at(job, i + 1)) - int(pool.number_at(job, i))
            for i in range(50)}
    assert gaps != {1}, "the sequence is just counting"


@pytest.mark.parametrize("raw,expected", [
    ("0912", "0912"), ("912", "0912"), ("98912", "0912"),
    ("+98 912", "0912"), ("0912-345", "0912345"),
])
def test_prefixes_are_normalised(raw, expected):
    assert pool._normalize_prefix(raw) == expected


# =========================================================================== #
# Block leasing — the heart of it
# =========================================================================== #
def test_two_leases_never_overlap(alice):
    job_id, _ = _job(alice)
    a = db.pool_lease_block(alice, job_id, 50)
    b = db.pool_lease_block(alice, job_id, 50)
    assert a == (0, 50) and b == (50, 100)
    assert set(range(*a)).isdisjoint(range(*b))


def test_parallel_leases_partition_the_space_with_no_duplicates(alice):
    """THE CENTRAL GUARANTEE. Five accounts lease at the same moment; every index
    must be handed out exactly once. A duplicate here is a probe the customer paid
    for twice."""
    job_id, _ = _job(alice)

    async def _lease():
        return db.pool_lease_block(alice, job_id, 10)

    async def _go():
        return await asyncio.gather(*[_lease() for _ in range(20)])
    ranges = asyncio.run(_go())

    indices = [i for start, end in ranges for i in range(start, end)]
    assert len(indices) == 200
    assert len(set(indices)) == 200, "two accounts were handed the same index"
    assert sorted(set(indices)) == list(range(200)), "the space has a hole in it"


def test_leases_translate_into_distinct_phone_numbers(alice):
    job_id, _ = _job(alice, prefix="0912")
    job = db.pool_get_job(alice, job_id)
    numbers = []
    for _ in range(10):
        start, end = db.pool_lease_block(alice, job_id, 20)
        numbers += [pool.number_at(job, i) for i in range(start, end)]
    assert len(numbers) == 200
    assert len(set(numbers)) == 200, "two blocks produced the same number"


def test_a_lease_on_a_missing_job_is_empty(alice):
    assert db.pool_lease_block(alice, 999999, 50) == (0, 0)


def test_a_lease_cannot_reach_another_customers_job(alice, bob):
    job_id, _ = _job(alice)
    assert db.pool_lease_block(bob, job_id, 50) == (0, 0)
    # alice's cursor was not moved by bob's attempt
    assert db.pool_get_job(alice, job_id)["cursor"] == 0


# =========================================================================== #
# Leeching
# =========================================================================== #
def _fake_probe(monkeypatch, hit_every=3, fail_phones=()):
    """Every `hit_every`-th number is a real user."""
    calls = []

    async def _call(customer_id, phone, fn, timeout=None):
        if phone in fail_phones:
            raise account_conn.InvalidAuthError("dead")
        number = fn.__defaults__[0]
        calls.append(number)
        if len(calls) % hit_every == 0:
            return {"user": {"user_guid": f"u-{number}"}}
        return {}
    monkeypatch.setattr(account_conn, "call", _call)
    return calls


def test_leeching_stops_when_the_target_is_reached(alice, monkeypatch):
    job_id, accs = _job(alice, target=5, n=2)
    _fake_probe(monkeypatch, hit_every=2)
    asyncio.run(pool.run_job(alice, job_id))
    counts = db.pool_counts(alice, job_id)
    assert counts["found"] >= 5
    # It must not run away far past the target; a small block is what bounds this.
    assert counts["found"] < 5 + config.POOL_BLOCK * 2


def test_no_number_is_probed_twice_across_accounts(alice, monkeypatch):
    """The property that makes the whole feature worth having, end to end."""
    job_id, accs = _job(alice, target=8, n=3)
    calls = _fake_probe(monkeypatch, hit_every=4)
    asyncio.run(pool.run_job(alice, job_id))
    assert len(calls) == len(set(calls)), "a number was probed more than once"


def test_every_probe_is_charged_to_the_daily_budget(alice, monkeypatch):
    """Running five accounts must be a way to spend the cap FASTER, never a way
    to spend more of it."""
    job_id, _ = _job(alice, target=4, n=3)
    _fake_probe(monkeypatch, hit_every=2)
    before = db.probe_budget_left(alice)
    asyncio.run(pool.run_job(alice, job_id))
    job = db.pool_get_job(alice, job_id)
    spent = before - db.probe_budget_left(alice)
    assert spent == job["probed"], "probes were made without being charged"
    assert spent > 0


def test_leeching_halts_when_the_budget_runs_out(alice, monkeypatch):
    job_id, _ = _job(alice, target=100000, n=2)
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 60)
    _fake_probe(monkeypatch, hit_every=1000)      # almost nothing found
    asyncio.run(pool.run_job(alice, job_id))
    assert db.probe_budget_left(alice) == 0
    job = db.pool_get_job(alice, job_id)
    # Recorded on the JOB, not per account: every account hits the same wall at
    # the same moment, and an account that later sent successfully should not be
    # labelled "budget_spent" forever.
    assert job["halt_reason"] == "budget_spent"
    assert job["probed"] <= 60
    assert "سهمیه" in pool._HALT_LABEL["budget_spent"]


def test_the_halt_reason_records_the_first_wall_hit(alice, monkeypatch):
    """Several accounts notice the same wall a moment apart. Letting a later
    reason overwrite would report "reached target" for a job that actually ran out
    of budget."""
    job_id, _ = _job(alice, n=2)
    db.pool_set_halt(alice, job_id, "budget_spent")
    db.pool_set_halt(alice, job_id, "target")
    assert db.pool_get_job(alice, job_id)["halt_reason"] == "budget_spent"


def test_reaching_the_target_is_recorded_as_the_reason(alice, monkeypatch):
    job_id, _ = _job(alice, target=4, n=2)
    _fake_probe(monkeypatch, hit_every=2)
    asyncio.run(pool.run_job(alice, job_id))
    assert db.pool_get_job(alice, job_id)["halt_reason"] == "target"


def test_the_budget_is_never_overspent_by_parallel_accounts(alice, monkeypatch):
    """Five accounts each checking "is there budget left?" could each be told yes
    and then collectively blow past the cap. The block size is clamped to what
    remains, which bounds the overshoot to one block per account."""
    job_id, _ = _job(alice, target=100000, n=4)
    monkeypatch.setattr(config, "PROBE_DAILY_CAP", 100)
    monkeypatch.setattr(config, "POOL_BLOCK", 10)
    _fake_probe(monkeypatch, hit_every=1000)
    asyncio.run(pool.run_job(alice, job_id))
    probed = db.pool_get_job(alice, job_id)["probed"]
    assert probed <= 100 + 4 * 10, f"overshot the cap badly: {probed}"


def test_one_dead_account_does_not_stop_the_pool(alice, monkeypatch):
    """A pool that dies with its weakest member would be less reliable than the
    single-account brain it replaces."""
    job_id, accs = _job(alice, target=6, n=3)
    dead_phone = db.get_account(alice, accs[0])["phone"]
    _fake_probe(monkeypatch, hit_every=2, fail_phones={dead_phone})
    asyncio.run(pool.run_job(alice, job_id))

    rows = {a["phone"]: a for a in db.pool_accounts(alice, job_id)}
    assert rows[dead_phone]["status"] == "failed"
    assert rows[dead_phone]["note"], "the report must say why it was dropped"
    others = [r for p, r in rows.items() if p != dead_phone]
    assert any(r["found"] > 0 for r in others), "the survivors did no work"
    assert db.pool_counts(alice, job_id)["found"] > 0


def test_an_account_that_is_busy_is_skipped_not_failed(alice, monkeypatch):
    """It is doing something else, which is not an error — and the pool must not
    open a second connection on top of it."""
    job_id, accs = _job(alice, target=4, n=2)
    acc = db.get_account(alice, accs[0])
    key = pool._key(alice, acc["phone"])
    busy.acquire(key, "send", customer_id=alice, extra={"account_id": accs[0]})
    calls = _fake_probe(monkeypatch, hit_every=2)
    try:
        asyncio.run(pool.run_job(alice, job_id))
    finally:
        busy.release(key, "send")

    rows = {a["phone"]: a for a in db.pool_accounts(alice, job_id)}
    assert rows[acc["phone"]]["status"] == "busy"
    assert acc["phone"] not in [c for c in calls], "the busy session was used"


def test_the_kill_switch_stops_a_pool(alice, monkeypatch):
    job_id, _ = _job(alice, target=50, n=2)
    db.set_sends_frozen(True)
    _fake_probe(monkeypatch, hit_every=2)
    asyncio.run(pool.run_job(alice, job_id))
    statuses = {a["status"] for a in db.pool_accounts(alice, job_id)}
    assert "frozen" in statuses
    assert db.pool_get_job(alice, job_id)["probed"] == 0


def test_the_space_running_out_ends_the_job_cleanly(alice, monkeypatch):
    """A ten-digit prefix leaves ten numbers. The job must finish rather than
    lease forever past the end of the space."""
    job_id, _ = _job(alice, prefix="0912345678", target=999, n=2)
    monkeypatch.setattr(config, "POOL_BLOCK", 4)
    _fake_probe(monkeypatch, hit_every=1000)
    asyncio.run(pool.run_job(alice, job_id))
    assert db.pool_get_job(alice, job_id)["probed"] <= 20
    statuses = {a["status"] for a in db.pool_accounts(alice, job_id)}
    assert statuses & {"exhausted", "done"}


# =========================================================================== #
# Two accounts finding the same person
# =========================================================================== #
def test_the_same_person_is_only_counted_once(alice, monkeypatch):
    """Two numbers can belong to one account. Without the guid uniqueness the
    pool counts them twice and later messages the person twice."""
    job_id, accs = _job(alice, target=100, n=2)
    assert db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    assert not db.pool_add_contact(alice, job_id, accs[1], "09120000002", "u-1")
    assert db.pool_hit_count(alice, job_id) == 1


def test_a_hit_with_no_guid_is_not_recorded(alice):
    job_id, accs = _job(alice)
    assert not db.pool_add_contact(alice, job_id, accs[0], "09120000001", "")
    assert db.pool_hit_count(alice, job_id) == 0


# =========================================================================== #
# Sending
# =========================================================================== #
def test_each_account_sends_only_to_the_contacts_it_found(alice, monkeypatch):
    """A contact belongs to the session that added it, so the account that found
    somebody is the only one that can reach them."""
    job_id, accs = _job(alice, target=100, mode="text", content="hello", n=2)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    db.pool_add_contact(alice, job_id, accs[1], "09120000002", "u-2")
    db.pool_set_status(alice, job_id, "sending")

    sent = []

    async def _call(customer_id, phone, fn, timeout=None):
        sent.append((phone, fn.__defaults__[0]))
        return True
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))

    p0 = db.get_account(alice, accs[0])["phone"]
    p1 = db.get_account(alice, accs[1])["phone"]
    assert (p0, "u-1") in sent and (p1, "u-2") in sent
    assert (p0, "u-2") not in sent, "an account messaged a contact it never added"


def test_only_confirmed_sends_are_recorded(alice, monkeypatch):
    """So a resumed job continues instead of messaging people a second time."""
    job_id, accs = _job(alice, target=100, n=2)
    for i in range(4):
        db.pool_add_contact(alice, job_id, accs[0], f"0912000{i}", f"u-{i}")
    db.pool_set_status(alice, job_id, "sending")

    async def _call(customer_id, phone, fn, timeout=None):
        guid = fn.__defaults__[0]
        if guid in ("u-1", "u-3"):
            raise RuntimeError("refused")
        return True
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))

    assert db.pool_counts(alice, job_id)["sent"] == 2
    unsent = {r["guid"] for r in
              db.pool_account_guids(alice, job_id, accs[0], unsent_only=True)}
    assert unsent == {"u-1", "u-3"}


def test_a_resumed_job_does_not_message_anyone_twice(alice, monkeypatch):
    job_id, accs = _job(alice, target=100, n=2)
    for i in range(4):
        db.pool_add_contact(alice, job_id, accs[0], f"0912000{i}", f"u-{i}")
    db.pool_mark_sent(alice, job_id, "u-0")
    db.pool_mark_sent(alice, job_id, "u-1")
    db.pool_set_status(alice, job_id, "sending")

    sent = []

    async def _call(customer_id, phone, fn, timeout=None):
        sent.append(fn.__defaults__[0])
        return True
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))
    assert set(sent) == {"u-2", "u-3"}, "an already-messaged contact was retried"


def test_a_dead_session_during_sending_stops_only_that_account(alice, monkeypatch):
    job_id, accs = _job(alice, target=100, n=2)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    db.pool_add_contact(alice, job_id, accs[1], "09120000002", "u-2")
    db.pool_set_status(alice, job_id, "sending")
    dead = db.get_account(alice, accs[0])["phone"]

    async def _call(customer_id, phone, fn, timeout=None):
        if phone == dead:
            raise account_conn.InvalidAuthError("revoked")
        return True
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))

    rows = {a["phone"]: a for a in db.pool_accounts(alice, job_id)}
    assert rows[dead]["status"] == "failed"
    assert db.pool_counts(alice, job_id)["sent"] == 1


def test_a_missing_marker_fails_that_account_without_sending(alice, monkeypatch):
    job_id, accs = _job(alice, target=100, mode="marker", content="MARK", n=2)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    db.pool_set_status(alice, job_id, "sending")

    async def _call(customer_id, phone, fn, timeout=None):
        return ("self-guid", None)          # marker not found
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))

    rows = {a["account_id"]: a for a in db.pool_accounts(alice, job_id)}
    assert rows[accs[0]]["status"] == "failed"
    assert "marker" in rows[accs[0]]["note"]
    assert db.pool_counts(alice, job_id)["sent"] == 0


def test_sending_updates_the_account_lifetime_total(alice, monkeypatch):
    job_id, accs = _job(alice, target=100, n=2)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    db.pool_set_status(alice, job_id, "sending")

    async def _call(customer_id, phone, fn, timeout=None):
        return True
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))
    assert db.get_account(alice, accs[0])["sent_total"] == 1


# =========================================================================== #
# Stopping
# =========================================================================== #
def test_a_stop_before_the_job_starts_is_not_lost(alice):
    """The customer taps stop the instant the card appears, before run_job has
    created its control entry. Assignment instead of setdefault would drop it and
    the button would silently do nothing."""
    job_id, _ = _job(alice)
    asyncio.run(pool.stop_job(alice, job_id))
    assert pool._jobs[job_id]["stop"] is True


def test_stopping_mid_leech_ends_the_job(alice, monkeypatch):
    job_id, _ = _job(alice, target=100000, n=2)

    async def _call(customer_id, phone, fn, timeout=None):
        await pool.stop_job(alice, job_id)
        return {}
    monkeypatch.setattr(account_conn, "call", _call)
    asyncio.run(pool.run_job(alice, job_id))
    assert db.pool_get_job(alice, job_id)["status"] == "stopped"


def test_a_stopped_job_is_not_resumed_by_recovery(alice, monkeypatch):
    job_id, _ = _job(alice)
    db.pool_set_status(alice, job_id, "stopped")
    started = []
    monkeypatch.setattr(pool, "run_job",
                        lambda cid, jid, msg=None: started.append(jid))
    asyncio.run(pool.restore_pending())
    assert started == []


# =========================================================================== #
# Isolation
# =========================================================================== #
def test_a_job_is_invisible_to_another_customer(alice, bob):
    job_id, accs = _job(alice)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    assert db.pool_get_job(bob, job_id) is None
    assert db.pool_accounts(bob, job_id) == []
    assert db.pool_hit_count(bob, job_id) == 0
    assert db.pool_counts(bob, job_id)["found"] == 0
    assert db.pool_list_jobs(bob) == []


def test_another_customer_cannot_stop_or_delete_a_job(alice, bob):
    job_id, _ = _job(alice)
    db.pool_delete_job(bob, job_id)
    assert db.pool_get_job(alice, job_id) is not None
    db.pool_set_status(bob, job_id, "stopped")
    assert db.pool_get_job(alice, job_id)["status"] == "leeching"


@pytest.mark.parametrize("fn,args", [
    ("pool_get_job", (1,)), ("pool_accounts", (1,)), ("pool_hit_count", (1,)),
    ("pool_counts", (1,)), ("pool_list_jobs", ()), ("pool_lease_block", (1, 5)),
    ("pool_add_contact", (1, 1, "0912", "g")), ("pool_mark_sent", (1, "g")),
])
def test_pool_db_calls_refuse_a_missing_customer(fn, args):
    with pytest.raises(db.ScopeError):
        getattr(db, fn)(None, *args)


def test_deleting_a_customer_leaves_no_pool_debris(alice):
    job_id, accs = _job(alice)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    db.delete_customer(alice)
    assert db.owner_pool_unfinished() == []


# =========================================================================== #
# Restart recovery
# =========================================================================== #
def test_unfinished_jobs_are_resumed(alice, monkeypatch):
    """A pool job can represent hundreds of already-spent probes; losing it on a
    restart means the customer paid the daily allowance for nothing."""
    leech, _ = _job(alice, target=10)
    send, _ = _job(alice, target=10)
    done, _ = _job(alice, target=10)
    db.pool_set_status(alice, send, "sending")
    db.pool_set_status(alice, done, "done")

    started = []
    monkeypatch.setattr(pool, "run_job",
                        lambda cid, jid, msg=None: started.append(jid))
    asyncio.run(pool.restore_pending())
    assert sorted(started) == sorted([leech, send])


def test_recovery_survives_a_broken_read(monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(db, "owner_pool_unfinished", boom)
    asyncio.run(pool.restore_pending())          # must not raise


def test_a_job_with_no_live_accounts_fails_instead_of_hanging(alice, monkeypatch):
    job_id, accs = _job(alice, n=2)
    for aid in accs:
        db.set_status(alice, aid, "quarantined")
    asyncio.run(pool.run_job(alice, job_id))
    assert db.pool_get_job(alice, job_id)["status"] == "failed"


# =========================================================================== #
# Cards
# =========================================================================== #
def test_cards_render_and_keep_the_house_style(alice):
    job_id, accs = _job(alice)
    db.pool_add_contact(alice, job_id, accs[0], "09120000001", "u-1")
    for text in (pool.menu_card(alice), pool.progress_card(alice, job_id)):
        assert isinstance(text, str) and cards.LINE in text
    for bad in ("_____", "•••", "═══"):
        assert bad not in pool.progress_card(alice, job_id)


def test_the_progress_card_survives_a_missing_job(alice):
    assert isinstance(pool.progress_card(alice, 999999), str)


def test_the_picker_needs_two_accounts_before_it_offers_start(alice):
    """With one account the plain brain is faster, so offering the pool would be
    offering something worse."""
    one = db.add_account(alice, "09121110001", name="a")
    assert "plgo" not in _flat(pool.picker_buttons(alice, [one]))
    two = db.add_account(alice, "09121110002", name="b")
    assert "plgo" in _flat(pool.picker_buttons(alice, [one, two]))


def test_the_picker_hides_dead_accounts(alice):
    db.add_account(alice, "09121110001", name="a")
    bad = db.add_account(alice, "09121110002", name="b")
    db.set_status(alice, bad, "quarantined")
    flat = _flat(pool.picker_buttons(alice, []))
    assert "09121110001" in flat
    assert "09121110002" not in flat



# =========================================================================== #
# The backstop for when a correctness check is itself broken
# =========================================================================== #
def test_the_round_cap_bounds_the_loop_even_with_a_broken_budget(alice,
                                                                monkeypatch):
    """Every other exit from the leech loop is a correctness check. This one is
    the backstop for when a correctness check is broken.

    Found by mutation testing: deleting the `probe_spend` call did not make a test
    fail, it made the suite HANG — because the budget shrinking was the only thing
    bounding the loop, and a four-digit prefix has ten million numbers behind it.
    """
    job_id, _ = _job(alice, target=999999, n=1)
    monkeypatch.setattr(config, "POOL_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "POOL_BLOCK", 5)
    monkeypatch.setattr(db, "probe_spend", lambda *a, **k: 0)   # budget broken
    _fake_probe(monkeypatch, hit_every=10000)                   # nothing found

    asyncio.run(pool.run_job(alice, job_id))                    # must terminate

    job = db.pool_get_job(alice, job_id)
    assert job["probed"] == 15, "the cap did not bound the work"
    assert job["halt_reason"] == "round_cap"


def test_the_round_cap_does_not_interfere_with_a_normal_job(alice, monkeypatch):
    """The backstop must be far enough away that real jobs never touch it."""
    job_id, _ = _job(alice, target=4, n=2)
    _fake_probe(monkeypatch, hit_every=2)
    asyncio.run(pool.run_job(alice, job_id))
    assert db.pool_get_job(alice, job_id)["halt_reason"] == "target"
