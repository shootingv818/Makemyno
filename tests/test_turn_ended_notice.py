"""
The customer was never told why the send moved on to the next account.

WHAT WAS WRONG
--------------
The parked / gave-up notices were written with logbus.customer_action, and that
function posts to the OWNER's log group and only mirrors to the customer when
`mirror=True` is passed. It never was. So the person actually waiting on the send
watched an account quietly turn into a ⏳ with no explanation anywhere, and the
job as a whole look frozen while it was merely waiting out a limit.

A customer who believes a send has died stops it and starts it again. That
double-messages everybody who was already reached, which is precisely what gets
accounts reported — so the missing message was not cosmetic.

WHAT IT SAYS NOW
----------------
Every way a turn can end produces one card TO THE CUSTOMER: what the account
managed, what it did not, and — the part that matters — what the system is going
to do next. Ends that need no action say so; ends that do (a dead session, an
error burst) say which action.

Every test below was mutation-verified.
"""
import asyncio
import os

import pytest

import cards
import db
import telegram_multi_send as multi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Bot:
    """Captures what would be sent to the customer."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def edit_message(self, *a, **k):
        pass


@pytest.fixture
def bot(monkeypatch):
    fake = _Bot()
    monkeypatch.setattr(multi, "_bot", fake)
    return fake


def _notice(alice, bot, outcome, **kw):
    kw.setdefault("sent", 10)
    kw.setdefault("failed", 0)
    kw.setdefault("skipped", 0)
    kw.setdefault("total", 100)
    asyncio.run(multi._tell_customer_turn_ended(
        alice, "job1", "09120000001", outcome, **kw))
    assert bot.sent, f"nothing was sent to the customer for {outcome!r}"
    return bot.sent[-1][1]


# --------------------------------------------------------------------------- #
# it reaches the customer at all
# --------------------------------------------------------------------------- #
def test_the_notice_is_sent_to_the_customer(alice, bot):
    _notice(alice, bot, "floodwait", resumes_in=600)
    chat_id, _text = bot.sent[-1]
    assert int(chat_id) == int(alice), \
        ("the log group already had this; the person waiting on the send is who "
         "needed it")


def test_every_outcome_has_its_own_wording(alice, bot):
    seen = set()
    for outcome in ("done", "floodwait", "gave_up", "auth_failed",
                    "error_burst", "stopped"):
        bot.sent.clear()
        text = _notice(alice, bot, outcome, resumes_in=60)
        assert text not in seen, f"{outcome} reuses another outcome's wording"
        seen.add(text)


def test_an_unknown_outcome_still_says_something_useful(alice, bot):
    text = _notice(alice, bot, "something_new")
    assert "اکانت بعدی" in text, \
        "a state we did not anticipate must not produce a blank card"


# --------------------------------------------------------------------------- #
# the wording actually answers "why did it move on, and what now"
# --------------------------------------------------------------------------- #
def test_a_throttled_account_is_described_as_temporary_and_automatic(alice, bot):
    text = _notice(alice, bot, "floodwait", resumes_in=1800)
    assert "متوقف نشده" in text, \
        "the customer must understand the send is alive, not dead"
    assert "خودش" in text and "ادامه می‌دهد" in text, \
        "and that it resumes without them doing anything"
    assert "30 دقیقه" in text, "and roughly when"


def test_a_dead_session_tells_the_customer_what_to_do(alice, bot):
    text = _notice(alice, bot, "auth_failed")
    assert "ورود" in text or "وارد" in text, \
        "a dead session needs a fresh login; the card must say so"


def test_giving_up_admits_the_recipients_that_were_not_reached(alice, bot):
    text = _notice(alice, bot, "gave_up", sent=40, left_unsent=260)
    assert "260" in text, \
        ("claiming a finish while 260 people were never messaged is the failure "
         "mode this whole card exists to prevent")
    assert "ارسال نشدند" in text


def test_an_error_burst_offers_the_continue_button(alice, bot):
    text = _notice(alice, bot, "error_burst", failed=5)
    assert "ادامه" in text


def test_uncertain_is_explained_not_just_counted(alice, bot):
    text = _notice(alice, bot, "gave_up", uncertain=3, left_unsent=10)
    assert "Uncertain" in text and "معلوم نشد" in text, \
        "a bare number nobody can interpret is noise, not information"


def test_a_clean_finish_says_it_reached_everyone(alice, bot):
    text = _notice(alice, bot, "done", sent=100, total=100)
    assert "همهٔ مخاطبانش" in text


def test_counts_the_customer_cares_about_are_present(alice, bot):
    text = _notice(alice, bot, "error_burst", sent=37, failed=5, skipped=2,
                   total=400)
    assert "37" in text and "400" in text
    assert "5" in text and "2" in text


def test_zero_rows_are_omitted(alice, bot):
    """An always-present "Failed: 0" trains people to ignore the row."""
    text = _notice(alice, bot, "done", sent=100, failed=0, skipped=0, total=100)
    assert "Failed" not in text and "Skipped" not in text
    assert "Uncertain" not in text and "Not sent" not in text


# --------------------------------------------------------------------------- #
# a notice must never be able to break a send
# --------------------------------------------------------------------------- #
def test_a_failing_bot_does_not_break_the_send(alice, monkeypatch):
    class _Broken:
        async def send_message(self, *a, **k):
            raise RuntimeError("telegram is down")

    monkeypatch.setattr(multi, "_bot", _Broken())
    # must not raise
    asyncio.run(multi._tell_customer_turn_ended(
        alice, "j", "0912", "floodwait", sent=1, failed=0, skipped=0, total=2,
        resumes_in=10))


def test_no_bot_bound_is_survivable(alice, monkeypatch):
    monkeypatch.setattr(multi, "_bot", None)
    asyncio.run(multi._tell_customer_turn_ended(
        alice, "j", "0912", "done", sent=1, failed=0, skipped=0, total=1))


# --------------------------------------------------------------------------- #
# the human-readable wait
# --------------------------------------------------------------------------- #
def test_wait_is_rendered_in_units_a_person_reads():
    assert multi._human_wait(45) == "45 ثانیه"
    assert multi._human_wait(600) == "10 دقیقه"
    assert multi._human_wait(3600) == "1 ساعت"
    assert multi._human_wait(5400) == "1 ساعت و 30 دقیقه"
    assert multi._human_wait(-5) == "0 ثانیه"


# --------------------------------------------------------------------------- #
# the runner sends it, and the live card explains each account
# --------------------------------------------------------------------------- #
def _code_of(name, kind="async def"):
    src = open(os.path.join(ROOT, "telegram_multi_send.py"),
               encoding="utf-8").read()
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


def test_every_end_of_turn_path_notifies_the_customer():
    code = _code_of("_run_account")
    assert code.count("_tell_customer_turn_ended(") >= 3, \
        ("all three branches — parked, given up, and every other ending — must "
         "explain themselves; a silent switch is the original bug")


def test_the_waiting_job_tells_the_customer_directly():
    code = _code_of("_run")
    assert "ارسال در انتظار است" in code, \
        ("when every account is throttled nothing moves at all, and that is "
         "exactly when silence reads as a dead send")


def test_the_live_card_annotates_each_account_state():
    code = _code_of("progress_card", kind="def")
    assert "_ACCOUNT_NOTE_FA" in code, \
        "a coloured dot does not tell the customer a session needs re-login"
    for state in ("floodwait", "failed", "stopped"):
        assert state in multi._ACCOUNT_NOTE_FA, state
