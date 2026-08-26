"""
The Rubika login return contract.

THE BUG THIS PINS
-----------------
`rb.finish_login` used to `return result` — the raw rubpy response object — while
every caller treated it as a dict:

    rubika_panel:  info.get("name")
    worker_api:    {"ok": True, **(info or {})}

The rubpy object carries a field literally named `get` whose value is None, so
`info.get("name")` evaluated to `None("name")` and every Rubika login failed with

    TypeError: 'NoneType' object is not callable

reported against the `db.add_account(...)` line, because that is where the
sub-expression lives. The traceback pointed at the wrong suspect and cost three
rounds of debugging: a boot check proved `db.add_account` was a healthy function,
which it was — the None was `info.get`.

The lesson encoded here: a function crossing a module boundary returns a shape its
callers agreed to, and the shape gets a test.
"""
import asyncio

import pytest

import rubika_client as rb


class _RubpyResult:
    """A stand-in for the rubpy sign_in response.

    The important detail is `get = None`: an attribute that exists and is not
    callable. A plain object without `get` would raise AttributeError and would
    NOT reproduce the bug.
    """
    get = None                       # ← the actual landmine

    def __init__(self):
        self.status = "OK"
        self.auth = "AUTHDATA"
        self.user = _RubpyUser()


class _RubpyUser:
    get = None

    def __init__(self):
        self.user_guid = "u-123"
        self.first_name = "Ali"
        self.last_name = ""
        self.phone = "989120000001"


class _FakeClient:
    def __init__(self):
        self.session = _FakeSession()
        self.user_agent = "UA"
        self.name = "bot"
        self.auth = None
        self.key = None

    async def sign_in(self, **kwargs):
        return _RubpyResult()

    async def register_device(self, **kwargs):
        return None


class _FakeSession:
    def insert(self, **kwargs):
        return None


@pytest.fixture
def ctx(monkeypatch):
    """A login context, with the crypto steps stubbed to plain values."""
    monkeypatch.setattr(rb.Crypto, "decrypt_RSA_OAEP",
                        staticmethod(lambda pk, enc: "DECRYPTED"), raising=False)
    monkeypatch.setattr(rb.Crypto, "passphrase",
                        staticmethod(lambda d: "PASSKEY"), raising=False)
    monkeypatch.setattr(rb.Crypto, "decode_auth",
                        staticmethod(lambda a: "DECODED"), raising=False)
    monkeypatch.setattr(rb, "_import_key_from_private", lambda pk: None)
    return {"client": _FakeClient(), "phone": "989120000001",
            "private_key": "PRIVKEY", "phone_code_hash": "HASH",
            "public_key": "PUBKEY"}


def test_finish_login_returns_a_real_dict(ctx):
    """Not the rubpy object. Everything else follows from this."""
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert isinstance(info, dict), "callers do info.get(...) and **info"


def test_the_get_method_is_callable(ctx):
    """THE EXACT FAILURE, reproduced: info.get had to stop being None."""
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert callable(info.get)
    # The precise expression from rubika_panel that used to blow up.
    assert info.get("name") == "Ali"


def test_it_carries_every_key_the_callers_read(ctx):
    """rubika_panel reads name/guid/session_values; the token flow reads phone."""
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    for key in ("guid", "name", "phone", "session_values"):
        assert key in info, f"caller reads info[{key!r}]"


def test_the_guid_is_extracted_from_the_nested_user(ctx):
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["guid"] == "u-123"


def test_the_session_values_can_be_packed_into_a_token(ctx):
    """The whole point of session_values: restoring the account later without a
    new SMS code."""
    import db
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    values = info["session_values"]
    # The DECRYPTED auth, not the encrypted blob from the response: that is what
    # a restored session actually needs to sign requests.
    assert values["auth"] == "DECRYPTED"
    assert values["key"] == "PASSKEY"
    assert values["private_key"] == "PRIVKEY"
    assert values["phone"]
    token = db.session_pack(values)
    assert token.startswith("MMSESS:")
    assert db.session_unpack(token)["phone"] == values["phone"]


def test_the_dict_survives_being_splatted_like_the_worker_does(ctx):
    """worker_api returns {"ok": True, **(info or {})} — that needs a mapping."""
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    merged = {"ok": True, **(info or {})}
    assert merged["ok"] is True and merged["guid"] == "u-123"


def test_the_exact_caller_expression_does_not_raise(ctx):
    """Line-for-line what rubika_panel._finish_login does with the result."""
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    w = None
    name = info.get("name") or ""
    user_id = info.get("guid") or ""
    worker_id = (w or {}).get("id")
    assert (name, user_id, worker_id) == ("Ali", "u-123", None)
    if info.get("session_values"):
        assert isinstance(info["session_values"], dict)


def test_a_bad_status_still_raises(ctx, monkeypatch):
    """Normalising the shape must not swallow a real sign-in rejection."""
    class _Bad(_RubpyResult):
        def __init__(self):
            super().__init__()
            self.status = "INVALID_CODE"

    async def _sign_in(**kwargs):
        return _Bad()
    ctx["client"].sign_in = _sign_in
    with pytest.raises(RuntimeError):
        asyncio.run(rb.finish_login(ctx, "00000"))


def test_a_missing_name_becomes_an_empty_string_not_none(ctx):
    """`name=info.get("name") or ""` is the caller's guard, but the column is NOT
    NULL-friendly, so the contract keeps it a string."""
    ctx["client"].sign_in = lambda **k: _no_name()

    async def _no_name_async(**kwargs):
        return _no_name()
    ctx["client"].sign_in = _no_name_async
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert isinstance(info["name"], str)


def _no_name():
    r = _RubpyResult()
    r.user.first_name = ""
    r.user.last_name = ""
    return r



# --------------------------------------------------------------------------- #
# The contact count, read while the client is guaranteed live
# --------------------------------------------------------------------------- #
class _ContactfulClient(_FakeClient):
    """A client that answers get_me and get_contacts like rubpy does."""

    def __init__(self, contacts=3):
        super().__init__()
        self._contacts = contacts

    async def get_me(self):
        return _RubpyUser()

    async def get_contacts(self, start_id=None):
        users = []
        for i in range(self._contacts):
            u = _RubpyUser()
            u.user_guid = f"c-{i}"
            users.append(u)
        return _Contacts(users)


class _Contacts:
    get = None                       # same landmine shape as the real payload

    def __init__(self, users):
        self.users = users


def test_the_contact_count_is_read_after_login(ctx):
    """The login card said "0 contacts" on an account with thousands, because
    nothing ever counted them. This is the one moment a live client is
    guaranteed, so it is counted here."""
    ctx["client"] = _ContactfulClient(contacts=5)
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["contacts"] == 5


def test_the_identity_comes_from_get_me(ctx):
    """get_me is stable across rubpy versions; the sign_in payload is not, which
    is why the base project reads it this way."""
    ctx["client"] = _ContactfulClient()
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["guid"] == "u-123"
    assert info["name"] == "Ali"


def test_a_contact_read_that_fails_still_completes_the_login(ctx):
    """A login must never be lost because the contact count could not be read —
    the session is already valid at that point."""
    class _Broken(_ContactfulClient):
        async def get_contacts(self, start_id=None):
            raise RuntimeError("network hiccup")

    ctx["client"] = _Broken()
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["contacts"] == 0
    assert info["guid"], "the login itself must still have succeeded"


def test_a_failing_get_me_falls_back_to_the_sign_in_payload(ctx):
    class _NoMe(_ContactfulClient):
        async def get_me(self):
            raise RuntimeError("unavailable")

    ctx["client"] = _NoMe()
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert info["guid"] == "u-123", "the sign_in payload is the fallback"


def test_contacts_is_always_an_int(ctx):
    """rubika_panel does int(info.get("contacts") or 0) and then writes it to an
    INTEGER column."""
    ctx["client"] = _ContactfulClient(contacts=0)
    info = asyncio.run(rb.finish_login(ctx, "12345"))
    assert isinstance(info["contacts"], int)
