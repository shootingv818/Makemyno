"""
The contract of get_contacts_ordered, pinned.

This function shipped returning (ordered_list, mutual_COUNT) while both callers
unpacked it as (mutuals, others) and then iterated `others` — a plain
`'int' object is not iterable` the first time a real Telegram account tried to
send. The stubbed tests never caught it because they never drove this path with a
real contact list.

These tests drive it with fake user objects and assert the SHAPE both callers
depend on: two lists, mutuals first, every element iterable.
"""
import asyncio
import types

import pytest

import telegram_client as tg


class _FakeUser:
    def __init__(self, uid, mutual):
        self.id = uid
        self.mutual_contact = mutual


class _FakeClient:
    """Answers GetContactsRequest with a fixed user list. Nothing else."""

    def __init__(self, users):
        self._users = users

    async def __call__(self, request):
        return types.SimpleNamespace(users=self._users)


def _client(*users):
    return _FakeClient(list(users))


def test_it_returns_two_lists_not_a_list_and_a_count():
    """The exact bug: the second value must be ITERABLE, not an int."""
    client = _client(_FakeUser(1, True), _FakeUser(2, False))
    mutuals, others = asyncio.run(tg.get_contacts_ordered(client))
    assert isinstance(mutuals, list)
    assert isinstance(others, list), "the second value was an int in the shipped bug"
    # The failure mode, reproduced: both callers do `for user in others`.
    for _ in others:
        pass


def test_mutuals_and_others_are_split_correctly():
    client = _client(_FakeUser(1, True), _FakeUser(2, False),
                     _FakeUser(3, True), _FakeUser(4, False))
    mutuals, others = asyncio.run(tg.get_contacts_ordered(client))
    assert [u.id for u in mutuals] == [1, 3]
    assert [u.id for u in others] == [2, 4]


def test_no_contact_is_lost_or_duplicated():
    client = _client(_FakeUser(1, True), _FakeUser(2, False), _FakeUser(3, False))
    mutuals, others = asyncio.run(tg.get_contacts_ordered(client))
    all_ids = {u.id for u in mutuals} | {u.id for u in others}
    assert all_ids == {1, 2, 3}
    assert len(mutuals) + len(others) == 3


def test_an_empty_contact_list_gives_two_empty_lists():
    mutuals, others = asyncio.run(tg.get_contacts_ordered(_client()))
    assert mutuals == [] and others == []


def test_all_mutual_leaves_others_empty_but_iterable():
    client = _client(_FakeUser(1, True), _FakeUser(2, True))
    mutuals, others = asyncio.run(tg.get_contacts_ordered(client))
    assert len(mutuals) == 2
    assert others == []
    for _ in others:                     # would have raised on the int bug
        pass


def test_the_way_both_callers_consume_it_does_not_raise():
    """Exactly the expression from tg_panel and telegram_multi_send."""
    client = _client(_FakeUser(1, True), _FakeUser(2, False))
    mutuals, others = asyncio.run(tg.get_contacts_ordered(client))
    recipients = [(u, True) for u in mutuals] + [(u, False) for u in others]
    assert recipients == [(mutuals[0], True), (others[0], False)]
