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
        """Stub client that records handler registration instead of connecting.

        `on` has to behave like the real decorator (return the function
        unchanged) so importing a bot module registers its handlers without a
        network client — that is what makes the panel logic testable at all.
        """

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.handlers = []

        def on(self, event_spec):
            def decorator(fn):
                self.handlers.append((event_spec, fn))
                return fn
            return decorator

        async def start(self, *args, **kwargs):
            return self

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def send_message(self, *args, **kwargs):
            return None

        async def send_file(self, *args, **kwargs):
            return None

        async def run_until_disconnected(self):
            return None

    class _Button:
        @staticmethod
        def inline(text, data=None):
            return ("inline", text, data)

        @staticmethod
        def url(text, link):
            return ("url", text, link)

    events = types.ModuleType("telethon.events")

    class _EventSpec:
        """Accepts the same kwargs as the real event builders and remembers them,
        so a test can assert which callback data a handler was bound to."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        class Event:                     # for isinstance() checks in handlers
            pass

    class NewMessage(_EventSpec):
        pass

    class CallbackQuery(_EventSpec):
        pass

    events.NewMessage = NewMessage
    events.CallbackQuery = CallbackQuery

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


def _install_httpx() -> None:
    """Stub httpx whose behaviour a test can steer.

    Set `httpx.NEXT_ERROR` to raise on the next request, or `httpx.NEXT_JSON` to
    control the payload. This lets the master->worker call path (tunnel reuse,
    tunnel teardown on failure) be tested without a network.
    """
    if "httpx" in sys.modules:
        return

    httpx = types.ModuleType("httpx")
    httpx.NEXT_ERROR = None
    httpx.NEXT_JSON = {"ok": True}
    httpx.CALLS = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            httpx.CALLS.append((method, url, kwargs))
            if httpx.NEXT_ERROR is not None:
                error, httpx.NEXT_ERROR = httpx.NEXT_ERROR, None
                raise error
            return _Response(httpx.NEXT_JSON)

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

    httpx.AsyncClient = AsyncClient
    httpx.Response = _Response
    sys.modules["httpx"] = httpx


def install() -> None:
    _install_rubpy()
    _install_telethon()
    _install_httpx()
