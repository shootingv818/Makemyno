"""
Minimal stand-ins for the unofficial third-party clients.

`rubpy` and `telethon` are runtime dependencies that talk to real servers. The
test suite exercises OUR logic — session naming, tenancy scoping, the busy
registry, job bookkeeping — none of which needs a live client. Installing them
just to import a module would also make the suite unrunnable on a machine
without network access.

These stubs are installed into sys.modules before the project modules are
imported. They provide only the names the project imports at module level; any
test that actually needs client behaviour injects its own fake instead (see
_FakeRB in test_session_paths.py).
"""
from __future__ import annotations

import sys
import types


def _install_rubpy() -> None:
    if "rubpy" in sys.modules:
        return

    rubpy = types.ModuleType("rubpy")

    class Client:                      # noqa: D401 - stub
        """Stub rubpy Client: records how it was constructed, does nothing."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.session = types.SimpleNamespace(insert=lambda *a, **k: None)

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    class Crypto:
        @staticmethod
        def passphrase(auth):
            return f"key({auth})"

        @staticmethod
        def decode_auth(auth):
            return f"decoded({auth})"

        @staticmethod
        def create_keys():
            return "public", "private"

    rubpy.Client = Client
    crypto_mod = types.ModuleType("rubpy.crypto")
    crypto_mod.Crypto = Crypto
    rubpy.crypto = crypto_mod

    sys.modules["rubpy"] = rubpy
    sys.modules["rubpy.crypto"] = crypto_mod


def _install_telethon() -> None:
    if "telethon" in sys.modules:
        return

    telethon = types.ModuleType("telethon")

    class TelegramClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

    class _Button:
        @staticmethod
        def inline(text, data=None):
            return ("inline", text, data)

        @staticmethod
        def url(text, link):
            return ("url", text, link)

    events = types.ModuleType("telethon.events")

    class _Event:
        class Event:
            pass

    events.NewMessage = _Event
    events.CallbackQuery = _Event

    errors = types.ModuleType("telethon.errors")

    class MessageNotModifiedError(Exception):
        pass

    errors.MessageNotModifiedError = MessageNotModifiedError

    sessions = types.ModuleType("telethon.sessions")

    class StringSession:
        def __init__(self, value=""):
            self.value = value

        def save(self):
            return self.value

    sessions.StringSession = StringSession

    tl = types.ModuleType("telethon.tl")
    functions = types.ModuleType("telethon.tl.functions")
    contacts = types.ModuleType("telethon.tl.functions.contacts")

    class GetContactsRequest:
        def __init__(self, hash=0):
            self.hash = hash

    contacts.GetContactsRequest = GetContactsRequest
    functions.contacts = contacts
    tl.functions = functions

    telethon.TelegramClient = TelegramClient
    telethon.Button = _Button
    telethon.events = events
    telethon.errors = errors
    telethon.sessions = sessions
    telethon.tl = tl

    sys.modules["telethon"] = telethon
    sys.modules["telethon.events"] = events
    sys.modules["telethon.errors"] = errors
    sys.modules["telethon.sessions"] = sessions
    sys.modules["telethon.tl"] = tl
    sys.modules["telethon.tl.functions"] = functions
    sys.modules["telethon.tl.functions.contacts"] = contacts


def install() -> None:
    _install_rubpy()
    _install_telethon()
