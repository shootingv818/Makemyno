"""
Rubika integration layer (wraps the `rubpy` library, v7.x).
===========================================================

Scope of THIS project (on purpose):
  * logs into the USER'S OWN account (phone + code + optional 2FA),
  * reads the account's own contacts (paginated),
  * finds a message the user marked in their OWN Saved Messages,
  * FORWARDS that message to a list of the user's own contacts.

There is intentionally NO proxy support, NO multi-account orchestration and
NO batching/anti-rate-limit machinery here. This is a small personal tool.

All rubpy-specific calls live in this file. rubpy is unofficial, so method
names / response shapes can differ between versions; the helpers below are
written defensively for that reason.
"""
import asyncio
import contextvars
import inspect
import os

from rubpy import Client
from rubpy.crypto import Crypto

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "data", "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)


def session_dir(customer_id) -> str:
    """The folder holding ONE customer's session files.

    Sessions are namespaced per customer, not stored in one flat directory keyed
    only by phone number. Two customers can legitimately own the same number
    (SIMs get resold and shared); with a flat layout their session files would
    be the same file, and each login would silently kick the other out —
    endlessly, with neither of them able to tell why.
    """
    try:
        cid = int(customer_id)
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        raise ValueError("session_dir requires a customer id "
                         "(refusing to share one session folder between customers)")
    path = os.path.join(SESSIONS_DIR, f"c{cid}")
    os.makedirs(path, exist_ok=True)
    return path


def session_path(phone: str, customer_id) -> str:
    """Absolute path of one account's session file, scoped to its customer.

    The number is NORMALISED here, not merely stripped of punctuation. It used to
    be `"".join(ch for ch in phone if ch.isdigit())`, so the SAME account landed on
    two different files depending on what the caller happened to hold:

        session_path("09227458187")  -> acc_09227458187
        session_path("989227458187") -> acc_989227458187

    start_login and account_conn normalise first, so they agreed. Callers that
    pass the number straight from the database — which stores it as the customer
    typed it — did not. And rubpy's SQLiteSession CREATES the file it is pointed
    at, so asking for the wrong path silently produced an EMPTY session rather
    than an error: connect() then read no auth, every request went out
    unauthenticated, and the first signed call answered INVALID_AUTH.

    Normalising in here means every caller lands on one file no matter which form
    of the number it is holding.
    """
    safe = normalize_phone(phone)
    return os.path.join(session_dir(customer_id), f"acc_{safe}")


def is_auth_failure(err: Exception) -> bool:
    """Is this the platform saying the session itself is not valid?

    Lives here as well as in account_conn because the read helpers in this module
    must be able to tell an auth failure from an ordinary hiccup without importing
    account_conn (which imports this module).
    """
    text = str(err).upper()
    return ("INVALID_AUTH" in text or "INVALIDAUTH" in text
            or "NOT_REGISTERED" in text or "AUTH_FROM_ANOTHER" in text)


class SessionNotSignable(RuntimeError):
    """The session cannot sign requests, so every signed call would be refused.

    Raised instead of letting the client reach Rubika and come back with a bare
    INVALID_AUTH that names nothing. The message says which piece is missing.
    """


# --------------------------------------------------------------------------- #
# Transient platform failures
# --------------------------------------------------------------------------- #
# Rubika answers a request it cannot serve right now with HTTP 200 and a body of
# {'status': 'ERROR_TRY_AGAIN', 'status_det': 'SERVER_ERROR'}. rubpy's generic
# method path (methods/advanced/build.py) raises ServerError for that immediately
# — while rubpy's OWN upload loop (network.py) reinitialises and RESTARTS on the
# very same status, logging "Server requested reinitialization". Its request()
# retry only covers transport errors, so a 200-with-error-status never gets one.
#
# Nothing in this project or in the reference handled it, so a single hiccup on
# any page of getContacts/getChats aborted the whole prepare step and an account
# with hundreds of recipients was reported failed before one message went out.
_TRANSIENT_MARKERS = (
    "ERROR_TRY_AGAIN", "ERRORTRYAGAIN",
    "SERVER_ERROR", "SERVERERROR",
    "TOO_REQUESTS", "TOOREQUESTS",
    "ERROR_GENERIC", "ERRORGENERIC",
)

# What the retry loop did, for the card the owner reads. "It failed" is not
# actionable; "it failed three times on get_contacts" is.
#
# PER-REQUEST, not module-global. A worker can prepare two different accounts at
# the same time (the busy registry serialises one SESSION, not the process), and a
# shared counter would have reported account A's retries on account B's card —
# a wrong number on a diagnostic card is worse than no number, because it sends
# the next investigation somewhere real work is not happening. A ContextVar is
# copied per asyncio task, so each request gets its own dict; the dict is MUTATED
# in place rather than reassigned, so nested tasks (asyncio.wait_for creates one)
# still record into the request that owns them.
_TRANSIENT_TRACE: contextvars.ContextVar = contextvars.ContextVar(
    "rb_transient_trace", default=None)


def _trace() -> dict:
    trace = _TRANSIENT_TRACE.get()
    if trace is None:
        trace = {"retries": 0, "where": "", "last": ""}
        _TRANSIENT_TRACE.set(trace)
    return trace


def last_transient() -> dict:
    """Retry bookkeeping for THIS request. Read-only snapshot."""
    return dict(_trace())


def reset_transient() -> None:
    """Start a fresh trace for the current request."""
    _TRANSIENT_TRACE.set({"retries": 0, "where": "", "last": ""})


def is_transient_failure(err: Exception) -> bool:
    """Is this the platform saying "not now, ask again"?

    Auth is checked FIRST and always wins. Retrying an INVALID_AUTH would be the
    worst thing we could do: repeated calls on a session the platform has already
    rejected is the exact pattern that gets an account revoked, and the caller
    upstream needs to see the auth failure to run its repair path.
    """
    if is_auth_failure(err):
        return False
    text = str(err).upper()
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return True
    # A timeout on a read is the same class of problem: nothing was written, and
    # asking again is safe. Kept narrow — only real timeout types, not anything
    # whose message happens to mention the word.
    return isinstance(err, (asyncio.TimeoutError, TimeoutError))


def _retry_settings() -> tuple:
    """(tries, base, jitter) from config, tolerating a missing config module.

    Read at CALL time rather than import time so a test — or the owner editing
    .env and restarting — changes behaviour without touching this file.
    """
    try:
        import config
        tries = max(1, int(getattr(config, "RB_RETRY_TRIES", 3) or 1))
        base = float(getattr(config, "RB_RETRY_BASE", 2.0) or 0)
        jitter = float(getattr(config, "RB_RETRY_JITTER", 0.5) or 0)
    except Exception:      # noqa: BLE001 - config is never optional in practice
        tries, base, jitter = 3, 2.0, 0.5
    return tries, base, jitter


async def retry_transient(fn, *args, tries: int = None, where: str = "",
                          **kwargs):
    """Call ``fn`` and retry ONLY a transient platform answer.

    Deliberately not a general-purpose retry:
      * an auth failure is never retried (see is_transient_failure),
      * the LAST failure is re-raised with its original type and message, so the
        real reason still reaches the error card instead of being replaced by a
        generic "gave up",
      * only ever wrapped around READ-ONLY calls. A send must not be retried
        blindly — the recipient would get the message twice.

    With RB_RETRY_TRIES=1 this is exactly the old behaviour: one attempt, raise.
    """
    tries_cfg, base, jitter = _retry_settings()
    tries = tries or tries_cfg
    label = where or getattr(fn, "__name__", "call")
    for attempt in range(tries):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:      # noqa: BLE001
            trace = _trace()
            if not is_transient_failure(exc) or attempt >= tries - 1:
                if is_transient_failure(exc):
                    # Exhausted, not swallowed: record it before it flies past.
                    trace["where"] = label
                    trace["last"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                raise
            trace["retries"] += 1
            trace["where"] = label
            trace["last"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            delay = base * (2 ** attempt)
            if jitter:
                import random
                delay += random.uniform(0, jitter)
            if delay > 0:
                await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# File names
# --------------------------------------------------------------------------- #
# Characters that would break a path or that a filesystem refuses. NOTHING else
# is removed — the previous filter kept only `isalnum() or ._-`, which deleted
# spaces and brackets, so "لیست قیمت (نهایی) 1404.xlsx" reached the recipient as
# "لیستقیمت(نهایی)1404.xlsx" minus its brackets too. A customer who sends a file
# expects the file's own name back.
_UNSAFE_NAME_CHARS = set('/\\:*?"<>|\r\n\t\0')


def _keep_file_name() -> bool:
    """Owner kill-switch: KEEP_FILE_NAME=0 restores the old naming everywhere."""
    try:
        import config
        return bool(getattr(config, "KEEP_FILE_NAME", True))
    except Exception:      # noqa: BLE001
        return True


def safe_file_name(name: str, fallback: str = "file") -> str:
    """The customer's own file name, minus only what a path cannot contain.

    Never returns a path, and keeps spaces, Persian letters, brackets and
    parentheses intact.

    `fallback` is returned when nothing usable is left, and passing fallback=""
    deliberately yields "" — a caller that only wants to OVERRIDE a name when it
    has a real one needs to be able to tell "nothing" from "call it file".
    """
    raw = (name or "").replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1].strip()
    cleaned = "".join(ch for ch in raw
                      if ch not in _UNSAFE_NAME_CHARS and ch.isprintable())
    cleaned = cleaned.strip(". ").strip()
    if not cleaned:
        return fallback
    # Long names are trimmed from the MIDDLE of the stem so the extension — the
    # part that decides whether the recipient can open the file at all — survives.
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 12:
            cleaned = stem[:120 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:120]
    return cleaned


async def page_pause() -> None:
    """Wait between pages of a paginated read.

    The burst is the cause, the retry is only the cure. /prepare fired up to ~400
    paginated requests with no gap on a freshly opened connection, and the more
    contacts an account had the more reliably Rubika answered ERROR_TRY_AGAIN.
    RB_PAGE_DELAY=0 restores that burst exactly.
    """
    try:
        import config
        delay = float(getattr(config, "RB_PAGE_DELAY", 0.0) or 0)
    except Exception:      # noqa: BLE001
        delay = 0.0
    if delay > 0:
        await asyncio.sleep(delay)


def normalize_phone(phone: str) -> str:
    """Rubika expects digits with country code, no '+' and no leading 0.
    '+989121234567' -> '989121234567', '09121234567' -> '989121234567'
    """
    p = "".join(ch for ch in phone if ch.isdigit())
    if p.startswith("0"):
        p = "98" + p[1:]
    return p


def _make_client(name: str) -> Client:
    return Client(name=name)


def open_client(phone: str, customer_id) -> Client:
    """Return a rubpy client bound to the account's SAVED session.

    customer_id is required on purpose: it is what keeps two customers who own
    the same phone number on two separate session files.
    """
    return _make_client(session_path(phone, customer_id))


# --------------------------------------------------------------------------- #
# Connect + rebuild the signing material that rubpy's connect() can omit.
# --------------------------------------------------------------------------- #
async def connect_ready(client: Client):
    await client.connect()
    auth = getattr(client, "auth", None)
    private_key = getattr(client, "private_key", None)

    # WHY THIS FUNCTION IS LOAD-BEARING, AND WHY IT MUST NOT SWALLOW
    #
    # From rubpy's own source: connect() reads only auth, guid and private_key
    # out of the session file. It never sets decode_auth or import_key — those
    # are populated ONLY by start(), the interactive login path we do not use.
    # And network.send builds every api_version-6 request as
    #
    #     data["auth"] = client.decode_auth
    #     data["sign"] = Crypto.sign(client.import_key, data["data_enc"])
    #
    # so a client whose decode_auth or import_key is None talks to Rubika with no
    # identity and no signature. The server answers INVALID_AUTH.
    #
    # Every step below used to sit in its own `try: ... except Exception: pass`.
    # When one failed the client was still returned, looking perfectly healthy,
    # and the failure surfaced hundreds of lines away as
    # "rubpy.exceptions.InvalidAuth on addChannel" — which reads as a channel bug.
    # Hours went into channels, connection shapes and session placement because
    # of it.
    #
    # Now each piece is rebuilt and then CHECKED, and a client that cannot sign
    # says so, in these words, before it ever reaches the platform.
    missing = []

    if auth in (None, ""):
        missing.append("auth (the session file has no auth — it was never "
                       "written, or it was written for a different phone/path)")
    else:
        if getattr(client, "key", None) in (None, ""):
            client.key = Crypto.passphrase(auth)
        client.decode_auth = Crypto.decode_auth(auth)
        if not client.decode_auth:
            missing.append("decode_auth (Crypto.decode_auth returned nothing)")

    if private_key in (None, ""):
        missing.append("private_key (the session file has no RSA key, so nothing "
                       "can be signed — this account must be logged in again)")
    elif getattr(client, "import_key", None) is None:
        try:
            client.import_key = _import_key_from_private(private_key)
        except Exception as exc:      # noqa: BLE001
            missing.append(f"import_key ({type(exc).__name__}: {str(exc)[:120]})")
        else:
            if client.import_key is None:
                missing.append("import_key (the private key is not a usable "
                               "RSA key)")

    if missing:
        raise SessionNotSignable(
            "this session cannot sign requests, so Rubika will refuse every "
            "signed call with INVALID_AUTH — missing: " + "; ".join(missing))
    return client


# --------------------------------------------------------------------------- #
# Programmatic login (mirrors rubpy's own start.py flow)
# --------------------------------------------------------------------------- #
def _get(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v not in (None, ""):
            return v
        if isinstance(obj, dict) and obj.get(n) not in (None, ""):
            return obj.get(n)
    return None


PEM_HEAD = "-----BEGIN RSA PRIVATE KEY-----"
PEM_TAIL = "-----END RSA PRIVATE KEY-----"


def _as_pem(private_key) -> str | None:
    """Return the private key as a PEM document RSA.import_key will accept.

    rubpy only repairs a bare key in Client.__init__ — it wraps a body that lacks
    the BEGIN/END lines. A key that arrives any other way (read back out of the
    session file, or restored from a portable session token) is handed to
    RSA.import_key exactly as stored, and if the armour is missing that raises.
    We used to swallow that, leave import_key as None, and let every signed
    request go out unsigned.
    """
    if private_key is None:
        return None
    if isinstance(private_key, (bytes, bytearray)):
        try:
            private_key = private_key.decode()
        except Exception:      # noqa: BLE001
            return None
    text = str(private_key).strip()
    if not text:
        return None
    if PEM_HEAD in text:
        return text
    return f"{PEM_HEAD}\n{text}\n{PEM_TAIL}"


def _import_key_from_private(private_key):
    """Build the signing key exactly like rubpy start.py does.

    rubpy signs EVERY api_version-6 request with
    ``Crypto.sign(client.import_key, data_enc)`` and populates import_key ONLY in
    start(); connect() leaves it None. So this is the one thing standing between a
    session file and a usable client, and returning None here means every signed
    call is refused with INVALID_AUTH.
    """
    pem = _as_pem(private_key)
    if pem is None:
        return None
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pkcs1_15
    return pkcs1_15.new(RSA.import_key(pem.encode()))


def import_session(phone: str, customer_id, values: dict) -> bool:
    """WRITE a portable session onto this machine's session store. NO connect.

    A Rubika session is five values (auth, private_key, guid, phone,
    user_agent). Given those, the session can be rebuilt on ANY server without a
    login code — which is how an account moves between the master and a worker.

    This is deliberately WRITE-ONLY, exactly as the reference does it:
    session.insert() only touches the session file, so importing can never open a
    second live connection and therefore can never provoke AUTH_FROM_ANOTHER. The
    real connection happens later, when a job runs, under the busy registry.

    Returns True when a session file was written.
    """
    if not values or not values.get("auth"):
        return False
    # A session with no RSA key can READ but can never SIGN, so every channel
    # create, member add and forward would be refused with INVALID_AUTH. Writing
    # one and reporting success is worse than writing nothing: session_store.place
    # then tells run_with_repair the session was repaired, the retry fails
    # identically, and the log blames the platform.
    if not values.get("private_key"):
        raise SessionNotSignable(
            "refusing to write a session with no private_key: it could only read, "
            "and every signed call would come back INVALID_AUTH. This account "
            "needs a fresh login.")
    normalized = normalize_phone(phone or values.get("phone") or "")
    if not normalized:
        return False
    client = _make_client(session_path(normalized, customer_id))
    client.session.insert(
        auth=values.get("auth"),
        guid=values.get("guid"),
        user_agent=values.get("user_agent"),
        phone_number=normalized,
        private_key=values.get("private_key"),
    )
    return True


async def start_login(phone: str, customer_id, pass_key: str = None) -> dict:
    """Phase 1: connect + request the login code (handles 2FA pass_key)."""
    phone = normalize_phone(phone)
    client = _make_client(session_path(phone, customer_id))
    await client.connect()

    public_key, private_key = Crypto.create_keys()

    if pass_key:
        result = await client.send_code(phone_number=phone, pass_key=pass_key)
    else:
        result = await client.send_code(phone_number=phone)

    return {
        "client": client,
        "phone": phone,
        "status": _get(result, "status") or "",
        "phone_code_hash": _get(result, "phone_code_hash"),
        "hint": _get(result, "hint_pass_key"),
        "public_key": public_key,
        "private_key": private_key,
    }


async def finish_login(ctx: dict, code: str):
    """Phase 2: sign in with the code, then replicate rubpy start.py steps."""
    client: Client = ctx["client"]
    phone = ctx["phone"]
    private_key = ctx["private_key"]

    result = await client.sign_in(
        phone_code=code,
        phone_number=phone,
        phone_code_hash=ctx["phone_code_hash"],
        public_key=ctx["public_key"],
    )

    status = _get(result, "status") or ""
    if str(status).upper() not in ("OK", ""):
        raise RuntimeError(f"sign_in status: {status}")

    enc_auth = _get(result, "auth")
    decrypted = Crypto.decrypt_RSA_OAEP(private_key, enc_auth)

    client.private_key = private_key
    client.key = Crypto.passphrase(decrypted)
    client.auth = decrypted
    try:
        client.decode_auth = Crypto.decode_auth(client.auth)
    except Exception:
        pass
    ik = _import_key_from_private(private_key)
    if ik is not None:
        client.import_key = ik

    try:
        user = _get(result, "user")
        guid = _guid_of(user) or _guid_of(result)
        phone_number = _get(user, "phone") or phone
        user_agent = getattr(client, "user_agent", None)
        client.session.insert(
            auth=client.auth,
            guid=guid,
            user_agent=user_agent,
            phone_number=phone_number,
            private_key=private_key,
        )
    except Exception:
        pass

    try:
        await client.register_device(device_model=getattr(client, "name", "RubikaBot"))
    except Exception:
        try:
            await client.register_device()
        except Exception:
            pass

    # Return a NORMALISED DICT, never the raw rubpy object.
    #
    # This used to `return result`, and every caller treated it as a dict:
    # rubika_panel does `info.get("name")` and worker_api does `{**(info or {})}`.
    # The rubpy response object carries a field literally named `get` whose value
    # is None, so `info.get("name")` evaluated to `None("name")` and every single
    # Rubika login died with "'NoneType' object is not callable" — pointing at the
    # db.add_account line, which sent three rounds of debugging after the wrong
    # suspect. The shape the callers want is the shape this returns.
    #
    # The identity comes from get_me(), not from the sign_in response: the base
    # project reads it that way for a reason — sign_in's payload varies between
    # rubpy versions, while get_me is stable and authoritative.
    guid = _guid_of(_get(result, "user")) or _guid_of(result) or ""
    name = _name_of(_get(result, "user"), "") or ""
    phone_out = _get(_get(result, "user"), "phone") or phone
    try:
        me = await client.get_me()
        guid = _guid_of(me) or guid
        name = _name_of(me, name) or name
    except Exception:      # noqa: BLE001 - identity is a nicety, the login stands
        pass

    # Contact count, read here because this is the one moment we are guaranteed a
    # live client. Without it the login card said "0 contacts" on an account with
    # thousands, and the customer's first impression was a broken import.
    contacts = 0
    try:
        contacts = len(await get_contacts_full(client))
    except Exception:      # noqa: BLE001
        pass

    # DISCONNECT THE LOGIN CLIENT before returning. This one line is why every
    # post-login operation was failing.
    #
    #   * Rubika allows ONE live connection per session. If this client stays
    #     connected, the next call (send / channel / contacts) opens a SECOND
    #     connection on the same session and Rubika answers AUTH_FROM_ANOTHER ->
    #     INVALID_AUTH — exactly what channel creation hit on master and worker.
    #   * rubpy commits its session store on disconnect(). Never closing can leave
    #     the file uncommitted, so a reopened client reads no auth and connects
    #     unauthenticated — which is why "get contacts" came back empty on an
    #     account that plainly has contacts.
    #
    # Everything the caller needs (session_values, name, guid, contacts) is
    # already captured above, so closing here loses nothing. The reference project
    # disconnects at this same point for the same reason.
    try:
        await client.disconnect()
    except Exception:      # noqa: BLE001
        pass

    return {
        "guid": guid,
        "name": name,
        "phone": phone_out,
        "contacts": contacts,
        # The five portable values, so the account can be restored later from a
        # session token without another SMS code.
        "session_values": {
            "auth": client.auth,
            "key": getattr(client, "key", None),
            "private_key": private_key,
            "guid": guid,
            "user_agent": getattr(client, "user_agent", None),
            "phone": phone_out,
            "name": name,
        },
        "raw_status": str(status or "OK"),
    }


# --------------------------------------------------------------------------- #
# Tolerant field extractors (shapes vary across rubpy versions)
# --------------------------------------------------------------------------- #
def _data_of(obj):
    for attr in ("original_update", "to_dict"):
        v = getattr(obj, attr, None)
        if isinstance(v, dict):
            return v
    if isinstance(obj, dict):
        return obj
    return {}


def _guid_of(obj):
    if obj is None:
        return None
    d = _data_of(obj)
    for key in ("object_guid", "user_guid", "guid"):
        if d.get(key):
            return d[key]
    for attr in ("object_guid", "user_guid", "guid"):
        v = getattr(obj, attr, None)
        if v:
            return v
    user = getattr(obj, "user", None)
    if user is not None and user is not obj:
        return _guid_of(user)
    if isinstance(d.get("user"), dict):
        u = d["user"]
        for key in ("object_guid", "user_guid", "guid"):
            if u.get(key):
                return u[key]
    return None


def _name_of(obj, default="-"):
    d = _data_of(obj)
    first = d.get("first_name") or ""
    last = d.get("last_name") or ""
    name = (str(first) + " " + str(last)).strip()
    if name:
        return name
    for key in ("name", "title", "first_name"):
        if d.get(key):
            return d[key]
    for attr in ("first_name", "name", "title"):
        v = getattr(obj, attr, None)
        if v:
            return v
    return default


def _type_of(obj):
    d = _data_of(obj)
    t = d.get("type")
    if not t and isinstance(d.get("abs_object"), dict):
        t = d["abs_object"].get("type")
    if not t:
        abs_obj = getattr(obj, "abs_object", None) or obj
        t = getattr(abs_obj, "type", None)
        if t is None and isinstance(abs_obj, dict):
            t = abs_obj.get("type")
    return (t or "").lower()


def _last_online_of(u):
    d = _data_of(u)
    v = d.get("last_online")
    if v is None:
        ot = d.get("online_time")
        if isinstance(ot, dict):
            v = ot.get("exact_time")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _is_online(u):
    d = _data_of(u)
    status = (d.get("status") or "").lower()
    return status == "online"


# --------------------------------------------------------------------------- #
# Contacts (paginated; Rubika returns ~100 per page)
# --------------------------------------------------------------------------- #
def _next_start_id(result):
    return _get(result, "next_start_id") or _get(result, "next_start_index")


def _phone_of(obj):
    """Best-effort extraction of a contact's phone number.

    Rubika contact objects carry the number you added them by, under one of
    several key names depending on the payload shape.
    """
    # _get first: rubpy hands back objects with attributes, and _data_of only
    # understands dicts and to_dict payloads — it returns {} for a plain object, so
    # a dict-only lookup would silently find nothing on the shape that actually
    # arrives.
    direct = _get(obj, "phone", "phone_number", "phone_no")
    if direct:
        return str(direct)
    user = _get(obj, "user")
    if user is not None:
        nested = _get(user, "phone", "phone_number", "phone_no")
        if nested:
            return str(nested)
    return ""


async def get_contact_phones(client: Client, should_stop=None,
                             on_progress=None) -> list:
    """An ordered, de-duplicated list of contact phone numbers, digits only.

    Ported from the reference project because contact export was quietly broken
    without it: the export read get_contacts_full(), whose dicts carry
    {guid, name, last_online, online} and NO phone, so `item.get("phone")` was
    always None and every export returned zero numbers. Nothing errored — the
    customer just got an empty file.

    `should_stop` is checked between pages so a long export can be cancelled;
    `on_progress` reports the running count for a live card.
    """
    out: list = []
    seen = set()
    start_id = None
    for _ in range(200):          # safety cap: 200 * ~100 = 20k contacts
        if should_stop is not None and should_stop():
            break
        result = await retry_transient(client.get_contacts, start_id,
                                       where="get_contacts") if start_id \
            else await retry_transient(client.get_contacts,
                                       where="get_contacts")
        users = getattr(result, "users", None)
        if users is None and isinstance(result, dict):
            users = result.get("users", [])
        for user in users or []:
            digits = "".join(ch for ch in _phone_of(user) if ch.isdigit())
            if digits and digits not in seen:
                seen.add(digits)
                out.append(digits)
        if on_progress is not None:
            try:
                await on_progress(len(out))
            except Exception:      # noqa: BLE001 - progress is best-effort
                pass
        start_id = _next_start_id(result)
        if not start_id or not users:
            break
        await page_pause()
    return out


async def get_contacts_full(client: Client) -> list:
    """Return ALL contacts as dicts {guid, name, last_online, online}, paginated.

    Every page goes through retry_transient: this loop is where the campaign died.
    One ERROR_TRY_AGAIN on page 7 of an account's contacts used to abort the whole
    prepare step, and the customer was told the account had failed.
    """
    out = []
    seen = set()
    start_id = None
    for _ in range(200):  # safety cap (200 * ~100 = 20k)
        result = await retry_transient(client.get_contacts, start_id,
                                       where="get_contacts") if start_id \
            else await retry_transient(client.get_contacts,
                                       where="get_contacts")
        users = getattr(result, "users", None)
        if users is None and isinstance(result, dict):
            users = result.get("users", [])
        for u in users or []:
            guid = _guid_of(u)
            if guid and guid not in seen:
                seen.add(guid)
                out.append({
                    "guid": guid,
                    "name": _name_of(u),
                    "last_online": _last_online_of(u),
                    "online": _is_online(u),
                })
        start_id = _next_start_id(result)
        if not start_id or not users:
            break
        await page_pause()
    return out


async def get_chats_user_guids(client: Client):
    """Return an ORDERED list of guids of USER chats (most recent activity first)
    and the total number of groups the account is in.

    Same retry and same pause as the contact pages: this runs immediately after
    them on the same connection, so it is the second half of the burst.
    """
    user_chats = []
    seen_u = set()
    n_groups = 0
    seen_g = set()
    start_id = None
    for _ in range(200):
        result = await retry_transient(client.get_chats, start_id,
                                      where="get_chats") if start_id \
            else await retry_transient(client.get_chats, where="get_chats")
        chats = getattr(result, "chats", None)
        if chats is None and isinstance(result, dict):
            chats = result.get("chats", [])
        for chat in chats or []:
            ctype = _type_of(chat)
            guid = _guid_of(chat)
            if not guid:
                continue
            if ctype == "user" and guid not in seen_u:
                seen_u.add(guid)
                user_chats.append(guid)
            elif ctype == "group" and guid not in seen_g:
                seen_g.add(guid)
                n_groups += 1
        start_id = _next_start_id(result)
        if not start_id or not chats:
            break
        await page_pause()
    return user_chats, n_groups


async def get_ordered_recipients(client: Client) -> list:
    """Build the recipient list for the account's OWN CONTACTS only.

    RETURNS A PLAIN LIST of {guid, name}. It used to return (ordered, stats), and
    both callers — rubika_panel._collect_targets and worker_api /prepare — passed
    the whole tuple onward as the target list. A send to an account with hundreds
    of contacts therefore reported "Targets: 2" (the two tuple elements) and then
    failed twice, once for the list and once for the stats dict.

    Nothing ever read `stats`, so the tuple bought nothing and cost two identical
    bugs. The contact count now comes from finish_login, which already has a live
    client.

    ORDER, exactly as the reference project orders it:
      1) contacts who are ONLINE RIGHT NOW — within that tier the ones we already
         have a chat with come first (by recent activity), then the rest of the
         online ones by most-recent last-seen
      2) then contacts we have a chat with but who are offline (recent first)
      3) then everyone else by LAST SEEN, most recent visit first

    This repo had drifted into a different order — chat-first, online second —
    which buries the people most likely to read the message right now behind an
    old conversation list. Online first is the whole point: a message that arrives
    while someone is in the app is read, and a read message is the one that does
    not get reported.
    """
    contacts = await get_contacts_full(client)
    user_chats, n_groups = await get_chats_user_guids(client)

    by_guid = {c["guid"]: c for c in contacts if c["guid"]}

    online_set = {g for g in by_guid if by_guid[g]["online"]}

    # recent-activity order of the chats we have (drives tiers 1 and 2)
    chat_order = [g for g in user_chats if g in by_guid]
    chat_rank = {g: i for i, g in enumerate(chat_order)}

    def _last(guid):
        return by_guid[guid]["last_online"] or 0

    # 1) ONLINE now
    tier1 = sorted(online_set,
                   key=lambda g: (chat_rank.get(g, len(chat_rank)), -_last(g)))
    # 2) offline, but we have a chat with them
    tier2 = [g for g in chat_order if g not in online_set]
    # 3) everyone else, most recently seen first
    placed = online_set | set(tier2)
    tier3 = sorted((g for g in by_guid if g not in placed), key=lambda g: -_last(g))

    ordered_guids = tier1 + tier2 + tier3
    return [{"guid": g, "name": by_guid[g]["name"]} for g in ordered_guids]


# --------------------------------------------------------------------------- #
# POOL BRAIN ranking (Q2): rank the account's OWN contacts by
#   1) currently online first,
#   2) then most-recent last-seen.
# Deliberately NO chat-first step (that is the difference from
# get_ordered_recipients, which is left untouched). Used worker-side to order a
# pool account's freshly-leeched slice; guids not present here (no presence yet)
# are pushed to the end by the caller's default rank.
# --------------------------------------------------------------------------- #
async def presence_rank(client: Client) -> dict:
    """Return {guid: rank} where lower rank = higher priority
    (online first, then last-seen desc). Never raises out useful data."""
    contacts = await get_contacts_full(client)
    ordered = sorted(
        (c for c in contacts if c.get("guid")),
        key=lambda c: (1 if c.get("online") else 0, c.get("last_online") or 0),
        reverse=True,
    )
    return {c["guid"]: i for i, c in enumerate(ordered)}


# --------------------------------------------------------------------------- #
# Find a marked message in the account's OWN Saved Messages.
# --------------------------------------------------------------------------- #
def _msg_id_of(msg):
    return _get(msg, "message_id", "id")


# Why the last marker search found nothing, for the card the customer sees.
# "marker not found" with no numbers is unactionable: it cannot tell apart an
# empty Saved chat, a search that stopped after one page, and a marker that
# genuinely is not there.
_LAST_MARKER_SCAN: dict = {"scanned": 0, "marker": "", "error": ""}


def last_marker_scan() -> dict:
    return dict(_LAST_MARKER_SCAN)


def _msg_text_of(msg):
    """The searchable text of a message, INCLUDING a media caption.

    A plain `text`/`caption` lookup misses the common case: the advert is a photo
    or a file, and some builds carry its caption on the attachment rather than on
    the message. The marker then never matched, on a post that visibly had it.
    """
    direct = _get(msg, "text", "caption")
    if direct:
        return direct
    for holder in ("file_inline", "file", "attachment", "media"):
        nested = _get(msg, holder)
        if nested is None:
            continue
        found = _get(nested, "caption", "text", "file_name")
        if found:
            return found
    return ""


async def get_self_guid(client: Client) -> str:
    """The account's own guid.

    Retried, because this is the FIRST call made on a just-opened connection and
    a server-side hiccup here failed prepare before it had read anything at all.
    """
    me = await retry_transient(client.get_me, where="get_me")
    guid = _guid_of(me)
    if not guid:
        raise RuntimeError("could not resolve self guid")
    return guid


async def find_marked_message(client: Client, marker: str):
    """Search Saved Messages for a message containing `marker`.

    RETURNS THE MESSAGE ID, or None when there is no such message.

    It used to return (saved_guid, message_id), and that broke every caller in two
    ways at once. All five did `found = await find_marked_message(...)` and then
    `if not found:` — but a 2-tuple is truthy even as `(guid, None)`, so the
    "marker not found" branch could never fire. They then called
    `_msg_id_of(found)` on the tuple, which yields None, so forward mode sent with
    no message id and failed for every recipient.

    The guid half was redundant anyway: every caller already fetches it with
    get_self_guid in the same breath.
    """
    marker = (marker or "").strip()
    if not marker:
        return None
    saved_guid = await get_self_guid(client)
    max_id = "0"
    seen_ids = set()
    scanned = 0
    last_error = ""
    for _ in range(50):  # up to ~50 pages of recent saved messages
        try:
            # max_id MUST be a str. rubpy declares get_messages(object_guid,
            # max_id: str, limit: str) and the platform rejects an int outright.
            # The first page passed the literal "0" and worked; every later page
            # passed _msg_id_of(...) straight through, which is an int, so page 2
            # errored and the bare `except: break` below ended the search. Only
            # the newest 20 messages were ever examined, and any account whose
            # marked post sat further back reported "marker not found" while the
            # marker was plainly there.
            #
            # Retried too. This except-branch does not fail the search, it ENDS
            # it — so a one-off ERROR_TRY_AGAIN on page 3 silently reported
            # "marker not found" for a marker that was sitting on page 4.
            result = await retry_transient(client.get_messages, saved_guid,
                                           str(max_id), "20",
                                           where="get_messages")
        except Exception as exc:      # noqa: BLE001
            # An AUTH failure must NOT be swallowed. A bare `except: break` here
            # once hid a dead session for hours: reading Saved returned None as
            # though the marker simply did not exist, and the first error anyone
            # saw was INVALID_AUTH from addChannel, so the channel code was blamed
            # for a session problem.
            if is_auth_failure(exc):
                raise
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            break
        messages = getattr(result, "messages", None)
        if messages is None and isinstance(result, dict):
            messages = result.get("messages", [])
        if not messages:
            break
        for msg in messages:
            scanned += 1
            if marker in _msg_text_of(msg):
                return _msg_id_of(msg)
        next_id = _msg_id_of(messages[-1])
        # Stop if the platform hands back a page we have already walked. Without
        # this a server that ignores max_id turns the loop into 50 rescans of the
        # same 20 messages and reports "not found" after a long silence.
        if not next_id or str(next_id) in seen_ids:
            break
        seen_ids.add(str(next_id))
        max_id = next_id
        await page_pause()
    _LAST_MARKER_SCAN.update({"scanned": scanned, "marker": marker,
                              "error": last_error})
    return None


# --------------------------------------------------------------------------- #
# Forwarding (version-tolerant): forward the marked message to one recipient.
# Forwarding reuses media already uploaded from the user's phone, so the bot
# never needs to upload anything itself.
# --------------------------------------------------------------------------- #
async def forward_message(client: Client, from_guid: str, to_guid: str, message_id):
    """Forward one message, adapting to whatever signature this rubpy build uses."""
    fn = getattr(client, "forward_messages", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no forward_messages()")

    mids = [message_id]
    try:
        params = [p for p in inspect.signature(fn).parameters.keys() if p != "self"]
    except (TypeError, ValueError):
        params = []

    if params:
        kwargs = {}
        for p in params:
            lp = p.lower()
            if "from" in lp and "guid" in lp:
                kwargs[p] = from_guid
            elif "to" in lp and "guid" in lp:
                kwargs[p] = to_guid
            elif lp in ("object_guid", "from_object_guid"):
                kwargs[p] = from_guid
            elif "message_ids" in lp or lp in ("messages", "message_ids"):
                kwargs[p] = mids
            elif "message_id" in lp:
                kwargs[p] = message_id
        # Only use kwargs if we matched the from/to/message params sensibly.
        if kwargs.get(_first_match(params, "from"), None) is not None:
            try:
                return await fn(**kwargs)
            except TypeError:
                pass

    # Fallbacks: try the two most common positional orders.
    try:
        return await fn(from_guid, to_guid, mids)
    except TypeError:
        return await fn(from_guid, mids, to_guid)


def _first_match(params, needle):
    for p in params:
        if needle in p.lower():
            return p
    return None


# --------------------------------------------------------------------------- #
# Channels (version-tolerant, like forward_message above).
# rubpy is unofficial, so method names / signatures differ between versions;
# we try the most common shapes and fail with a clear message otherwise.
# --------------------------------------------------------------------------- #
def _channel_guid_of(obj):
    """Pull a channel guid out of whatever shape create_channel returned."""
    d = _data_of(obj)
    for key in ("channel_guid", "object_guid", "guid"):
        if d.get(key):
            return d[key]
    # sometimes nested under "channel"
    ch = d.get("channel")
    if isinstance(ch, dict):
        for key in ("channel_guid", "object_guid", "guid"):
            if ch.get(key):
                return ch[key]
    for attr in ("channel_guid", "object_guid", "guid"):
        v = getattr(obj, attr, None)
        if v:
            return v
    ch = getattr(obj, "channel", None)
    if ch is not None and ch is not obj:
        return _channel_guid_of(ch)
    return None


async def _try_call(fn, attempts):
    """Call `fn` trying several arg shapes; return first non-TypeError result."""
    last_err = None
    for make_args in attempts:
        args, kwargs = make_args()
        try:
            return await fn(*args, **kwargs)
        except TypeError as e:  # signature mismatch -> try the next shape
            last_err = e
            continue
    raise RuntimeError(f"signature mismatch: {last_err}")


async def create_channel(client: Client, title: str, description: str = None) -> str:
    """Create a channel and return its guid. Tolerant of rubpy version diffs.

    IMPORTANT: never pass an empty-string description — Rubika's addChannel
    rejects it with INVALID_INPUT. Omit it (None) when there is no description.
    Verified against rubpy 7.3.5 where the method is `add_channel(title, ...)`.
    """
    fn = getattr(client, "add_channel", None) or getattr(client, "create_channel", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no add_channel()/create_channel()")
    desc = description or None  # turn "" into None
    if desc:
        attempts = [
            lambda: ((), {"title": title, "description": desc}),
            lambda: ((title, desc), {}),
            lambda: ((), {"title": title}),
        ]
    else:
        attempts = [
            lambda: ((), {"title": title}),
            lambda: ((title,), {}),
        ]
    result = await _try_call(fn, attempts)
    guid = _channel_guid_of(result)
    if not guid:
        raise RuntimeError("channel created but its guid was not found in the response")
    return guid


class ChannelNotPermitted(RuntimeError):
    """addChannel was refused while the session itself is provably healthy."""


async def create_channel_checked(client: Client, title: str,
                                 description: str = None) -> str:
    """create_channel, but able to tell WHY an INVALID_AUTH happened.

    addChannel answering INVALID_AUTH does not mean the session is bad. Every
    api_version-6 request in rubpy is signed the same way, so if the session were
    unusable then sending would fail too — and in production it does not: an
    account sent 5 of 1376 messages happily while every channel creation on the
    same session, on the same connection, came back INVALID_AUTH. That points at
    a per-operation refusal (Rubika restricting channel creation for the account),
    not at auth.

    Three rounds of work went into auth, connection shape and session placement
    because the error text said INVALID_AUTH and nothing distinguished the two
    cases. So on failure this makes ONE cheap SIGNED call on the SAME client:

      * get_me succeeds -> the session signs correctly, so the refusal belongs to
        addChannel alone. Raise ChannelNotPermitted, which the panel turns into a
        sentence the customer can act on.
      * get_me fails too -> the session really is the problem, and the original
        error is re-raised untouched.
    """
    try:
        return await create_channel(client, title, description)
    except Exception as exc:      # noqa: BLE001
        if not is_auth_failure(exc):
            raise
        try:
            me = await get_self_guid(client)
        except Exception:      # noqa: BLE001 - the session is the problem
            raise exc from None
        if not me:
            raise
        raise ChannelNotPermitted(
            "Rubika refused addChannel for this account while the session is "
            f"provably valid (a signed call on the same connection returned "
            f"{me}). The account is not permitted to create a channel — usually "
            f"a new or restricted number. Original: {type(exc).__name__}: "
            f"{str(exc)[:120]}") from exc


async def add_channel_members(client: Client, channel_guid: str, member_guids: list):
    """Add a batch of member guids to a channel. Tolerant of rubpy version diffs."""
    if not member_guids:
        return None
    fn = (getattr(client, "add_channel_members", None)
          or getattr(client, "add_channel_member", None))
    if fn is None:
        raise RuntimeError("this rubpy build has no add_channel_members()")
    return await _try_call(fn, [
        lambda: ((channel_guid, member_guids), {}),
        lambda: ((), {"channel_guid": channel_guid, "member_guids": member_guids}),
        lambda: ((), {"object_guid": channel_guid, "member_guids": member_guids}),
        lambda: ((), {"channel_guid": channel_guid, "user_ids": member_guids}),
    ])


async def seed_channel_with_contacts(client: Client, channel_guid: str,
                                     target: int = 300, batch: int = 80,
                                     delay: float = 2.0) -> int:
    """Add the account's OWN contacts to `channel_guid`, in chunks of `batch`,
    until `target` is reached. Returns how many members were added.

    Contacts are read with get_contacts_full() which already paginates Rubika's
    ~100-per-page contact list, so we transparently walk past the 100 limit.
    """
    contacts = await get_contacts_full(client)            # paginated read
    guids = [c["guid"] for c in contacts if c.get("guid")][:max(0, int(target))]
    added = 0
    for i in range(0, len(guids), max(1, int(batch))):
        chunk = guids[i:i + batch]
        try:
            await add_channel_members(client, channel_guid, chunk)
            added += len(chunk)
        except Exception:
            # best-effort: skip a failed batch and keep going to the next one
            pass
        if i + batch < len(guids):
            await asyncio.sleep(delay)
    return added


# --------------------------------------------------------------------------- #
# Plain text send + group listing (for the Automation feature).
# Verified against rubpy 7.3.5: send_message(object_guid, text=...).
# --------------------------------------------------------------------------- #
async def send_text(client: Client, object_guid: str, text: str):
    """Send a plain text message to a chat/group. Tolerant of rubpy diffs."""
    fn = getattr(client, "send_message", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no send_message()")
    return await _try_call(fn, [
        lambda: ((object_guid, text), {}),
        lambda: ((), {"object_guid": object_guid, "text": text}),
    ])


async def get_group_guids(client: Client) -> list:
    """Return ALL groups the account is in as {guid, name}, paginated."""
    out = []
    seen = set()
    start_id = None
    for _ in range(200):  # safety cap
        result = await client.get_chats(start_id) if start_id else await client.get_chats()
        chats = getattr(result, "chats", None)
        if chats is None and isinstance(result, dict):
            chats = result.get("chats", [])
        for ch in chats or []:
            if _type_of(ch) == "group":
                g = _guid_of(ch)
                if g and g not in seen:
                    seen.add(g)
                    out.append({"guid": g, "name": _name_of(ch)})
        start_id = _next_start_id(result)
        if not start_id or not chats:
            break
    return out


async def join_group_by_link(client: Client, link: str):
    """Join a group/channel via its invite link. Tolerant of rubpy diffs:
    tries join_group / join_chat / join_channel_by_link with link or hash."""
    link = (link or "").strip()
    if not link:
        raise RuntimeError("empty link")
    # the join "hash" is the last path segment of the invite link
    hash_part = link.rstrip("/").split("/")[-1]
    candidates = ("join_group", "join_chat", "join_channel_by_link",
                  "join_channel_action")
    last_err = None
    for name in candidates:
        fn = getattr(client, name, None)
        if fn is None:
            continue
        for arg in (link, hash_part):
            for make in (lambda a=arg: ((a,), {}),
                         lambda a=arg: ((), {"link": a}),
                         lambda a=arg: ((), {"hash": a})):
                args, kwargs = make()
                try:
                    return await fn(*args, **kwargs)
                except TypeError as e:
                    last_err = e
                    continue
                except Exception as e:   # wrong arg value for THIS method; try next
                    last_err = e
                    break
    raise RuntimeError(f"could not join via link: {last_err}")



# =========================================================================== #
# ADDITIVE helpers for the automation EXTRAS (secretary / channel report /
# profile sync / reply responder). These DO NOT change any existing function;
# they only add new, version-tolerant wrappers around rubpy 7.3.5 methods that
# were verified on the owner's account:
#   update_profile(first_name, last_name, bio)
#   get_channel_info(channel_guid)            -> channel.count_members
#   get_messages(object_guid, max_id, limit)  -> message.count_seen (views)
#   get_chats_updates(state)                  -> chats + new_state
# =========================================================================== #
def _find_first_key(obj, needles, _depth=0):
    """Recursively return the first scalar value whose key name contains any of
    `needles` (case-insensitive). Used as a defensive fallback when a field is
    nested differently across rubpy versions."""
    if _depth > 6:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (any(n in str(k).lower() for n in needles)
                    and not isinstance(v, (dict, list))):
                return v
        for v in obj.values():
            r = _find_first_key(v, needles, _depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first_key(v, needles, _depth + 1)
            if r is not None:
                return r
    return None


# --------------------------------------------------------------------------- #
# Feature 3: profile (name + bio) — read current + update.
# --------------------------------------------------------------------------- #
async def get_my_profile(client: Client) -> dict:
    """Return the account's current {first_name, last_name, bio}."""
    me = await client.get_me()
    d = _data_of(me)
    u = d.get("user") if isinstance(d.get("user"), dict) else d
    return {
        "first_name": (u.get("first_name") or "") if isinstance(u, dict) else "",
        "last_name": (u.get("last_name") or "") if isinstance(u, dict) else "",
        "bio": (u.get("bio") or "") if isinstance(u, dict) else "",
    }


async def update_profile(client: Client, first_name=None, last_name=None, bio=None):
    """Update name/bio. Verified shape: update_profile(first_name, last_name, bio).
    Only sends the fields that are not None; tolerant of small signature diffs."""
    fn = getattr(client, "update_profile", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no update_profile()")
    kwargs = {}
    if first_name is not None:
        kwargs["first_name"] = first_name
    if last_name is not None:
        kwargs["last_name"] = last_name
    if bio is not None:
        kwargs["bio"] = bio
    try:
        return await fn(**kwargs)
    except TypeError:
        # positional fallback (first_name, last_name, bio)
        return await fn(first_name or "", last_name or "", bio if bio is not None else "")


# --------------------------------------------------------------------------- #
# Feature 2: channel info (member count) + last post views, + resolve a
# link/@username/guid into a channel guid.
# --------------------------------------------------------------------------- #
async def get_channel_info(client: Client, channel_guid: str):
    fn = getattr(client, "get_channel_info", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no get_channel_info()")
    return await _try_call(fn, [
        lambda: ((channel_guid,), {}),
        lambda: ((), {"channel_guid": channel_guid}),
        lambda: ((), {"object_guid": channel_guid}),
    ])


def channel_member_count(info) -> int:
    """Member count from get_channel_info(). Verified field: channel.count_members."""
    d = _data_of(info)
    ch = d.get("channel") if isinstance(d.get("channel"), dict) else d
    for key in ("count_members", "member_count", "members_count", "subscriber_count"):
        if isinstance(ch, dict) and ch.get(key) is not None:
            try:
                return int(ch[key])
            except (TypeError, ValueError):
                return ch[key]
    v = _find_first_key(d, ("member", "subscriber"))
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def channel_title_of(info) -> str:
    d = _data_of(info)
    ch = d.get("channel") if isinstance(d.get("channel"), dict) else d
    if isinstance(ch, dict):
        return ch.get("channel_title") or ch.get("title") or ""
    return ""


async def resolve_channel(client: Client, ref: str):
    """Turn a guid / @username / link into (channel_guid, channel_title).
    Raises if it cannot be resolved."""
    ref = (ref or "").strip()
    if ref.startswith("c0"):
        return ref, ""
    if ref.startswith("@"):
        username = ref[1:]
    elif ref.startswith("http"):
        username = ref.rstrip("/").split("/")[-1].lstrip("@")
    else:
        username = ref.lstrip("@")
    for name in ("get_object_by_username", "get_info_by_username",
                 "get_channel_info_by_username"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            res = await _try_call(fn, [
                lambda u=username: ((u,), {}),
                lambda u=username: ((), {"username": u}),
            ])
        except Exception:
            continue
        d = _data_of(res)
        ch = d.get("channel") if isinstance(d.get("channel"), dict) else {}
        guid = (ch.get("channel_guid") if isinstance(ch, dict) else None) \
            or _channel_guid_of(res) or _guid_of(res)
        title = (ch.get("channel_title") if isinstance(ch, dict) else "") or ""
        if guid:
            return guid, title
    raise RuntimeError("could not resolve channel (use the channel guid 'c0...')")


async def get_last_post_views(client: Client, channel_guid: str):
    """Return (views, message_id) for the channel's newest post. Verified field:
    message.count_seen. Falls back to a recursive search, then (None, mid)."""
    result = await client.get_messages(channel_guid, "0", "1")
    messages = getattr(result, "messages", None)
    if messages is None and isinstance(result, dict):
        messages = result.get("messages", [])
    if not messages:
        return None, None
    m = messages[0]
    md = _data_of(m)
    mid = _msg_id_of(m)
    for key in ("count_seen", "views", "view_count", "count_views", "seen_count"):
        if md.get(key) is not None:
            return md.get(key), mid
    v = _find_first_key(md, ("seen", "view"))
    return v, mid


# --------------------------------------------------------------------------- #
# Feature 1 & 5: chat updates polling (new PVs / new group messages).
# --------------------------------------------------------------------------- #
async def get_chats_updates(client: Client, state):
    """Call get_chats_updates(state). rubpy expects an INTEGER state (unix
    seconds). An empty/None/0 state means 'first run': we pass nothing and let
    rubpy default it (it uses ~now-200s, so we don't pull the whole history).
    Passing '' directly makes rubpy run int('') and crash, so we coerce here."""
    fn = getattr(client, "get_chats_updates", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no get_chats_updates()")
    s = None
    if state not in (None, "", "0", 0):
        try:
            s = int(state)
        except (TypeError, ValueError):
            s = None
    try:
        return await fn() if s is None else await fn(s)
    except TypeError:
        try:
            return await fn(state=s) if s is not None else await fn()
        except TypeError:
            return await fn()


def parse_chats_updates(result):
    """Return (chats: list, new_state: str|None) from a get_chats_updates() result."""
    d = _data_of(result)
    chats = None
    for key in ("chats", "chats_updates", "updated_chats", "chat_updates"):
        v = d.get(key)
        if isinstance(v, list):
            chats = v
            break
    new_state = (d.get("new_state") or d.get("state") or d.get("next_state")
                 or d.get("timestamp"))
    return (chats or []), new_state


def chat_object_guid(chat):
    d = _data_of(chat)
    return d.get("object_guid") or _guid_of(chat)


def chat_type(chat) -> str:
    """Lower-case chat type ('user' / 'group' / 'channel' / ...)."""
    return _type_of(chat)


def chat_last_message(chat) -> dict:
    d = _data_of(chat)
    lm = d.get("last_message")
    return lm if isinstance(lm, dict) else {}


def chat_last_message_id(chat):
    d = _data_of(chat)
    return d.get("last_message_id") or chat_last_message(chat).get("message_id")


def message_author_guid(msg) -> str:
    d = _data_of(msg)
    for key in ("author_object_guid", "author_guid", "author_object_id"):
        if d.get(key):
            return d[key]
    a = d.get("author")
    if isinstance(a, dict):
        return a.get("object_guid") or a.get("guid") or ""
    return ""


def message_reply_to_id(msg):
    d = _data_of(msg)
    return d.get("reply_to_message_id") or d.get("reply_to_object")


# --------------------------------------------------------------------------- #
# Feature 5 helpers: read recent messages, fetch a message by id, send a reply.
# --------------------------------------------------------------------------- #
async def get_recent_messages(client: Client, object_guid: str, limit: int = 20) -> list:
    """Return up to `limit` recent messages of a chat (newest first)."""
    result = await client.get_messages(object_guid, "0", str(limit))
    messages = getattr(result, "messages", None)
    if messages is None and isinstance(result, dict):
        messages = result.get("messages", [])
    return messages or []


async def get_messages_by_id(client: Client, object_guid: str, message_ids: list):
    """Fetch specific messages by id (tolerant). Returns a list of messages or []"""
    for name in ("get_messages_by_id", "get_message_by_id", "get_messages_by_ID"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            res = await _try_call(fn, [
                lambda: ((object_guid, message_ids), {}),
                lambda: ((), {"object_guid": object_guid, "message_ids": message_ids}),
            ])
        except Exception:
            continue
        messages = getattr(res, "messages", None)
        if messages is None and isinstance(res, dict):
            messages = res.get("messages", [])
        return messages or []
    return []


async def send_reply(client: Client, object_guid: str, text: str, reply_to_message_id):
    """Send a text message as a reply to a specific message. Tolerant of diffs."""
    fn = getattr(client, "send_message", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no send_message()")
    return await _try_call(fn, [
        lambda: ((), {"object_guid": object_guid, "text": text,
                      "reply_to_message_id": reply_to_message_id}),
        lambda: ((object_guid, text), {"reply_to_message_id": reply_to_message_id}),
        lambda: ((object_guid, text), {}),
    ])


async def forward_to(client: Client, from_guid: str, to_guid: str, message_id):
    """Alias kept for clarity in the secretary 'marker' mode (forward the marked
    Saved-Messages post to a single new PV). Delegates to forward_message()."""
    return await forward_message(client, from_guid, to_guid, message_id)



async def leave_group(client: Client, group_guid: str):
    """Leave a group. Tolerant of rubpy version differences in method name."""
    for name in ("leave_group", "leave_chat", "left_group"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        return await _try_call(fn, [
            lambda: ((group_guid,), {}),
            lambda: ((), {"group_guid": group_guid}),
            lambda: ((), {"object_guid": group_guid}),
        ])
    raise RuntimeError("this rubpy build has no leave_group()")



# =========================================================================== #
# ADDITIVE helpers for the GENERATOR engine (موتور مولد). Verified method names
# against rubpy 7.3.5 via scripts/test_generator.py:
#   add_channel(title, description, member_guids)
#   add_group(title, member_guids)            (create a group)
#   join_group(link) / join_channel_by_link(link)
#   user_is_admin(object_guid, user_guid)     (check admin status)
#   add_channel_members / add_group_members
#   create_join_link(object_guid, ...)        (to invite other accounts)
# These DO NOT change any existing function.
# =========================================================================== #
async def create_group(client: Client, title: str, member_guids: list = None) -> str:
    """Create a group and return its guid. Tolerant of rubpy version diffs.

    Rubika's addGroup REQUIRES at least one member guid; sending an empty list
    returns INVALID_INPUT. So if no members are given we seed the group with the
    account ITSELF (the verified test created a group exactly this way)."""
    fn = getattr(client, "add_group", None) or getattr(client, "create_group", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no add_group()/create_group()")
    members = list(member_guids or [])
    if not members:
        try:
            members = [await get_self_guid(client)]
        except Exception:
            members = []
    result = await _try_call(fn, [
        lambda: ((), {"title": title, "member_guids": members}),
        lambda: ((title, members), {}),
        lambda: ((), {"title": title}),
        lambda: ((title,), {}),
    ])
    guid = _guid_of(result) or _channel_guid_of(result)
    if not guid:
        raise RuntimeError("group created but its guid was not found in the response")
    return guid


async def create_object(client: Client, kind: str, title: str) -> str:
    """Create a channel OR group depending on `kind` ('channel'/'group')."""
    if str(kind).lower() == "group":
        return await create_group(client, title)
    return await create_channel(client, title)


async def make_join_link(client: Client, object_guid: str) -> str:
    """Create (or fetch) an invite link for a channel/group so OTHER accounts
    can join it. Tolerant of rubpy version diffs."""
    fn = getattr(client, "create_join_link", None)
    if fn is not None:
        try:
            res = await _try_call(fn, [
                lambda: ((), {"object_guid": object_guid}),
                lambda: ((object_guid,), {}),
            ])
            d = _data_of(res)
            for key in ("join_link", "invite_link", "link"):
                if d.get(key):
                    return d[key]
            jl = d.get("join_link") or d.get("link")
            if isinstance(jl, dict):
                for key in ("join_link", "invite_link", "link", "url"):
                    if jl.get(key):
                        return jl[key]
        except Exception:
            pass
    # fall back to get_join_links
    fn2 = getattr(client, "get_join_links", None)
    if fn2 is not None:
        try:
            res = await _try_call(fn2, [
                lambda: ((), {"object_guid": object_guid}),
                lambda: ((object_guid,), {}),
            ])
            d = _data_of(res)
            links = d.get("join_links") or d.get("links") or []
            if isinstance(links, list) and links:
                first = links[0]
                if isinstance(first, dict):
                    for key in ("join_link", "invite_link", "link", "url"):
                        if first.get(key):
                            return first[key]
                elif isinstance(first, str):
                    return first
        except Exception:
            pass
    raise RuntimeError("could not create/get a join link for this object")


async def user_is_admin(client: Client, object_guid: str, user_guid: str) -> bool:
    """Return True if user_guid is an admin of object_guid. Tolerant of diffs."""
    fn = getattr(client, "user_is_admin", None)
    if fn is not None:
        try:
            res = await _try_call(fn, [
                lambda: ((object_guid, user_guid), {}),
                lambda: ((), {"object_guid": object_guid, "user_guid": user_guid}),
            ])
            if isinstance(res, bool):
                return res
            d = _data_of(res)
            for key in ("is_admin", "user_is_admin", "result"):
                if isinstance(d.get(key), bool):
                    return d[key]
            # some builds return the access list when admin, nothing when not
            if d.get("access_list") or d.get("admin_access_list"):
                return True
        except Exception:
            pass
    # fallback: scan the admin members list
    for name in ("get_channel_admin_members", "get_group_admin_members"):
        f = getattr(client, name, None)
        if f is None:
            continue
        try:
            res = await _try_call(f, [
                lambda: ((object_guid,), {}),
                lambda: ((), {"channel_guid": object_guid}),
                lambda: ((), {"group_guid": object_guid}),
            ])
            d = _data_of(res)
            admins = d.get("in_chat_members") or d.get("admins") or d.get("members") or []
            for a in admins:
                ad = _data_of(a) if not isinstance(a, str) else {}
                g = (ad.get("member_guid") or ad.get("object_guid")
                     or ad.get("user_guid")) if ad else a
                if g == user_guid:
                    return True
            return False
        except Exception:
            continue
    return False


async def add_members_to_object(client: Client, kind: str, object_guid: str,
                                 member_guids: list):
    """Add members to a channel OR group depending on `kind`."""
    if not member_guids:
        return None
    if str(kind).lower() == "group":
        fn = getattr(client, "add_group_members", None)
        if fn is not None:
            return await _try_call(fn, [
                lambda: ((object_guid, member_guids), {}),
                lambda: ((), {"group_guid": object_guid, "member_guids": member_guids}),
            ])
    return await add_channel_members(client, object_guid, member_guids)


async def seed_object_with_contacts(client: Client, kind: str, object_guid: str,
                                    target: int = 300, batch: int = 80,
                                    delay: float = 2.0,
                                    exclude: set = None) -> int:
    """Add the account's OWN contacts to a channel/group in chunks, up to
    `target`. Skips guids in `exclude` (anti-duplicate across accounts).
    Returns how many were added."""
    contacts = await get_contacts_full(client)
    exclude = exclude or set()
    guids = [c["guid"] for c in contacts
             if c.get("guid") and c["guid"] not in exclude][:max(0, int(target))]
    added = 0
    for i in range(0, len(guids), max(1, int(batch))):
        chunk = guids[i:i + batch]
        try:
            await add_members_to_object(client, kind, object_guid, chunk)
            added += len(chunk)
        except Exception:
            pass
        if i + batch < len(guids):
            await asyncio.sleep(delay)
    return added



# --------------------------------------------------------------------------- #
# Generator (channel-only) helpers — VERIFIED against rubpy 7.3.5:
#   check_channel_username(username) -> {"exist": bool}
#   update_channel_username(channel_guid, username)
#   get_object_by_username(username) -> {channel:{channel_guid}}
#   join_channel_action(channel_guid, 'Join')
# Used to make a fresh channel public with a RANDOM username so the other
# accounts can join it by that username (private invite links don't work for
# joining reliably, per the project owner's testing).
# --------------------------------------------------------------------------- #
import random as _random
import string as _string


def random_username(prefix: str = "ch", length: int = 18) -> str:
    """A random public username, e.g. 'ch6bsmf11lxmz91yin76' (letters+digits)."""
    body = "".join(_random.choices(_string.ascii_lowercase + _string.digits,
                                   k=max(5, length)))
    return f"{prefix}{body}"[:32]


async def channel_username_free(client: Client, username: str) -> bool:
    """True if the channel username is available. check returns {'exist': False}
    when it's FREE."""
    fn = getattr(client, "check_channel_username", None)
    if fn is None:
        return True
    try:
        res = await _try_call(fn, [
            lambda: ((username,), {}),
            lambda: ((), {"username": username}),
        ])
        d = _data_of(res)
        if "exist" in d:
            return not bool(d.get("exist"))
    except Exception:
        pass
    return True


async def set_channel_username(client: Client, channel_guid: str, username: str):
    """Set a public username on a channel."""
    fn = getattr(client, "update_channel_username", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no update_channel_username()")
    return await _try_call(fn, [
        lambda: ((channel_guid, username), {}),
        lambda: ((), {"channel_guid": channel_guid, "username": username}),
    ])


async def assign_random_channel_username(client: Client, channel_guid: str,
                                         tries: int = 6) -> str:
    """Pick a free random username and set it on the channel. Returns the
    username that was set (or raises if none worked)."""
    last_err = None
    for _ in range(max(1, tries)):
        u = random_username()
        try:
            if await channel_username_free(client, u):
                await set_channel_username(client, channel_guid, u)
                return u
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"could not assign a username: {last_err}")


async def resolve_username_to_guid(client: Client, username: str) -> str:
    """Turn a public username into a channel guid via get_object_by_username."""
    fn = getattr(client, "get_object_by_username", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no get_object_by_username()")
    res = await _try_call(fn, [
        lambda: ((username,), {}),
        lambda: ((), {"username": username}),
    ])
    d = _data_of(res)
    ch = d.get("channel") if isinstance(d.get("channel"), dict) else {}
    guid = (ch.get("channel_guid") if isinstance(ch, dict) else None) \
        or _channel_guid_of(res) or _guid_of(res)
    if not guid:
        raise RuntimeError("could not resolve username to a channel guid")
    return guid


async def join_channel_by_guid(client: Client, channel_guid: str):
    """Join a channel by its guid (the verified working way). Tolerant of diffs."""
    fn = getattr(client, "join_channel_action", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no join_channel_action()")
    return await _try_call(fn, [
        lambda: ((channel_guid, "Join"), {}),
        lambda: ((), {"channel_guid": channel_guid, "action": "Join"}),
    ])


async def join_channel_by_username(client: Client, username: str) -> str:
    """Resolve a username to a guid then join it. Returns the channel guid."""
    guid = await resolve_username_to_guid(client, username)
    await join_channel_by_guid(client, guid)
    return guid



# =========================================================================== #
# ADDITIVE helpers for: (1) "channel broadcast" engine (each account makes its
# OWN channel, forwards the marked post, seeds its own contacts), and (2) the
# PV image -> PDF export. All verified-method-based, no existing func changed.
# =========================================================================== #

# ---- channel broadcast: set title is via add_channel; seed via existing
#      seed_channel_with_contacts; forward via existing forward_message ----

async def get_chat_list_guids(client: Client, only_users: bool = True) -> list:
    """Return guids of chats. If only_users, just private (user) chats — used by
    the PV image export. Paginated."""
    out = []
    seen = set()
    start_id = None
    for _ in range(200):
        result = await client.get_chats(start_id) if start_id else await client.get_chats()
        chats = getattr(result, "chats", None)
        if chats is None and isinstance(result, dict):
            chats = result.get("chats", [])
        for ch in chats or []:
            g = _guid_of(ch)
            if not g or g in seen:
                continue
            if only_users and _type_of(ch) != "user":
                continue
            seen.add(g)
            out.append(g)
        start_id = _next_start_id(result)
        if not start_id or not chats:
            break
    return out


def _msg_is_photo(msg) -> bool:
    """True if a message carries a PHOTO (not video/gif/file)."""
    d = _data_of(msg)
    # file_inline holds media metadata in rubpy
    fi = d.get("file_inline") or {}
    if isinstance(fi, dict):
        t = (fi.get("type") or "").lower()
        if t:
            return t == "image"          # 'Image' for photos; 'Video'/'Gif'/'File' otherwise
        mime = (fi.get("mime") or "").lower()
        if mime:
            return mime in ("jpg", "jpeg", "png", "webp", "bmp")
    return False


def _file_inline_of(msg):
    d = _data_of(msg)
    fi = d.get("file_inline")
    return fi if isinstance(fi, dict) else None


async def iter_chat_photos(client: Client, object_guid: str, max_pages: int = 200):
    """Yield (message_id, file_inline) for every PHOTO message in a chat,
    walking the whole history page by page (oldest pagination via max_id)."""
    max_id = None
    for _ in range(max_pages):
        try:
            if max_id:
                result = await client.get_messages(object_guid, max_id, "50")
            else:
                result = await client.get_messages(object_guid, "0", "50")
        except Exception:
            break
        messages = getattr(result, "messages", None)
        if messages is None and isinstance(result, dict):
            messages = result.get("messages", [])
        if not messages:
            break
        for m in messages:
            if _msg_is_photo(m):
                fi = _file_inline_of(m)
                if fi:
                    yield _msg_id_of(m), fi
        last = messages[-1]
        nxt = _msg_id_of(last)
        if not nxt or nxt == max_id:
            break
        max_id = nxt


async def download_photo(client: Client, file_inline) -> bytes:
    """Download a photo's bytes from its file_inline. Tolerant of rubpy diffs."""
    fn = getattr(client, "download", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no download()")
    # rubpy's download usually accepts the file_inline dict/object directly
    res = await _try_call(fn, [
        lambda: ((file_inline,), {}),
        lambda: ((), {"file_inline": file_inline}),
    ])
    if isinstance(res, (bytes, bytearray)):
        return bytes(res)
    # some builds return an object with .data / bytes
    for attr in ("data", "content", "bytes"):
        v = getattr(res, attr, None)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
    if isinstance(res, dict):
        for k in ("data", "content", "bytes"):
            if isinstance(res.get(k), (bytes, bytearray)):
                return bytes(res[k])
    raise RuntimeError("download() returned no bytes")


# =========================================================================== #
# AUTO-UPLOAD — put a file into the account's OWN Saved Messages and hand the
# resulting (saved_guid, message_id) to the normal forward engine.
#
# Ported verbatim in behaviour from the reference project, which this repo had
# dropped: without it the ONLY way to send media was for the customer to post it
# by hand in Saved and tag it with the marker. There was no upload path at all,
# so /upload/prepare could not exist and every media campaign silently degraded
# to "marker not found".
#
# Two deliberate constraints, both learned the hard way in the reference:
#   * send_document ONLY. rubpy 7.3.5 has no send_file, and the media senders
#     re-type the payload — a zip or apk sent through them arrives as a
#     gif/photo/video and is useless. Never substitute them.
#   * do NOT trust the send result's shape. Confirm the upload by spotting a NEW
#     message at the top of Saved Messages.
# =========================================================================== #
async def _newest_saved_message_id(client: Client, saved_guid: str):
    """message_id of the most recent message in Saved Messages, or None.

    Reuses the same get_messages() shape as find_marked_message so a rubpy build
    that works for one works for the other.
    """
    try:
        result = await client.get_messages(saved_guid, "0", "5")
    except Exception:      # noqa: BLE001 - a read failure just means "unknown"
        return None
    messages = getattr(result, "messages", None)
    if messages is None and isinstance(result, dict):
        messages = result.get("messages", [])
    if not messages:
        return None
    # Newest-first in the builds we target, but take the max id to be safe, and
    # keep only numeric ids so the result is always int | None.
    ids = []
    for msg in messages:
        try:
            ids.append(int(_msg_id_of(msg)))
        except (TypeError, ValueError):
            continue
    return max(ids) if ids else None


async def upload_file_to_self(client: Client, file_path: str, caption: str = "",
                              file_name: str = None):
    """Upload a local file to Saved Messages; return (saved_guid, message_id).

    Bounded to UPLOAD_TIMEOUT for the WHOLE operation so a stuck upload reports
    itself instead of hanging the job; the caller then falls back to the marker
    flow.
    """
    if not file_path or not os.path.exists(file_path):
        raise RuntimeError("file not found for upload")
    saved_guid = await get_self_guid(client)
    text = caption or None
    name = file_name or os.path.basename(file_path)

    before = await _newest_saved_message_id(client, saved_guid)

    fn = getattr(client, "send_document", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no send_document()")

    # Map arguments by NAME so the exact send_document signature never matters.
    try:
        params = [p for p in inspect.signature(fn).parameters.keys()
                  if p != "self"]
    except (TypeError, ValueError):
        params = []
    kwargs = {}
    for param in params:
        low = param.lower()
        if low in ("object_guid", "guid", "chat_id", "chat_guid"):
            kwargs[param] = saved_guid
        elif low in ("document", "file", "path", "file_path", "media", "doc"):
            kwargs[param] = file_path
        elif "caption" in low or low == "text":
            kwargs[param] = text
        elif "file_name" in low or low in ("name", "filename"):
            kwargs[param] = name
    have_guid = any(k.lower() in ("object_guid", "guid", "chat_id", "chat_guid")
                    for k in kwargs)
    have_file = any(k.lower() in ("document", "file", "path", "file_path",
                                  "media", "doc") for k in kwargs)

    # THE NAME THE RECIPIENT SEES. The signature-inspection above can never place
    # file_name, because rubpy's send_document is declared
    #   (object_guid, document, caption, reply_to_message_id, auto_delete,
    #    *args, **kwargs)
    # with no file_name parameter at all — so `name` was computed, never passed,
    # and silently discarded. It only ever looked right by accident, because the
    # worker happens to write the upload to a path whose basename is the real name.
    #
    # rubpy does honour it as a keyword: send_document forwards **kwargs into
    # send_message, which fills kwargs['file_name'] (defaulting to the path's
    # basename) and network.py puts that value into the file_inline the recipient
    # sees. So pass it explicitly instead of hoping the path is right.
    if name and _keep_file_name() and not any(
            ("file_name" in key.lower() or key.lower() in ("name", "filename"))
            for key in kwargs):
        kwargs["file_name"] = name

    upload_timeout = 60

    async def _attempt(with_name: bool):
        if have_guid and have_file:
            call_kwargs = dict(kwargs)
            if not with_name:
                call_kwargs.pop("file_name", None)
            return await fn(**call_kwargs)
        if with_name and name and _keep_file_name():
            return await fn(saved_guid, file_path, caption=text, file_name=name)
        return await fn(saved_guid, file_path, caption=text)

    try:
        try:
            res = await asyncio.wait_for(_attempt(True), timeout=upload_timeout)
        except TypeError:
            # A rubpy build that refuses the extra keyword. Losing the original
            # name is bad; failing the whole upload over it is worse, so fall back
            # to exactly the call this function made before.
            res = await asyncio.wait_for(_attempt(False), timeout=upload_timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"upload timed out after {upload_timeout}s")

    # Prefer the id the send returned; otherwise confirm via the new top message
    # in Saved. No media-sender fallback, ever.
    mid = _msg_id_of(res)
    if not mid:
        after = await _newest_saved_message_id(client, saved_guid)
        if after is not None and after != before:
            mid = after
    if not mid:
        raise RuntimeError("send_document sent but no message id was found")
    return saved_guid, mid


# =========================================================================== #
# update_end ADDITION — add a phone number to the account's contacts (address
# book). Version-tolerant across rubpy builds. Returns the new contact's guid
# when the response exposes it, else None. Never changes any existing function.
# =========================================================================== #
async def add_contact(client: Client, phone: str, first_name: str = "",
                      last_name: str = ""):
    """Add one phone number to the account's Rubika contacts AND report whether
    that number is actually a Rubika user.

    Returns a dict: {"on_rubika": bool, "guid": str|None}.
      • on_rubika=True  -> the number belongs to a Rubika account (real contact,
                           guid returned -> can be messaged).
      • on_rubika=False -> added to the address book but the number has no Rubika
                           account, so it does NOT show as a Rubika contact.

    rubpy exposes this as add_address_book on most builds; we map arguments by
    inspecting the real signature (name-based) so argument ORDER never matters.
    """
    phone = normalize_phone(phone)
    first_name = (first_name or "").strip() or phone
    last_name = (last_name or "").strip()
    fn = (getattr(client, "add_address_book", None)
          or getattr(client, "add_contact", None)
          or getattr(client, "addAddressBook", None))
    if fn is None:
        raise RuntimeError("this rubpy build has no add_address_book()/add_contact()")

    res = None
    try:
        params = [p for p in inspect.signature(fn).parameters.keys() if p != "self"]
    except (TypeError, ValueError):
        params = []
    if params and any("phone" in p.lower() for p in params):
        kwargs = {}
        for p in params:
            lp = p.lower()
            if "phone" in lp:
                kwargs[p] = phone
            elif "first" in lp:
                kwargs[p] = first_name
            elif "last" in lp:
                kwargs[p] = last_name
        try:
            res = await fn(**kwargs)
        except TypeError:
            res = None
    if res is None:
        res = await _try_call(fn, [
            lambda: ((), {"phone": phone, "first_name": first_name, "last_name": last_name}),
            lambda: ((), {"phone_number": phone, "first_name": first_name, "last_name": last_name}),
            lambda: ((phone, first_name, last_name), {}),
            lambda: ((first_name, last_name, phone), {}),
            lambda: ((phone, first_name), {}),
            lambda: ((phone,), {}),
        ])

    # the response carries the user object (with a guid) ONLY when the number
    # is a real Rubika account.
    guid = _guid_of(res)
    if not guid:
        d = _data_of(res)
        u = d.get("user") if isinstance(d.get("user"), dict) else None
        if u:
            guid = _guid_of(u)
    return {"on_rubika": bool(guid), "guid": guid}



# =========================================================================== #
# YoudonoaAx UPDATE — Item 3 helper: extract Rubika GROUP invite links from a
# block of text (used by the linkdooni engine to harvest group links posted in
# "linkdooni" channels). Only GROUP join links (joing) are returned — channel
# links (joinc) are intentionally ignored. Additive: changes nothing above.
# =========================================================================== #
import re as _re_links

_GROUP_LINK_RE = _re_links.compile(
    r"https?://(?:rubika\.ir|rubika\.me|rubika\.app)/joing/[A-Za-z0-9_\-]+",
    _re_links.IGNORECASE)


def extract_group_links(text: str) -> list:
    """Return a de-duplicated list of Rubika GROUP invite links found in `text`."""
    if not text:
        return []
    out = []
    seen = set()
    for m in _GROUP_LINK_RE.findall(text):
        link = m.rstrip("/")
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


async def get_group_guid_by_link(client: Client, link: str):
    """Resolve a GROUP invite link into its group guid WITHOUT joining, when the
    rubpy build supports it. Returns the guid or None."""
    link = (link or "").strip()
    if not link:
        return None
    hash_part = link.rstrip("/").split("/")[-1]
    for name in ("group_preview_by_join_link", "get_group_info_by_link",
                 "group_preview", "get_join_link_info"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            res = await _try_call(fn, [
                lambda a=link: ((a,), {}),
                lambda a=hash_part: ((a,), {}),
                lambda a=link: ((), {"link": a}),
                lambda a=hash_part: ((), {"hash": a}),
            ])
        except Exception:
            continue
        d = _data_of(res)
        g = (d.get("group_guid") if isinstance(d, dict) else None) or _guid_of(res)
        if g:
            return g
    return None


def join_result_group_guid(res):
    """Pull the group guid out of a join_group_by_link() result, if present."""
    d = _data_of(res)
    if isinstance(d, dict):
        for key in ("group_guid", "object_guid"):
            if d.get(key):
                return d[key]
        grp = d.get("group")
        if isinstance(grp, dict):
            return grp.get("group_guid") or grp.get("object_guid")
    return _guid_of(res)
