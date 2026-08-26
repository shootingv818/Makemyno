"""
A FloodWait froze the whole job; a partial backup never said why.

WHAT WAS WRONG
--------------
1. FLOODWAIT BLOCKED EVERYTHING.
   The multi-account sender answered a FloodWait with `await asyncio.sleep(wait)`
   inside the recipient loop, still holding the account's session claim. Telegram
   hands out waits measured in hours, so one throttled account froze the entire
   job: every other account queued behind it and the customer saw a send that had
   simply stopped. An account is now PARKED with a cooldown, the job carries on
   with the others, and the runner picks the account back up when its cooldown
   expires and continues from the recipients it still has left.

2. ACCOUNTS WERE WALKED ONCE.
   A single pass could never come back to a parked account, so a throttled
   account lost the rest of its recipients even though the limit expired long
   before the job ended.

3. A RECIPIENT IN FLIGHT WAS UNTRACKED.
   Nothing marked a recipient as being attempted, so a crash mid-send left no
   record: on resume that person was either messaged twice or never again. Rows
   are claimed 'inflight' before the send, requeued on resume, and turned into
   'uncertain' when an account is given up on — because we genuinely do not know
   whether that one arrived, and both "sent" and "failed" would be a claim we
   cannot support.

4. THE BACKUP CARD COUNTED WITHOUT EXPLAINING.
   "⚠️ partial — 1 worker(s) unreachable" was printed for a wrong SSH password, a
   firewalled port AND a worker that simply had no sessions directory yet. The
   first two are faults, the third is normal, and telling them apart needed SSH.

5. THE CHANNEL REPORT NEVER SAID WHETHER THE ADVERT LANDED.
   It reported "Members added: 300" whether or not the post was in the channel.

Every test below was mutation-verified.
"""
import asyncio
import os
import time

import pytest

import backup
import cards
import db
import telegram_multi_send as multi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_of(filename, name, kind="def"):
    """One function's source with comments and docstrings removed."""
    src = open(os.path.join(ROOT, filename), encoding="utf-8").read()
    start = src.index(f"{kind} {name}")
    body = src[start:]
    for marker in ("\nasync def ", "\ndef ", "\nclass "):
        at = body.find(marker, 10)
        if at != -1:
            body = body[:at]
    out, in_doc = [], False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"""', "'''")):
            ticks = stripped[:3]
            if not in_doc:
                in_doc = not (stripped.endswith(ticks) and len(stripped) > 3)
                continue
            in_doc = False
            continue
        if in_doc or stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


@pytest.fixture
def job(alice):
    """A job with one account and three recipients."""
    aid = db.tg_add_account(alice, "09120000001", session="s")
    job_id = db.tgm_create_job(alice, [{"kind": "text", "text": "hi"}], 0.01,
                               "both")
    db.tgm_add_account(alice, job_id, aid, "09120000001", 0)
    db.tgm_add_recipients(alice, job_id, aid,
                          [(str(i), {"kind": "user", "id": i}, False)
                           for i in (1, 2, 3)], 0)
    db.tgm_update_account(alice, job_id, aid, total=3, state="pending")
    db.tgm_update_job(alice, job_id, total=3)
    return job_id, aid


# --------------------------------------------------------------------------- #
# parking
# --------------------------------------------------------------------------- #
def test_parking_sets_a_cooldown_and_counts_the_round(alice, job):
    job_id, aid = job
    rounds = db.tgm_park_account(alice, job_id, aid, 3600, "FloodWait 3600s")
    assert rounds == 1
    account = db.tgm_job_accounts(alice, job_id)[0]
    assert account["state"] == "floodwait"
    assert account["cooldown_until"] > time.time() + 3000


def test_parking_returns_an_inflight_recipient_to_pending(alice, job):
    job_id, aid = job
    db.tgm_set_recipient(alice, job_id, 0, "inflight")
    db.tgm_park_account(alice, job_id, aid, 60, "FloodWait 60s")
    assert db.tgm_counts(alice, job_id).get("pending") == 3, \
        ("a recipient we never got a result for must be retried, not dropped "
         "silently")


def test_rounds_accumulate_across_parkings(alice, job):
    job_id, aid = job
    assert db.tgm_park_account(alice, job_id, aid, 10, "x") == 1
    assert db.tgm_park_account(alice, job_id, aid, 10, "x") == 2
    assert db.tgm_park_account(alice, job_id, aid, 10, "x") == 3


def test_next_cooldown_reports_the_soonest_wake_up(alice, job):
    job_id, aid = job
    assert db.tgm_next_cooldown(alice, job_id) is None
    db.tgm_park_account(alice, job_id, aid, 120, "x")
    remaining = db.tgm_next_cooldown(alice, job_id)
    assert remaining is not None and 100 < remaining <= 121


def test_an_expired_cooldown_wakes_the_account(alice, job):
    job_id, aid = job
    db.tgm_park_account(alice, job_id, aid, 1, "x")
    db.tgm_update_account(alice, job_id, aid, cooldown_until=time.time() - 5)
    assert db.tgm_wake_cooled(alice, job_id) == 1
    assert db.tgm_job_accounts(alice, job_id)[0]["state"] == "pending"


def test_a_live_cooldown_is_not_woken_early(alice, job):
    job_id, aid = job
    db.tgm_park_account(alice, job_id, aid, 3600, "x")
    assert db.tgm_wake_cooled(alice, job_id) == 0
    assert db.tgm_job_accounts(alice, job_id)[0]["state"] == "floodwait"


def test_giving_up_skips_the_rest_and_marks_the_inflight_one_uncertain(alice, job):
    job_id, aid = job
    db.tgm_set_recipient(alice, job_id, 0, "inflight")
    skipped, uncertain = db.tgm_give_up_account(alice, job_id, aid, "too many")
    assert (skipped, uncertain) == (2, 1), \
        "the in-flight recipient is uncertain, the untouched ones are skipped"
    counts = db.tgm_counts(alice, job_id)
    assert counts.get("uncertain") == 1 and counts.get("skipped") == 2
    assert db.tgm_job_accounts(alice, job_id)[0]["state"] == "failed"


def test_an_orphaned_inflight_row_is_quarantined_not_resent(alice, job):
    """It was claimed before the send and rewritten after it.

    So a process that died in between left no evidence either way, and requeueing
    it — which is what this used to do — sends that person the advert a SECOND
    time. Duplicate messages are what get an account reported, so the reference
    marks these 'uncertain' and never retries them; the card counts uncertain
    toward the total so the job still completes.
    """
    job_id, _aid = job
    db.tgm_set_recipient(alice, job_id, 1, "inflight")
    assert db.tgm_reset_inflight(alice, job_id) == 1
    counts = db.tgm_counts(alice, job_id)
    assert counts.get("uncertain") == 1
    assert counts.get("pending") == 2, "the claimed row must NOT go back in line"
    assert int(db.tgm_get_job(alice, job_id)["uncertain_count"]) == 1, \
        "the job counter must move too, or the card can never reach 100%"


def test_resume_requeues_stopped_accounts_but_not_failed_ones(alice, job):
    job_id, aid = job
    db.tgm_update_account(alice, job_id, aid, state="stopped")
    assert db.tgm_requeue_stopped_accounts(alice, job_id) == 1
    assert db.tgm_job_accounts(alice, job_id)[0]["state"] == "pending"

    db.tgm_update_account(alice, job_id, aid, state="failed")
    assert db.tgm_requeue_stopped_accounts(alice, job_id) == 0, \
        "a dead account must not be retried on every resume"


# --------------------------------------------------------------------------- #
# the runner honours all of it
# --------------------------------------------------------------------------- #
def test_a_long_floodwait_parks_instead_of_sleeping():
    code = _code_of("telegram_multi_send.py", "_run_account", kind="async def")
    assert "FLOOD_INLINE_MAX" in code, \
        "every FloodWait length is treated the same, so a long one blocks the job"
    assert 'stop_reason = "floodwait"' in code
    assert multi.FLOOD_INLINE_MAX <= 300, \
        "an inline wait this long still looks like a hang to the customer"


def test_the_runner_walks_accounts_in_rounds():
    code = _code_of("telegram_multi_send.py", "_run", kind="async def")
    assert "while not control" in code, \
        "a single pass can never return to a parked account"
    assert "tgm_next_cooldown" in code and "tgm_wake_cooled" in code


def test_only_runnable_accounts_are_restarted():
    """The round loop re-ran an account that had hit its error ceiling."""
    code = _code_of("telegram_multi_send.py", "_run", kind="async def")
    assert "RUNNABLE_STATES" in code
    for state in ("done", "failed", "stopped", "skipped", "floodwait"):
        assert state not in multi.RUNNABLE_STATES, state


def test_the_cooldown_wait_can_be_interrupted():
    control = {"stop": False}

    async def _go():
        task = asyncio.ensure_future(
            multi._sleep_unless_stopped(control, 30, step=0.01))
        await asyncio.sleep(0.05)
        control["stop"] = True
        assert await asyncio.wait_for(task, timeout=2) is False

    asyncio.run(_go())


def test_a_recipient_is_claimed_before_the_send():
    code = _code_of("telegram_multi_send.py", "_run_account", kind="async def")
    assert '"inflight"' in code, \
        ("without claiming the row first, a crash mid-send leaves no trace and "
         "that person is either messaged twice or never again")


def test_the_live_card_shows_a_parked_job_as_waiting_not_dead():
    code = _code_of("telegram_multi_send.py", "progress_card")
    assert "floodwait" in code
    assert "Resumes in" in code, \
        "a job waiting out a limit looks identical to a dead one without this"
    assert "uncertain" in code


# --------------------------------------------------------------------------- #
# backup reasons
# --------------------------------------------------------------------------- #
def test_backup_card_prints_the_reason_not_just_a_count():
    rows = backup.summary_rows({
        "rb_local": 7, "rb_workers": 0, "tg": 2,
        "unreachable": ["wk-0db9: ssh connect failed: TimeoutError: timed out"]})
    blob = "\n".join(rows)
    assert "wk-0db9" in blob and "ssh connect failed" in blob, \
        ("a wrong password, a firewalled port and a worker with no sessions all "
         "printed the same line, and only SSH could tell them apart")


def test_backup_card_says_complete_when_nothing_failed():
    blob = "\n".join(backup.summary_rows(
        {"rb_local": 7, "rb_workers": 3, "tg": 2, "unreachable": []}))
    assert "complete" in blob
    assert "Why" not in blob


def test_backup_card_caps_the_reason_list():
    rows = backup.summary_rows({"rb_local": 0, "rb_workers": 0, "tg": 0,
                                "unreachable": [f"w{i}: boom" for i in range(9)]})
    blob = "\n".join(rows)
    assert blob.count("Why") == 5 and "+4" in blob, \
        "nine reasons would push the rest of the card out of view"


def test_a_missing_sessions_directory_is_not_reported_as_a_fault():
    """A worker that has never held an account has no sessions/ yet."""
    code = _code_of("worker.py", "_one", kind="async def")
    assert "No such file" in code, \
        ("treating an absent directory as unreachable sent the owner looking "
         "for a network fault that did not exist")
    assert "ssh connect failed" in code, "a real connect failure must say so"


# --------------------------------------------------------------------------- #
# channel reporting
# --------------------------------------------------------------------------- #
def test_the_channel_report_states_whether_the_post_landed():
    code = _code_of("rubika_panel.py", "_channel_flow", kind="async def")
    # The ROW itself, not merely the variable: the reason was being computed and
    # then not printed, and asserting on the variable alone let that through.
    assert 'cards.kv("Post", post_line)' in code, \
        ("'Members added: 300' on a channel with no advert in it makes the "
         "customer believe the campaign ran")
    assert "marker_found" in code and "forward_error" in code
    assert "Members added" in code


def test_the_channel_report_distinguishes_the_three_failures():
    """A missing marker, a failed forward and no marker at all differ."""
    code = _code_of("rubika_panel.py", "_channel_flow", kind="async def")
    assert "elif not marker_found:" in code, \
        "a marker that is absent from Saved needs a different message"
    assert "forward_error or" in code, \
        "a forward that failed must report the platform's own reason"


def test_the_channel_flow_reports_progress_while_it_runs():
    code = _code_of("rubika_panel.py", "_channel_flow", kind="async def")
    # Three at minimum: before creating, and after creating on each of the remote
    # and local paths. Counting matters — one surviving call let a mutation that
    # removed the first update pass.
    assert code.count("await _live(") >= 3, \
        ("seeding 300 members takes many minutes; with no updates a working "
         "campaign is indistinguishable from a hung one and gets restarted")
    assert "⏳ ساخت کانال" in code, "the first step must announce itself"
