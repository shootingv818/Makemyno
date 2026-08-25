"""
Two defects reported together: a campaign that died on a hiccup, and file names
the customer did not recognise.

THE HICCUP
----------
An account's whole send failed with

    WorkerAPIError: worker wk-0db9 (400) on /prepare:
    ServerError: {'status': 'ERROR_TRY_AGAIN', 'status_det': 'SERVER_ERROR'}

That is not a broken session and not a bug in our logic: it is Rubika answering
HTTP 200 with a body that says "cannot serve this right now". rubpy raises it as
fatal on the generic method path (methods/advanced/build.py), even though rubpy's
OWN upload loop reinitialises and restarts on the identical status
(network.py: "Server requested reinitialization"). Its request() retry only covers
transport errors, so a 200-with-error-status never gets one.

Nothing in this project or in the reference retried it, so ONE failed page of
getContacts aborted the prepare step and an account with hundreds of recipients
was reported failed before a single message went out.

THE FILE NAMES
--------------
Telethon takes a document's name from os.path.basename() of the path it is handed
(telethon/utils.py, get_attributes). We stored uploads as
"<uid>_<random>_<name>" and additionally stripped the name to `isalnum() or ._-`,
so "لیست قیمت (نهایی) 1404.xlsx" reached recipients as
"7658493021_9f3c1a44_لیستقیمت1404.xlsx". The tg_content table had carried the real
name since the day media was added, and nothing on the send path ever read it.

On the Rubika side the same argument was computed and then silently dropped:
rubpy's send_document has no file_name PARAMETER (it forwards **kwargs), so the
signature-inspection mapper in upload_file_to_self could never place it.
"""
import asyncio
import os

import pytest

import config
import db
import logbus
import rubika_client as rb
import session_store
import telegram_client as tg
import telegram_multi_send as multi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServerError(Exception):
    """Shaped like the real rubpy exception: the message IS the response dict."""

    def __init__(self, status="ERROR_TRY_AGAIN", det="SERVER_ERROR"):
        super().__init__(str({"status": status, "status_det": det}))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Keep the backoff real in logic but instant in time.

    The delays are RECORDED so a test can prove a retry actually waited — a retry
    that hammers the platform immediately would make the throttling worse, and a
    monkeypatch that simply skipped sleeping could not tell the two apart.
    """
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(config, "RB_RETRY_BASE", 2.0)
    monkeypatch.setattr(config, "RB_RETRY_JITTER", 0.0)
    monkeypatch.setattr(config, "RB_RETRY_TRIES", 3)
    monkeypatch.setattr(config, "RB_PAGE_DELAY", 0.4)
    rb.reset_transient()
    return slept


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Contacts:
    """A paginated contact list that fails the way the platform failed.

    `fail_on` maps a page number (1-based) to how many times that page should
    answer ERROR_TRY_AGAIN before succeeding.
    """

    def __init__(self, pages, fail_on=None):
        self.pages = pages                  # list of list-of-guid
        self.fail_on = dict(fail_on or {})
        self.calls = 0
        self.page_calls = []

    async def get_contacts(self, start_id=None):
        page = 0 if start_id is None else int(start_id)
        self.calls += 1
        self.page_calls.append(page)
        remaining = self.fail_on.get(page + 1, 0)
        if remaining:
            self.fail_on[page + 1] = remaining - 1
            raise ServerError()
        users = [{"user_guid": guid, "first_name": guid}
                 for guid in self.pages[page]]
        nxt = str(page + 1) if page + 1 < len(self.pages) else None
        return {"users": users, "next_start_id": nxt}

    async def get_chats(self, start_id=None):
        return {"chats": [], "next_start_id": None}

    async def get_me(self):
        return {"user": {"user_guid": "u_self"}}


# --------------------------------------------------------------------------- #
# 1. The reported failure, fixed
# --------------------------------------------------------------------------- #
def test_a_contact_page_that_says_try_again_no_longer_loses_the_account():
    """The exact reported case: page 2 hiccups, the campaign must still run."""
    client = _Contacts([["g1", "g2"], ["g3", "g4"], ["g5"]], fail_on={2: 1})
    got = asyncio.run(rb.get_contacts_full(client))
    assert [c["guid"] for c in got] == ["g1", "g2", "g3", "g4", "g5"], \
        "one transient page must not cost the other pages"


def test_the_retry_actually_waits_before_asking_again(no_waiting):
    client = _Contacts([["g1"], ["g2"]], fail_on={2: 1})
    asyncio.run(rb.get_contacts_full(client))
    assert 2.0 in no_waiting, \
        "a retry with no backoff makes platform throttling worse, not better"


def test_an_exhausted_retry_reports_the_platforms_own_reason():
    """Never a generic 'gave up': the real type and message must survive."""
    client = _Contacts([["g1"], ["g2"]], fail_on={2: 99})
    with pytest.raises(ServerError) as caught:
        asyncio.run(rb.get_contacts_full(client))
    assert "ERROR_TRY_AGAIN" in str(caught.value)
    assert rb.last_transient()["where"] == "get_contacts", \
        "the card must be able to name what was retried"
    assert rb.last_transient()["retries"] == 2, "tries=3 means two retries"


def test_an_invalid_auth_is_never_retried():
    """Retrying a rejected session is how an account gets revoked."""
    class _Dead:
        def __init__(self):
            self.calls = 0

        async def get_contacts(self, start_id=None):
            self.calls += 1
            raise RuntimeError("INVALID_AUTH")

    client = _Dead()
    with pytest.raises(RuntimeError):
        asyncio.run(rb.get_contacts_full(client))
    assert client.calls == 1, "an auth failure must be raised on the first try"


def test_retries_can_be_switched_off_from_the_env(monkeypatch):
    """RB_RETRY_TRIES=1 restores exactly the old behaviour."""
    monkeypatch.setattr(config, "RB_RETRY_TRIES", 1)
    client = _Contacts([["g1"], ["g2"]], fail_on={2: 1})
    with pytest.raises(ServerError):
        asyncio.run(rb.get_contacts_full(client))
    assert client.calls == 2, "one attempt per page, no retry"


def test_pages_are_spaced_so_the_burst_that_causes_this_is_gone(no_waiting):
    """~400 requests with no gap is what earns ERROR_TRY_AGAIN in the first place."""
    client = _Contacts([["g1"], ["g2"], ["g3"]])
    asyncio.run(rb.get_contacts_full(client))
    assert no_waiting.count(0.4) == 2, \
        "there must be a pause between pages, not only after a failure"


def test_the_page_pause_can_be_switched_off(monkeypatch, no_waiting):
    monkeypatch.setattr(config, "RB_PAGE_DELAY", 0.0)
    asyncio.run(rb.get_contacts_full(_Contacts([["g1"], ["g2"]])))
    assert no_waiting == [], "RB_PAGE_DELAY=0 must restore the old burst"


def test_get_self_guid_survives_a_hiccup_on_a_fresh_connection():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        async def get_me(self):
            self.calls += 1
            if self.calls == 1:
                raise ServerError()
            return {"user": {"user_guid": "u_self"}}

    client = _Flaky()
    assert asyncio.run(rb.get_self_guid(client)) == "u_self"
    assert client.calls == 2


def test_a_hiccup_no_longer_truncates_the_marker_search(monkeypatch):
    """The old `except: break` turned a hiccup into a false 'marker not found'."""
    async def _guid(_client):
        return "u_self"

    monkeypatch.setattr(rb, "get_self_guid", _guid)

    class _Saved:
        def __init__(self):
            self.calls = 0

        async def get_messages(self, guid, max_id, limit):
            self.calls += 1
            if self.calls == 2:
                raise ServerError()
            if max_id == "0":
                return {"messages": [{"message_id": 900 - i, "text": "filler"}
                                     for i in range(20)]}
            return {"messages": [{"message_id": 800, "text": "advert MARK"}]}

    chat = _Saved()
    assert asyncio.run(rb.find_marked_message(chat, "MARK")) == 800, \
        "the marker was there; only a transient error hid it"


# --------------------------------------------------------------------------- #
# 2. The master's own retry, kept apart from the session-repair path
# --------------------------------------------------------------------------- #
def test_a_transient_prepare_is_retried_once_after_settling(monkeypatch, alice,
                                                            no_waiting):
    monkeypatch.setattr(config, "SESSION_SETTLE_SEC", 5.0)
    calls = []

    async def _op():
        calls.append(1)
        if len(calls) == 1:
            raise ServerError()
        return {"targets": ["g1", "g2"]}

    got = asyncio.run(session_store.run_resilient(alice, {"id": 1,
                                                          "phone": "0912"}, _op))
    assert got["targets"] == ["g1", "g2"]
    assert len(calls) == 2
    assert 5.0 in no_waiting, \
        "reopening a session without settling is what revokes it"


def test_a_transient_failure_never_triggers_a_session_rewrite(alice, monkeypatch):
    """place() exists for a MISSING session. A busy server is not that."""
    placed = []

    async def _place(_cid, _acc):
        placed.append(1)
        return True

    monkeypatch.setattr(session_store, "place", _place)

    async def _op():
        raise ServerError()

    with pytest.raises(ServerError):
        asyncio.run(session_store.run_resilient(alice, {"id": 1, "phone": "09"},
                                                _op))
    assert placed == [], "a transient answer must not re-place a healthy session"


def test_an_ordinary_failure_is_still_raised_immediately(alice):
    calls = []

    async def _op():
        calls.append(1)
        raise ValueError("something we did wrong")

    with pytest.raises(ValueError):
        asyncio.run(session_store.run_resilient(alice, {"id": 1, "phone": "09"},
                                                _op))
    assert len(calls) == 1, "only transient and auth failures get a second go"


def test_the_worker_error_arrives_as_text_and_is_still_recognised():
    """Across the HTTP hop the rubpy type is gone; only the string survives."""
    wrapped = RuntimeError(
        "worker wk-0db9 (400) on /prepare: ServerError: "
        "{'status': 'ERROR_TRY_AGAIN', 'status_det': 'SERVER_ERROR'}")
    assert rb.is_transient_failure(wrapped), \
        "a type-based check would never match what the master actually receives"


def test_the_customer_gets_a_sentence_not_a_response_dict():
    said = logbus.humanize_error(ServerError(), kind="prepare")
    assert "ERROR_TRY_AGAIN" not in said and "{" not in said
    assert "روبیکا" in said
    # An auth failure must keep its OWN sentence; these need opposite actions.
    assert logbus.humanize_error(RuntimeError("INVALID_AUTH")) != said


# --------------------------------------------------------------------------- #
# 3. /prepare says WHERE it failed
# --------------------------------------------------------------------------- #
def _func_src(filename, name):
    """One function, sliced by indentation — never a fixed byte window."""
    import re
    with open(os.path.join(ROOT, filename), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = indent = None
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:async\s+)?def\s+" + re.escape(name) + r"\b",
                         line)
        if match:
            start, indent = i, len(match.group(1))
            break
    assert start is not None, f"{name} not found in {filename}"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if not lines[j].strip():
            continue
        cur = len(lines[j]) - len(lines[j].lstrip())
        if cur <= indent and not lines[j].lstrip().startswith(("#", ")", "]",
                                                              "}", "@")):
            end = j
            break
    return "\n".join(lines[start:end])


def test_prepare_names_the_stage_it_failed_at():
    """"ServerError" alone cannot tell a getMe hiccup from a throttled page."""
    src = _func_src("worker_api.py", "prepare")
    for stage in ("get_self_guid", "find_marked_message",
                  "get_ordered_recipients"):
        assert f'stage["at"] = "{stage}"' in src, f"{stage} is unlabelled"
    assert "stage=" in src, "the stage must travel with the error detail"
    assert '"retries"' in src, "a run that needed retries must say so"


def test_prepare_timeouts_leave_room_for_the_new_delays():
    """Page pauses plus backoff must not simply become a TimeoutError."""
    assert config.PREPARE_TIMEOUT >= 300
    assert config.PREPARE_CALL_TIMEOUT > config.PREPARE_TIMEOUT, \
        "the master must not give up while the worker is still working"
    assert "config.PREPARE_TIMEOUT" in _func_src("worker_api.py", "prepare")
    assert "config.PREPARE_CALL_TIMEOUT" in _func_src("rubika_panel.py",
                                                      "_collect_targets")


# --------------------------------------------------------------------------- #
# 4. File names — Rubika
# --------------------------------------------------------------------------- #
class _Uploader:
    """rubpy's real send_document shape: NO file_name parameter, but **kwargs."""

    def __init__(self, accept_kwargs=True):
        self.accept_kwargs = accept_kwargs
        self.seen = None

    async def get_me(self):
        return {"user": {"user_guid": "u_self"}}

    async def get_messages(self, guid, max_id, limit):
        return {"messages": []}

    async def send_document(self, object_guid, document, caption=None,
                            reply_to_message_id=None, auto_delete=None,
                            *args, **kwargs):
        if kwargs and not self.accept_kwargs:
            raise TypeError("send_document() got an unexpected keyword argument")
        self.seen = {"guid": object_guid, "document": document,
                     "caption": caption, "kwargs": dict(kwargs)}
        return {"message_id": 555}


def test_rubika_upload_passes_the_real_name_to_the_platform(tmp_path):
    """rubpy puts file_name into the file_inline the recipient sees."""
    path = tmp_path / "لیست قیمت (نهایی) 1404.xlsx"
    path.write_bytes(b"x")
    client = _Uploader()
    guid, mid = asyncio.run(rb.upload_file_to_self(
        client, str(path), caption="hi", file_name="لیست قیمت (نهایی) 1404.xlsx"))
    assert mid == 555
    assert client.seen["kwargs"].get("file_name") == \
        "لیست قیمت (نهایی) 1404.xlsx", \
        "the name was computed and then dropped, because send_document has no " \
        "file_name parameter for the signature mapper to find"


def test_a_build_that_refuses_the_extra_kwarg_still_uploads(tmp_path):
    """Losing the nice name is bad; failing the campaign is worse."""
    path = tmp_path / "report.pdf"
    path.write_bytes(b"x")
    client = _Uploader(accept_kwargs=False)
    guid, mid = asyncio.run(rb.upload_file_to_self(client, str(path),
                                                   file_name="report.pdf"))
    assert mid == 555
    assert client.seen["kwargs"] == {}


def test_the_worker_writes_the_upload_under_its_own_name():
    """Uniqueness in the directory, never in the file name."""
    src = _func_src("worker_api.py", "upload_prepare")
    assert "uuid.uuid4()" in src
    assert "safe_file_name" in src
    assert "os.rmdir" in src, \
        "a per-request directory must be cleaned up, not left to pile up"


# --------------------------------------------------------------------------- #
# 5. File names — Telegram
# --------------------------------------------------------------------------- #
def test_telethon_is_told_the_name_instead_of_guessing_it():
    attrs = tg._name_attributes("/data/media/content/7/ab12/سند نهایی.pdf")
    assert attrs and attrs[0].file_name == "سند نهایی.pdf"


def test_the_stored_path_can_no_longer_leak_into_the_recipients_view():
    """The reported symptom: a uid and a random hex in front of the name."""
    attrs = tg._name_attributes("/data/media/7658493021_9f3c1a44_report.pdf",
                                "report.pdf")
    assert attrs[0].file_name == "report.pdf"


def test_the_name_override_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "KEEP_FILE_NAME", False)
    assert tg._name_attributes("/data/media/x_y_report.pdf", "report.pdf") is None


def test_a_send_that_rejects_attributes_falls_back_to_the_plain_call():
    """A photo has no filename on Telegram's side; that must not fail a send."""
    calls = []

    class _Client:
        async def send_file(self, entity, file, caption=None, **kwargs):
            calls.append(kwargs)
            if "attributes" in kwargs:
                raise TypeError("this build does not accept attributes here")
            return "sent"

    got = asyncio.run(tg._send_file_named(_Client(), "me", "/tmp/a.jpg", "",
                                          file_path="/tmp/a.jpg",
                                          file_name="a.jpg"))
    assert got == "sent"
    assert len(calls) == 2 and "attributes" not in calls[1]


def test_the_campaign_upload_carries_the_customers_name(monkeypatch):
    """One upload decides the name for every recipient of that campaign."""
    seen = {}

    class _FakeTg:
        async def upload_to_saved(self, client, path, caption="", file_name=""):
            seen["name"] = file_name
            return None

        async def send_media(self, *a, **k):
            return None

        async def send_text(self, *a, **k):
            return None

    monkeypatch.setattr(multi, "tg", _FakeTg())
    asyncio.run(multi.prepare_content(object(), [{
        "kind": "media", "file_path": "/tmp/7_ab_قرارداد.pdf",
        "file_name": "قرارداد.pdf", "text": ""}]))
    assert seen["name"] == "قرارداد.pdf", \
        "tg_content has stored the real name all along and nothing read it"


def test_stored_media_keeps_the_name_the_customer_sent(tmp_path, monkeypatch,
                                                       alice):
    """End to end through the panel step: the name must survive storage."""
    import tg_panel

    monkeypatch.setattr(tg_panel, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "KEEP_FILE_NAME", True)
    downloaded = {}

    class _File:
        name = "لیست قیمت (نهایی) 1404.xlsx"

    class _Event:
        sender_id = alice
        file = _File()
        raw_text = "کپشن"

        async def download_media(self, path):
            downloaded["path"] = path
            with open(path, "wb") as fh:
                fh.write(b"data")
            return path

        async def respond(self, *a, **k):
            return None

    async def _respond(*a, **k):
        return None

    monkeypatch.setattr(tg_panel, "_respond", _respond)

    async def _action(*a, **k):
        return None

    monkeypatch.setattr(logbus, "customer_action", _action)
    asyncio.run(tg_panel._step_add_media(_Event(), {}))

    items = db.tg_content_list(alice)
    assert len(items) == 1
    assert items[0]["file_name"] == "لیست قیمت (نهایی) 1404.xlsx"
    # The basename matters as much as the column: Telethon and rubpy both fall
    # back to it, so a prefixed path is a wrong name even when the column is right.
    assert os.path.basename(items[0]["file_path"]) == \
        "لیست قیمت (نهایی) 1404.xlsx"
    assert os.path.exists(downloaded["path"])
