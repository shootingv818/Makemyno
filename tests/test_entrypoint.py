"""
The entrypoint and the single-instance lock.

Both exist because of one production bug. The systemd units set the role with
`Environment=MODE=...` while also loading a shared `EnvironmentFile=`, and systemd
lets the FILE win — so a stray `MODE=owner` line started the owner bot twice and
the customer bot never. The visible symptoms pointed in three directions:

  * /start on the owner bot answered with two dashboards
  * /start on the customer bot answered with nothing
  * the log group filled up normally, so both bots "looked fine"

The role is now an argument, which no file can override, and a lock makes a
duplicate process fail loudly instead of silently double-answering.
"""
import os
import subprocess
import sys

import pytest

import config
import main
import single_instance

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# The argument wins over the environment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["owner", "customer", "worker"])
def test_the_argument_selects_the_role(role):
    assert main.resolve_role([role]) == role


def test_the_argument_beats_mode_in_the_environment(monkeypatch):
    """THE BUG. A shared config file said owner; the unit asked for customer. The
    explicit request must win, or two services run the same role."""
    monkeypatch.setattr(config, "MODE", "owner")
    assert main.resolve_role(["customer"]) == "customer"


def test_mode_is_still_honoured_when_no_argument_is_given(monkeypatch):
    """Docker Compose and a bare `python main.py` still work."""
    monkeypatch.setattr(config, "MODE", "worker")
    assert main.resolve_role([]) == "worker"


def test_the_default_role_is_owner(monkeypatch):
    monkeypatch.setattr(config, "MODE", "")
    assert main.resolve_role([]) == "owner"


def test_case_and_whitespace_are_forgiven():
    assert main.resolve_role(["  CUSTOMER  "]) == "customer"


def test_flags_are_not_mistaken_for_a_role():
    """`python main.py -u customer` must still find the role."""
    assert main.resolve_role(["-u", "customer"]) == "customer"


def test_an_unknown_role_fails_with_a_useful_message(monkeypatch):
    monkeypatch.setattr(config, "MODE", "")
    with pytest.raises(SystemExit) as exc:
        main.main(["banana"])
    text = str(exc.value)
    assert "banana" in text
    assert "customer" in text, "the message must list the valid roles"


# --------------------------------------------------------------------------- #
# The systemd unit must not reintroduce the bug
# --------------------------------------------------------------------------- #
def _unit(role="customer"):
    with open(os.path.join(ROOT, "deploy", "makemyno.service.template"),
              encoding="utf-8") as fh:
        return fh.read().replace("{{ROLE}}", role).replace("{{APP_DIR}}", "/opt/x")


def test_the_unit_passes_the_role_as_an_argument():
    assert "main.py customer" in _unit("customer")
    assert "main.py owner" in _unit("owner")


def test_the_unit_does_not_set_mode_in_the_environment():
    """This is the exact line that caused the outage. EnvironmentFile= overrides
    Environment=, so setting the role that way is not reliable."""
    body = _unit()
    for line in body.splitlines():
        if line.strip().startswith("#"):
            continue
        assert not line.startswith("Environment=MODE="), (
            "the role must not be set through the environment")


def test_the_env_template_has_no_mode_line():
    """A MODE line in the shared file is what overrode the per-service role."""
    with open(os.path.join(ROOT, "deploy", "env.template"), encoding="utf-8") as fh:
        for line in fh:
            assert not line.startswith("MODE="), (
                "MODE in the shared env file overrides the per-service role")


def _sections(body: str) -> dict:
    """Parse the unit into {section: [directive lines]}.

    A real parse, not a `split("[Service]")`: the file explains in a COMMENT why
    StartLimit* must not sit under [Service], and splitting on the raw string
    matched that sentence and cut the section in half.
    """
    out, current = {}, None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            out[current] = []
        elif current and stripped and not stripped.startswith("#"):
            out[current].append(stripped)
    return out


def test_the_restart_rate_limit_is_in_the_unit_section():
    """systemd moved StartLimit* to [Unit] in v229 and silently ignores them
    under [Service] — the limit would look configured and do nothing."""
    sections = _sections(_unit())
    unit = "\n".join(sections["Unit"])
    service = "\n".join(sections["Service"])
    for key in ("StartLimitBurst", "StartLimitIntervalSec"):
        assert key in unit, f"{key} must be under [Unit]"
        assert key not in service, f"{key} under [Service] is silently ignored"


def test_the_unit_disables_bytecode_caching():
    """On this fleet a git reset did not reliably invalidate .pyc, so stale
    bytecode kept running fixed source. No cache, no ghost."""
    service = "\n".join(_sections(_unit())["Service"])
    assert "PYTHONDONTWRITEBYTECODE=1" in service


def test_update_clears_the_bytecode_cache():
    with open(os.path.join(ROOT, "deploy", "makemyno.sh"), encoding="utf-8") as fh:
        body = fh.read()
    assert "__pycache__" in body, "update must clear stale bytecode after a pull"


def test_the_unit_sets_the_things_that_keep_it_alive():
    service = "\n".join(_sections(_unit())["Service"])
    assert "Restart=always" in service, "a bot that dies at 3am must come back"
    # SIGINT rather than SIGTERM: the shutdown path releases session claims and
    # honours the settle delay, and skipping it is what revokes a session.
    assert "KillSignal=SIGINT" in service


def test_the_install_script_strips_a_legacy_mode_line():
    with open(os.path.join(ROOT, "deploy", "install.sh"), encoding="utf-8") as fh:
        body = fh.read()
    assert "/^MODE=/d" in body, "upgrading an existing install must remove it"


# --------------------------------------------------------------------------- #
# One process per role
# --------------------------------------------------------------------------- #
def test_a_second_process_of_the_same_role_is_refused(tmp_path, monkeypatch):
    """Two processes on one token answer every update twice: two dashboards for
    one /start, and — the one that really matters — two connections on one
    session, which is what revokes it."""
    monkeypatch.setattr(single_instance, "DATA_DIR", str(tmp_path))
    single_instance.claim("owner")
    try:
        code = ("import sys; sys.path.insert(0, %r);"
                "import single_instance as s;"
                "s.DATA_DIR = %r;"
                "s.claim('owner')" % (ROOT, str(tmp_path)))
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode != 0, "the second process was allowed to start"
        assert "owner" in (result.stderr + result.stdout)
    finally:
        single_instance.release()


def test_the_two_roles_may_run_side_by_side(tmp_path, monkeypatch):
    """Per-role, not global: the owner bot and the customer bot are SUPPOSED to
    run together. A global lock would have broken the product to fix the bug."""
    monkeypatch.setattr(single_instance, "DATA_DIR", str(tmp_path))
    single_instance.claim("owner")
    try:
        code = ("import sys; sys.path.insert(0, %r);"
                "import single_instance as s;"
                "s.DATA_DIR = %r;"
                "s.claim('customer');"
                "print('ok')" % (ROOT, str(tmp_path)))
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout
    finally:
        single_instance.release()


def test_the_lock_is_released_when_the_process_ends(tmp_path, monkeypatch):
    """flock is tied to the file descriptor, so a hard kill cannot leave a stale
    lock that needs manual cleanup — which is exactly why this is not a PID file."""
    monkeypatch.setattr(single_instance, "DATA_DIR", str(tmp_path))
    code = ("import sys; sys.path.insert(0, %r);"
            "import single_instance as s;"
            "s.DATA_DIR = %r;"
            "s.claim('owner')" % (ROOT, str(tmp_path)))
    first = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=30)
    assert first.returncode == 0
    # The lock file still exists, but the lock itself is gone with the process.
    single_instance.claim("owner")
    single_instance.release()


def test_claiming_twice_in_one_process_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(single_instance, "DATA_DIR", str(tmp_path))
    single_instance.claim("owner")
    single_instance.claim("owner")          # same process, same fd family
    single_instance.release()


def test_main_takes_the_lock_before_connecting():
    """Order matters: the check is worthless after a token is already polling."""
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        body = fh.read()
    assert body.index("single_instance.claim") < body.index("import owner_bot")
    assert body.index("single_instance.claim") < body.index("import customer_bot")
    assert body.index("single_instance.claim") < body.index("import worker_api")


# --------------------------------------------------------------------------- #
# The owner dashboard does not leak into the log group
# --------------------------------------------------------------------------- #
def test_the_owner_start_handler_is_private_only():
    """The owner bot is a member of the log group. Without the filter, a /start
    typed there would print the whole dashboard into the group."""
    with open(os.path.join(ROOT, "owner_bot.py"), encoding="utf-8") as fh:
        body = fh.read()
    start = body.index('async def start_handler')
    decorator = body[body.rindex("@bot.on", 0, start):start]
    assert "is_private" in decorator


def test_the_customer_start_handler_is_private_only():
    with open(os.path.join(ROOT, "customer_bot.py"), encoding="utf-8") as fh:
        body = fh.read()
    start = body.index('async def start_handler')
    decorator = body[body.rindex("@bot.on", 0, start):start]
    assert "is_private" in decorator
