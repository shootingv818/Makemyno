"""
The PV photo archive: two collection modes, the fallback, and cumulative delivery.

The parallel mode is the one worth testing carefully, because "several downloads
at once" is only safe in one specific form: many requests over the SAME
connection. A second connection would revoke the session, which is the failure
this whole project is built to avoid.
"""
import asyncio

import pytest

import busy
import config
import db
import logbus
import rubika_panel


@pytest.fixture(autouse=True)
def silent_logs(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(logbus, "to_group", noop)
    monkeypatch.setattr(logbus, "to_pv", noop)
    busy.clear_all()
    rubika_panel._jobs.clear()
    yield
    busy.clear_all()
    rubika_panel._jobs.clear()


class _FakeBot:
    def __init__(self):
        self.files = []
        self.messages = []

    async def send_file(self, uid, path, caption="", **kwargs):
        # record the page count from the caption instead of reading the PDF
        self.files.append((uid, caption))

    async def send_message(self, uid, text, **kwargs):
        self.messages.append((uid, text))
        return None


@pytest.fixture
def fake_bot(monkeypatch):
    bot = _FakeBot()
    monkeypatch.setattr(rubika_panel, "_bot", bot)
    return bot


# --------------------------------------------------------------------------- #
# Mode selection
# --------------------------------------------------------------------------- #
def test_mode_defaults_and_persists(alice):
    assert rubika_panel._pdf_mode(alice) == config.PV_EXPORT_MODE_DEFAULT
    db.set_setting(alice, "pv_mode", "safe")
    assert rubika_panel._pdf_mode(alice) == "safe"


def test_an_unknown_mode_falls_back_to_auto(alice):
    db.set_setting(alice, "pv_mode", "nonsense")
    assert rubika_panel._pdf_mode(alice) == "auto"


def test_parallel_setting_is_clamped(alice):
    db.set_setting(alice, "pv_parallel", 999)
    assert rubika_panel._pdf_parallel(alice) == config.PV_EXPORT_PARALLEL_MAX
    db.set_setting(alice, "pv_parallel", 0)
    assert rubika_panel._pdf_parallel(alice) == 1


# --------------------------------------------------------------------------- #
# Cumulative delivery
# --------------------------------------------------------------------------- #
def test_a_file_is_delivered_every_batch(alice, fake_bot, monkeypatch):
    """The customer asked for output every 100 photos, so progress is visible
    instead of one long silence."""
    async def fake_build(jpegs, path):
        return len(jpegs)

    import pdf_export
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    async def scenario():
        delivery = rubika_panel._PdfDelivery(alice, "09120000001", batch=10)
        for i in range(25):
            await delivery.add(b"jpeg-%d" % i)
        await delivery.flush(final=True)
        return delivery

    delivery = asyncio.run(scenario())
    # 10, 20, then the final flush at 25
    assert delivery.files_sent == 3
    assert delivery.found == 25
    assert len(fake_bot.files) == 3
    assert "پایان" in fake_bot.files[-1][1]


def test_delivery_is_cumulative_not_incremental(alice, fake_bot, monkeypatch):
    """Each file contains everything so far, so the last file is the complete
    album and the customer never has to stitch anything together."""
    page_counts = []
    import pdf_export
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: page_counts.append(len(jpegs))
                        or len(jpegs))

    async def scenario():
        delivery = rubika_panel._PdfDelivery(alice, "0912", batch=5)
        for i in range(12):
            await delivery.add(b"x")
        await delivery.flush(final=True)

    asyncio.run(scenario())
    assert page_counts == [5, 10, 12]


def test_nothing_is_sent_when_no_photos_were_found(alice, fake_bot):
    async def scenario():
        delivery = rubika_panel._PdfDelivery(alice, "0912", batch=10)
        await delivery.flush(final=True)
        return delivery

    delivery = asyncio.run(scenario())
    assert delivery.files_sent == 0
    assert fake_bot.files == []


def test_empty_jpegs_are_ignored(alice, fake_bot, monkeypatch):
    import pdf_export
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    async def scenario():
        delivery = rubika_panel._PdfDelivery(alice, "0912", batch=5)
        for blob in (None, b"", b"real"):
            await delivery.add(blob)
        return delivery

    delivery = asyncio.run(scenario())
    assert delivery.found == 1


def test_a_build_failure_does_not_abort_the_export(alice, fake_bot, monkeypatch):
    """One bad file must not lose the photos already collected."""
    import pdf_export

    def boom(jpegs, path):
        raise RuntimeError("reportlab exploded")

    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs", boom)

    async def scenario():
        delivery = rubika_panel._PdfDelivery(alice, "0912", batch=2)
        for i in range(4):
            await delivery.add(b"x")
        return delivery

    delivery = asyncio.run(scenario())
    assert delivery.found == 4            # still collected
    assert delivery.files_sent == 0       # just not delivered


# --------------------------------------------------------------------------- #
# Concurrency cap protects the SERVER (not the session)
# --------------------------------------------------------------------------- #
def test_only_one_export_runs_at_a_time(alice, monkeypatch):
    monkeypatch.setattr(config, "PV_EXPORT_MAX_CONCURRENT", 1)
    assert busy.take_slot("pdf", config.PV_EXPORT_MAX_CONCURRENT) is True
    assert busy.take_slot("pdf", config.PV_EXPORT_MAX_CONCURRENT) is False
    busy.free_slot("pdf")
    assert busy.take_slot("pdf", config.PV_EXPORT_MAX_CONCURRENT) is True
    busy.free_slot("pdf")


def test_a_queued_export_tells_the_customer_why(alice, fake_bot, monkeypatch):
    """Decoding images is memory-heavy, so a second customer waits — and is told
    that, rather than seeing a button do nothing."""
    monkeypatch.setattr(config, "PV_EXPORT_MAX_CONCURRENT", 1)
    busy.take_slot("pdf", 1)
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)

    class _Msg:
        def __init__(self):
            self.text = ""

        async def edit(self, text, **kwargs):
            self.text = text

    msg = _Msg()
    asyncio.run(rubika_panel._run_pdf(alice, acc, msg))
    assert "در دسترس نیست" in msg.text
    busy.free_slot("pdf")


def test_the_slot_is_released_even_when_the_job_fails(alice, monkeypatch,
                                                      fake_bot):
    monkeypatch.setattr(config, "PV_EXPORT_MAX_CONCURRENT", 1)

    async def boom(*args, **kwargs):
        raise RuntimeError("collection failed")

    monkeypatch.setattr(rubika_panel, "_pdf_local", boom)
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    assert busy.slot_used("pdf") == 0
    assert busy.is_busy(rubika_panel._key(alice, "09120000001")) is False


def test_a_busy_account_is_refused_before_taking_a_slot(alice, fake_bot):
    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    busy.acquire(rubika_panel._key(alice, "09120000001"), "send",
                 customer_id=alice)

    class _Msg:
        def __init__(self):
            self.text = ""

        async def edit(self, text, **kwargs):
            self.text = text

    msg = _Msg()
    asyncio.run(rubika_panel._run_pdf(alice, acc, msg))
    assert "ارسال" in msg.text            # explains which job is holding it
    assert busy.slot_used("pdf") == 0


# --------------------------------------------------------------------------- #
# Parallel collection over ONE connection
# --------------------------------------------------------------------------- #
def test_parallel_collection_uses_one_client_and_bounds_concurrency(alice,
                                                                   fake_bot,
                                                                   monkeypatch):
    """The key property: many requests, ONE connection. Opening a second
    connection is what revokes a session."""
    monkeypatch.setattr(config, "PV_EXPORT_MAX_PHOTOS", 100)
    monkeypatch.setattr(config, "PV_EXPORT_PDF_BATCH", 1000)   # no delivery noise

    clients_opened = []
    in_flight = 0
    peak = 0

    import account_conn
    import pdf_export
    import rubika_client as rb

    class _Client:
        pass

    async def fake_call(customer_id, phone, fn, timeout=None):
        clients_opened.append(phone)
        return await fn(_Client())

    async def fake_chats(client, only_users=True):
        return ["g1"]

    async def fake_iter(client, guid, max_pages=200):
        for i in range(20):
            yield i, f"inline-{i}"

    async def fake_download(client, inline):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return b"rawphoto"

    monkeypatch.setattr(account_conn, "call", fake_call)
    monkeypatch.setattr(rb, "get_chat_list_guids", fake_chats)
    monkeypatch.setattr(rb, "iter_chat_photos", fake_iter)
    monkeypatch.setattr(rb, "download_photo", fake_download)
    monkeypatch.setattr(pdf_export, "prepare_image",
                        lambda blob, q=45, m=1000: b"jpeg")
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    db.set_setting(alice, "pv_mode", "parallel")
    db.set_setting(alice, "pv_parallel", 4)

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))

    assert len(clients_opened) == 1          # exactly one session was opened
    assert 1 < peak <= 4                     # genuinely parallel, but bounded
    assert db.usage_today(alice, "pdf") == 20


def test_safe_mode_downloads_strictly_one_at_a_time(alice, fake_bot, monkeypatch):
    monkeypatch.setattr(config, "PV_EXPORT_PDF_BATCH", 1000)
    in_flight = 0
    peak = 0

    import account_conn
    import pdf_export
    import rubika_client as rb

    async def fake_call(customer_id, phone, fn, timeout=None):
        return await fn(object())

    async def fake_chats(client, only_users=True):
        return ["g1"]

    async def fake_iter(client, guid, max_pages=200):
        for i in range(6):
            yield i, f"inline-{i}"

    async def fake_download(client, inline):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.005)
        in_flight -= 1
        return b"raw"

    monkeypatch.setattr(account_conn, "call", fake_call)
    monkeypatch.setattr(rb, "get_chat_list_guids", fake_chats)
    monkeypatch.setattr(rb, "iter_chat_photos", fake_iter)
    monkeypatch.setattr(rb, "download_photo", fake_download)
    monkeypatch.setattr(pdf_export, "prepare_image",
                        lambda blob, q=45, m=1000: b"jpeg")
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    db.set_setting(alice, "pv_mode", "safe")

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    assert peak == 1


def test_auto_mode_falls_back_after_repeated_failures(alice, fake_bot,
                                                     monkeypatch):
    """A platform that rejects the faster pattern must degrade the export, not
    fail it."""
    monkeypatch.setattr(config, "PV_EXPORT_PDF_BATCH", 1000)
    monkeypatch.setattr(config, "PV_EXPORT_FALLBACK_ERRORS", 3)

    import account_conn
    import pdf_export
    import rubika_client as rb

    async def fake_call(customer_id, phone, fn, timeout=None):
        return await fn(object())

    async def fake_chats(client, only_users=True):
        return ["g1", "g2"]

    async def fake_iter(client, guid, max_pages=200):
        for i in range(8):
            yield i, f"{guid}-{i}"

    calls = {"n": 0}

    async def flaky_download(client, inline):
        calls["n"] += 1
        if str(inline).startswith("g1"):
            raise RuntimeError("parallel rejected")
        return b"raw"

    monkeypatch.setattr(account_conn, "call", fake_call)
    monkeypatch.setattr(rb, "get_chat_list_guids", fake_chats)
    monkeypatch.setattr(rb, "iter_chat_photos", fake_iter)
    monkeypatch.setattr(rb, "download_photo", flaky_download)
    monkeypatch.setattr(pdf_export, "prepare_image",
                        lambda blob, q=45, m=1000: b"jpeg")
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    db.set_setting(alice, "pv_mode", "auto")
    db.set_setting(alice, "pv_parallel", 4)

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))

    # it switched to safe mode and still collected the second chat's photos
    assert db.usage_today(alice, "pdf") == 8


def test_a_stop_request_ends_collection(alice, fake_bot, monkeypatch):
    monkeypatch.setattr(config, "PV_EXPORT_PDF_BATCH", 1000)

    import account_conn
    import pdf_export
    import rubika_client as rb

    async def fake_call(customer_id, phone, fn, timeout=None):
        return await fn(object())

    async def fake_chats(client, only_users=True):
        return [f"g{i}" for i in range(20)]

    async def fake_iter(client, guid, max_pages=200):
        for i in range(5):
            yield i, f"{guid}-{i}"

    async def fake_download(client, inline):
        ctl = rubika_panel._jobs.get(account_id)
        if ctl and ctl["found"] >= 6:
            ctl["stop"] = True
        return b"raw"

    monkeypatch.setattr(account_conn, "call", fake_call)
    monkeypatch.setattr(rb, "get_chat_list_guids", fake_chats)
    monkeypatch.setattr(rb, "iter_chat_photos", fake_iter)
    monkeypatch.setattr(rb, "download_photo", fake_download)
    monkeypatch.setattr(pdf_export, "prepare_image",
                        lambda blob, q=45, m=1000: b"jpeg")
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    account_id = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, account_id)
    db.set_setting(alice, "pv_mode", "safe")

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    assert db.usage_today(alice, "pdf") < 100        # stopped well short


def test_the_photo_cap_is_respected(alice, fake_bot, monkeypatch):
    monkeypatch.setattr(config, "PV_EXPORT_MAX_PHOTOS", 5)
    monkeypatch.setattr(config, "PV_EXPORT_PDF_BATCH", 1000)

    import account_conn
    import pdf_export
    import rubika_client as rb

    async def fake_call(customer_id, phone, fn, timeout=None):
        return await fn(object())

    async def fake_chats(client, only_users=True):
        return ["g1", "g2", "g3"]

    async def fake_iter(client, guid, max_pages=200):
        for i in range(10):
            yield i, f"{guid}-{i}"

    async def fake_download(client, inline):
        return b"raw"

    monkeypatch.setattr(account_conn, "call", fake_call)
    monkeypatch.setattr(rb, "get_chat_list_guids", fake_chats)
    monkeypatch.setattr(rb, "iter_chat_photos", fake_iter)
    monkeypatch.setattr(rb, "download_photo", fake_download)
    monkeypatch.setattr(pdf_export, "prepare_image",
                        lambda blob, q=45, m=1000: b"jpeg")
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    aid = db.add_account(alice, "09120000001")
    acc = db.get_account(alice, aid)
    db.set_setting(alice, "pv_mode", "safe")

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    assert db.usage_today(alice, "pdf") <= 5


# --------------------------------------------------------------------------- #
# The remote path streams instead of returning one huge body
# --------------------------------------------------------------------------- #
def test_remote_export_polls_and_streams_batches(alice, fake_bot, monkeypatch):
    """A 2000-photo account returned in one base64 body is roughly 800 MB through
    an SSH tunnel; polling keeps memory and timeouts sane."""
    import base64
    import worker

    monkeypatch.setattr(config, "PV_EXPORT_POLL_SEC", 0.01)
    monkeypatch.setattr(config, "PV_EXPORT_PDF_BATCH", 1000)

    wid = db.add_worker("wk-a", "1.2.3.4", 22, "root", "e", 8765, "t")
    db.update_worker_health(wid, "ok", 50, 1)
    aid = db.add_account(alice, "09120000001", worker_id=wid)
    acc = db.get_account(alice, aid)

    responses = [
        {"state": "running", "batch": [base64.b64encode(b"a").decode()],
         "pending": 0, "fallback": False},
        {"state": "running", "batch": [base64.b64encode(b"b").decode()],
         "pending": 0, "fallback": True},
        {"state": "done", "batch": [], "pending": 0, "fallback": True},
    ]

    async def fake_api(w, method, path, payload=None, timeout=120):
        if path == "/pvexport/start":
            return {"ok": True, "job_id": "job1"}
        if path.startswith("/pvexport/status"):
            return responses.pop(0) if responses else {"state": "done",
                                                       "batch": [], "pending": 0}
        return {}

    import pdf_export
    monkeypatch.setattr(worker, "api_call", fake_api)
    monkeypatch.setattr(pdf_export, "build_pdf_from_jpegs",
                        lambda jpegs, path: len(jpegs))

    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    assert db.usage_today(alice, "pdf") == 2


def test_remote_export_gives_up_after_repeated_poll_failures(alice, fake_bot,
                                                            monkeypatch):
    import worker
    monkeypatch.setattr(config, "PV_EXPORT_POLL_SEC", 0.01)
    monkeypatch.setattr(config, "PV_EXPORT_MAX_POLL_FAILS", 2)

    wid = db.add_worker("wk-a", "1.2.3.4", 22, "root", "e", 8765, "t")
    aid = db.add_account(alice, "09120000001", worker_id=wid)
    acc = db.get_account(alice, aid)

    async def fake_api(w, method, path, payload=None, timeout=120):
        if path == "/pvexport/start":
            return {"ok": True, "job_id": "job1"}
        raise OSError("tunnel died")

    monkeypatch.setattr(worker, "api_call", fake_api)
    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    # the job ended rather than looping forever, and the session was released
    assert busy.is_busy(rubika_panel._key(alice, "09120000001")) is False


def test_remote_export_handles_a_missing_job_id(alice, fake_bot, monkeypatch):
    import worker
    wid = db.add_worker("wk-a", "1.2.3.4", 22, "root", "e", 8765, "t")
    aid = db.add_account(alice, "09120000001", worker_id=wid)
    acc = db.get_account(alice, aid)

    async def fake_api(w, method, path, payload=None, timeout=120):
        return {"ok": False}

    monkeypatch.setattr(worker, "api_call", fake_api)
    asyncio.run(rubika_panel._run_pdf(alice, acc, None))
    assert busy.slot_used("pdf") == 0
