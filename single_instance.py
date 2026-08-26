"""
single_instance.py — refuse to start a role that is already running.

WHY THIS EXISTS
---------------
Two processes polling one bot token both receive every update, so every button
press is handled twice: two dashboards for one /start, two sends for one tap, two
sessions opened on one account. The last of those is the failure this whole
project is built to prevent.

It happened for real, from a systemd precedence mistake, and the symptom gave no
hint of the cause — the panel simply appeared twice. A lock turns that into one
sentence in the log before anything else can go wrong.

The lock is advisory (flock) and per role, which is exactly the granularity that
matters: the owner bot and the customer bot are supposed to run side by side, but
never two of either.
"""
from __future__ import annotations

import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_handle = None          # kept alive for the process lifetime; closing unlocks
_role = None


class AlreadyRunning(RuntimeError):
    """Another process already holds this role's lock."""


def claim(role: str) -> None:
    """Take the lock for `role`, or raise AlreadyRunning.

    The lock is released automatically when the process exits, including on a
    hard kill: a stale lock file left behind by SIGKILL is harmless, because
    flock is tied to the open file descriptor, not to the file's existence. A
    PID file would have needed manual cleanup after every crash.
    """
    global _handle, _role
    try:
        import fcntl
    except ImportError:                      # non-POSIX; nothing to enforce
        return

    # Idempotent on purpose. flock is held per OPEN FILE DESCRIPTION, not per
    # process, so a second open() of the same path from this same process would
    # collide with our own lock and report "already running" about ourselves —
    # the most misleading error message available.
    if _handle is not None and _role == role:
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{role}.lock")
    handle = open(path, "w")                 # noqa: SIM115 - must outlive scope
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning(
            f"یک پروسه‌ی «{role}» از قبل در حال اجراست.\n"
            f"دو پروسه با یک توکن، هر آپدیت را دو بار می‌گیرند: یک /start دو پنل "
            f"می‌دهد و یک ارسال دو بار انجام می‌شود.\n"
            f"بررسی کن:  systemctl status makemyno-owner makemyno-customer"
        ) from None

    handle.write(str(os.getpid()))
    handle.flush()
    _handle = handle
    _role = role


def release() -> None:
    global _handle, _role
    if _handle is not None:
        try:
            _handle.close()
        except OSError:
            pass
        _handle = None
        _role = None
