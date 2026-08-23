"""
Eight production errors: ONE was a bug, five needed plain Persian, two were both.

THE BUG: A LOGIN CLAIM THAT NEVER LET GO
    409 {'busy': True, 'what': 'login', 'held_for': 4035}
login_start took the session claim and released it only on EXCEPTION. On success
it held the claim expecting /login/code to release it — so a customer who opened a
login and walked away held their own account hostage for BUSY_STALE_SEC, which is
two hours. 4035 seconds is 67 minutes of an account being unusable because
somebody pressed a button once and stopped.

The reference takes NO claim on login at all. Two changes bring us back in line
without losing the protection that matters:
  * a login claim gets its own short TTL — the code it is waiting for expires in
    about two minutes, so a two-hour lifetime was never right;
  * the same (customer, phone) may TAKE OVER its own login claim, because the key
    is scoped to one customer and one number: the only thing that can collide with
    a login is that same person's earlier attempt. A claim of a DIFFERENT kind (a
    running send) is still a genuine conflict and is still refused.

NOT BUGS — the platform or the customer, needing a sentence instead of a code:
  NOT_REGISTERED        the Rubika account was deleted; "log in again" is a loop
                        the customer cannot win
  CodeIsInvalid         they mistyped the code
  PasswordHashInvalid   they mistyped the 2FA password
  PhoneNumberInvalid    +98363399307 is nine digits after the country code
  INVALID_AUTH with client_show_message
                        Rubika sent its OWN Persian explanation — that a new
                        account cannot be created yet because the previous one was
                        deleted too recently — and we threw it away and showed a
                        generic apology instead

An error code for a mistyped password is worse than useless: it tells the customer
to contact support about something they could fix in five seconds, and it fills the
owner's inbox. Those failures now get the explanation and NO code.

Every test below was mutation-verified with __pycache__ cleared.
"""
import os

import pytest

import busy
import config
import iran_numbers
import logbus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The real strings, copied from the production cards.
NOT_REGISTERED = "{'status': 'ERROR_ACTION', 'status_det': 'NOT_REGISTERED'}"
RUBIKA_ALERT = (
    "InvalidAuth: {'status': 'ERROR_GENERIC', 'status_det': 'INVALID_AUTH', "
    "'client_show_message': {'link': {'type': 'alert', 'alert_data': "
    "{'message': 'با توجه به زمان حذف حساب کاربری قبلی شما، ساخت حساب کاربری "
    "جدید تا ۴۸ ساعت امکان\u200cپذیر نیست.'}}}}")


_TQ = chr(34) * 3
_SQ = chr(39) * 3


def _code_only(text: str) -> str:
    """Strip comments and docstrings.

    Three mutation checks first passed on MY OWN comments: the note explaining
    why takeover=True is needed contains the literal "takeover=True", and the
    note explaining need_active=False contains "need_active=False". Deleting the
    real code left the explanation behind and the tests stayed green.
    """
    out, in_doc, delim = [], False, None
    for line in text.splitlines():
        stripped = line.strip()
        if in_doc:
            if delim in stripped:
                in_doc = False
            continue
        if stripped.startswith(_TQ) or stripped.startswith(_SQ):
            delim = stripped[:3]
            if delim in stripped[3:]:
                continue
            in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# THE BUG: the login claim
# --------------------------------------------------------------------------- #
def test_a_login_claim_expires_long_before_a_job_claim():
    assert busy._ttl_for("login") < config.BUSY_STALE_SEC, \
        ("a login waits for a code that expires in about two minutes; giving it "
         "the two-hour job lifetime locked accounts out for an hour")
    assert busy._ttl_for("send") == float(config.BUSY_STALE_SEC), \
        "a real send must keep the long lifetime"


def test_the_same_login_can_be_restarted():
    busy.clear_all()
    assert busy.acquire("c1:989120000001", "login", customer_id=1) is True
    assert busy.acquire("c1:989120000001", "login", customer_id=1,
                        takeover=True) is True, \
        ("pressing the login button twice must not lock the customer out of "
         "their own account")


def test_a_login_cannot_take_over_a_running_send():
    busy.clear_all()
    assert busy.acquire("c1:989120000001", "send", customer_id=1) is True
    assert busy.acquire("c1:989120000001", "login", customer_id=1,
                        takeover=True) is False, \
        "a send in progress is a real conflict; only the SAME kind may take over"


def test_takeover_is_off_by_default():
    busy.clear_all()
    busy.acquire("c1:989120000001", "login", customer_id=1)
    assert busy.acquire("c1:989120000001", "login", customer_id=1) is False, \
        "every other caller must keep the old refuse-if-held behaviour"


def test_a_stale_login_claim_is_reclaimed(monkeypatch):
    busy.clear_all()
    busy.acquire("c1:989120000001", "login", customer_id=1)
    entry = busy._held["c1:989120000001"]
    entry["since"] = entry["since"] - busy._ttl_for("login") - 5
    assert busy.is_busy("c1:989120000001") is False


def test_the_worker_login_endpoint_asks_for_takeover():
    src = open(os.path.join(ROOT, "worker_api.py"), encoding="utf-8").read()
    start = src.index("async def login_start")
    section = _code_only(src[start:src.index("@app.post", start + 10)])
    assert "takeover=True" in section, \
        ("without it a login that was never finished refuses every retry with "
         "409 for two hours")


# --------------------------------------------------------------------------- #
# Rubika's own message must be shown, not discarded
# --------------------------------------------------------------------------- #
def test_rubikas_own_persian_message_is_extracted():
    got = logbus.platform_message(RuntimeError(RUBIKA_ALERT))
    assert "ساخت حساب کاربری" in got, \
        ("the platform explained itself better than we can; discarding that and "
         "showing a generic apology is indefensible")


def test_the_platform_message_wins_over_the_generic_one():
    got = logbus.humanize_error(RuntimeError(RUBIKA_ALERT), kind="login")
    assert "ساخت حساب کاربری" in got
    assert "پشتیبانی" not in got, \
        "there is nothing for support to do about a Rubika waiting period"


def test_an_error_without_a_platform_message_falls_back():
    assert logbus.platform_message(RuntimeError("boom")) == ""


# --------------------------------------------------------------------------- #
# each real failure gets its own sentence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("error,must_contain", [
    (RuntimeError("sign_in status: CodeIsInvalid"), "کد تأیید"),
    (RuntimeError("PasswordHashInvalidError: the password is invalid"), "رمز"),
    (RuntimeError(NOT_REGISTERED), "حساب فعالی ندارد"),
    (RuntimeError("PhoneNumberInvalidError: The phone number is invalid"),
     "شماره درست نیست"),
    (RuntimeError("TooRequests"), "محدودیت"),
    (RuntimeError("{'busy': True, 'what': 'login', 'held_for': 4035}"),
     "کار دیگری در جریان"),
])
def test_each_failure_has_its_own_wording(error, must_contain):
    assert must_contain in logbus.humanize_error(error)


def test_not_registered_does_not_tell_them_to_log_in_again():
    """That is a loop they cannot win: the account no longer exists."""
    message = logbus.humanize_error(RuntimeError(NOT_REGISTERED))
    assert "دوباره وارد شو" not in message
    assert "شمارهٔ دیگری" in message


def test_a_revoked_session_does_tell_them_to_log_in_again():
    message = logbus.humanize_error(RuntimeError("AUTH_FROM_ANOTHER"))
    assert "وارد شو" in message


def test_an_unrecognised_error_uses_the_kind():
    assert "ورود" in logbus.humanize_error(RuntimeError("???"), kind="login")
    assert "خروجی" in logbus.humanize_error(RuntimeError("???"), kind="export")


def test_humanize_never_leaks_a_repr():
    for error in (RuntimeError("Traceback: File x line 3"),
                  ValueError("<object at 0x7f39>")):
        got = logbus.humanize_error(error)
        assert "0x" not in got and "Traceback" not in got


# --------------------------------------------------------------------------- #
# a mistake the customer can fix gets no error code
# --------------------------------------------------------------------------- #
def test_self_inflicted_failures_are_recognised():
    for error in (RuntimeError("sign_in status: CodeIsInvalid"),
                  RuntimeError("PasswordHashInvalidError"),
                  RuntimeError("PhoneNumberInvalidError"),
                  RuntimeError(NOT_REGISTERED)):
        assert logbus._self_inflicted(error) is True, error


def test_platform_and_internal_failures_still_get_a_code():
    for error in (RuntimeError("TooRequests"), RuntimeError("boom"),
                  RuntimeError("INVALID_AUTH")):
        assert logbus._self_inflicted(error) is False, error


def test_the_customer_card_omits_the_code_for_a_typo():
    src = open(os.path.join(ROOT, "logbus.py"), encoding="utf-8").read()
    start = src.index("if notify and cid:")
    section = src[start:src.index("_SELF_INFLICTED = (")]
    assert "_self_inflicted(exc)" in section, \
        ("an error code for a mistyped password tells the customer to contact "
         "support about their own typo, and fills the owner's inbox")
    assert "humanize_error(exc" in section


def test_the_login_cards_show_the_reason_not_a_code():
    for filename, needle in (("rubika_panel.py", 'humanize_error(exc, kind="code")'),
                             ("tg_panel.py", 'humanize_error(exc, kind="code")')):
        src = open(os.path.join(ROOT, filename), encoding="utf-8").read()
        assert needle in src, f"{filename} still answers a bad code with a code"


# --------------------------------------------------------------------------- #
# tools: the Iranian number generator
# --------------------------------------------------------------------------- #
def test_a_prefix_is_accepted_in_every_form():
    for raw in ("0913", "913", "+98913", "0098913", "98913"):
        assert iran_numbers.clean_prefix(raw) == "0913", raw
    assert iran_numbers.clean_prefix("abc") is None


def test_the_operator_and_region_are_reported():
    assert iran_numbers.detect("0917") == ("همراه اول",
                                           "فارس / بوشهر / هرمزگان (شیراز)")
    assert iran_numbers.detect("0901")[0] == "ایرانسل"
    assert iran_numbers.detect("0921")[0] == "رایتل"
    assert iran_numbers.detect("0800") == (None, None)


def test_only_real_mobile_prefixes_are_valid():
    assert iran_numbers.is_valid_prefix("0913") is True
    assert iran_numbers.is_valid_prefix("0800") is False
    assert iran_numbers.is_valid_prefix("021") is False


def test_generated_numbers_are_eleven_digits_and_unique():
    numbers = iran_numbers.gen_unique("0913", 200)
    assert len(numbers) == 200
    assert len(set(numbers)) == 200
    assert all(len(n) == 11 and n.startswith("0913") for n in numbers)


def test_a_long_prefix_only_fills_what_is_left():
    numbers = iran_numbers.gen_unique("0913613", 50)
    assert all(n.startswith("0913613") and len(n) == 11 for n in numbers)


def test_a_search_space_smaller_than_the_request_does_not_hang():
    """0913613456 leaves ONE digit: at most ten numbers exist."""
    numbers = iran_numbers.gen_unique("0913613456", 500)
    assert 0 < len(numbers) <= 10
    assert len(set(numbers)) == len(numbers)


def test_existing_numbers_are_skipped():
    first = iran_numbers.gen_unique("0913613456", 5)
    second = iran_numbers.gen_unique("0913613456", 5, existing=first)
    assert not (set(first) & set(second))


def test_the_tool_reports_the_cap_instead_of_applying_it_silently():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_numgen")
    section = src[start:src.index("async def _bot_send_file")]
    assert "asked > count" in section, \
        ("a customer who asked for 50000 and silently got 5000 would treat the "
         "file as complete")
    assert "NUMGEN_MAX" in section


def test_the_tool_admits_the_numbers_are_not_verified():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_numgen")
    section = src[start:src.index("async def _bot_send_file")]
    assert ("تصادفی ساخته شده" in section
            and "تضمینی نیست" in section), \
        ("these are random digits; letting a customer believe they are real "
         "accounts wastes their probe budget")


def test_the_tool_needs_no_active_subscription():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def tools_cb")
    section = _code_only(src[start:src.index("async def tool_numgen_cb")])
    assert "need_active=False" in section, \
        "a lapsed customer deciding whether to renew should still see the tools"
