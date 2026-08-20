"""
worker_api.py — the headless worker node (MODE=worker).
======================================================

A worker is a clean IP with a copy of this code. It executes work for a session
it is handed and holds nothing else: no bot token, no owner id, no customer
roster, no database. The only state is the session files under data/sessions/
and whatever job is in flight.

SECURITY SHAPE
--------------
  * The API binds to the host's loopback only. The master reaches it through an
    SSH local port-forward, so the port is never exposed to the internet.
  * Every endpoint except /ping requires the shared bearer token that the master
    generated for this worker during provisioning.
  * Every session-touching request carries a customer_id, and sessions are
    stored per customer (data/sessions/c<id>/). Two customers who own the same
    phone number therefore never share a session file.

THE WORKER HAS ITS OWN BUSY REGISTRY
------------------------------------
The master keeps one too, but it cannot see what a worker is doing right now.
Without a registry on this side, the master could ask a worker to verify an
account while that same worker is mid-send on it — a second connection on one
session, which the platform answers by revoking it. So /account/verify answers
"busy, therefore alive" instead of connecting, and every job claims its session
first.
"""
from __future__ import annotations

import asyncio
import base64
import os
import random
import time
import uuid

import busy
import config
import rubika_client as rb

# job_id -> live job state (in memory; a worker restart abandons in-flight work
# and the master's own persistence is what makes a job resumable)
_jobs: dict = {}
_login_ctx: dict = {}


# --------------------------------------------------------------------------- #
# Request bodies — defined at MODULE level, not inside build_app().
#
# They used to live inside build_app(), and pydantic v2 crashed the worker on
# startup with "PydanticUndefinedAnnotation: name 'StartLogin' is not defined".
# A subclass annotation is a forward reference that pydantic resolves against the
# MODULE namespace, and a class defined inside a function is not in that namespace
# — so every model inheriting from Account (all of them) failed to build. The
# container came up, uvicorn never bound the port, and the master saw only
# "Server disconnected without a response".
#
# The stubbed tests never caught it because they do not build the real app. At
# module level the annotations resolve normally. Guarded, so merely importing this
# module on the master (which has no pydantic need) does not require it.
# --------------------------------------------------------------------------- #
try:
    from pydantic import BaseModel as _BaseModel

    class Account(_BaseModel):
        customer_id: int
        phone: str

    class StartLogin(Account):
        pass_key: str | None = None

    class LoginCode(Account):
        code: str

    class LoginPassword(Account):
        password: str

    class Prepare(Account):
        marker: str = ""
        mode: str = "marker"           # "marker" | "text"

    class SendStart(Account):
        targets: list
        mode: str = "marker"           # "marker" | "text"
        text: str = ""
        from_guid: str = ""
        message_id: str | int | None = None
        delay: float = 1.0
        max_errors: int = 5
        # Auto-resume knobs. Rubika answers a burst of forwards with errors long
        # before it revokes anything, so a burst must PAUSE the job and resume it,
        # not end it. Defaults keep old callers working.
        send_timeout: int = 60
        resume_wait: int = 300
        max_retries: int = 2

    class SessionImport(Account):
        auth: str | None = None
        private_key: str | None = None
        guid: str | None = None
        user_agent: str | None = None

    class ChannelCreate(Account):
        title: str
        description: str | None = None
        # The marked post is forwarded into the new channel as its first
        # message. Optional so a caller that predates this field still works
        # (it just gets an empty channel, which is what ALL callers used to get).
        marker: str = ""

    class UploadPrepare(Account):
        file_b64: str
        file_name: str = ""
        caption: str = ""

    class ChannelAdd(Account):
        channel_guid: str
        target: int = 300
        batch: int = 80
        delay: float = 2.0

    class ContactsAdd(Account):
        pairs: list                    # [[phone, name], ...]
        delay: float = 1.0

    class Probe(Account):
        numbers: list
        delay: float = 0.7

    class GroupLink(Account):
        link: str

    class GroupGuid(Account):
        guid: str

    class GroupsSend(Account):
        texts: list
        guids: list = []
        delay_min: float = 0.5
        delay_max: float = 2.0

    class SecretaryPass(Account):
        mode: str = "text"
        text: str = ""
        marker: str = ""
        skip: list = []
        delay: float = 2.0

    class PvExport(Account):
        max_chats: int = 1000
        max_photos: int = 2000
        mode: str = "auto"
        parallel: int = 4

    _HAVE_MODELS = True
except ImportError:      # pragma: no cover - the master does not need pydantic
    _HAVE_MODELS = False


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="rb")


async def _confirm_session_dead(customer_id, phone: str) -> bool:
    """True only when a suspected dead session is CONFIRMED dead.

    An auth-looking error is not proof: a muted or banned recipient, a throttle
    or a network hiccup produce the same text. The check runs on a connection of
    its own so the caller's socket is left alone, and a confirmed-dead session is
    reported to the master through the usual notifier.
    """
    import account_conn
    try:
        dead = await asyncio.wait_for(
            account_conn.verify_session_dead(customer_id, phone), timeout=45)
    except Exception:      # noqa: BLE001 - an inconclusive probe means "alive"
        return False
    if dead:
        await account_conn.notify_invalid(customer_id, phone)
        return True
    return False


async def _sleep_with_stop(job: dict, seconds: float, step: float = 2.0) -> None:
    """Sleep up to `seconds`, but give up early once the job is stopped.

    A plain asyncio.sleep(resume_wait) would make a stop request sit unanswered
    for five minutes, so the customer presses stop and nothing happens.
    """
    waited = 0.0
    while waited < seconds:
        if job.get("stop"):
            return
        chunk = min(step, seconds - waited)
        await asyncio.sleep(chunk)
        waited += chunk


def _worker_code_version() -> str:
    """Short git revision of this worker's code, reported via /ping.

    Inside the Docker image `git` is not installed but the .git directory is
    copied in, so fall back to reading the ref by hand.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=base,
                             capture_output=True, text=True, timeout=10)
        rev = (out.stdout or "").strip()
        if rev:
            return rev
    except Exception:
        pass
    try:
        git_dir = os.path.join(base, ".git")
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = os.path.join(git_dir, ref)
            if os.path.exists(ref_path):
                with open(ref_path, encoding="utf-8") as fh:
                    return fh.read().strip()[:7]
            packed = os.path.join(git_dir, "packed-refs")
            if os.path.exists(packed):
                with open(packed, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip().endswith(ref):
                            return line.split()[0][:7]
        else:
            return head[:7]
    except Exception:
        pass
    return "?"


def build_app():
    """Construct the FastAPI app. Imported lazily so that merely importing this
    module (for example on the master) does not require fastapi."""
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel

    app = FastAPI(title="Makemyno worker", docs_url=None, redoc_url=None)

    def _auth(authorization: str) -> None:
        expected = f"Bearer {config.WORKER_API_TOKEN}"
        if not config.WORKER_API_TOKEN or authorization != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    # ---- helpers ---------------------------------------------------------- #
    async def _hold_or_409(customer_id, phone: str, what: str):
        """Claim the session or refuse with 409 so the master can report why."""
        key = _key(customer_id, phone)
        if not busy.acquire(key, what, customer_id=customer_id):
            holder = busy.who(key) or {}
            raise HTTPException(status_code=409, detail={
                "busy": True,
                "what": holder.get("what"),
                "held_for": int(time.time() - float(holder.get("since") or 0)),
            })
        return key

    def _release(key: str, what: str) -> None:
        busy.release(key, what)

    async def _settle() -> None:
        """Pause before anyone reconnects to the session just released."""
        if config.SESSION_SETTLE_SEC > 0:
            await asyncio.sleep(float(config.SESSION_SETTLE_SEC))

    # ---- liveness --------------------------------------------------------- #
    @app.get("/ping")
    async def ping():
        """Unauthenticated and instant: the master's health path uses this.

        It performs no platform calls, so a transient Rubika outage cannot make
        every healthy worker look blocked.
        """
        return {"ok": True, "version": _worker_code_version(),
                "jobs": len(_jobs), "busy": len(busy.snapshot())}

    @app.get("/health")
    async def health(authorization: str = Header(None)):
        """Probe the platform's upload route. Slower, so it is not on the health
        path — the owner panel calls it deliberately."""
        _auth(authorization)
        import httpx
        code = 0
        route_ok = False
        try:
            async with httpx.AsyncClient(timeout=config.HEALTH_TIMEOUT) as client:
                resp = await client.get(config.HEALTH_URL)
                code = resp.status_code
                route_ok = code in (200, 404)
        except Exception:
            route_ok = False
        return {"route_ok": route_ok, "status_code": code,
                "version": _worker_code_version()}

    @app.get("/jobs")
    async def jobs(authorization: str = Header(None)):
        _auth(authorization)
        return {"jobs": [{"id": jid, **{k: v for k, v in job.items()
                                        if k not in ("task", "targets")}}
                         for jid, job in _jobs.items()],
                "busy": busy.snapshot()}

    # ---- login relay ------------------------------------------------------ #
    @app.post("/login/start")
    async def login_start(body: StartLogin, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "login")
        try:
            ctx = await rb.start_login(body.phone, body.customer_id,
                                       pass_key=body.pass_key)
            _login_ctx[key] = ctx
            return {"ok": True, "status": str(ctx.get("status") or ""),
                    "hint": ctx.get("hint") or ""}
        except Exception as exc:
            _release(key, "login")
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")

    @app.post("/login/password")
    async def login_password(body: LoginPassword,
                             authorization: str = Header(None)):
        _auth(authorization)
        key = _key(body.customer_id, body.phone)
        ctx = _login_ctx.get(key)
        if not ctx:
            raise HTTPException(status_code=404, detail="no login in progress")
        try:
            ctx2 = await rb.start_login(body.phone, body.customer_id,
                                        pass_key=body.password)
            _login_ctx[key] = ctx2
            return {"ok": True, "status": str(ctx2.get("status") or "")}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")

    @app.post("/login/code")
    async def login_code(body: LoginCode, authorization: str = Header(None)):
        _auth(authorization)
        key = _key(body.customer_id, body.phone)
        ctx = _login_ctx.get(key)
        if not ctx:
            raise HTTPException(status_code=404, detail="no login in progress")
        try:
            info = await rb.finish_login(ctx, body.code)
            _login_ctx.pop(key, None)
            return {"ok": True, **(info or {})}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            if key not in _login_ctx:
                _release(key, "login")
                await _settle()

    # ---- session health --------------------------------------------------- #
    @app.post("/account/verify")
    async def account_verify(body: Account, authorization: str = Header(None)):
        """Is this session still valid?

        A busy account is reported ALIVE WITHOUT CONNECTING. Opening a second
        connection to check is exactly what revokes a session, and an account
        that is mid-job has already proved it works.
        """
        _auth(authorization)
        key = _key(body.customer_id, body.phone)
        holder = busy.who(key)
        if holder:
            return {"dead": False, "skipped": True, "reason": holder.get("what")}
        got = busy.acquire(key, "verify", customer_id=body.customer_id)
        if not got:
            return {"dead": False, "skipped": True, "reason": "busy"}
        try:
            import account_conn
            dead = await account_conn.verify_session_dead(body.customer_id,
                                                          body.phone)
            return {"dead": bool(dead), "skipped": False}
        except Exception as exc:
            # An error here is NOT proof of death: report unknown and let the
            # master leave the account alone.
            return {"dead": False, "skipped": True,
                    "reason": f"check failed: {type(exc).__name__}"}
        finally:
            _release(key, "verify")

    # ---- portable session import ------------------------------------------ #
    @app.post("/session/import")
    async def session_import(body: SessionImport,
                             authorization: str = Header(None)):
        """WRITE a session onto this worker's store. Never connects.

        This is what lets an account run on a worker without a fresh SMS code,
        and — more importantly in practice — what REPAIRS a worker that is
        missing the session file for an account the master thinks lives here.
        Without it, the worker connected unauthenticated and answered every
        signed call with INVALID_AUTH.

        session.insert() only writes the file, so there is no second live
        connection and no AUTH_FROM_ANOTHER risk. Any warm connection is closed
        first so nothing is fighting over the file.
        """
        _auth(authorization)
        if not body.auth:
            return {"ok": False, "error": "missing auth"}
        try:
            import account_conn
            try:
                await account_conn.close(body.customer_id, body.phone)
            except Exception:      # noqa: BLE001
                pass
            wrote = rb.import_session(body.phone, body.customer_id, {
                "auth": body.auth,
                "private_key": body.private_key,
                "guid": body.guid,
                "user_agent": body.user_agent,
                "phone": body.phone,
            })
            return {"ok": bool(wrote)}
        except Exception as exc:      # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # ---- prepare a marked message ---------------------------------------- #
    @app.post("/prepare")
    async def prepare(body: Prepare, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "send")
        try:
            import account_conn

            text_mode = (getattr(body, "mode", "marker") or "marker").lower() \
                == "text"

            # Fresh single connection, and read recipients regardless of the
            # marker. The old code returned "marker not found" (and NO targets)
            # whenever the account had no marked post, so a plain-text send —
            # which needs no marker at all — reported "no contacts" on an
            # account with hundreds of them. The marker is now advisory: text
            # mode never looks for it, and marker mode reports whether it was
            # found without hiding the recipient list.
            async def _work(client):
                self_guid = await rb.get_self_guid(client)
                message_id = None if text_mode else \
                    await rb.find_marked_message(client, body.marker)
                recipients = await rb.get_ordered_recipients(client)
                return self_guid, message_id, recipients

            self_guid, message_id, recipients = await account_conn.fresh_call(
                body.customer_id, body.phone, _work, timeout=180)
            # Plain guid strings over the wire. get_ordered_recipients yields
            # {"guid", "name"} dicts, and shipping those made the master str() a
            # dict into every send target.
            targets = [str(r.get("guid") if isinstance(r, dict) else r)
                       for r in (recipients or [])
                       if (r.get("guid") if isinstance(r, dict) else r)]
            return {"ok": True, "from_guid": self_guid,
                    "message_id": message_id,
                    "marker_found": bool(message_id) or text_mode,
                    "targets": targets}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "send")
            await _settle()

    @app.post("/upload/prepare")
    async def upload_prepare(body: UploadPrepare,
                            authorization: str = Header(None)):
        """Upload a file into the account's Saved Messages and return its id.

        The whole auto-upload path was missing from this worker, so a media
        campaign had only one route: the customer posting the file by hand in
        Saved and tagging it with the marker. Anything else reported
        "marker not found".

        Deliberately never raises: on failure it returns ok=False WITH the reason
        so the master can fall back to the marker flow instead of the request
        blowing up as a 500.
        """
        _auth(authorization)
        try:
            raw = base64.b64decode(body.file_b64)
        except Exception as exc:      # noqa: BLE001
            return {"ok": False,
                    "error": f"bad file payload: {type(exc).__name__}"}
        if not raw:
            return {"ok": False, "error": "empty file payload"}

        up_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "uploads")
        os.makedirs(up_dir, exist_ok=True)
        # basename only: never let a caller-supplied name escape up_dir with an
        # absolute path or a ../ traversal.
        fname = os.path.basename(body.file_name or "") or "file.bin"
        path = os.path.join(up_dir, fname)
        try:
            with open(path, "wb") as fh:
                fh.write(raw)
        except Exception as exc:      # noqa: BLE001
            return {"ok": False,
                    "error": f"write failed: {type(exc).__name__}: {str(exc)[:120]}"}

        key = await _hold_or_409(body.customer_id, body.phone, "upload")
        try:
            import account_conn

            async def _work(client):
                saved_guid, mid = await rb.upload_file_to_self(
                    client, path, caption=body.caption or "", file_name=fname)
                recipients = await rb.get_ordered_recipients(client)
                targets = [str(r.get("guid") if isinstance(r, dict) else r)
                           for r in (recipients or [])
                           if (r.get("guid") if isinstance(r, dict) else r)]
                return str(saved_guid), mid, targets

            saved_guid, mid, targets = await account_conn.fresh_call(
                body.customer_id, body.phone, _work, timeout=300)
            return {"ok": True, "from_guid": saved_guid, "message_id": mid,
                    "targets": targets, "total": len(targets)}
        except Exception as exc:      # noqa: BLE001 - report, never 500
            return {"ok": False,
                    "error": f"upload failed: {type(exc).__name__}: {str(exc)[:160]}"}
        finally:
            _release(key, "upload")
            await _settle()
            try:
                os.remove(path)
            except OSError:
                pass

    # ---- sending ---------------------------------------------------------- #
    @app.post("/send/start")
    async def send_start(body: SendStart, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "send")
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "sent": 0, "failed": 0, "total": len(body.targets),
               "stop": False, "error": "", "key": key, "retry_count": 0,
               "reason": "", "started": config.now_str()}
        _jobs[job_id] = job
        job["task"] = asyncio.create_task(_run_send(job_id, body))
        return {"ok": True, "job_id": job_id, "total": job["total"]}

    async def _run_send(job_id: str, body: SendStart) -> None:
        """Send to every target, on ONE connection the job owns, with a brake.

        Two things here were wrong and both had to change together.

        1) It called account_conn.call() PER RECIPIENT, i.e. over the shared warm
           socket. When a send raised something auth-looking, account_conn.call
           ran verify_session_dead(), and that drops and reopens the connection —
           the very socket the job was sending on. So one muted recipient tore
           down a healthy send mid-flight, and the rapid reconnect is itself what
           makes Rubika revoke a session. The job now holds its OWN dedicated
           connection for its whole life and confirms a suspected dead session on
           a separate connection, leaving its own socket untouched. This is the
           reference's shape, which its own comment calls the only difference from
           the proven build.

        2) A burst of errors ended the job for good (state="error_burst"). Rubika
           throttles long before it revokes, so a burst has to PAUSE and resume:
           wait resume_wait, reconnect a fresh client, carry on from where we
           stopped, up to max_retries times.
        """
        job = _jobs[job_id]
        import account_conn

        targets = list(body.targets)
        total = len(targets)
        idx = 0
        delay = max(0.05, float(body.delay or 1.0))
        max_errors = max(1, int(body.max_errors or 5))
        max_retries = max(0, int(body.max_retries or 0))
        send_timeout = max(5, int(body.send_timeout or config.SEND_TIMEOUT))

        async def _send_one(client, guid):
            if body.mode == "text":
                return await rb.send_text(client, guid, body.text)
            return await rb.forward_message(client, body.from_guid, guid,
                                            body.message_id)

        try:
            while True:
                consecutive = 0
                hit_max = False
                # A fresh dedicated connection per attempt. Entering this closes
                # any warm socket for the session first, so there is exactly one
                # connection for the account while the job runs.
                async with account_conn.fresh_connection(
                        body.customer_id, body.phone) as client:
                    while idx < total:
                        if job["stop"]:
                            job["state"] = "stopped"
                            job["reason"] = "manual_stop"
                            return
                        guid = targets[idx]
                        idx += 1
                        try:
                            await asyncio.wait_for(_send_one(client, guid),
                                                   timeout=send_timeout)
                            job["sent"] += 1
                            consecutive = 0     # CONSECUTIVE errors only
                        except Exception as exc:      # noqa: BLE001
                            # Do NOT tear down this client on an auth-looking
                            # error. Confirm on a separate connection; if the
                            # session is alive it was transient and we keep going
                            # on the SAME socket.
                            if account_conn.is_auth_error(exc):
                                if await _confirm_session_dead(body.customer_id,
                                                               body.phone):
                                    job["state"] = "auth_failed"
                                    job["error"] = (f"{type(exc).__name__}: "
                                                    f"{str(exc)[:120]}")
                                    job["reason"] = "invalid_auth"
                                    return
                            job["failed"] += 1
                            consecutive += 1
                            job["error"] = (f"{type(exc).__name__}: "
                                            f"{str(exc)[:120]}")
                            if consecutive >= max_errors:
                                hit_max = True
                                break
                        await _sleep_with_stop(job, delay)

                if not hit_max:
                    break                        # the whole list is done
                if job["retry_count"] >= max_retries:
                    job["state"] = "error_burst"
                    job["reason"] = (f"max_errors({max_errors}) reached, "
                                     f"retries exhausted at {idx}/{total}")
                    return
                job["retry_count"] += 1
                job["state"] = "waiting"
                job["reason"] = (f"paused after {max_errors} consecutive errors; "
                                 f"resuming at {idx}/{total}")
                await _sleep_with_stop(job, float(body.resume_wait or 300))
                if job["stop"]:
                    job["state"] = "stopped"
                    job["reason"] = "manual_stop"
                    return
                job["state"] = "running"
                # loop round: the `async with` above opens a brand-new client.

            # Never report a cheerful "done" for a job that reached nobody.
            if total and not job["sent"]:
                job["state"] = "failed"
                job["reason"] = (job["error"]
                                 or f"0 of {total} targets were reached")
            else:
                job["state"] = "done"
                job["reason"] = ""
        except asyncio.CancelledError:
            job["state"] = "stopped"
            job["reason"] = "cancelled"
            raise
        except Exception as exc:      # noqa: BLE001 - the reason must survive
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            job["reason"] = "fatal"
        finally:
            _release(job["key"], "send")
            await _settle()

    @app.get("/send/status/{job_id}")
    async def send_status(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        return {k: v for k, v in job.items() if k not in ("task", "key")}

    @app.post("/send/stop/{job_id}")
    async def send_stop(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        job["stop"] = True
        return {"ok": True}

    @app.post("/send/to_list")
    async def send_to_list(body: SendStart, authorization: str = Header(None)):
        """Blocking send to an explicit list (used by brain / pool)."""
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "send")
        sent = failed = 0
        try:
            import account_conn
            for target in body.targets:
                try:
                    if body.mode == "text":
                        async def _one(client, guid=target):
                            return await rb.send_text(client, guid, body.text)
                    else:
                        async def _one(client, guid=target):
                            return await rb.forward_message(
                                client, body.from_guid, guid, body.message_id)
                    await account_conn.call(body.customer_id, body.phone, _one,
                                            timeout=config.SEND_TIMEOUT)
                    sent += 1
                except Exception:      # noqa: BLE001
                    failed += 1
                await asyncio.sleep(max(0.05, float(body.delay or 1.0)))
            return {"ok": True, "sent": sent, "failed": failed}
        finally:
            _release(key, "send")
            await _settle()

    # ---- channel-style send ---------------------------------------------- #
    @app.post("/channel/create")
    async def channel_create(body: ChannelCreate,
                             authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "channel")
        try:
            import account_conn

            # Creating a channel is a signed call Rubika rejects with
            # INVALID_AUTH over a reused warm socket -> fresh single connection.
            #
            # All THREE steps share that one connection, exactly as the reference
            # does. This endpoint used to call rb.create_channel and nothing else:
            # it never looked for the marked post and never forwarded it, and the
            # master never even sent a marker. So every channel campaign produced
            # a channel with NO content in it — the accounts were then seeded into
            # an empty channel, which is why "ساخت کانال" looked like it worked
            # (a guid came back) while accomplishing nothing.
            async def _work(client):
                message_id = None
                if body.marker:
                    message_id = await rb.find_marked_message(client, body.marker)
                guid = await rb.create_channel(client, body.title,
                                               body.description)
                forwarded = False
                forward_error = ""
                if message_id and guid:
                    saved_guid = await rb.get_self_guid(client)
                    try:
                        await rb.forward_message(client, saved_guid, guid,
                                                 message_id)
                        forwarded = True
                    except Exception as exc:      # noqa: BLE001
                        # The channel exists, so this is NOT a failure of the
                        # endpoint — but the reason must survive, otherwise the
                        # owner sees an empty channel and no explanation.
                        forward_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                return guid, bool(message_id), forwarded, forward_error

            guid, marker_found, forwarded, forward_error = \
                await account_conn.signed_call(body.customer_id, body.phone,
                                               _work, timeout=180)
            return {"ok": True, "channel_guid": guid,
                    "marker_found": marker_found, "forwarded": forwarded,
                    "forward_error": forward_error}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "channel")
            await _settle()

    @app.post("/channel/add")
    async def channel_add(body: ChannelAdd, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "channel")
        try:
            import account_conn

            # Adding members is signed too -> fresh single connection.
            async def _work(client):
                return await rb.seed_channel_with_contacts(
                    client, body.channel_guid, target=body.target,
                    batch=body.batch, delay=body.delay)

            added = await account_conn.signed_call(body.customer_id, body.phone,
                                                   _work, timeout=1800)
            return {"ok": True, "added": added}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "channel")
            await _settle()

    # ---- contacts --------------------------------------------------------- #
    @app.post("/contacts/add")
    async def contacts_add(body: ContactsAdd, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "contacts")
        try:
            import account_conn

            # The WHOLE batch runs inside ONE connection, the way the reference
            # does it. It used to call account_conn.call() per number, so a list
            # of 500 numbers acquired and released the session 500 times; that
            # churn is exactly what Rubika treats as suspicious.
            #
            # It also had no brake and no memory of why anything failed: the
            # handler was `except Exception: failed += 1`, so a throttled batch
            # came back as "0 added, 500 failed" with not one reason recorded, and
            # the run ended instead of pausing. And success was tested with
            # rb._guid_of(res), while add_contact's documented contract is
            # {"on_rubika": bool, "guid": str|None} — a number that IS on Rubika
            # but whose response omitted the guid was counted as "not a user".
            async def _do(client):
                added = 0          # the number is a real Rubika account
                not_user = 0       # in the address book, but not on Rubika
                failed = 0
                guids = []
                results = []
                last_error = ""
                consecutive = 0
                for pair in (body.pairs or []):
                    is_pair = isinstance(pair, (list, tuple))
                    raw = str(pair[0]) if is_pair else str(pair)
                    name = ((pair[1] if is_pair and len(pair) > 1 else "")
                            or config.CONTACT_DEFAULT_FIRST)
                    phone = rb.normalize_phone(raw)
                    if not phone:
                        continue
                    try:
                        res = await asyncio.wait_for(
                            rb.add_contact(client, phone, first_name=name),
                            timeout=config.SEND_TIMEOUT)
                        consecutive = 0
                        on_rubika = bool((res or {}).get("on_rubika"))
                        guid = (res or {}).get("guid") if on_rubika else None
                        if on_rubika:
                            added += 1
                            if guid:
                                guids.append(guid)
                        else:
                            not_user += 1
                        results.append({"phone": phone, "on_rubika": on_rubika,
                                        "guid": guid})
                    except Exception as exc:      # noqa: BLE001
                        failed += 1
                        consecutive += 1
                        last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                        results.append({"phone": phone, "on_rubika": False,
                                        "guid": None, "error": last_error})
                        if consecutive >= config.CONTACT_MAX_ERRORS:
                            # Throttled, almost certainly. Pause and carry on
                            # rather than abandoning the rest of the list.
                            await asyncio.sleep(config.CONTACT_RESUME_WAIT)
                            consecutive = 0
                    await asyncio.sleep(max(0.0, float(body.delay or 1.0)))
                return {"added": added, "not_user": not_user, "failed": failed,
                        "guids": guids, "results": results,
                        "last_error": last_error}

            res = await account_conn.call(body.customer_id, body.phone, _do,
                                          timeout=7200)
            return {"ok": True, **res}
        except Exception as exc:
            # A batch that never ran is a failure WITH a reason, not "0 added".
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "contacts")
            await _settle()

    @app.post("/contacts/phones")
    async def contacts_phones(body: Account, authorization: str = Header(None)):
        """Export the account's own contacts as plain phone numbers."""
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "export")
        try:
            import account_conn

            async def _work(client):
                # get_contact_phones, NOT get_contacts_full: the latter returns
                # {guid, name, last_online, online} with no phone at all, so the
                # old code read item["phone"] off dicts that never had it and
                # every export came back empty without erroring.
                return await rb.get_contact_phones(client)

            phones = await account_conn.call(body.customer_id, body.phone,
                                             _work, timeout=600)
            phones = [str(p) for p in (phones or [])]
            return {"ok": True, "phones": phones, "count": len(phones)}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "export")
            await _settle()

    @app.post("/probe")
    async def probe(body: Probe, authorization: str = Header(None)):
        """Check which of these numbers exist on the platform, by adding them.

        The master meters this against the customer's daily probe budget before
        calling — probing is the operation that actually stresses the platform.
        """
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "discovery")
        found, missing = [], 0
        try:
            import account_conn
            for number in body.numbers:
                async def _one(client, p=str(number)):
                    return await rb.add_contact(
                        client, p, first_name=config.CONTACT_DEFAULT_FIRST)
                try:
                    res = await account_conn.call(body.customer_id, body.phone,
                                                  _one, timeout=60)
                    guid = rb._guid_of(res) if res else None   # noqa: SLF001
                    if guid:
                        found.append({"phone": str(number), "guid": guid})
                    else:
                        missing += 1
                except Exception:      # noqa: BLE001
                    missing += 1
                await asyncio.sleep(max(0.05, float(body.delay or 0.7)))
            return {"ok": True, "found": found, "probed": len(body.numbers),
                    "missing": missing}
        finally:
            _release(key, "discovery")
            await _settle()

    # ---- groups (tabchi) -------------------------------------------------- #
    @app.post("/group/join")
    async def group_join(body: GroupLink, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "join")
        try:
            import account_conn

            async def _work(client):
                return await rb.join_group_by_link(client, body.link)

            res = await account_conn.call(body.customer_id, body.phone, _work,
                                          timeout=120)
            guid = rb.join_result_group_guid(res)
            return {"ok": bool(guid), "guid": guid or ""}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "join")
            await _settle()

    @app.post("/group/leave")
    async def group_leave(body: GroupGuid, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "join")
        try:
            import account_conn

            async def _work(client):
                return await rb.leave_group(client, body.guid)

            await account_conn.call(body.customer_id, body.phone, _work,
                                    timeout=60)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "join")
            await _settle()

    @app.post("/groups/list")
    async def groups_list(body: Account, authorization: str = Header(None)):
        """The groups this account is ALREADY a member of."""
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "tabchi")
        try:
            import account_conn

            async def _work(client):
                return await rb.get_group_guids(client)

            groups = await account_conn.call(body.customer_id, body.phone,
                                             _work, timeout=180)
            return {"ok": True, "groups": groups or []}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "tabchi")
            await _settle()

    @app.post("/groups/send")
    async def groups_send(body: GroupsSend, authorization: str = Header(None)):
        """One tabchi pass: post a rotating text into each listed group."""
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "tabchi")
        sent = 0
        failures = []
        try:
            import account_conn
            texts = [t for t in (body.texts or []) if str(t).strip()]
            if not texts:
                return {"ok": False, "error": "no texts"}
            targets = body.guids or []
            if not targets:
                async def _list(client):
                    return await rb.get_group_guids(client)
                groups = await account_conn.call(body.customer_id, body.phone,
                                                 _list, timeout=180)
                targets = [g.get("guid") for g in (groups or []) if g.get("guid")]

            last_index = None
            for guid in targets:
                index = random.randrange(len(texts))
                if len(texts) > 1 and index == last_index:
                    index = (index + 1) % len(texts)
                last_index = index

                async def _one(client, g=guid, t=texts[index]):
                    return await rb.send_text(client, g, t)

                try:
                    await account_conn.call(body.customer_id, body.phone, _one,
                                            timeout=config.SEND_TIMEOUT)
                    sent += 1
                except Exception as exc:      # noqa: BLE001
                    failures.append({"guid": guid,
                                     "error": f"{type(exc).__name__}"})
                await asyncio.sleep(random.uniform(
                    float(body.delay_min or 0.5), float(body.delay_max or 2.0)))
            return {"ok": True, "sent": sent, "failures": failures}
        finally:
            _release(key, "tabchi")
            await _settle()

    # ---- secretary -------------------------------------------------------- #
    @app.post("/secretary/pass")
    async def secretary_pass(body: SecretaryPass,
                             authorization: str = Header(None)):
        """One secretary pass: answer new private chats we have not answered.

        `skip` carries the guids already replied to, so the same person is never
        answered twice — that ledger lives on the master because it must survive
        a worker restart.
        """
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "secretary")
        replied = []
        try:
            import account_conn
            skip = {str(s) for s in (body.skip or [])}

            async def _chats(client):
                return await rb.get_chats_user_guids(client)

            guids = await account_conn.call(body.customer_id, body.phone,
                                            _chats, timeout=180) or []
            marker_msg = None
            self_guid = ""
            if body.mode == "marker" and body.marker:
                async def _find(client):
                    return (await rb.get_self_guid(client),
                            await rb.find_marked_message(client, body.marker))
                self_guid, marker_msg = await account_conn.call(
                    body.customer_id, body.phone, _find, timeout=120)
                # A real check now: this received a truthy 2-tuple before, so a
                # missing marker sailed through and every forward failed instead.
                if not marker_msg:
                    return {"ok": False, "error": "marker not found"}

            for guid in guids:
                if str(guid) in skip:
                    continue
                try:
                    if marker_msg is not None:
                        async def _one(client, g=guid):
                            # marker_msg IS the id now, not a tuple to dig into.
                            return await rb.forward_message(
                                client, self_guid, g, marker_msg)
                    else:
                        async def _one(client, g=guid):
                            return await rb.send_text(client, g, body.text)
                    await account_conn.call(body.customer_id, body.phone, _one,
                                            timeout=60)
                    replied.append(str(guid))
                except Exception:      # noqa: BLE001
                    continue
                await asyncio.sleep(max(0.1, float(body.delay or 2.0)))
            return {"ok": True, "replied": replied}
        finally:
            _release(key, "secretary")
            await _settle()

    # ---- PV photo export (streamed, not one giant response) --------------- #
    @app.post("/pvexport/start")
    async def pvexport_start(body: PvExport, authorization: str = Header(None)):
        """Start collecting photos and return a job id.

        The master then polls for prepared batches. The base project returned
        every photo base64-encoded in ONE response — for a 2000-photo account
        that is roughly 800 MB in a single HTTP body through an SSH tunnel, which
        either times out or exhausts memory.
        """
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "pdf")
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "found": 0, "sent_cursor": 0, "stop": False,
               "photos": [], "error": "", "key": key, "mode": body.mode,
               "fallback": False, "started": config.now_str()}
        _jobs[job_id] = job
        job["task"] = asyncio.create_task(_run_pvexport(job_id, body))
        return {"ok": True, "job_id": job_id}

    async def _run_pvexport(job_id: str, body: PvExport) -> None:
        job = _jobs[job_id]
        import account_conn
        import pdf_export
        try:
            wanted_parallel = config.clamp_pv_parallel(body.parallel)
            use_parallel = body.mode in ("auto", "parallel") and wanted_parallel > 1

            async def _collect(client):
                guids = await rb.get_chat_list_guids(client, only_users=True)
                consecutive_errors = 0
                for guid in guids[:int(body.max_chats)]:
                    if job["stop"] or job["found"] >= int(body.max_photos):
                        return
                    inlines = []
                    async for _mid, file_inline in rb.iter_chat_photos(client, guid):
                        inlines.append(file_inline)
                        if len(inlines) >= 400:
                            break
                    if not inlines:
                        continue

                    async def _fetch(file_inline):
                        return await rb.download_photo(client, file_inline)

                    if use_parallel and not job["fallback"]:
                        # Several downloads in flight over the SAME connection.
                        # This is multiplexing, NOT a second connection — a second
                        # connection would revoke the session.
                        sem = asyncio.Semaphore(wanted_parallel)

                        async def _guarded(file_inline):
                            async with sem:
                                try:
                                    return await _fetch(file_inline)
                                except Exception:
                                    return None

                        for i in range(0, len(inlines), wanted_parallel * 4):
                            if job["stop"] or job["found"] >= int(body.max_photos):
                                return
                            chunk = inlines[i:i + wanted_parallel * 4]
                            blobs = await asyncio.gather(
                                *[_guarded(fi) for fi in chunk],
                                return_exceptions=True)
                            bad = 0
                            for blob in blobs:
                                if isinstance(blob, Exception) or not blob:
                                    bad += 1
                                    continue
                                jpeg = await asyncio.to_thread(
                                    pdf_export.prepare_image, blob,
                                    config.PV_EXPORT_PDF_QUALITY,
                                    config.PV_EXPORT_PDF_MAX_EDGE)
                                if jpeg:
                                    job["photos"].append(jpeg)
                                    job["found"] += 1
                            consecutive_errors = (consecutive_errors + bad
                                                  if bad else 0)
                            if consecutive_errors >= config.PV_EXPORT_FALLBACK_ERRORS:
                                # Parallel mode is being rejected; drop to the
                                # strictly sequential path rather than fail.
                                job["fallback"] = True
                                job["mode"] = "safe"
                                consecutive_errors = 0
                                break
                    else:
                        for file_inline in inlines:
                            if job["stop"] or job["found"] >= int(body.max_photos):
                                return
                            try:
                                blob = await _fetch(file_inline)
                            except Exception:
                                continue
                            if not blob:
                                continue
                            jpeg = await asyncio.to_thread(
                                pdf_export.prepare_image, blob,
                                config.PV_EXPORT_PDF_QUALITY,
                                config.PV_EXPORT_PDF_MAX_EDGE)
                            if jpeg:
                                job["photos"].append(jpeg)
                                job["found"] += 1

            await account_conn.call(body.customer_id, body.phone, _collect,
                                    timeout=3600)
            job["state"] = "stopped" if job["stop"] else "done"
        except asyncio.CancelledError:
            job["state"] = "stopped"
            raise
        except Exception as exc:      # noqa: BLE001
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            _release(job["key"], "pdf")
            await _settle()

    @app.get("/pvexport/status/{job_id}")
    async def pvexport_status(job_id: str, take: int = 0,
                              authorization: str = Header(None)):
        """Progress plus, when `take` is set, the next batch of prepared JPEGs.

        Each image was decoded and re-encoded exactly once already, so the master
        can rebuild the cumulative PDF cheaply.
        """
        _auth(authorization)
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        batch = []
        if take:
            start = job["sent_cursor"]
            slice_ = job["photos"][start:start + int(take)]
            batch = [base64.b64encode(b).decode("ascii") for b in slice_]
            job["sent_cursor"] = start + len(slice_)
        return {"state": job["state"], "found": job["found"],
                "mode": job["mode"], "fallback": job["fallback"],
                "error": job["error"], "cursor": job["sent_cursor"],
                "pending": max(0, job["found"] - job["sent_cursor"]),
                "batch": batch}

    @app.post("/pvexport/stop/{job_id}")
    async def pvexport_stop(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        job["stop"] = True
        return {"ok": True}

    return app


def run() -> None:
    problems = config.validate_worker()
    if problems:
        raise SystemExit("worker settings missing: " + ", ".join(problems))
    import uvicorn
    uvicorn.run(build_app(), host=config.WORKER_BIND_HOST,
                port=config.WORKER_API_PORT, log_level="warning")
