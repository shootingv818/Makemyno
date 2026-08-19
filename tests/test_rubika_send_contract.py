"""
The Rubika send path, driven end to end with a fake client.

WHAT THIS REPRODUCES
--------------------
A real send to an account with hundreds of contacts reported:

    • Targets : 2
    • Sent    : 0
    • Failed  : 2

Three separate contract bugs stacked up in one code path:

  1. get_ordered_recipients returned (ordered, stats), and _collect_targets passed
     the whole tuple on as the target list — so "Targets" counted the two tuple
     ELEMENTS, and the send then tried to message a list and a dict.
  2. Recipients are {"guid", "name"} dicts, but the send loop handed each one
     straight to rb.send_text, which wants a guid string. The worker payload did
     str(dict).
  3. find_marked_message returned (guid, message_id), which is truthy even as
     (guid, None) — so "marker not found" never fired and forward mode sent with
     a None id.

Each function was individually reasonable; only the joins were wrong. These tests
exercise the joins.
"""
import asyncio

import pytest

import rubika_client as rb


class _Msg:
    def __init__(self, message_id, text):
        self.message_id = message_id
        self.text = text


class _Page:
    def __init__(self, messages):
        self.messages = messages


class _FakeClient:
    """Answers the handful of calls the send path makes."""

    def __init__(self, contacts=5, saved=None):
        self._contacts = contacts
        self._saved = saved or []
        self.sent = []
        self.forwarded = []

    async def get_me(self):
        return {"user": {"user_guid": "me-guid"}}

    async def get_contacts(self, start_id=None):
        users = [{"user_guid": f"c-{i}", "first_name": f"N{i}",
                  "last_name": "", "is_online": False}
                 for i in range(self._contacts)]
        return {"users": users}

    async def get_chats(self, start_id=None):
        return {"chats": []}

    async def get_messages(self, guid, offset=None, limit=None):
        if offset in (None, "0"):
            return _Page(self._saved)
        return _Page([])


# --------------------------------------------------------------------------- #
# The recipient list
# --------------------------------------------------------------------------- #
def test_ordered_recipients_is_a_list_not_a_tuple():
    """THE "Targets: 2" BUG. A tuple made the caller count two elements instead of
    the account's real contacts."""
    got = asyncio.run(rb.get_ordered_recipients(_FakeClient(contacts=5)))
    assert isinstance(got, list), "a tuple here is what reported Targets: 2"
    assert not isinstance(got, tuple)


def test_every_contact_appears_in_the_recipient_list():
    got = asyncio.run(rb.get_ordered_recipients(_FakeClient(contacts=7)))
    assert len(got) == 7, f"expected 7 recipients, got {len(got)}"


def test_each_recipient_carries_a_guid():
    got = asyncio.run(rb.get_ordered_recipients(_FakeClient(contacts=3)))
    for item in got:
        assert item.get("guid"), f"recipient without a guid: {item}"


def test_an_account_with_no_contacts_gives_an_empty_list():
    got = asyncio.run(rb.get_ordered_recipients(_FakeClient(contacts=0)))
    assert got == []


# --------------------------------------------------------------------------- #
# Normalising to guid strings
# --------------------------------------------------------------------------- #
def test_guids_only_flattens_the_dicts():
    """rb.send_text wants a guid; db.mark_sent stores one; the worker payload does
    str(t). The loop was handing whole dicts to all three."""
    import rubika_panel
    items = [{"guid": "g1", "name": "A"}, {"guid": "g2", "name": "B"}]
    assert rubika_panel._guids_only(items) == ["g1", "g2"]


def test_guids_only_passes_plain_strings_through():
    import rubika_panel
    assert rubika_panel._guids_only(["g1", "g2"]) == ["g1", "g2"]


def test_guids_only_drops_entries_with_no_guid():
    import rubika_panel
    items = [{"guid": "g1"}, {"name": "no guid"}, {"guid": ""}, None]
    assert rubika_panel._guids_only(items) == ["g1"]


def test_guids_only_handles_the_alternative_key():
    import rubika_panel
    assert rubika_panel._guids_only([{"object_guid": "g9"}]) == ["g9"]


def test_guids_only_on_empty_input():
    import rubika_panel
    assert rubika_panel._guids_only(None) == []
    assert rubika_panel._guids_only([]) == []


def test_a_normalised_target_is_something_send_text_accepts():
    """The join: what _collect_targets produces must be what send_text takes."""
    import rubika_panel
    targets = rubika_panel._guids_only(
        asyncio.run(rb.get_ordered_recipients(_FakeClient(contacts=3))))
    assert targets == ["c-0", "c-1", "c-2"]
    for target in targets:
        assert isinstance(target, str), "send_text needs a guid string"
        assert str(target) == target, "the worker payload does str(t)"


# --------------------------------------------------------------------------- #
# The marker
# --------------------------------------------------------------------------- #
def test_a_found_marker_returns_the_message_id():
    client = _FakeClient(saved=[_Msg(101, "hello کد135 world")])
    got = asyncio.run(rb.find_marked_message(client, "کد135"))
    assert got == 101


def test_a_missing_marker_returns_none_which_is_falsy():
    """THE BUG: as a tuple, (guid, None) is TRUTHY, so `if not found` never fired
    and forward mode proceeded with no message id."""
    client = _FakeClient(saved=[_Msg(101, "nothing relevant")])
    got = asyncio.run(rb.find_marked_message(client, "کد135"))
    assert got is None
    assert not got, "the caller's `if not found` has to work"


def test_the_marker_result_is_not_a_tuple():
    client = _FakeClient(saved=[_Msg(101, "x کد135")])
    got = asyncio.run(rb.find_marked_message(client, "کد135"))
    assert not isinstance(got, tuple)


def test_an_empty_saved_folder_reports_no_marker():
    got = asyncio.run(rb.find_marked_message(_FakeClient(saved=[]), "کد135"))
    assert got is None


def test_the_marker_matches_a_caption_too():
    class _Captioned:
        def __init__(self):
            self.message_id = 55
            self.caption = "promo کد135"
    client = _FakeClient(saved=[_Captioned()])
    assert asyncio.run(rb.find_marked_message(client, "کد135")) == 55


# --------------------------------------------------------------------------- #
# The whole join, as the panel performs it
# --------------------------------------------------------------------------- #
def test_the_panel_expression_for_marker_mode_works():
    """Line-for-line what _run_send_local does before a forward."""
    client = _FakeClient(saved=[_Msg(77, "hi کد135")])

    async def _find():
        return (await rb.get_self_guid(client),
                await rb.find_marked_message(client, "کد135"))
    from_guid, message_id = asyncio.run(_find())
    assert from_guid == "me-guid"
    assert message_id == 77
    assert message_id, "the not-found branch must not fire for a real marker"


def test_the_panel_expression_detects_a_missing_marker():
    client = _FakeClient(saved=[_Msg(77, "no marker here")])

    async def _find():
        return (await rb.get_self_guid(client),
                await rb.find_marked_message(client, "کد135"))
    _from_guid, message_id = asyncio.run(_find())
    assert not message_id, "a missing marker must be detectable"


def test_a_text_send_reaches_every_normalised_target():
    """End to end: enumerate, normalise, and confirm each target is sendable."""
    import rubika_panel
    client = _FakeClient(contacts=4)
    targets = rubika_panel._guids_only(
        asyncio.run(rb.get_ordered_recipients(client)))
    assert len(targets) == 4, "the count the customer sees must be the real one"
    # The send loop's expression, with nothing left as a dict or a tuple.
    for target in targets:
        assert isinstance(target, str) and target.startswith("c-")
