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


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="rb")


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

    # ---- request bodies --------------------------------------------------- #
    class Account(BaseModel):
        customer_id: int
        phone: str

    class StartLogin(Account):
        pass_key: str | None = None

    class LoginCode(Account):
        code: str

    class LoginPassword(Account):
        password: str

    class Prepare(Account):
        marker: str

    class SendStart(Account):
        targets: list
        mode: str = "marker"           # "marker" | "text"
        text: str = ""
        from_guid: str = ""
        message_id: str | int | None = None
        delay: float = 1.0
        max_errors: int = 5

    class ChannelCreate(Account):
        title: str
        description: str | None = None

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

    # ---- prepare a marked message ---------------------------------------- #
    @app.post("/prepare")
    async def prepare(body: Prepare, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "send")
        try:
            import account_conn

            async def _work(client):
                self_guid = await rb.get_self_guid(client)
                message_id = await rb.find_marked_message(client, body.marker)
                recipients = await rb.get_ordered_recipients(client)
                return self_guid, message_id, recipients

            self_guid, message_id, recipients = await account_conn.call(
                body.customer_id, body.phone, _work, timeout=180)
            if not message_id:
                return {"ok": False, "error": "marker not found"}
            # Plain guid strings over the wire. get_ordered_recipients yields
            # {"guid", "name"} dicts, and shipping those made the master str() a
            # dict into every send target.
            targets = [str(r.get("guid") if isinstance(r, dict) else r)
                       for r in (recipients or [])
                       if (r.get("guid") if isinstance(r, dict) else r)]
            return {"ok": True, "from_guid": self_guid,
                    "message_id": message_id, "targets": targets}
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            _release(key, "send")
            await _settle()

    # ---- sending ---------------------------------------------------------- #
    @app.post("/send/start")
    async def send_start(body: SendStart, authorization: str = Header(None)):
        _auth(authorization)
        key = await _hold_or_409(body.customer_id, body.phone, "send")
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "sent": 0, "failed": 0, "total": len(body.targets),
               "stop": False, "error": "", "key": key,
               "started": config.now_str()}
        _jobs[job_id] = job
        job["task"] = asyncio.create_task(_run_send(job_id, body))
        return {"ok": True, "job_id": job_id, "total": job["total"]}

    async def _run_send(job_id: str, body: SendStart) -> None:
        job = _jobs[job_id]
        import account_conn
        consecutive = 0
        try:
            for target in body.targets:
                if job["stop"]:
                    job["state"] = "stopped"
                    return
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
                    job["sent"] += 1
                    consecutive = 0
                except account_conn.InvalidAuthError:
                    job["state"] = "auth_failed"
                    job["error"] = "session invalid"
                    return
                except Exception as exc:      # noqa: BLE001
                    job["failed"] += 1
                    consecutive += 1
                    job["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    if consecutive >= int(body.max_errors or 5):
                        job["state"] = "error_burst"
                        return
                await asyncio.sleep(max(0.05, float(body.delay or 1.0)))
            job["state"] = "done"
        except asyncio.CancelledError:
            job["state"] = "stopped"
            raise
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

            async def _work(client):
                return await rb.create_channel(client, body.title,
                                               body.description)

            guid = await account_conn.call(body.customer_id, body.phone, _work,
                                           timeout=120)
            return {"ok": True, "channel_guid": guid}
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

            async def _work(client):
                return await rb.seed_channel_with_contacts(
                    client, body.channel_guid, target=body.target,
                    batch=body.batch, delay=body.delay)

            added = await account_conn.call(body.customer_id, body.phone, _work,
                                            timeout=1800)
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
        added = not_user = failed = 0
        guids = []
        try:
            import account_conn
            for pair in body.pairs:
                phone = str(pair[0]) if isinstance(pair, (list, tuple)) else str(pair)
                name = (pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1
                        else "") or config.CONTACT_DEFAULT_FIRST

                async def _one(client, p=phone, n=name):
                    return await rb.add_contact(client, p, first_name=n)

                try:
                    res = await account_conn.call(body.customer_id, body.phone,
                                                  _one, timeout=60)
                    guid = rb._guid_of(res) if res else None   # noqa: SLF001
                    if guid:
                        added += 1
                        guids.append(guid)
                    else:
                        not_user += 1
                except Exception:      # noqa: BLE001
                    failed += 1
                await asyncio.sleep(max(0.05, float(body.delay or 1.0)))
            return {"ok": True, "added": added, "not_user": not_user,
                    "failed": failed, "guids": guids}
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
