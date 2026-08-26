"""
Live progress for a worker build.

The card used to say "this takes a few minutes" and then sit motionless for ten,
so the only way to tell progress from a hang was to open a second SSH session and
poke at the server. This streams the build and shows a percentage.

Docker's LEGACY builder prints "Step 3/12 : RUN ...", which is an exact fraction.
That is only available because DOCKER_BUILDKIT=0 is pinned — BuildKit prints
nothing comparable — so an earlier decision made for a different reason pays for
itself again here.
"""
import asyncio
import os

import pytest

import cards
import config
import worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


BUILD_OUTPUT = [
    "Sending build context to Docker daemon  120kB",
    "Step 1/12 : FROM python:3.11-slim",
    " ---> a1b2c3d4",
    "Step 2/12 : ENV PYTHONUNBUFFERED=1",
    " ---> Running in 9f8e7d",
    "Step 6/12 : RUN pip install --upgrade pip  && pip install -r requirements.txt",
    "Collecting telethon==1.36.0",
    "  Downloading Telethon-1.36.0-py3-none-any.whl (700 kB)",
    "Collecting cryptography==42.0.8",
    "Installing collected packages: telethon, cryptography",
    "Step 12/12 : CMD [\"python\", \"main.py\", \"worker\"]",
    "Successfully built abcdef123456",
    "Successfully tagged makemyno-worker:latest",
]


# --------------------------------------------------------------------------- #
# Parsing progress out of the build output
# --------------------------------------------------------------------------- #
def test_a_step_line_sets_the_fraction():
    state = {}
    assert worker.build_progress("Step 3/12 : RUN apt-get update", state) is True
    assert state["step"] == 3
    assert state["steps"] == 12
    assert "apt-get update" in state["detail"]


def test_noise_lines_do_not_count_as_progress():
    state = {"step": 1, "steps": 12}
    assert worker.build_progress(" ---> a1b2c3", state) is False
    assert state["step"] == 1, "the fraction must not drift on unrelated output"


def test_pip_output_fills_the_gap_inside_the_slowest_step():
    """Step 6 of 12 can take eight minutes on its own; without this the bar sits
    at 50% and looks stuck."""
    state = {}
    assert worker.build_progress("Collecting telethon==1.36.0", state) is True
    assert "telethon" in state["sub"]
    assert worker.build_progress("Downloading Telethon-1.36.0.whl", state) is True
    assert "Telethon" in state["sub"]


def test_installing_packages_is_reported():
    state = {}
    assert worker.build_progress("Installing collected packages: a, b", state)
    assert state["sub"] == "نصب بسته‌ها"


def test_success_is_reported():
    state = {}
    assert worker.build_progress("Successfully tagged makemyno-worker", state)
    assert "ساخته شد" in state["sub"]


def test_a_new_step_clears_the_previous_substep():
    """Otherwise "downloading telethon" lingers under a step that has moved on."""
    state = {}
    worker.build_progress("Step 6/12 : RUN pip install", state)
    worker.build_progress("Collecting telethon", state)
    assert state["sub"]
    worker.build_progress("Step 7/12 : COPY . .", state)
    assert state["sub"] == ""


def test_the_whole_build_walks_from_start_to_finish():
    state = {"started": 0}
    for line in BUILD_OUTPUT:
        worker.build_progress(line, state)
    assert state["step"] == 12 and state["steps"] == 12


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_the_card_shows_a_bar_and_a_percentage():
    import time
    state = {"step": 6, "steps": 12, "detail": "RUN pip install",
             "sub": "دریافت telethon", "started": time.time()}
    rows = worker.progress_card_rows(state)
    body = "\n".join(rows)
    assert "50%" in body
    assert "(6/12)" in body
    assert cards.bar(6, 12) in body
    assert "telethon" in body


def test_the_card_shows_elapsed_time():
    import time
    rows = worker.progress_card_rows({"step": 1, "steps": 12,
                                      "started": time.time() - 125})
    assert "2:05" in "\n".join(rows), "elapsed time makes a slow build legible"


def test_the_card_renders_before_the_first_step_is_known():
    rows = worker.progress_card_rows({"started": 0})
    assert rows, "it must render something even with no fraction yet"


# --------------------------------------------------------------------------- #
# The stream itself
# --------------------------------------------------------------------------- #
class _FakeProcess:
    def __init__(self, lines, exit_status=0):
        self._lines = lines
        self.exit_status = exit_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def stdout(self):
        async def _gen():
            for line in self._lines:
                yield line + "\n"
        return _gen()

    async def wait(self):
        return self.exit_status


class _FakeConn:
    def __init__(self, lines, exit_status=0):
        self._lines = lines
        self._exit = exit_status

    def create_process(self, command):
        return _FakeProcess(self._lines, self._exit)


def test_streaming_reports_every_progress_line():
    seen = []

    async def _on_line(line):
        seen.append(line)

    code, tail = asyncio.run(worker._run_streaming(
        _FakeConn(BUILD_OUTPUT), "docker build .", _on_line))
    assert code == 0
    assert len(seen) == len(BUILD_OUTPUT)
    assert "Successfully tagged makemyno-worker:latest" in tail


def test_streaming_returns_the_exit_status():
    async def _noop(line):
        return None
    code, _tail = asyncio.run(worker._run_streaming(
        _FakeConn(["boom"], exit_status=1), "docker build .", _noop))
    assert code == 1


def test_the_tail_is_kept_for_diagnosis():
    """A failed build still has to be explainable by _explain_setup_failure."""
    async def _noop(line):
        return None
    lines = ["noise"] * 50 + ["No space left on device"]
    _code, tail = asyncio.run(worker._run_streaming(
        _FakeConn(lines, exit_status=1), "docker build .", _noop))
    assert "No space left on device" in tail
    assert "دیسک" in worker._explain_setup_failure("", tail, "1.2.3.4")


def test_a_reporting_failure_does_not_break_the_build():
    """A Telegram edit failing must not abort a ten-minute build."""
    async def _boom(line):
        raise RuntimeError("telegram said no")

    code, _tail = asyncio.run(worker._run_streaming(
        _FakeConn(BUILD_OUTPUT), "docker build .", _boom))
    assert code == 0, "the build must survive a broken progress report"


def test_the_line_buffer_is_bounded():
    """A build prints thousands of lines; only the tail is ever useful."""
    async def _noop(line):
        return None
    _code, tail = asyncio.run(worker._run_streaming(
        _FakeConn([f"line {i}" for i in range(5000)]), "x", _noop))
    assert len(tail.splitlines()) <= 120


def test_a_stream_that_never_ends_times_out_with_the_tail():
    class _Hanging(_FakeConn):
        def create_process(self, command):
            class _P(_FakeProcess):
                @property
                def stdout(self):
                    async def _gen():
                        yield "Step 1/12 : FROM python\n"
                        await asyncio.sleep(10)
                    return _gen()
            return _P(["Step 1/12 : FROM python"])

    async def _noop(line):
        return None

    with pytest.raises(worker.SSHStepTimeout) as exc:
        asyncio.run(worker._run_streaming(_Hanging([]), "x", _noop,
                                          timeout=0.05, label="ساخت ایمیج"))
    text = str(exc.value)
    assert "ساخت ایمیج" in text
    assert "Step 1/12" in text, "the last lines must be in the timeout report"


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_the_build_step_is_streamed():
    body = _src("worker.py")
    start = body.index("async def provision_worker")
    section = body[start:start + 9000]
    assert "_run_streaming" in section, "the build must stream, not block"
    assert "2>&1" in section, "stderr must be merged or half the output is lost"


def test_progress_edits_are_throttled():
    """A build prints thousands of lines and Telegram rate-limits edits."""
    assert config.PROVISION_REPORT_EVERY >= 2.0
    body = _src("worker.py")
    assert "PROVISION_REPORT_EVERY" in body


def test_the_owner_card_replaces_the_live_block_instead_of_stacking_it():
    """Appending every update would turn one build into a hundred stacked copies
    of its own progress bar."""
    body = _src("owner_bot.py")
    start = body.index("async def _provision")
    section = body[start:start + 2000]
    assert "live[:] = block" in section, "the in-progress block must be replaced"
    assert "done.append" in section, "finished steps should remain as history"


def test_an_identical_card_is_not_re_sent():
    """Telegram rejects an edit with unchanged content; that error is pure noise."""
    body = _src("owner_bot.py")
    start = body.index("async def _provision")
    section = body[start:start + 2000]
    assert "== last[0]" in section or "last[0]" in section
