"""
The sponsor-channel lock, and the one rule that keeps it from locking everybody out.

Telegram only lets a bot read a user's membership when the bot is an ADMIN in the
channel. So "cannot verify" happens for reasons the CUSTOMER cannot see or fix:
the bot is not admin yet, the username is wrong, the channel was deleted, Telegram
is rate-limiting. Treating any of those as "not a member" would lock out every
customer at once over an owner-side mistake, and they would all reach support in
the same minute.

    UNVERIFIABLE THEREFORE MEANS LET THROUGH.

That is not a shortcut — it is the same choice the reference project made, and it
inverts the usual instinct, so it is the thing most likely to be "tidied up" later
by someone who reads the code without this context. Hence the tests.

The consequence is that a lock which protects nothing looks exactly like one that
works, which is why the owner panel has a test button that reports, per channel,
whether membership can actually be read.

Every test below was mutation-verified with __pycache__ cleared.
"""
import asyncio
import os

import pytest

import config
import db
import forcedjoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _NotParticipant(Exception):
    pass


@pytest.fixture(autouse=True)
def _telethon_error(monkeypatch):
    """Make forcedjoin's UserNotParticipantError import resolve to our stub."""
    import sys
    import types
    module = sys.modules.get("telethon.errors")
    if module is None:
        module = types.ModuleType("telethon.errors")
        sys.modules["telethon.errors"] = module
    monkeypatch.setattr(module, "UserNotParticipantError", _NotParticipant,
                        raising=False)
    forcedjoin.clear_cache()
    yield
    forcedjoin.clear_cache()


class _Bot:
    """A bot whose membership answers we control per channel."""

    def __init__(self, behaviour: dict):
        self.behaviour = behaviour
        self.asked = []

    async def get_permissions(self, chat, uid):
        self.asked.append((chat, uid))
        outcome = self.behaviour.get(chat, "member")
        if outcome == "member":
            return object()
        if outcome == "absent":
            raise _NotParticipant("not a participant")
        raise RuntimeError(outcome)          # e.g. bot is not an admin


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def test_a_channel_can_be_added_once():
    assert db.add_forced_channel("@spon", "spon", "https://t.me/spon") is True
    assert db.add_forced_channel("@spon", "spon", "https://t.me/spon") is False
    assert [c["chat"] for c in db.list_forced_channels()] == ["@spon"]


def test_only_enabled_channels_are_listed_when_asked():
    db.add_forced_channel("@a")
    db.add_forced_channel("@b")
    channel = db.list_forced_channels()[0]
    db.set_forced_channel_enabled(channel["id"], False)
    assert [c["chat"] for c in db.list_forced_channels(only_enabled=True)] == ["@b"]


def test_deleting_a_channel_removes_it():
    db.add_forced_channel("@a")
    db.delete_forced_channel(db.list_forced_channels()[0]["id"])
    assert db.list_forced_channels() == []


def test_the_lock_is_off_when_nothing_is_configured():
    assert forcedjoin.is_active() is False
    db.add_forced_channel("@a")
    assert forcedjoin.is_active() is True
    db.set_forced_channel_enabled(db.list_forced_channels()[0]["id"], False)
    assert forcedjoin.is_active() is False, \
        "a list of disabled channels must not gate anybody"


# --------------------------------------------------------------------------- #
# THE rule: never block on uncertainty
# --------------------------------------------------------------------------- #
def test_an_unverifiable_channel_does_not_block():
    db.add_forced_channel("@spon")
    bot = _Bot({"@spon": "ChatAdminRequiredError"})
    missing = asyncio.run(forcedjoin.missing_for(bot, 555))
    assert missing == [], (
        "the bot not being an admin is an OWNER-side mistake; blocking on it "
        "locks out every customer at once over something they cannot fix")


def test_a_genuine_non_member_is_blocked():
    db.add_forced_channel("@spon")
    bot = _Bot({"@spon": "absent"})
    missing = asyncio.run(forcedjoin.missing_for(bot, 555))
    assert [c["chat"] for c in missing] == ["@spon"]


def test_a_member_passes():
    db.add_forced_channel("@spon")
    bot = _Bot({"@spon": "member"})
    assert asyncio.run(forcedjoin.missing_for(bot, 555)) == []


def test_only_the_channels_actually_missed_are_listed():
    db.add_forced_channel("@one")
    db.add_forced_channel("@two")
    db.add_forced_channel("@three")
    bot = _Bot({"@one": "member", "@two": "absent",
                "@three": "ChannelPrivateError"})
    missing = asyncio.run(forcedjoin.missing_for(bot, 555))
    assert [c["chat"] for c in missing] == ["@two"]


def test_the_owner_is_never_gated(monkeypatch):
    db.add_forced_channel("@spon")
    monkeypatch.setattr(config, "OWNER_ID", 999)
    bot = _Bot({"@spon": "absent"})
    assert asyncio.run(forcedjoin.missing_for(bot, 999)) == [], \
        "locking yourself out of your own panel with a bad entry is a real trap"
    assert bot.asked == [], "and it must not even cost a network call"


# --------------------------------------------------------------------------- #
# the cache
# --------------------------------------------------------------------------- #
def test_a_pass_is_cached_so_every_button_press_is_not_a_network_call():
    db.add_forced_channel("@spon")
    bot = _Bot({"@spon": "member"})
    asyncio.run(forcedjoin.missing_for(bot, 555))
    asyncio.run(forcedjoin.missing_for(bot, 555))
    assert len(bot.asked) == 1, \
        "_gate runs on every press; five menus would be five round-trips"


def test_a_failure_is_never_cached():
    """The customer is about to join and press the button."""
    db.add_forced_channel("@spon")
    bot = _Bot({"@spon": "absent"})
    asyncio.run(forcedjoin.missing_for(bot, 555))
    asyncio.run(forcedjoin.missing_for(bot, 555))
    assert len(bot.asked) == 2


def test_adding_a_channel_invalidates_the_cache():
    db.add_forced_channel("@one")
    bot = _Bot({"@one": "member", "@two": "absent"})
    asyncio.run(forcedjoin.missing_for(bot, 555))
    db.add_forced_channel("@two")
    missing = asyncio.run(forcedjoin.missing_for(bot, 555))
    assert [c["chat"] for c in missing] == ["@two"], \
        ("a cached pass counted the channels it was granted for; a new channel "
         "must re-check or the lock never applies to anyone already in")


def test_clear_cache_targets_one_user_or_everyone():
    db.add_forced_channel("@spon")
    bot = _Bot({"@spon": "member"})
    asyncio.run(forcedjoin.missing_for(bot, 1))
    asyncio.run(forcedjoin.missing_for(bot, 2))
    forcedjoin.clear_cache(1)
    asyncio.run(forcedjoin.missing_for(bot, 1))
    asyncio.run(forcedjoin.missing_for(bot, 2))
    assert len(bot.asked) == 3, "only user 1 should have been re-checked"


# --------------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------------- #
class _Button:
    def __init__(self, kind, label, payload):
        self.kind, self.label, self.payload = kind, label, payload

    @classmethod
    def url(cls, label, link):
        return cls("url", label, link)

    @classmethod
    def inline(cls, label, data):
        return cls("inline", label, data)


def test_the_prompt_offers_a_link_per_channel_and_one_check_button():
    channels = [{"chat": "@one", "title": "One", "link": ""},
                {"chat": "@two", "title": "", "link": "https://t.me/joinchat/x"}]
    text, rows = forcedjoin.prompt(channels, _Button)
    links = [b.payload for row in rows for b in row if b.kind == "url"]
    assert links == ["https://t.me/one", "https://t.me/joinchat/x"], \
        "a private channel's invite link must be used as given, not rebuilt"
    checks = [b for row in rows for b in row if b.kind == "inline"]
    assert [b.payload for b in checks] == [b"fj_check"]
    assert "عضو شدم" in checks[0].label
    assert "عضویت" in text
    # The channel's own name on its button, so a customer with three to join can
    # tell which one they have not done yet.
    url_labels = [b.label for row in rows for b in row if b.kind == "url"]
    assert "One" in url_labels[0] and "@two" in url_labels[1]


def test_a_channel_with_no_link_at_all_is_skipped_not_crashed():
    text, rows = forcedjoin.prompt([{"chat": "", "title": "", "link": ""}],
                                   _Button)
    assert rows == [[rows[-1][0]]] or len(rows) == 1
    assert any(b.payload == b"fj_check" for row in rows for b in row)


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def _code(name, filename, kind="async def"):
    src = open(os.path.join(ROOT, filename), encoding="utf-8").read()
    start = src.index(f"{kind} {name}")
    line_start = src.rfind("\n", 0, start) + 1
    indent = start - line_start
    lines = src[line_start:].splitlines()
    kept = [lines[0]]
    for line in lines[1:]:
        if line.strip():
            here = len(line) - len(line.lstrip())
            if here <= indent and line.lstrip().startswith(
                    ("def ", "async def ", "@", "class ")):
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


def test_the_customer_gate_enforces_the_lock():
    code = _code("_gate", "customer_bot.py")
    assert "forcedjoin.is_active()" in code, "the lock is not wired into the gate"
    assert "forcedjoin.enforce" in code
    # After the rate limit: the check is a network call, so a flooder must not be
    # able to make us spend one per press.
    assert code.index("ratelimit.guard") < code.index("forcedjoin.is_active")
    # Before the subscription check, so an expired customer sees the join prompt
    # rather than two contradictory refusals.
    assert code.index("forcedjoin.is_active") < code.index("db.is_active")


def test_the_check_button_does_not_go_through_the_gate():
    """Otherwise the prompt's own button answers with the prompt, forever."""
    code = _code("fj_check_cb", "customer_bot.py")
    assert "_gate(" not in code, \
        "_gate is what shows the prompt; routing its button through it loops"
    assert "forcedjoin.clear_cache(uid)" in code, \
        "the customer joined seconds ago, so any cached verdict is stale"
    assert "ratelimit.guard" in code, "the button still must not be spammable"
    assert "db.is_blocked" in code


def test_the_lock_lives_in_db_not_central_db():
    """The customer bot must not import central_db — owner_bot says so itself."""
    src = open(os.path.join(ROOT, "forcedjoin.py"), encoding="utf-8").read()
    assert "central_db" not in src, \
        ("forcedjoin is imported by the customer bot, so importing central_db "
         "here would drag the fleet, roster and backup into that process")
    assert "import db" in src


def test_the_owner_panel_can_test_the_lock():
    code = _code("fj_test_cb", "owner_bot.py")
    assert "get_permissions" in code, \
        ("the lock fails SILENTLY — an unreadable channel is skipped — so one "
         "that protects nothing looks exactly like one that works")
    assert "قفل عملاً خاموش است" in code, \
        "and the owner must be told plainly when that is the case"


def test_adding_a_channel_reports_whether_the_bot_can_read_it():
    code = _code("_step_fj_channel", "owner_bot.py")
    assert "get_permissions" in code
    assert "forcedjoin.clear_cache()" in code, \
        "a new channel nobody has been checked against invalidates every pass"


def test_toggling_and_deleting_clear_the_cache():
    for name in ("fjtog_cb", "fjdel_cb"):
        assert "forcedjoin.clear_cache()" in _code(name, "owner_bot.py"), \
            (f"{name}: turning a channel off would leave customers blocked by it")


def test_any_pasted_channel_form_is_accepted():
    from owner_bot import _clean_channel_username as clean
    for raw in ("@mychannel", "mychannel", "t.me/mychannel",
                "https://t.me/mychannel", "https://t.me/mychannel?start=1",
                "  @mychannel  "):
        assert clean(raw) == "mychannel", raw
    assert clean("") == ""
