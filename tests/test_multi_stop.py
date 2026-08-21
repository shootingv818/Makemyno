"""
The Rubika multi-account send had no way to stop it.

The card carried no buttons at all, so the only way to end a run was to open each
account's own screen and stop it there — and an account that had not started yet
had no screen. A campaign going wrong could not be halted.

Three controls now, and the distinction between them matters:

  ⛔ توقف همه       stop the running accounts AND cancel the queue
  ⏹ توقف اکانت فعلی  end the current turn only, carry on with the next account
  ✅ ادامه           restart from where it stopped, skipping anyone already reached

The run-level flag is what makes "stop all" honest. Setting only each running
account's flag would let the NEXT account start a second later, which reads as the
button being broken — and is worse than no button, because the customer presses it
again and again while messages keep going out.

Every test below was mutation-verified with __pycache__ cleared, after a previous
round found four mutations silently validating nothing because pytest was running
stale bytecode.
"""
import os

import pytest

import rubika_panel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code(name, kind="async def", filename="rubika_panel.py"):
    """One function's code, sliced by INDENTATION.

    These handlers are nested inside register(bot), so they are indented. Looking
    for the next "\\nasync def " at column 0 never matched them and the slice ran
    to the end of the file — which swallowed every following handler. Two mutation
    checks passed on strings belonging to a DIFFERENT handler because of it, and
    reported a removed ownership check and a removed concurrency guard as present.
    Stop at the next line that is a def or a decorator at the same indent or less.
    """
    src = open(os.path.join(ROOT, filename), encoding="utf-8").read()
    start = src.index(f"{kind} {name}")
    line_start = src.rfind("\n", 0, start) + 1
    indent = start - line_start
    lines = src[line_start:].splitlines()
    kept = [lines[0]]
    for line in lines[1:]:
        if line.strip():
            here = len(line) - len(line.lstrip())
            if here <= indent and (line.lstrip().startswith(("def ", "async def ",
                                                            "@", "class "))):
                break
        kept.append(line)
    body = "\n".join(kept)
    out, in_doc, delim = [], False, None
    for line in body.splitlines():
        stripped = line.strip()
        if in_doc:
            if delim in stripped:
                in_doc = False
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            delim = stripped[:3]
            if delim in stripped[3:]:
                continue
            in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


def _nested(outer, inner):
    """A nested function's body, for checks inside _run_multi."""
    src = open(os.path.join(ROOT, "rubika_panel.py"), encoding="utf-8").read()
    start = src.index(f"async def {outer}")
    body = src[start:]
    at = body.index(f"async def {inner}")
    rest = body[at:]
    end = rest.find("\n    async def ", 10)
    if end == -1:
        end = rest.find("\n    try:", 10)
    return rest[:end if end != -1 else len(rest)]


# --------------------------------------------------------------------------- #
# the buttons exist and are on the live card
# --------------------------------------------------------------------------- #
def test_the_card_carries_stop_controls():
    buttons = rubika_panel._multi_buttons(5818420346)
    flat = str(buttons)
    assert "rbmstop_5818420346" in flat, "there was no way to stop a run"
    assert "rbmskip_5818420346" in flat
    assert "توقف همه" in flat


def test_the_buttons_are_attached_to_every_card_edit():
    """A button that only appears on the first render is not a control."""
    refresh = _nested("_run_multi", "_refresh")
    assert "_multi_buttons(customer_id)" in refresh, \
        "the refresher replaces the message; without buttons they vanish"
    run = _code("_run_multi")
    assert run.count("_multi_buttons(customer_id)") >= 2, \
        "the first render needs them too"


def test_the_buttons_are_scoped_to_one_customer():
    assert "rbmstop_1" in str(rubika_panel._multi_buttons(1))
    assert "rbmstop_2" in str(rubika_panel._multi_buttons(2))


# --------------------------------------------------------------------------- #
# stop all: the queue must be cancelled too
# --------------------------------------------------------------------------- #
def test_the_run_state_carries_a_stop_flag():
    run = _code("_run_multi")
    assert '"stop": False' in run, \
        "without a run-level flag a stop cannot cancel accounts that have not begun"
    assert '"account_ids"' in run, \
        "the handler needs to find every account's control dict"


def test_the_queue_checks_the_flag_before_starting_an_account():
    sequential = _nested("_run_multi", "_sequential")
    assert 'if state["stop"]:' in sequential, \
        ("pressing stop and then watching the next account start anyway reads as "
         "the button being broken")
    assert 'continue' in sequential
    # And the flag must be checked BEFORE the send is launched, not after.
    assert sequential.index('state["stop"]') < sequential.index("_prepare_and_send")


def test_a_cancelled_account_is_marked_and_given_a_reason():
    sequential = _nested("_run_multi", "_sequential")
    assert 'slot["state"] = "stopped"' in sequential
    assert 'slot["reason"]' in sequential, \
        "an account that shows as stopped must say it was the customer's request"


def test_stop_all_sets_both_the_run_flag_and_every_account(monkeypatch):
    """The handler's contract, checked at source level.

    A behavioural test would need a live Telethon event; what matters here is that
    it does BOTH things, because doing only one is the defect.
    """
    handler = _code("rb_multi_stop")
    assert 'state["stop"] = True' in handler
    assert 'ctl["stop"] = True' in handler
    # The flag must be set OUTSIDE the loop. Moving it inside still works while
    # there are accounts to iterate, but with an empty list the queue is never
    # closed at all — and asserting only on the order of the two lines let exactly
    # that mutation through.
    assert handler.index('state["stop"] = True') < handler.index("for account_id"), \
        ("the queue must be closed unconditionally, before and independently of "
         "walking the running accounts")


def test_stop_all_refuses_another_customers_card():
    handler = _code("rb_multi_stop")
    assert "!= int(uid)" in handler, \
        "a callback id is guessable; the card must belong to the presser"


def test_stop_all_says_so_when_nothing_is_running():
    handler = _code("rb_multi_stop")
    assert "ارسالی در جریان نیست" in handler, \
        "a silent no-op leaves the customer pressing the button again"


# --------------------------------------------------------------------------- #
# skip: end this turn only
# --------------------------------------------------------------------------- #
def test_skip_does_not_touch_the_run_flag():
    handler = _code("rb_multi_skip")
    assert 'state["stop"] = True' not in handler, \
        ("one throttled account is not a reason to abandon a campaign the other "
         "accounts are running fine")
    assert 'ctl["stop"] = True' in handler


def test_skip_reports_when_no_account_is_sending():
    handler = _code("rb_multi_skip")
    assert "هیچ اکانتی همین حالا در حال ارسال نیست" in handler


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #
def test_a_stopped_run_offers_continue_instead_of_a_dead_end():
    run = _code("_run_multi")
    assert "rbmresume_" in run, "a stopped run must be one press from resuming"
    assert 'if state["stop"]:' in run


def test_resume_refuses_while_a_run_is_still_going():
    handler = _code("rb_multi_resume")
    assert "_multi_jobs.get(int(uid))" in handler, \
        "two concurrent multi-runs would double-message every shared contact"


def test_resume_relies_on_the_already_sent_ledger():
    handler = _code("rb_multi_resume")
    assert "دوباره پیام" in handler, \
        ("restarting from zero would message thousands twice, which is what gets "
         "accounts reported — the card must say it does not")


def test_the_finished_card_drops_the_stop_buttons():
    run = _code("_run_multi")
    tail = run[run.index("buttons = [_back(b\"rbaccs\")]"):]
    assert "rbmstop_" not in tail, \
        "offering stop on a finished run is a button that cannot do anything"
