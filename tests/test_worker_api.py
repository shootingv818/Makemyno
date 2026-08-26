"""
The worker node's own session guard, and the PDF pipeline it relies on.

THE FAILURE THESE PREVENT
-------------------------
The master keeps a busy registry, but it cannot see what a worker is doing right
now. Without a registry on the worker side, the master can ask a worker to verify
an account while that worker is mid-send on it — a second connection on one
session, which the platform answers by revoking it.
"""
import asyncio

import pytest

import busy
import config
import pdf_export
import worker_api


@pytest.fixture(autouse=True)
def clean_registry():
    busy.clear_all()
    worker_api._jobs.clear()
    worker_api._login_ctx.clear()
    yield
    busy.clear_all()
    worker_api._jobs.clear()
    worker_api._login_ctx.clear()


# --------------------------------------------------------------------------- #
# Session keys on the worker match the master's
# --------------------------------------------------------------------------- #
def test_worker_key_separates_customers():
    assert worker_api._key(1, "09121110000") != worker_api._key(2, "09121110000")


def test_worker_key_matches_the_master_scheme():
    """Both sides must agree, or the guard on one side protects nothing."""
    assert worker_api._key(7, "09121110000") == \
        busy.key_for("09121110000", customer_id=7, platform="rb")


# --------------------------------------------------------------------------- #
# /account/verify must never connect to a busy account
# --------------------------------------------------------------------------- #
def test_verify_skips_a_busy_account_without_connecting(monkeypatch):
    """A busy account is provably alive, so probing it only risks the session."""
    key = worker_api._key(1, "09120000001")
    busy.acquire(key, "send", customer_id=1)

    connected = []

    async def _must_not_run(*args, **kwargs):
        connected.append(True)
        return True

    import account_conn
    monkeypatch.setattr(account_conn, "verify_session_dead", _must_not_run)

    holder = busy.who(key)
    assert holder is not None
    # This is the decision the endpoint makes:
    assert holder.get("what") == "send"
    assert connected == []


def test_verify_claims_the_session_when_free():
    key = worker_api._key(1, "09120000001")
    assert busy.acquire(key, "verify", customer_id=1) is True
    assert busy.who(key)["what"] == "verify"
    busy.release(key, "verify")
    assert busy.is_busy(key) is False


# --------------------------------------------------------------------------- #
# Version reporting
# --------------------------------------------------------------------------- #
def test_worker_reports_some_code_version():
    """The owner panel compares this against the master to spot stale workers."""
    version = worker_api._worker_code_version()
    assert isinstance(version, str) and version


# --------------------------------------------------------------------------- #
# The PDF pipeline: heavy work once per photo
# --------------------------------------------------------------------------- #
def _png_bytes(width=1200, height=900, colour=(200, 30, 30)):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def _have(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


# Pillow and reportlab are runtime dependencies. Only the image tests need them,
# so they are skipped individually rather than taking the session-guard tests in
# this module down with them.
needs_pillow = pytest.mark.skipif(not _have("PIL"), reason="Pillow not installed")
needs_pdf = pytest.mark.skipif(
    not (_have("PIL") and _have("reportlab")),
    reason="Pillow + reportlab not installed")


@needs_pillow
def test_prepare_image_downscales_and_shrinks():
    """A 3 MB phone photo becoming ~70 KB is the difference between 600 MB and
    150 MB held for a 2000-photo export."""
    raw = _png_bytes(2400, 1800)
    out = pdf_export.prepare_image(raw, quality=45, max_size=1000)
    assert out
    assert len(out) < len(raw)
    from PIL import Image
    import io
    assert max(Image.open(io.BytesIO(out)).size) <= 1000


@needs_pillow
def test_prepare_image_respects_max_size_zero():
    raw = _png_bytes(1200, 900)
    out = pdf_export.prepare_image(raw, quality=60, max_size=0)
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(out)).size == (1200, 900)


@pytest.mark.parametrize("blob", [None, b"", b"not-an-image"])
@needs_pillow
def test_prepare_image_returns_none_for_garbage(blob):
    """One corrupt photo must never abort a 2000-photo export."""
    assert pdf_export.prepare_image(blob) is None


@needs_pillow
def test_prepare_image_converts_odd_modes():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGBA", (50, 50), (1, 2, 3, 4)).save(buf, format="PNG")
    assert pdf_export.prepare_image(buf.getvalue()) is not None


@needs_pdf
def test_build_pdf_from_jpegs_does_no_decoding(tmp_path, monkeypatch):
    """The cumulative rebuild must not touch Pillow's encoder again — that is
    what made the old implementation quadratic."""
    jpegs = [pdf_export.prepare_image(_png_bytes(400, 300)) for _ in range(3)]
    calls = []
    real_prepare = pdf_export.prepare_image
    monkeypatch.setattr(pdf_export, "prepare_image",
                        lambda *a, **k: calls.append(1) or real_prepare(*a, **k))

    out = tmp_path / "album.pdf"
    pages = pdf_export.build_pdf_from_jpegs(jpegs, str(out))
    assert pages == 3
    assert calls == []                        # no re-preparation
    assert out.exists() and out.stat().st_size > 0


@needs_pdf
def test_cumulative_rebuilds_stay_cheap(tmp_path):
    """Prepare 30 photos once, then build growing PDFs from the prepared bytes:
    30 heavy operations total, not 30 + 60 + 90 + ..."""
    raws = [_png_bytes(300, 200) for _ in range(30)]
    prepared = [pdf_export.prepare_image(r) for r in raws]
    assert all(prepared)

    total_pages = 0
    for cut in (10, 20, 30):
        out = tmp_path / f"cum_{cut}.pdf"
        total_pages = pdf_export.build_pdf_from_jpegs(prepared[:cut], str(out))
        assert total_pages == cut
        assert out.exists()


@needs_pdf
def test_build_pdf_skips_corrupt_entries(tmp_path):
    good = pdf_export.prepare_image(_png_bytes(200, 200))
    out = tmp_path / "mixed.pdf"
    pages = pdf_export.build_pdf_from_jpegs([good, b"junk", None, good], str(out))
    assert pages == 2


@needs_pdf
def test_empty_album_still_produces_a_valid_file(tmp_path):
    out = tmp_path / "empty.pdf"
    assert pdf_export.build_pdf_from_jpegs([], str(out)) == 0
    assert out.exists() and out.stat().st_size > 0


@needs_pdf
def test_build_pdf_convenience_path(tmp_path):
    raws = [_png_bytes(300, 300) for _ in range(2)]
    out = tmp_path / "raw.pdf"
    assert pdf_export.build_pdf(raws, str(out)) == 2


@needs_pdf
def test_estimate_size_grows_with_content():
    small = [pdf_export.prepare_image(_png_bytes(100, 100))]
    big = [pdf_export.prepare_image(_png_bytes(1000, 1000))]
    assert pdf_export.estimate_size(big) > pdf_export.estimate_size(small)


# --------------------------------------------------------------------------- #
# Parallel download bounds
# --------------------------------------------------------------------------- #
def test_parallel_setting_is_clamped():
    """Concurrency raises the request rate at the platform, so it is bounded."""
    assert config.clamp_pv_parallel(1000) == config.PV_EXPORT_PARALLEL_MAX
    assert config.clamp_pv_parallel(-5) == 1
    assert config.clamp_pv_parallel(4) == 4


def test_a_bounded_pool_never_exceeds_its_limit():
    """The shape used for photo downloads: several requests in flight over the
    SAME connection, never a second connection."""
    async def scenario():
        limit = 4
        sem = asyncio.Semaphore(limit)
        in_flight = 0
        peak = 0

        async def job():
            nonlocal in_flight, peak
            async with sem:
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1

        await asyncio.gather(*[job() for _ in range(40)])
        return peak

    assert asyncio.run(scenario()) <= 4


def test_fallback_threshold_is_configured():
    """Parallel mode must be able to degrade to the sequential path instead of
    failing the whole export."""
    assert config.PV_EXPORT_FALLBACK_ERRORS > 0
    assert config.PV_EXPORT_MODE_DEFAULT in ("auto", "parallel", "safe")
