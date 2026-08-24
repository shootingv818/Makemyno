"""
The send card cut the advert at 80 characters, and APK -> ZIP was never ported.

THE TRUNCATION
    • Content : سلام خوبی چندتا عکس یادگاری ساختم ازت خواستی ببین گفتم شاید خوشحال شی

    https://t
The card rendered the customer's whole advert through kv("Content", text[:80]).
Eighty characters lands in the middle of a sentence or, as above, in the middle of
a URL — and nothing said it had been cut, so the owner could not tell what was
actually being sent. A kv row is for one short value; a paragraph needs rows of its
own. cards.body() does that, and ANNOUNCES the cut with the real length when one
is unavoidable.

THE MISSING TOOL
APK -> ZIP existed in the reference and I skipped it. Ported now, including the
part that matters: the archive is VERIFIED after writing — testzip(), the stored
size, and a SHA-256 of the extracted bytes against the source. The point of the
tool is to get an installer past an extension filter, so an APK that comes out
corrupted is worse than no tool at all: the customer would blame the app.

Every test below was mutation-verified with __pycache__ cleared.
"""
import hashlib
import io
import os
import zipfile

import pytest

import cards
import config
import customer_bot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LONG_ADVERT = ("سلام خوبی چندتا عکس یادگاری ساختم ازت خواستی ببین گفتم شاید "
               "خوشحال شی\n\nhttps://t.me/example/1234567890")


# --------------------------------------------------------------------------- #
# cards.body
# --------------------------------------------------------------------------- #
def test_a_whole_advert_survives_the_card():
    rows = cards.body(LONG_ADVERT)
    joined = "\n".join(rows)
    assert "https://t.me/example/1234567890" in joined, \
        ("the URL was being cut after 'https://t', which is exactly the part the "
         "owner needs to see")
    assert "…" not in joined, "nothing under the limit should be marked as cut"


def test_each_line_becomes_its_own_row():
    rows = cards.body("first\n\nsecond\nthird")
    assert rows == ["first", "second", "third"], \
        "blank lines are dropped; real lines are kept separate"


def test_a_cut_is_announced_with_the_real_length():
    rows = cards.body("x" * 900, limit=100)
    assert rows[-1].startswith("…")
    assert "900" in rows[-1], \
        ("a truncated preview that does not say so is how an 80-character cut "
         "went unnoticed")


def test_the_limit_is_respected():
    rows = cards.body("y" * 500, limit=50)
    assert len(rows[0]) == 50


def test_empty_content_renders_a_dash_not_a_blank():
    assert cards.body("") == ["—"]
    assert cards.body(None) == ["—"]
    assert cards.body("   \n  ") == ["—"]


def test_the_send_card_uses_body_not_a_kv_slice():
    src = open(os.path.join(ROOT, "rubika_panel.py"), encoding="utf-8").read()
    start = src.index('"send_start"')
    section = src[start:src.index("platform=\"Rubika\")", start) + 20]
    assert "cards.body(" in section, "the advert must be rendered as rows"
    assert "[:80]" not in section, "the 80-character slice was the defect"


# --------------------------------------------------------------------------- #
# file-name safety
# --------------------------------------------------------------------------- #
def test_a_name_cannot_escape_the_directory():
    # "...." and ".." are the traversal that is LEFT once the slashes are gone.
    # Without them the dots-only guard was never exercised and deleting it passed.
    for hostile in ("../../etc/passwd", "/etc/passwd", "..\\..\\win.ini",
                    "....", "..", "./../.."):
        got = customer_bot._safe_name(hostile, ".zip")
        assert "/" not in got and "\\" not in got and ".." not in got, hostile
        assert got.endswith(".zip")


def test_the_extension_is_forced_and_not_doubled():
    assert customer_bot._safe_name("my_app", ".zip") == "my_app.zip"
    assert customer_bot._safe_name("my_app.zip", ".zip") == "my_app.zip"
    assert customer_bot._safe_name("my_app.apk", ".zip") == "my_app.zip"


def test_an_empty_or_hostile_name_still_produces_something():
    assert customer_bot._safe_name("", ".zip") == "file.zip"
    assert customer_bot._safe_name("!!!", ".zip") == "file.zip"


def test_the_name_is_length_capped():
    assert len(customer_bot._safe_name("a" * 500, ".zip")) <= 64


def test_spaces_become_underscores():
    assert customer_bot._safe_name("my cool app", ".zip") == "my_cool_app.zip"


# --------------------------------------------------------------------------- #
# the zip must round-trip byte-for-byte
# --------------------------------------------------------------------------- #
@pytest.fixture
def apk(tmp_path):
    path = tmp_path / "src.apk"
    # Deliberately incompressible-ish random bytes: a length check alone would
    # pass on zeroes even if the content were mangled.
    path.write_bytes(os.urandom(200_000))
    return str(path)


def test_the_hash_helper_matches_hashlib(apk):
    expected = hashlib.sha256(open(apk, "rb").read()).hexdigest()
    assert customer_bot._sha256_file(apk) == expected


def test_a_zip_written_the_way_the_tool_writes_it_round_trips(apk):
    """The contract the tool verifies, asserted directly."""
    zip_path = apk + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(apk, arcname="app.apk")

    source = open(apk, "rb").read()
    with zipfile.ZipFile(zip_path, "r") as archive:
        assert archive.testzip() is None
        assert archive.getinfo("app.apk").file_size == len(source)
        assert archive.read("app.apk") == source, \
            "an installer that arrives corrupted is worse than no tool"


def test_the_tool_verifies_all_three_things():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_apk_zipname")
    section = src[start:src.index("async def _edit_or_send")]
    assert "testzip()" in section, "a corrupt archive must be caught"
    assert "file_size != os.path.getsize" in section, "a size mismatch must be caught"
    assert "hexdigest() != source_hash" in section, \
        ("size alone is not proof of identity; the extracted bytes must hash to "
         "the source")


def test_the_tool_shows_the_hash_to_the_customer():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_apk_zipname")
    section = src[start:src.index("async def _edit_or_send")]
    assert 'cards.kv("SHA-256"' in section, \
        "the customer should be able to check the file themselves"


def test_both_copies_are_deleted_afterwards():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_apk_zipname")
    section = src[start:src.index("async def _edit_or_send")]
    assert "finally:" in section
    assert "for leftover in (apk_path, zip_path)" in section, \
        ("these are whole APKs; leaving them behind fills the disk one customer "
         "at a time")


def test_the_size_ceiling_is_checked_before_downloading():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_apk")
    section = src[start:src.index("async def _step_apk_zipname")]
    # The GUARD, not merely a mention of the constant: the constant also appears
    # in the card rows INSIDE the guard, so asserting on the name alone survived
    # replacing the condition with `if False`.
    guard = "if size and size > config.APK_ZIP_MAX_MB"
    assert guard in section, "the size ceiling is not enforced"
    assert section.index(guard) < section.index("download_media"), \
        "failing fast beats failing after a 200MB download"


def test_only_apk_files_are_accepted():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def _step_apk")
    section = src[start:src.index("async def _step_apk_zipname")]
    assert '.apk' in section and 'endswith' in section


def test_the_tool_is_wired_into_the_menu_and_the_router():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    assert 'b"tool_apkzip"' in src
    assert '"await_apk": _step_apk' in src, "the file step is not routed"
    assert '"await_apk_zipname": _step_apk_zipname' in src, \
        "the name step is not routed"


def test_the_tool_needs_no_active_subscription():
    src = open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8").read()
    start = src.index("async def tool_apkzip_cb")
    section = src[start:src.index("async def _step_apk")]
    assert "need_active=False" in section


def test_the_ceiling_is_configurable():
    assert config.APK_ZIP_MAX_MB >= 1
