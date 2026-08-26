"""
"The marker IS in the post but it says it is not" — three separate defects.

1. PAGING DIED AFTER THE FIRST PAGE.
   rubpy declares get_messages(object_guid, max_id: str, limit: str). The first
   page passed the literal "0" and worked; every later page passed
   _msg_id_of(messages[-1]) straight through, which is an int. The platform
   rejects that, and the bare `except: break` ended the search — so only the
   NEWEST 20 messages in Saved were ever examined. An account whose marked post
   sat further back reported "marker not found" forever, while an account that had
   just posted it worked. That is exactly the pattern seen in production: one
   account of 1376 contacts failed, another of 34 did not.

2. A MEDIA CAPTION WAS NOT SEARCHED.
   The advert is usually a photo or a file, and some builds carry the caption on
   the attachment rather than on the message, so _msg_text_of returned "".

3. THE MARKER WAS NOT STRIPPED ON READ.
   The reference strips on write AND read; this stripped only on write, so any row
   written by an older build kept its trailing newline, and
   `"هسهسه\\n" in "هسهسه"` is False.

Plus the reporting rules the owner needs: a search that finds nothing says how
many messages it scanned, and a send that reached nobody is never called done.

Every test below was mutation-verified.
"""
import asyncio
import os

import pytest

import config
import db
import rubika_client as rb
import rubika_panel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Saved:
    """A Saved-Messages chat that pages exactly as the platform does.

    The important behaviour, and the reason a looser fake would have proved
    nothing: max_id MUST arrive as a string. An int raises, just as the real API
    rejects it.
    """

    def __init__(self, texts, page=20):
        # newest first, ids descending
        self.msgs = [{"message_id": 10_000 - i, "text": t}
                     for i, t in enumerate(texts)]
        self.page = page
        self.calls = []

    async def get_me(self):
        return {"user": {"user_guid": "u_self"}}

    async def get_messages(self, object_guid, max_id, limit):
        if not isinstance(max_id, str):
            raise RuntimeError("INVALID_INPUT: max_id must be a string")
        self.calls.append(max_id)
        start = 0
        if max_id != "0":
            for idx, m in enumerate(self.msgs):
                if str(m["message_id"]) == max_id:
                    start = idx + 1
                    break
        return {"messages": self.msgs[start:start + int(limit)]}


@pytest.fixture
def self_guid(monkeypatch):
    async def _g(_client):
        return "u_self"
    monkeypatch.setattr(rb, "get_self_guid", _g)


# --------------------------------------------------------------------------- #
# 1. paging
# --------------------------------------------------------------------------- #
def test_marker_is_found_on_the_first_page(self_guid):
    chat = _Saved(["hello", "buy now MARK", "other"])
    got = asyncio.run(rb.find_marked_message(chat, "MARK"))
    assert got == 9999


def test_marker_is_found_far_beyond_the_first_page(self_guid):
    """The whole defect: the post is real but not among the newest 20."""
    texts = ["filler"] * 130 + ["the advert MARK"] + ["filler"] * 5
    chat = _Saved(texts)
    got = asyncio.run(rb.find_marked_message(chat, "MARK"))
    assert got == 10_000 - 130, \
        ("the search must page past the newest 20 messages; stopping there is "
         "why a marker that plainly exists was reported missing")
    assert len(chat.calls) > 1, "it never asked for a second page"


def test_every_page_request_passes_max_id_as_a_string(self_guid):
    chat = _Saved(["filler"] * 60 + ["x MARK"])
    asyncio.run(rb.find_marked_message(chat, "MARK"))
    assert all(isinstance(c, str) for c in chat.calls)


def test_search_gives_up_when_the_platform_ignores_max_id(self_guid):
    """A server that keeps returning page one must not cause 50 rescans."""
    class _Stuck(_Saved):
        async def get_messages(self, object_guid, max_id, limit):
            self.calls.append(max_id)
            return {"messages": self.msgs[:int(limit)]}

    chat = _Stuck(["filler"] * 40)
    assert asyncio.run(rb.find_marked_message(chat, "MARK")) is None
    assert len(chat.calls) <= 3, \
        "a page we have already walked means stop, not carry on 50 times"


def test_missing_marker_reports_how_far_it_looked(self_guid):
    chat = _Saved(["a", "b", "c"])
    assert asyncio.run(rb.find_marked_message(chat, "NOPE")) is None
    scan = rb.last_marker_scan()
    assert scan["scanned"] == 3, \
        ("'marker not found' with no numbers cannot tell an empty chat from a "
         "search that stopped early")
    assert scan["marker"] == "NOPE"


def test_search_reports_the_error_that_stopped_it(self_guid):
    class _Broken(_Saved):
        async def get_messages(self, *a, **k):
            raise RuntimeError("TOO_REQUESTS")

    assert asyncio.run(rb.find_marked_message(_Broken([]), "MARK")) is None
    assert "TOO_REQUESTS" in rb.last_marker_scan()["error"]


def test_an_auth_failure_still_propagates(self_guid):
    class _Dead(_Saved):
        async def get_messages(self, *a, **k):
            raise RuntimeError("{'status_det': 'INVALID_AUTH'}")

    with pytest.raises(RuntimeError):
        asyncio.run(rb.find_marked_message(_Dead([]), "MARK"))


# --------------------------------------------------------------------------- #
# 2. media captions
# --------------------------------------------------------------------------- #
def test_marker_found_in_a_media_caption(self_guid):
    chat = _Saved([])
    chat.msgs = [{"message_id": 500,
                  "file_inline": {"caption": "big sale MARK"}}]
    assert asyncio.run(rb.find_marked_message(chat, "MARK")) == 500, \
        "the advert is usually media; its caption must be searched too"


def test_plain_text_still_wins_over_the_attachment():
    msg = {"text": "on the message", "file_inline": {"caption": "on the file"}}
    assert rb._msg_text_of(msg) == "on the message"


def test_msg_text_of_is_empty_for_a_message_with_neither():
    assert rb._msg_text_of({"message_id": 1}) == ""


# --------------------------------------------------------------------------- #
# 3. the marker string itself
# --------------------------------------------------------------------------- #
def test_get_marker_strips_stored_whitespace(alice):
    db.set_setting(alice, "rb_marker", "هسهسه\n")
    assert db.get_marker(alice) == "هسهسه", \
        ("an unstripped marker can never match: '\\n' is not in the post text, "
         "so a correct marker is reported missing")


def test_get_marker_falls_back_to_the_configured_default(alice):
    assert db.get_marker(alice) == config.FORWARD_MARKER.strip()


def test_find_marked_message_strips_the_marker_it_is_given(self_guid):
    chat = _Saved(["the advert MARK"])
    assert asyncio.run(rb.find_marked_message(chat, "  MARK \n")) == 10_000


def test_an_empty_marker_is_not_searched_for(self_guid):
    """Every message contains "", so an empty marker would match the newest."""
    chat = _Saved(["anything"])
    assert asyncio.run(rb.find_marked_message(chat, "   ")) is None


# --------------------------------------------------------------------------- #
# a token pasted at the phone prompt
# --------------------------------------------------------------------------- #
def test_a_session_token_is_not_accepted_as_a_phone_number():
    token = "MMSESS:" + "3" * 200
    assert rubika_panel._normalize_phone_input(token) == "", \
        ("a 200-digit 'phone' was sent to Rubika, which answered INVALID_INPUT "
         "and logged the whole token as the account being logged in")
    assert rubika_panel._looks_like_session_token(token)


def test_real_phone_forms_are_still_accepted():
    for raw in ("09123456789", "+98 912 345 6789", "989123456789",
                "00989123456789", "9123456789"):
        assert rubika_panel._normalize_phone_input(raw) == "09123456789", raw


def test_a_too_short_number_is_rejected():
    assert rubika_panel._normalize_phone_input("12345") == ""


def test_a_plain_phone_is_not_mistaken_for_a_token():
    assert not rubika_panel._looks_like_session_token("09123456789")


# --------------------------------------------------------------------------- #
# never call a run that reached nobody a finish
# --------------------------------------------------------------------------- #
def test_a_run_that_reached_nobody_is_demoted_to_failed():
    """The production report this exists for: 34 targets, Sent 0, '🏁 پایان'."""
    ctl = {"state": "done", "total": 34, "sent": 0, "failed": 0,
           "last_error": "", "reason": ""}
    rubika_panel._demote_empty_result(ctl)
    assert ctl["state"] == "failed", \
        ("telling the customer their advert went out when the loop never ran is "
         "the worst possible outcome")
    assert ctl["reason"], "a demoted result must say why"


def test_the_demotion_carries_the_last_error_when_there_is_one():
    ctl = {"state": "done", "total": 10, "sent": 0, "failed": 10,
           "last_error": "TooRequests: flood", "reason": ""}
    rubika_panel._demote_empty_result(ctl)
    assert ctl["state"] == "failed"
    assert "TooRequests" in ctl["reason"]


def test_a_genuine_finish_is_left_alone():
    ctl = {"state": "done", "total": 10, "sent": 10, "failed": 0,
           "last_error": "", "reason": ""}
    rubika_panel._demote_empty_result(ctl)
    assert ctl["state"] == "done"


def test_a_partial_send_is_still_a_finish():
    ctl = {"state": "done", "total": 10, "sent": 3, "failed": 7,
           "last_error": "x", "reason": ""}
    rubika_panel._demote_empty_result(ctl)
    assert ctl["state"] == "done", "reaching some is a finish, not a failure"


def test_nothing_to_send_is_not_a_failure():
    ctl = {"state": "done", "total": 0, "sent": 0, "failed": 0,
           "last_error": "", "reason": ""}
    rubika_panel._demote_empty_result(ctl)
    assert ctl["state"] == "done"


def test_a_non_done_state_is_never_overwritten():
    for state in ("stopped", "auth_failed", "no_marker", "frozen"):
        ctl = {"state": state, "total": 5, "sent": 0, "failed": 0,
               "last_error": "", "reason": "original"}
        rubika_panel._demote_empty_result(ctl)
        assert ctl["state"] == state
        assert ctl["reason"] == "original"


def test_finish_send_actually_applies_the_rule():
    """The pure function is useless if the finish path does not call it."""
    src = open(os.path.join(ROOT, "rubika_panel.py"), encoding="utf-8").read()
    start = src.index("async def _finish_send")
    body = src[start:]
    for marker in ("\nasync def ", "\ndef "):
        at = body.find(marker, 10)
        if at != -1:
            body = body[:at]
    code = "\n".join(line.split("#")[0] for line in body.splitlines()
                     if not line.strip().startswith("#"))
    assert "_demote_empty_result(ctl)" in code
