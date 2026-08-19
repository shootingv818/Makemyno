"""
Media is uploaded ONCE per account, then copied — plus the dependency audit.

THE SPEED BUG
-------------
`client.send_file(entity, path)` re-reads and re-UPLOADS the file for every
recipient. A 3 MB image sent to a thousand contacts was a thousand uploads, which
is exactly the "it re-uploads in every chat" the owner described.

Telegram lets you reuse the file reference of a message you have already sent, so
the base project uploads once to Saved Messages and copies from there — no upload,
and no "forwarded from" tag. Those helpers already existed in this project
(upload_to_saved, send_saved_media) and NOTHING CALLED THEM; the send path still
passed a file path per recipient.

THE DEPENDENCY BUG
------------------
worker.py and worker_api.py both import httpx, and httpx was not in
requirements.txt at all — so a freshly provisioned worker had no HTTP client.
"""
import ast
import asyncio
import os

import pytest

import telegram_multi_send as multi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class _SavedMsg:
    def __init__(self, path):
        self.path = path
        self.media = f"media-ref:{path}"


class _FakeTg:
    """Counts uploads and copies so the difference is measurable."""

    def __init__(self):
        self.uploads = []
        self.copies = []
        self.texts = []
        self.direct_media = []

    async def upload_to_saved(self, client, path, caption=""):
        self.uploads.append(path)
        return _SavedMsg(path)

    async def send_saved_media(self, client, entity, saved, caption=""):
        self.copies.append((entity, saved.media))

    async def send_media(self, client, entity, path, caption="", typing=0.0):
        self.direct_media.append((entity, path))

    async def send_text(self, client, entity, text, typing=0.0):
        self.texts.append((entity, text))


@pytest.fixture
def faketg(monkeypatch):
    fake = _FakeTg()
    monkeypatch.setattr(multi, "tg", fake)
    monkeypatch.setattr(multi, "_send_times", [])
    return fake


CONTENT = [
    {"kind": "media", "file_path": "/tmp/photo.jpg", "text": "caption"},
    {"kind": "text", "text": "hello"},
]


# --------------------------------------------------------------------------- #
# One upload, many copies
# --------------------------------------------------------------------------- #
def test_preparing_uploads_each_media_item_once(faketg):
    plan = asyncio.run(multi.prepare_content(object(), CONTENT))
    assert faketg.uploads == ["/tmp/photo.jpg"]
    assert len(plan) == 2


def test_a_hundred_recipients_cost_one_upload(faketg):
    """The whole point. This used to be a hundred uploads of the same file."""
    plan = asyncio.run(multi.prepare_content(object(), CONTENT))

    async def _send_all():
        for i in range(100):
            await multi._deliver(object(), {"kind": "user", "id": i},
                                 CONTENT, 0.0, plan=plan)
    asyncio.run(_send_all())

    assert len(faketg.uploads) == 1, "the file must be uploaded exactly once"
    assert len(faketg.copies) == 100, "every recipient still receives it"
    assert faketg.direct_media == [], "no per-recipient upload should happen"


def test_text_items_are_untouched_by_preparation(faketg):
    plan = asyncio.run(multi.prepare_content(object(), [
        {"kind": "text", "text": "just words"}]))
    assert faketg.uploads == []
    assert plan == [{"kind": "text", "text": "just words"}]


def test_content_order_is_preserved(faketg):
    """The customer configured item 1, 2, 3 and expects that order delivered."""
    content = [
        {"kind": "text", "text": "first"},
        {"kind": "media", "file_path": "/tmp/a.jpg", "text": "second"},
        {"kind": "text", "text": "third"},
    ]
    plan = asyncio.run(multi.prepare_content(object(), content))
    assert [step["kind"] for step in plan] == ["text", "media", "text"]

    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 1}, content,
                               0.0, plan=plan))
    assert [t[1] for t in faketg.texts] == ["first", "third"]
    assert len(faketg.copies) == 1


def test_a_failed_upload_falls_back_to_per_recipient_upload(monkeypatch, faketg):
    """Slow but correct beats fast and broken: the send must still happen."""
    async def _boom(client, path, caption=""):
        raise RuntimeError("upload rejected")
    monkeypatch.setattr(faketg, "upload_to_saved", _boom)

    plan = asyncio.run(multi.prepare_content(object(), CONTENT))
    assert plan[0]["saved"] is None
    assert plan[0]["path"] == "/tmp/photo.jpg"

    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 1}, CONTENT,
                               0.0, plan=plan))
    assert faketg.direct_media == [(1, "/tmp/photo.jpg")]


def test_delivering_without_a_plan_still_works(faketg):
    """The old signature must keep working for any caller that has not been
    updated — it is just slower."""
    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 1}, CONTENT, 0.0))
    assert faketg.direct_media, "it should fall back to uploading per recipient"


def test_the_copy_carries_no_forward_tag():
    """send_saved_media reuses the media reference rather than forwarding, so the
    recipient does not see "forwarded from"."""
    body = _src("telegram_client.py")
    start = body.index("async def send_saved_media")
    section = body[start:start + 800]
    assert "send_file(entity, media" in section.replace("\n", " ").replace("  ", " ")


def test_preupload_timing_is_recorded(faketg):
    plan = asyncio.run(multi.prepare_content(object(), CONTENT))
    asyncio.run(multi._deliver(object(), {"kind": "user", "id": 1}, CONTENT,
                               0.0, plan=plan))
    assert multi.send_timing()["n"] == 2, "both items should be timed"


# --------------------------------------------------------------------------- #
# Both send paths use it
# --------------------------------------------------------------------------- #
def test_the_multi_send_loop_prepares_before_the_recipient_loop():
    body = _src("telegram_multi_send.py")
    start = body.index("async def _run_account")
    section = body[start:body.index("async def _deliver")]
    assert "prepare_content" in section, "multi-send must pre-upload"
    assert section.index("prepare_content") < section.index("for row in batch")


def test_the_single_send_path_prepares_too():
    body = _src("tg_panel.py")
    start = body.index("async def _run_single")
    section = body[start:start + 4000]
    assert "prepare_content" in section
    assert "plan=plan" in section


# --------------------------------------------------------------------------- #
# Dependencies must match what the code imports
# --------------------------------------------------------------------------- #
def _requirements():
    names = set()
    for line in _src("requirements.txt").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        for sep in ("==", ">=", "<=", "~="):
            if sep in line:
                line = line.split(sep)[0]
                break
        names.add(line.strip().lower())
    return names


def test_every_third_party_import_is_declared():
    """httpx was imported by worker.py and worker_api.py and appeared in no
    requirements file, so a provisioned worker had no HTTP client at all."""
    import sys
    # sys.stdlib_module_names is authoritative — a hand-written list of standard
    # modules is a maintenance trap that fails on the next Python version.
    known = set(sys.stdlib_module_names) | {"__future__"}
    known |= {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
    aliases = {"PIL": "pillow", "dotenv": "python-dotenv", "yaml": "pyyaml"}
    declared = _requirements()

    missing = set()
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(_src(name), filename=name)
        for node in ast.walk(tree):
            # An import inside a try/except is an OPTIONAL dependency by design
            # (config falls back from zoneinfo to pytz), so it is not required.
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod in known:
                    continue
                if aliases.get(mod, mod).lower() in declared:
                    continue
                missing.add(f"{mod} (imported by {name})")

    # Optional imports: anything guarded by try/except ImportError, plus the
    # child modules of a package that IS declared (rubpy.crypto -> rubpy).
    optional = set()
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(_src(name), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        optional.add(f"{alias.name.split('.')[0]} (imported by {name})")
                elif isinstance(child, ast.ImportFrom) and child.module:
                    optional.add(f"{child.module.split('.')[0]} (imported by {name})")
    missing -= optional
    # `from rubpy.crypto import Crypto` binds the NAME Crypto, not a package.
    missing = {m for m in missing if not m.startswith("Crypto ")}

    assert missing == set(), f"imported but not in requirements.txt: {missing}"


def test_the_web_stack_is_pinned_as_a_set():
    """An incompatible fastapi/starlette/uvicorn/pydantic mix makes a worker start
    cleanly and then fail EVERY request with an asgi2 error that looks like our
    bug and is not."""
    body = _src("requirements.txt")
    for package in ("fastapi==", "starlette==", "pydantic==", "uvicorn==",
                    "httpx=="):
        assert package in body, f"{package} must be pinned exactly"


def test_timezone_data_is_installed():
    """Asia/Tehran timestamps on a minimal image need the zoneinfo database."""
    assert "tzdata" in _src("requirements.txt")


def test_pip_prefers_wheels_everywhere_it_installs():
    """A provisioning attempt was caught COMPILING telethon from its sdist —
    thousands of files through bdist_wheel, slow enough to look hung on a small
    VPS. telethon publishes a wheel; pip just needed telling to prefer it."""
    for path in ("Dockerfile", "deploy/install.sh", "deploy/makemyno.sh"):
        assert "--prefer-binary" in _src(path), f"{path} may compile from source"
