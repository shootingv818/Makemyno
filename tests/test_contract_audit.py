"""
A project-wide audit of return-shape contracts.

WHY THIS FILE EXISTS
--------------------
The same bug shape has now broken production four separate times:

  1. get_contacts_ordered returned (list, count); callers unpacked (mutuals,
     others) and iterated the int → "'int' object is not iterable" on every
     Telegram send.
  2. finish_login returned the raw rubpy object; callers called info.get(...) and
     that field is None on the object → "'NoneType' object is not callable" on
     every Rubika login.
  3. get_ordered_recipients returned (ordered, stats); callers passed the whole
     tuple on as the target list → an account with hundreds of contacts reported
     "Targets: 2" and failed both "sends".
  4. find_marked_message returned (guid, message_id); callers did `if not found`
     on a tuple that is truthy even as (guid, None), so a missing marker was never
     detected and forward mode sent with no message id.

Every one of them passed the unit tests, because each side was individually
reasonable — only the JOIN between them was wrong. So the audit is structural: for
each cross-module function, assert the number of values it returns matches what
every caller unpacks.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = sorted(f for f in os.listdir(ROOT) if f.endswith(".py"))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _code(name):
    """Source with comments stripped.

    These checks describe the bugs they guard against, by name, in comments right
    next to the fix — so matching raw text finds the explanation and reports the
    fix as the defect. Only executable lines count.
    """
    out = []
    for line in _src(name).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


def _return_arity(name: str):
    """How many values a function returns: an int, or None if it varies."""
    seen = set()
    for module in MODULES:
        tree = ast.parse(_src(module), filename=module)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != name:
                continue
            for ret in ast.walk(node):
                if not isinstance(ret, ast.Return) or ret.value is None:
                    continue
                if isinstance(ret.value, ast.Tuple):
                    seen.add(len(ret.value.elts))
                else:
                    seen.add(1)
    if not seen:
        return None
    return seen.pop() if len(seen) == 1 else None


def _unpack_counts(name: str):
    """[(module, how many names the caller unpacks into)] for every call site."""
    out = []
    pattern = re.compile(
        r"^\s*([A-Za-z_][\w, ]*?)\s*=\s*(?:await\s+)?(?:\w+\.)?" +
        re.escape(name) + r"\(")
    for module in MODULES:
        for line in _src(module).splitlines():
            match = pattern.match(line)
            if not match:
                continue
            names = [n for n in match.group(1).split(",") if n.strip()]
            out.append((module, len(names)))
    return out


# The functions that cross a module boundary and have burned us.
AUDITED = [
    "get_contacts_ordered", "get_ordered_recipients", "find_marked_message",
    "finish_login", "get_chats_user_guids", "pool_lease_block", "rate_hit",
    "build_archive", "affine_params", "get_contacts_full", "get_group_guids",
    "get_group_entities", "session_unpack", "get_session_blob",
]


@pytest.mark.parametrize("name", AUDITED)
def test_callers_unpack_exactly_what_the_function_returns(name):
    """The join between the two sides, checked structurally."""
    arity = _return_arity(name)
    if arity is None:
        pytest.skip(f"{name} has no single return arity to check")
    mismatches = [(module, count) for module, count in _unpack_counts(name)
                  if count != arity]
    assert mismatches == [], (
        f"{name}() returns {arity} value(s) but is unpacked differently in: "
        f"{mismatches}")


# --------------------------------------------------------------------------- #
# The four specific contracts, pinned by name
# --------------------------------------------------------------------------- #
def test_get_ordered_recipients_returns_a_plain_list():
    """It returned (ordered, stats), and both callers shipped the tuple onward as
    the target list — hence "Targets: 2" on an account with hundreds of contacts."""
    assert _return_arity("get_ordered_recipients") == 1
    body = _code("rubika_client.py")
    start = body.index("async def get_ordered_recipients")
    section = body[start:start + 3000]
    assert "return ordered, stats" not in section
    assert "-> list" in section.split("\n")[0]


def test_find_marked_message_returns_only_the_id():
    """As a tuple it was truthy even when the marker was missing, so `if not
    found` could never fire and forward mode sent with a None id."""
    assert _return_arity("find_marked_message") == 1


def test_no_caller_treats_the_marker_result_as_an_object():
    """`_msg_id_of(found)` on the old tuple silently produced None."""
    # AST, not text: the fix is explained in a DOCSTRING that quotes the broken
    # expression, and a textual search reports that explanation as the defect.
    # Only real calls count.
    offenders = []
    for module in MODULES:
        tree = ast.parse(_src(module), filename=module)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else \
                getattr(func, "id", "")
            if called != "_msg_id_of":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in ("found", "marker_msg"):
                    offenders.append(f"{module}:{node.lineno}")
    assert offenders == [], (
        f"these still derive the id from the old tuple shape: {offenders}")


def test_finish_login_returns_a_mapping():
    """Callers do info.get(...) and {**info}."""
    body = _code("rubika_client.py")
    start = body.index("async def finish_login")
    section = body[start:start + 6000]
    assert "return result" not in section, "the raw rubpy object must not escape"
    for key in ('"guid"', '"name"', '"contacts"', '"session_values"'):
        assert key in section, f"finish_login must provide {key}"


def test_send_targets_are_normalised_to_strings():
    """Recipients arrive as {"guid", "name"} dicts but every consumer wants a
    string: rb.send_text takes a guid, db.mark_sent stores one, and the worker
    payload does str(t). The send loop was handing whole dicts to send_text."""
    body = _src("rubika_panel.py")
    assert "def _guids_only" in body
    start = body.index("async def _collect_targets")
    section = body[start:start + 2000]
    assert "_guids_only" in section, "both paths must normalise"
    assert section.count("_guids_only") >= 2, "local AND remote paths"


def test_the_worker_ships_guid_strings_not_dicts():
    body = _src("worker_api.py")
    start = body.index('@app.post("/prepare")')
    section = body[start:start + 2500]
    assert '"targets": targets' in section
    assert 'r.get("guid")' in section, "dicts must be flattened before the wire"
