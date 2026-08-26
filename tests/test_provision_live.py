"""
The provisioning card froze on "🐳 بررسی و نصب Docker ..." and stayed there.

WHY, AND WHY STREAMING ALONE WAS NOT THE ANSWER
Only the docker BUILD was streamed. Every other long step used _run, which returns
once the command has finished, so between one `say()` and the next there was
nothing at all. The Docker install can sit for many minutes: apt waits up to 180
seconds for the dpkg lock a fresh Ubuntu box holds while unattended-upgrades runs,
then installs, then possibly falls back to `curl | sh` from get.docker.com.

And streaming by itself would NOT have fixed it, because those commands run with
-qq and print almost nothing. So _run_live adds a TICKER that re-renders on a
timer with an elapsed clock. The clock is what answers "is it stuck?" — a number
that keeps counting proves the step is still being waited on even in total
silence.

Two more things this fixes:
* _run_streaming reads stdout only, and apt and curl write to stderr. Without
  redirecting the whole block, the card was silent AND the failure diagnosis was
  empty.
* update_worker had NO progress whatsoever, and it is a full docker build per
  worker. Updating a fleet of four meant a silent hour.

Every test below was mutation-verified with __pycache__ cleared.
"""
import asyncio
import os

import pytest

import worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Proc:
    """An asyncssh-like process that emits given lines, optionally slowly."""

    def __init__(self, lines, delay=0.0, status=0):
        self._lines = list(lines)
        self._delay = delay
        self.exit_status = status

    @property
    def stdout(self):
        async def _gen():
            for line in self._lines:
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield line + "\n"
        return _gen()

    async def wait(self):
        return self.exit_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, lines, delay=0.0, status=0):
        self.lines, self.delay, self.status = lines, delay, status
        self.commands = []

    def create_process(self, command):
        self.commands.append(command)
        return _Proc(self.lines, self.delay, self.status)


# --------------------------------------------------------------------------- #
# _run_live
# --------------------------------------------------------------------------- #
def test_the_headline_is_posted_before_anything_runs():
    """A step that prints NOTHING and finishes fast must still appear.

    With output, the first streamed line renders the headline anyway, so a test
    that gives the command output cannot tell whether the headline was posted up
    front — that is how the missing initial render first passed unnoticed. A silent
    fast command is the only case that proves it.
    """
    said = []

    async def say(text):
        said.append(text)

    class _Silent(_Conn):
        def create_process(self, command):
            return _Proc([], status=0)

    asyncio.run(worker._run_live(_Silent([]), "cmd", say, "🐳 نصب Docker ...",
                                 tick=99))
    assert said, "the card must show the step immediately, not after it finishes"
    assert said[0].splitlines()[0] == "🐳 نصب Docker ..."


def test_an_elapsed_clock_is_always_shown():
    said = []

    async def say(text):
        said.append(text)

    asyncio.run(worker._run_live(_Conn(["x"]), "cmd", say, "H", tick=99))
    assert any("⏱" in s for s in said), \
        ("a clock that keeps counting is the only proof a silent step is still "
         "being waited on")


def test_the_ticker_updates_even_when_the_command_prints_nothing():
    """apt -qq prints almost nothing; streaming alone leaves the card frozen."""
    said = []

    async def say(text):
        said.append(text)

    # No output at all, but the command takes a while.
    class _Silent(_Conn):
        def create_process(self, command):
            return _Proc([], delay=0.0, status=0)

    class _SlowSilent(_Silent):
        def create_process(self, command):
            proc = _Proc([], status=0)

            async def _wait():
                await asyncio.sleep(0.25)
                return 0
            proc.wait = _wait
            return proc

    asyncio.run(worker._run_live(_SlowSilent([]), "cmd", say, "H", tick=0.05))
    assert len(said) >= 3, \
        f"the ticker did not fire on a silent command (updates: {len(said)})"


def test_the_headline_never_changes_between_updates():
    """The owner panel promotes a step to history when the headline CHANGES."""
    said = []

    async def say(text):
        said.append(text)

    class _Slow(_Conn):
        def create_process(self, command):
            return _Proc(["a", "b", "c"], delay=0.03)

    asyncio.run(worker._run_live(_Slow([]), "cmd", say, "🐳 H", tick=0.02))
    heads = {s.splitlines()[0] for s in said}
    assert heads == {"🐳 H"}, \
        f"a changing headline stacks one copy per tick: {heads}"


def test_the_last_output_line_is_shown():
    said = []

    async def say(text):
        said.append(text)

    class _Slow(_Conn):
        def create_process(self, command):
            return _Proc(["Setting up docker.io"], delay=0.01)

    asyncio.run(worker._run_live(_Slow([]), "cmd", say, "H", tick=0.02))
    assert any("docker.io" in s for s in said)


def test_the_ticker_stops_when_the_command_ends():
    said = []

    async def say(text):
        said.append(text)

    async def _go():
        await worker._run_live(_Conn(["x"]), "cmd", say, "H", tick=0.02)
        count = len(said)
        await asyncio.sleep(0.15)
        assert len(said) == count, "the ticker outlived its command"

    asyncio.run(_go())


def test_the_exit_status_and_tail_are_returned():
    async def say(text):
        pass

    code, tail = asyncio.run(worker._run_live(
        _Conn(["one", "two"], status=7), "cmd", say, "H", tick=99))
    assert code == 7
    assert "two" in tail, "a failure still has to be diagnosable"


def test_a_failing_card_does_not_break_the_step():
    async def say(text):
        raise RuntimeError("telegram is down")

    code, _tail = asyncio.run(worker._run_live(_Conn(["x"]), "cmd", say, "H",
                                              tick=0.02))
    assert code == 0


# --------------------------------------------------------------------------- #
# the long steps actually use it
# --------------------------------------------------------------------------- #
def _code(name, filename="worker.py", kind="async def"):
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


def test_docker_install_and_clone_report_live():
    code = _code("provision_worker")
    assert code.count("_run_live(") >= 3, \
        ("the Docker install, the clone and the disk cleanup are all multi-minute "
         "steps that reported nothing")
    assert "نصب Docker" in code


def test_stderr_is_redirected_so_apt_output_is_visible():
    code = _code("provision_worker")
    assert "2>&1" in code, \
        ("_run_streaming reads stdout only; apt and curl write to stderr, so "
         "without this the card is silent AND the failure diagnosis is empty")


def test_the_install_headline_is_not_also_said_separately():
    """Saying it twice freezes the first copy in the panel's history."""
    code = _code("provision_worker")
    assert 'await say("🐳' not in code, \
        "_run_live posts the headline itself"


def test_update_worker_can_report_progress():
    code = _code("update_worker")
    assert "on_progress" in code
    assert "build_progress(line, state)" in code, \
        "the existing build parser gives the percentage; do not invent a second"
    assert "progress_card_rows(state)" in code


def test_update_worker_still_works_without_a_reporter():
    """The health checker and any script caller pass nothing."""
    code = _code("update_worker")
    assert "if on_progress is None:" in code


def test_the_owner_panel_passes_a_reporter_to_the_update():
    code = _code("_run_worker_update", filename="owner_bot.py")
    assert "on_progress=_render" in code, \
        "updating four workers meant a silent hour"
    assert "msg.edit" in code, "one card edited in place, not a message per tick"
    # The GUARD, not merely a mention of the variable: asserting on "last[0]"
    # survived deleting the comparison, because the assignment below still
    # contains it.
    assert "text == last[0]" in code, \
        "Telegram rejects an identical edit, and the log fills with the error"
