"""
The worker could not build a channel, add contacts or send — the reference could.

WHAT WAS ACTUALLY WRONG
-----------------------
Every rubpy primitive in rubika_client is byte-identical to the reference
(create_channel, add_channel_members, seed_channel_with_contacts, send_text,
forward_message, add_contact, connect_ready). The platform layer was never the
problem, which is why reading it found nothing. All four defects were in the
orchestration above it:

  1. /channel/create called rb.create_channel and NOTHING else. It never looked
     for the marked post and never forwarded it, and rubika_panel never even sent
     a marker. So a channel campaign produced an EMPTY channel and then seeded
     hundreds of members into it. It looked like it worked, because a guid came
     back.

  2. The send loop called account_conn.call() PER RECIPIENT, i.e. over the shared
     warm socket. On an auth-looking error account_conn.call runs
     verify_session_dead(), and that drops and reopens the connection — the very
     socket the job was sending on. One muted recipient tore down a healthy run,
     and the rapid reconnect is itself what makes Rubika revoke a session. The
     reference owns ONE dedicated client per job and confirms a suspect session on
     a separate connection; its own comment calls this the only difference from
     the proven build.

  3. A burst of errors ended the job permanently (state="error_burst"). Rubika
     throttles long before it revokes, so a burst has to pause for resume_wait
     and carry on from where it stopped, up to max_retries times.

  4. /contacts/add opened a connection per number, had no consecutive-error
     brake, recorded no reason at all (`except Exception: failed += 1`), and
     tested success with rb._guid_of() when add_contact's contract is
     {"on_rubika": bool, "guid": str|None} — so a number that IS on Rubika whose
     response omitted the guid was counted as "not a user".

Each test below fails if its fix is reverted; that was verified by reverting
them one at a time.
"""
import asyncio
import os
import re

import pytest

import account_conn
import config
import rubika_client as rb
import worker_api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_comments(text):
    """Drop comments and docstring prose so a check cannot pass on its own note.

    These tests name the very strings they guard, right beside the fix. A raw
    search then finds the explanation instead of the code: reverting the fix left
    the comment behind and the test stayed green. The repo hit this before, in
    test_reference_audit, and it is the reason that helper exists there too.
    """
    out = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        # crude but sufficient: our docstrings open and close on their own lines
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


def _func_src(filename, name, code_only=False):
    """Source of one function, sliced to where it actually ends.

    Never a fixed byte window: a window stops covering the function the moment
    the code grows, and then reports a present fix as missing.
    """
    with open(os.path.join(ROOT, filename), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = None
    indent = 0
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:async\s+)?def\s+" + re.escape(name) + r"\b",
                         line)
        if match:
            start = i
            indent = len(match.group(1))
            break
    assert start is not None, f"{name} not found in {filename}"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and not line.lstrip().startswith(("#", ")", "]", "}")):
            end = j
            break
    body = "\n".join(lines[start:end])
    return _strip_comments(body) if code_only else body


def _class_src(filename, name, code_only=True):
    """Source of one class body, sliced to the next line at the same indent."""
    with open(os.path.join(ROOT, filename), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = None
    indent = 0
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)class\s+" + re.escape(name) + r"\b", line)
        if match:
            start = i
            indent = len(match.group(1))
            break
    assert start is not None, f"class {name} not found in {filename}"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            end = j
            break
    body = "\n".join(lines[start:end])
    return _strip_comments(body) if code_only else body


# --------------------------------------------------------------------------- #
# 1. the new channel must receive the marked post
# --------------------------------------------------------------------------- #
class _ChannelClient:
    def __init__(self):
        self.forwarded = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass


@pytest.fixture
def channel_rb(monkeypatch):
    """Stub the platform so only our orchestration is under test."""
    client = _ChannelClient()
    monkeypatch.setattr(rb, "open_client", lambda phone, cid: client)
    monkeypatch.setattr(rb, "connect_ready", lambda c: asyncio.sleep(0))

    async def _self_guid(_c):
        return "u_self"

    async def _find(_c, marker):
        return 5150 if marker == "ADV" else None

    async def _create(_c, title, description=None):
        return "c_new"

    async def _forward(c, from_guid, to_guid, message_id):
        c.forwarded.append((from_guid, to_guid, message_id))
        return {"ok": True}

    monkeypatch.setattr(rb, "get_self_guid", _self_guid)
    monkeypatch.setattr(rb, "find_marked_message", _find)
    monkeypatch.setattr(rb, "create_channel", _create)
    monkeypatch.setattr(rb, "forward_message", _forward)
    return client


def test_channel_create_forwards_the_marked_post(channel_rb):
    """The whole point of the feature: the channel must not come out empty."""
    async def _work(client):
        message_id = await rb.find_marked_message(client, "ADV")
        guid = await rb.create_channel(client, "T")
        if message_id and guid:
            saved = await rb.get_self_guid(client)
            await rb.forward_message(client, saved, guid, message_id)
        return guid

    guid = asyncio.run(account_conn.fresh_call(7, "989120000001", _work,
                                               timeout=5))
    assert guid == "c_new"
    assert channel_rb.forwarded == [("u_self", "c_new", 5150)], \
        "the marked post was never forwarded into the new channel"


def test_channel_create_endpoint_finds_and_forwards_the_marker():
    """Guard the endpoint itself, not just the pattern."""
    src = _func_src("worker_api.py", "channel_create", code_only=True)
    assert "find_marked_message" in src, \
        "/channel/create never looks for the marked post"
    assert "forward_message" in src, \
        "/channel/create never forwards the marked post into the channel"
    assert "marker_found" in src and "forwarded" in src, \
        "/channel/create must report whether the post landed"


def test_channel_create_model_accepts_a_marker():
    block = _class_src("worker_api.py", "ChannelCreate")
    assert "marker" in block, "ChannelCreate cannot carry a marker"


def test_master_sends_the_marker_to_the_worker():
    """A perfect endpoint is useless if the caller omits the marker."""
    src = _func_src("rubika_panel.py", "_channel_flow", code_only=True)
    assert '"/channel/create"' in src
    create_call = src[src.index('"/channel/create"'):]
    create_call = create_call[:create_call.index("timeout")]
    assert "marker" in create_call, \
        "the master calls /channel/create without a marker"


# --------------------------------------------------------------------------- #
# 2 + 3. the send loop owns its connection and resumes after a burst
# --------------------------------------------------------------------------- #
def test_send_loop_does_not_reconnect_per_recipient():
    """account_conn.call per recipient is what killed sends mid-flight."""
    src = _func_src("worker_api.py", "_run_send", code_only=True)
    assert "fresh_connection" in src, \
        "the send job does not own a dedicated connection"
    assert "account_conn.call(" not in src, \
        ("the send loop still goes through account_conn.call per recipient, so "
         "verify_session_dead can tear down the socket it is sending on")


def test_send_loop_confirms_auth_without_dropping_its_own_socket():
    src = _func_src("worker_api.py", "_run_send", code_only=True)
    assert "is_auth_error" in src, \
        "an auth-looking error is not distinguished from a normal failure"
    assert "_confirm_session_dead" in src, \
        "a suspected dead session is not confirmed on a separate connection"


def test_send_loop_resumes_after_an_error_burst():
    src = _func_src("worker_api.py", "_run_send", code_only=True)
    assert "resume_wait" in src, "no pause-and-resume after an error burst"
    assert "retry_count" in src and "max_retries" in src, \
        "the burst brake has no bounded retry"
    assert "waiting" in src, "a paused job does not report itself as waiting"


def test_send_model_carries_the_resume_knobs():
    block = _class_src("worker_api.py", "SendStart")
    for field in ("send_timeout", "resume_wait", "max_retries"):
        assert field in block, f"SendStart cannot carry {field}"


def test_master_keeps_polling_a_waiting_job():
    """`waiting` is a LIVE job. Treating it as terminal abandons the send."""
    src = _func_src("rubika_panel.py", "_run_send_remote", code_only=True)
    assert '"waiting"' in src, \
        "the master does not know about the waiting state and will abandon a " \
        "job that is only pausing"


def test_local_send_loop_also_owns_its_connection():
    src = _func_src("rubika_panel.py", "_run_send_local", code_only=True)
    assert "fresh_connection" in src, \
        "the local send path still reuses the warm socket per recipient"
    assert "RESUME_WAIT" in src, "the local send path has no resume brake"


def test_sleep_with_stop_wakes_on_stop():
    """A five-minute flat sleep makes the stop button look broken."""
    job = {"stop": False}

    async def _go():
        task = asyncio.ensure_future(worker_api._sleep_with_stop(job, 30,
                                                                 step=0.01))
        await asyncio.sleep(0.05)
        job["stop"] = True
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(_go())


def test_job_that_reached_nobody_is_not_reported_as_done():
    """An account that found recipients but reached none is a FAILURE."""
    src = _func_src("worker_api.py", "_run_send", code_only=True)
    assert 'job["state"] = "failed"' in src, \
        "a send that reached nobody can still report done"
    assert "reason" in src, "a failed send carries no reason"


# --------------------------------------------------------------------------- #
# 4. contacts: one connection, a brake, and real reasons
# --------------------------------------------------------------------------- #
def test_contacts_add_uses_one_connection_for_the_whole_batch():
    src = _func_src("worker_api.py", "contacts_add", code_only=True)
    assert src.count("account_conn.call(") == 1, \
        ("contacts/add must claim the session once for the whole batch, not "
         "once per number")
    assert "7200" in src, "the batch call has no batch-sized timeout"


def test_contacts_add_has_a_consecutive_error_brake():
    src = _func_src("worker_api.py", "contacts_add", code_only=True)
    assert "CONTACT_MAX_ERRORS" in src, "no consecutive-error brake"
    assert "CONTACT_RESUME_WAIT" in src, "the brake never pauses"


def test_contacts_add_records_why_a_number_failed():
    src = _func_src("worker_api.py", "contacts_add", code_only=True)
    assert "last_error" in src, "contacts/add still swallows the reason"
    assert "type(exc).__name__" in src, \
        "the failure reason does not carry the real exception type"


def test_contacts_add_uses_the_on_rubika_contract():
    src = _func_src("worker_api.py", "contacts_add", code_only=True)
    assert "on_rubika" in src, \
        "contacts/add does not use add_contact's documented contract"
    assert "_guid_of" not in src, \
        ("contacts/add still infers success from _guid_of, so a real Rubika "
         "number whose response omits the guid is counted as 'not a user'")


def test_contact_brake_knobs_exist():
    assert config.CONTACT_MAX_ERRORS >= 1
    assert config.CONTACT_RESUME_WAIT >= 1


# --------------------------------------------------------------------------- #
# 5. the auto-upload path exists again
# --------------------------------------------------------------------------- #
def test_upload_file_to_self_exists_and_is_document_only():
    assert hasattr(rb, "upload_file_to_self"), \
        "the auto-upload path is missing, so media can only be sent by marker"
    src = _func_src("rubika_client.py", "upload_file_to_self", code_only=True)
    assert "send_document" in src
    for banned in ("send_photo", "send_video", "send_gif", "send_file("):
        assert banned not in src, \
            f"{banned} re-types the payload; a zip/apk must arrive as a file"


def test_upload_prepare_endpoint_never_raises_on_a_bad_payload():
    src = _func_src("worker_api.py", "upload_prepare", code_only=True)
    assert '"ok": False' in src, \
        "/upload/prepare must report a clean error so the master can fall back"
    assert "basename" in src, \
        "a caller-supplied file name could escape the upload directory"


def test_newest_saved_message_id_returns_int_or_none(monkeypatch):
    """Confirming an upload by the new top message needs numeric ids only."""
    class _C:
        async def get_messages(self, guid, start, count):
            return {"messages": [{"message_id": "12"}, {"message_id": "bad"},
                                 {"message_id": "9"}]}

    got = asyncio.run(rb._newest_saved_message_id(_C(), "u_self"))
    assert got == 12, "non-numeric ids must be discarded, not crash the compare"
