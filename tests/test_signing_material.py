"""
INVALID_AUTH on addChannel was a SIGNING failure that connect_ready hid.

WHAT THE PLATFORM ACTUALLY DOES (read out of rubpy 7.3.5's own source)
---------------------------------------------------------------------
rubpy/methods/utilities/connect.py — connect() reads THREE things out of the
session file and nothing else:

    self.auth        = information[1]
    self.guid        = information[2]
    self.private_key = information[4]

rubpy/client.py — decode_auth and import_key start as None.
rubpy/methods/utilities/start.py — they are populated ONLY in start(), the
interactive login path this project does not use.
rubpy/network.py — every api_version-6 request is built as

    data["auth"] = client.decode_auth
    data["sign"] = Crypto.sign(client.import_key, data["data_enc"])

So a client made with Client(name=path) + connect() has NO identity and NO
signature until something rebuilds those two. connect_ready is that something,
which makes it the most load-bearing function in the project.

WHY IT TOOK SO LONG TO FIND
---------------------------
1. connect_ready wrapped each of its three steps in `try: ... except: pass`. When
   a step failed it still returned the client, looking healthy, and the failure
   surfaced far away as "rubpy.exceptions.InvalidAuth on addChannel" — which
   reads as a channel bug. Two rounds of work went into channels and connection
   shapes because of it.
2. find_marked_message swallowed the same error with a bare `except: break` and
   returned None, so the reads that should have screamed first stayed quiet.
3. session_path did not normalise the phone, so "09227458187" and "989227458187"
   were two different files — and rubpy's SQLiteSession CREATES the file it is
   pointed at, so a lookup on the wrong form silently produced an EMPTY session
   instead of an error.
4. import_session accepted a blob with no private_key and returned True, so
   session_store.place reported a successful repair and the retry failed
   identically.

Every test below was mutation-verified: the fix was reverted one at a time and
the matching test confirmed failing.
"""
import asyncio
import os
import sqlite3

import pytest

import rubika_client as rb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A real 1024-bit RSA key in PEM form, so the signing path is exercised for real
# rather than against a mock that cannot fail the way the platform fails.
try:
    from Crypto.PublicKey import RSA
    _KEY = RSA.generate(1024)
    PEM = _KEY.export_key().decode()
    BARE = "\n".join(PEM.strip().splitlines()[1:-1])   # body with no BEGIN/END
    _HAVE_RSA = True
except Exception:      # pragma: no cover
    _HAVE_RSA = False
    PEM = BARE = ""

needs_rsa = pytest.mark.skipif(not _HAVE_RSA, reason="pycryptodome not installed")


class _FakeClient:
    """A rubpy client as connect() actually leaves it.

    The important detail, and the reason a looser stub would have proved nothing:
    decode_auth and import_key are None after connect(), and only auth, guid and
    private_key come back from the session file.
    """

    def __init__(self, auth=None, private_key=None):
        self._auth = auth
        self._pk = private_key
        self.auth = None
        self.private_key = None
        self.key = None
        self.decode_auth = None
        self.import_key = None

    async def connect(self):
        self.auth = self._auth
        self.private_key = self._pk
        return self


# --------------------------------------------------------------------------- #
# the private key must be accepted in either armoured or bare form
# --------------------------------------------------------------------------- #
@needs_rsa
def test_import_key_accepts_a_full_pem():
    assert rb._import_key_from_private(PEM) is not None


@needs_rsa
def test_import_key_accepts_a_bare_key_body():
    """rubpy only re-armours a bare key in Client.__init__.

    A key that arrives any other way — read back out of the session file, or
    restored from a portable token — reaches RSA.import_key exactly as stored, and
    without the BEGIN/END lines that raises.
    """
    assert BARE.count("BEGIN") == 0
    assert rb._import_key_from_private(BARE) is not None, \
        "a bare key body must be re-armoured, not dropped"


@needs_rsa
def test_import_key_accepts_bytes():
    assert rb._import_key_from_private(PEM.encode()) is not None


def test_import_key_returns_none_for_nothing():
    assert rb._import_key_from_private(None) is None
    assert rb._import_key_from_private("") is None


# --------------------------------------------------------------------------- #
# connect_ready must rebuild both fields, and refuse to hand back a client
# that cannot sign
# --------------------------------------------------------------------------- #
@needs_rsa
def test_connect_ready_rebuilds_decode_auth_and_import_key():
    client = _FakeClient(auth="abcdefghijklmnopqrstuvwxyz012345",
                         private_key=PEM)
    asyncio.run(rb.connect_ready(client))
    assert client.decode_auth, \
        "network.send puts decode_auth in data['auth']; None means no identity"
    assert client.import_key is not None, \
        "network.send signs every request with import_key; None means no signature"
    assert client.key, "the encryption passphrase must be derived from auth"


def test_connect_ready_refuses_a_session_with_no_auth():
    client = _FakeClient(auth=None, private_key=PEM)
    with pytest.raises(rb.SessionNotSignable) as caught:
        asyncio.run(rb.connect_ready(client))
    assert "auth" in str(caught.value)


@needs_rsa
def test_connect_ready_refuses_a_session_with_no_private_key():
    """This is the exact shape that produced INVALID_AUTH on addChannel."""
    client = _FakeClient(auth="abcdefghijklmnopqrstuvwxyz012345",
                         private_key=None)
    with pytest.raises(rb.SessionNotSignable) as caught:
        asyncio.run(rb.connect_ready(client))
    message = str(caught.value)
    assert "private_key" in message
    assert "INVALID_AUTH" in message, \
        "the message must connect the cause to the symptom the owner sees"


def test_connect_ready_names_a_broken_key_rather_than_swallowing_it():
    client = _FakeClient(auth="abcdefghijklmnopqrstuvwxyz012345",
                         private_key="not-a-key-at-all")
    with pytest.raises(rb.SessionNotSignable) as caught:
        asyncio.run(rb.connect_ready(client))
    assert "import_key" in str(caught.value)


def test_connect_ready_has_no_bare_except_pass():
    """The bare handlers are the defect; they must not come back."""
    src = open(os.path.join(ROOT, "rubika_client.py"), encoding="utf-8").read()
    start = src.index("async def connect_ready")
    body = src[start:src.index("\ndef ", start + 10)]
    code = "\n".join(line.split("#")[0] for line in body.splitlines()
                     if not line.strip().startswith("#"))
    assert "except Exception:\n        pass" not in code, \
        ("connect_ready must not swallow — a swallowed failure here becomes an "
         "unexplained INVALID_AUTH hundreds of lines away")
    assert "SessionNotSignable" in code


# --------------------------------------------------------------------------- #
# one account, one session file, whatever form of the number the caller holds
# --------------------------------------------------------------------------- #
def test_session_path_normalises_the_phone():
    local = rb.session_path("09227458187", 7)
    intl = rb.session_path("989227458187", 7)
    plus = rb.session_path("+98 922 745 8187", 7)
    assert local == intl == plus, (
        "the same account must map to ONE session file. Two paths means a lookup "
        "on the wrong form CREATES an empty session, and rubpy then connects with "
        "no auth at all")
    assert local.endswith("acc_989227458187")


def test_session_path_is_still_scoped_per_customer():
    assert rb.session_path("09227458187", 7) != rb.session_path("09227458187", 8)


# --------------------------------------------------------------------------- #
# a session that cannot sign must never be written and called a repair
# --------------------------------------------------------------------------- #
def test_import_session_refuses_a_blob_with_no_private_key(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "SESSIONS_DIR", str(tmp_path))
    with pytest.raises(rb.SessionNotSignable):
        rb.import_session("09227458187", 7, {"auth": "A" * 32, "guid": "u0"})


def test_import_session_still_refuses_an_empty_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "SESSIONS_DIR", str(tmp_path))
    assert rb.import_session("09227458187", 7, {}) is False


@needs_rsa
def test_import_session_writes_a_signable_session(tmp_path, monkeypatch):
    """End to end: what is written must survive the round trip rubpy makes."""
    monkeypatch.setattr(rb, "SESSIONS_DIR", str(tmp_path))
    ok = rb.import_session("09227458187", 7, {
        "auth": "abcdefghijklmnopqrstuvwxyz012345", "guid": "u0",
        "private_key": PEM, "user_agent": "ua"})
    assert ok is True

    path = rb.session_path("09227458187", 7) + ".rp"
    assert os.path.exists(path), "no session file was written"
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("select phone, auth, guid, agent, private_key "
                           "from session").fetchone()
    finally:
        conn.close()
    # The column order rubpy's connect() indexes into: auth=1, guid=2, key=4.
    assert row[1] == "abcdefghijklmnopqrstuvwxyz012345"
    assert row[2] == "u0"
    assert row[4] == PEM
    assert rb._import_key_from_private(row[4]) is not None, \
        "the key read back out of the session file must still be usable"


# --------------------------------------------------------------------------- #
# reads must not hide an auth failure
# --------------------------------------------------------------------------- #
def test_is_auth_failure_recognises_the_platform_wording():
    assert rb.is_auth_failure(RuntimeError(
        "{'status': 'ERROR_GENERIC', 'status_det': 'INVALID_AUTH'}"))
    assert rb.is_auth_failure(RuntimeError("AUTH_FROM_ANOTHER"))
    assert not rb.is_auth_failure(RuntimeError("TOO_REQUESTS"))


def test_find_marked_message_reraises_an_auth_failure():
    """Returning None here is what disguised a dead session as a missing marker."""
    class _C:
        async def get_messages(self, *a, **k):
            raise RuntimeError("{'status_det': 'INVALID_AUTH'}")

    async def _self_guid(_c):
        return "u_self"

    real = rb.get_self_guid
    rb.get_self_guid = _self_guid
    try:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(rb.find_marked_message(_C(), "MARK"))
        assert "INVALID_AUTH" in str(caught.value)
    finally:
        rb.get_self_guid = real


def test_find_marked_message_still_tolerates_an_ordinary_hiccup():
    """A paging error is not a session problem; it just stops paging."""
    class _C:
        async def get_messages(self, *a, **k):
            raise RuntimeError("TOO_REQUESTS")

    async def _self_guid(_c):
        return "u_self"

    real = rb.get_self_guid
    rb.get_self_guid = _self_guid
    try:
        assert asyncio.run(rb.find_marked_message(_C(), "MARK")) is None
    finally:
        rb.get_self_guid = real


# --------------------------------------------------------------------------- #
# the diagnostic that makes this answerable without SSH
# --------------------------------------------------------------------------- #
def test_session_inspect_endpoint_reports_the_signing_state():
    src = open(os.path.join(ROOT, "worker_api.py"), encoding="utf-8").read()
    start = src.index('"/session/inspect"')
    body = src[start:src.index("@app.", start + 10)]
    for field in ("has_auth", "has_private_key", "signable", "session_path",
                  "phone_normalized", "exists"):
        assert field in body, f"/session/inspect must report {field}"

    # Comments and the docstring stripped first: the docstring says "No connect."
    # and a raw search matched its own promise instead of the code.
    code, in_doc = [], False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"""', "'''")):
            ticks = stripped[:3]
            if not in_doc:
                in_doc = not (stripped.endswith(ticks) and len(stripped) > 3)
                continue
            in_doc = False
            continue
        if in_doc or stripped.startswith("#"):
            continue
        code.append(line.split("#")[0])
    code = "\n".join(code).replace("sqlite3.connect", "")
    assert "connect" not in code, \
        "the inspector must never open a platform connection"


# --------------------------------------------------------------------------- #
# PEM re-armouring, provable without pycryptodome
#
# The RSA-backed tests above skip when pycryptodome is absent, which is exactly
# when a regression here would go unnoticed. These check the string contract
# directly so the fix stays guarded on any machine.
# --------------------------------------------------------------------------- #
def test_as_pem_wraps_a_bare_body():
    out = rb._as_pem("AAAABBBBCCCC")
    assert out.startswith(rb.PEM_HEAD)
    assert out.endswith(rb.PEM_TAIL)
    assert "AAAABBBBCCCC" in out


def test_as_pem_leaves_an_armoured_key_alone():
    armoured = f"{rb.PEM_HEAD}\nAAAA\n{rb.PEM_TAIL}"
    assert rb._as_pem(armoured) == armoured


def test_as_pem_decodes_bytes():
    assert rb._as_pem(b"AAAA").startswith(rb.PEM_HEAD)


def test_as_pem_rejects_nothing():
    assert rb._as_pem(None) is None
    assert rb._as_pem("") is None
    assert rb._as_pem("   ") is None
