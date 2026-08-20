"""
finish_login must disconnect the login client before returning.

THE BUG THIS PINS — and it explained three separate symptoms at once:

  * channel creation raised INVALID_AUTH on both master and worker,
  * sending on a worker reported "no contacts to send" though contacts exist,
  * a fresh account behaved as if it had never logged in.

All three trace to one omission. Rubika allows ONE live connection per session.
finish_login left its login client CONNECTED, so the next operation opened a
SECOND connection on the same session and Rubika answered AUTH_FROM_ANOTHER ->
INVALID_AUTH. And because rubpy commits its session store on disconnect(), the
never-closed client could leave the file uncommitted, so a reopened client read
no auth and came back with nothing.

The reference project disconnects at exactly this point. finish_login is the one
place both the master and the worker login paths converge, so the fix lives there.
"""
import asyncio

import pytest

import rubika_client as rb


class _RubpyResult:
    get = None                       # the field that once broke info.get(...)

    def __init__(self):
        self.status = "OK"
        self.auth = "ENC_AUTH"
        self.user = _User()


class _User:
    get = None

    def __init__(self):
        self.user_guid = "u-123"
        self.first_name = "Ali"
        self.last_name = ""
        self.phone = "989120000001"


class _Client:
    """Records the lifecycle so the test can assert ordering."""

    def __init__(self):
        self.events = []
        self.session = _Session()
        self.user_agent = "UA"
        self.name = "bot"
        self.auth = None
        self.key = None
        self.connected = True

    async def sign_in(self, **kwargs):
        self.events.append("sign_in")
        return _RubpyResult()

    async def register_device(self, **kwargs):
        self.events.append("register_device")

    async def get_me(self):
        self.events.append("get_me")
        assert self.connected, "get_me ran after disconnect"
        return _User()

    async def get_contacts(self, start_id=None):
        self.events.append("get_contacts")
        assert self.connected, "get_contacts ran after disconnect"
        return _Contacts([_User()])

    async def disconnect(self):
        self.events.append("disconnect")
        self.connected = False


class _Session:
    def insert(self, **kwargs):
        return None


class _Contacts:
    get = None

    def __init__(self, users):
        self.users = users


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(rb.Crypto, "decrypt_RSA_OAEP",
                        staticmethod(lambda pk, enc: "DECRYPTED"), raising=False)
    monkeypatch.setattr(rb.Crypto, "passphrase",
                        staticmethod(lambda d: "PASSKEY"), raising=False)
    monkeypatch.setattr(rb.Crypto, "decode_auth",
                        staticmethod(lambda a: "DECODED"), raising=False)
    monkeypatch.setattr(rb, "_import_key_from_private", lambda pk: None)
    return {"client": _Client(), "phone": "989120000001",
            "private_key": "PRIV", "phone_code_hash": "H", "public_key": "PUB"}


def test_finish_login_disconnects_the_client(ctx):
    """THE FIX. Without it, the login connection stays live and the next
    operation collides with it."""
    client = ctx["client"]
    asyncio.run(rb.finish_login(ctx, "12345"))
    assert "disconnect" in client.events, (
        "the login client must be disconnected, or the next call gets INVALID_AUTH")
    assert client.connected is False


def test_disconnect_happens_after_the_reads(ctx):
    """It must close AFTER get_me and get_contacts, or those reads fail — but
    BEFORE returning, so nothing is left holding the session."""
    client = ctx["client"]
    asyncio.run(rb.finish_login(ctx, "12345"))
    events = client.events
    assert events.index("disconnect") > events.index("get_me")
    assert events.index("disconnect") > events.index("get_contacts")
    assert events[-1] == "disconnect", "disconnect must be the final action"


def test_the_return_value_is_intact_after_disconnect(ctx):
    """Closing the client must not cost the caller anything it needs."""
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["guid"] == "u-123"
    assert info["name"] == "Ali"
    assert info["contacts"] == 1
    assert info["session_values"]["auth"] == "DECRYPTED"
    assert info["session_values"]["private_key"] == "PRIV"


def test_a_disconnect_that_fails_does_not_break_login(ctx):
    """The login already succeeded; a noisy disconnect must not undo it."""
    async def _bad_disconnect():
        ctx["client"].events.append("disconnect")
        raise RuntimeError("socket already closed")
    ctx["client"].disconnect = _bad_disconnect

    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["guid"] == "u-123", "login must stand even if the close is noisy"


def test_no_second_connection_is_opened_during_login(ctx):
    """Only the ONE login client is ever connected; finish_login must not open
    another (that would itself be the collision it is meant to prevent)."""
    client = ctx["client"]
    asyncio.run(rb.finish_login(ctx, "12345"))
    assert client.events.count("sign_in") == 1
    # get_me and get_contacts run on the SAME client, not a fresh one.
    assert "connect" not in client.events, "finish_login must not reconnect"
