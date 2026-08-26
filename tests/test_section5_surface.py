"""
What the customer can and cannot reach, and that every button leads somewhere.

Two promises are checked here. First: the customer never sees infrastructure —
no repo, no worker credentials, no service settings, and no hint that a log group
exists. Second: no button is a dead end, because a button that answers nothing
looks exactly like a broken bot.
"""
import ast
import os
import re

import pytest

import cards
import config
import db
import health
import help_text
import tabchi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOMER_FILES = ["customer_bot.py", "rubika_panel.py", "tg_panel.py",
                  "tabchi.py", "help_text.py", "telegram_multi_send.py"]


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _all_customer_source():
    return "\n".join(_src(n) for n in CUSTOMER_FILES)


# --------------------------------------------------------------------------- #
# The customer must never learn about the plumbing
# --------------------------------------------------------------------------- #
def test_the_log_group_is_never_mentioned_to_the_customer():
    """The owner's requirement: errors reach the customer as a code, and the
    existence of a log group is the owner's business alone."""
    src = _all_customer_source()
    for needle in ("LOG_GROUP", "log_group", "گپ لاگ", "گروه لاگ"):
        assert needle not in src, f"customer-facing code mentions {needle}"


def test_no_customer_screen_exposes_worker_credentials():
    src = _all_customer_source()
    for needle in ("ssh_pass", "SSH_PASS", "api_token", "API_TOKEN",
                   "ssh_user", "WORKER_SECRET"):
        assert needle not in src, f"customer-facing code touches {needle}"


def test_no_customer_screen_exposes_the_repository_or_deployment():
    """The customer is buying a service, not a codebase.

    The one allowed mention of `.env` is the startup validation, which aborts the
    process before it ever serves anybody — an operator message, not a screen.
    """
    for name in CUSTOMER_FILES:
        for line in _src(name).splitlines():
            if "SystemExit" in line:
                continue
            for needle in ("github.com", "git clone", "Dockerfile",
                           "requirements.txt", "docker-compose"):
                assert needle.lower() not in line.lower(), (
                    f"{name} mentions {needle}: {line.strip()}")


def _names_used(files):
    used = set()
    for name in files:
        for node in ast.walk(ast.parse(_src(name), filename=name)):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
    return used


def _db_calls(files):
    """Only `db.<something>` — a local variable that happens to be called
    `owner_msg` is not a database read, and matching on the bare name turns this
    check into a source of false alarms nobody will trust."""
    calls = set()
    for name in files:
        for node in ast.walk(ast.parse(_src(name), filename=name)):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "db":
                calls.add(node.attr)
    return calls


def test_the_customer_cannot_reach_privileged_operations():
    """Structural, not a permission flag somebody can forget to check: the
    functions simply are not referenced anywhere in the customer surface.

    Note this covers PRIVILEGED (state-changing) operations. Read-only,
    service-wide scans are a separate matter — see the next test.
    """
    forbidden = {"provision_worker", "register_provisioned", "teardown_worker",
                 "update_worker", "collect_worker_sessions", "run_backup",
                 "build_archive", "set_bot_online", "set_sends_frozen",
                 "add_days", "set_expiry", "set_blocked", "owner_answer_ticket",
                 "set_health_report"}
    hit = sorted(forbidden & _names_used(CUSTOMER_FILES))
    assert hit == [], f"customer surface calls privileged operations: {hit}"


def test_no_panel_screen_reads_across_customers():
    """A background loop in the customer process legitimately scans every
    customer — that is how an expiry notice gets sent. A PANEL must never do it,
    because a panel is rendering for one person and any cross-customer read there
    is a leak waiting to be printed into a card."""
    panels = ["rubika_panel.py", "tg_panel.py", "tabchi.py"]
    # The exemptions are all restart recovery: "what was running when we died?"
    recovery_only = {"owner_tabchi_enabled", "owner_secretary_enabled",
                     "owner_tgm_unfinished", "owner_cjobs_running",
                     "owner_list_paused_sends"}
    leaks = sorted(n for n in _db_calls(panels)
                   if n.startswith("owner_") and n not in recovery_only)
    assert leaks == [], f"panel code reads across customers: {leaks}"


def test_the_cross_customer_scans_that_do_exist_are_restart_recovery_only():
    """The handful of unscoped reads in the customer process all answer the same
    question — "what was running when we died?" — and each one re-scopes to a
    single customer before doing anything."""
    src = _src("tabchi.py")
    for fn in ("owner_tabchi_enabled", "owner_secretary_enabled"):
        assert fn in src
        # used only inside the recovery routine
        recovery = src.split("async def restore_engines")[1]
        assert fn in recovery, f"{fn} is used outside restart recovery"


def test_tabchi_reads_the_freeze_but_never_sets_it():
    """A customer-side engine obeys the kill switch; it must not be able to lift
    it."""
    src = _src("tabchi.py")
    assert "are_sends_frozen" in src
    assert "set_sends_frozen" not in src


# --------------------------------------------------------------------------- #
# Every button goes somewhere
# --------------------------------------------------------------------------- #
def _declared_callbacks(name):
    """Every callback pattern a module registers."""
    src = _src(name)
    literals = set(re.findall(r'CallbackQuery\(data=b"([a-z_]+)"', src))
    # `rb"tbacc_(\d+)"` -> the prefix `tbacc`, matching how buttons build it
    patterns = {p.rstrip("_") for p in
                re.findall(r'CallbackQuery\(pattern=rb"([a-z_]+)', src)}
    return literals, patterns


def _referenced_callbacks(name):
    """Every callback a button in this module points at."""
    src = _src(name)
    plain = set(re.findall(r'Button\.inline\([^,]+,\s*b"([a-z_]+)"', src))
    fstr = set(re.findall(r'Button\.inline\([^,]+,\s*f"([a-z_]+)_\{', src))
    return plain, fstr


def test_every_tabchi_button_has_a_handler():
    """A button that answers nothing is indistinguishable from a crash, and it is
    the single most common way a panel rots as it grows."""
    literals, patterns = _declared_callbacks("tabchi.py")
    plain, prefixed = _referenced_callbacks("tabchi.py")
    # Buttons that leave the section are handled by the Rubika panel.
    external = {"rb", "tabchi"}
    missing_plain = plain - literals - external
    missing_prefixed = prefixed - patterns
    assert missing_plain == set(), f"no handler for: {missing_plain}"
    assert missing_prefixed == set(), f"no handler for: {missing_prefixed}"


def test_the_rubika_menu_button_for_tabchi_now_resolves():
    """It was drawn in section 3 and dead until section 5 — exactly the kind of
    dangling reference this test exists to notice."""
    assert b'"tabchi"' in _src("rubika_panel.py").encode() or \
        'b"tabchi"' in _src("rubika_panel.py")
    literals, _ = _declared_callbacks("tabchi.py")
    assert "tabchi" in literals


def test_the_brain_report_button_now_resolves():
    """`rbbrainsend` was referenced by the brain's report card and had no handler
    until section 5."""
    src = _src("rubika_panel.py")
    assert 'b"rbbrainsend"' in src
    assert 'CallbackQuery(data=b"rbbrainsend")' in src


def test_every_step_the_panel_starts_can_be_finished():
    """A wizard step set but never registered leaves the customer typing into a
    bot that ignores them."""
    src = _src("tabchi.py")
    started = set(re.findall(r'"step":\s*"(\w+)"', src))
    assert started, "no steps found — the regex stopped matching the code"
    assert started <= set(tabchi._STEPS), (
        f"unregistered steps: {started - set(tabchi._STEPS)}")


def test_every_registered_step_is_reachable():
    """The other direction: a registered step nothing starts is dead code."""
    src = _src("tabchi.py")
    started = set(re.findall(r'"step":\s*"(\w+)"', src))
    assert set(tabchi._STEPS) <= started, (
        f"unreachable steps: {set(tabchi._STEPS) - started}")


# --------------------------------------------------------------------------- #
# Help covers what was built
# --------------------------------------------------------------------------- #
def test_help_covers_every_section_five_feature():
    for name in ("tabchi", "secretary"):
        assert name in help_text.topics(), f"no help for {name}"
        text, buttons = help_text.topic(name)
        assert cards.LINE in text, f"help topic {name} lost the house style"
        assert len(text.splitlines()) > 4, f"help topic {name} is a stub"
        assert buttons, f"help topic {name} has no way back"


def test_an_unknown_help_topic_does_not_crash():
    text, buttons = help_text.topic("no_such_topic")
    assert isinstance(text, str) and buttons


def test_every_help_topic_has_a_button_that_opens_it():
    """A topic with no way to open it is documentation nobody will ever read."""
    src = _src("help_text.py")
    for name in help_text.topics():
        assert f'b"help_{name}"' in src, f"no button opens help topic {name}"


def test_help_never_leaks_infrastructure():
    for name in help_text.topics():
        text, _ = help_text.topic(name)
        for needle in ("ssh", "docker", "github", "central_db", "sqlite",
                       "worker"):
            assert needle.lower() not in text.lower(), (
                f"help topic {name} mentions {needle}")


# --------------------------------------------------------------------------- #
# The card style survived
# --------------------------------------------------------------------------- #
def test_the_house_divider_is_thirty_one_dashes():
    """The owner asked for this look explicitly, and it is easy to lose while
    adding new screens."""
    assert cards.LINE == "-" * 31


def test_section_five_cards_all_use_the_house_divider(alice):
    aid = db.add_account(alice, "09120000001", name="acc")
    db.tabchi_add_group(alice, aid, "https://rubika.ir/joing/A")
    acc = db.get_account(alice, aid)
    for text in (tabchi.account_card(alice, acc),
                 tabchi.groups_card(alice, acc),
                 tabchi.secretary_card(alice, acc),
                 tabchi.section_card(alice)):
        assert cards.LINE in text, "a section 5 card used no divider at all"


def test_no_competing_divider_style_reaches_a_card(alice):
    """One divider, everywhere; the alternative is a panel that looks like it was
    assembled by three different people."""
    aid = db.add_account(alice, "09120000001", name="acc")
    acc = db.get_account(alice, aid)
    rendered = "\n".join([tabchi.account_card(alice, acc),
                          tabchi.groups_card(alice, acc),
                          tabchi.secretary_card(alice, acc),
                          tabchi.section_card(alice),
                          health.report_card()])
    for bad in ("_____", "•••", "=====", "－－－", "───"):
        assert bad not in rendered, f"a card used {bad} instead of cards.LINE"
