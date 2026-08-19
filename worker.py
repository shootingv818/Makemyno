"""
Worker subsystem (master side).
==============================

Lets the master orchestrate one or more worker servers so that login and send
jobs spread across several clean IPs. It deliberately does not touch the
sending/login logic in rubika_client.py — workers run that same unchanged code
behind a small API (worker_api.py).

Responsibilities here:
  * generate worker tags,
  * provision a fresh server over SSH + Docker,
  * keep a warm SSH tunnel to each worker's loopback-only API,
  * call the worker API,
  * pick a worker for a NEW account (sequential round-robin, persisted),
  * health checks in parallel with a small in-memory cache,
  * pull worker session files into the owner's backup.

WHO MAY IMPORT THIS
-------------------
Both bots do. The customer bot needs pick_worker_for_login / worker_for_account /
api_call to run a job, but it never reaches the provisioning or credential code
paths, and no customer-facing screen exposes any of it.

WHY ROUND-ROBIN AND NOT "FEWEST ACCOUNTS"
-----------------------------------------
"Fewest accounts" needs an accurate global count, and accounts that run on the
local master often carry no worker_id at all, so the count under-reports and the
same worker keeps winning. A sequential pointer needs no counting, and it is
persisted in the database — an in-memory pointer resets on every restart and
sends the next N logins all to worker #1.

Heavy third-party deps (asyncssh, httpx) are imported lazily inside the
functions that need them, so importing this module never fails on a machine
without them installed.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import time

import config
import crypto_util
import db

# Where the worker checkout / data live on the remote server.
REMOTE_DIR = "~/makemyno_worker"
REMOTE_DATA = "~/makemyno_worker_data"
CONTAINER = "makemyno-worker"
IMAGE = "makemyno-worker"

# worker_id -> {"conn":.., "listener":.., "local_port":int}
_tunnels: dict = {}
_tunnel_locks: dict = {}
# worker_id -> {"status","ping_ms","file_ok","ts","mono","detail"}
_health_cache: dict = {}
# worker_id -> last failure reason (diagnostics)
_health_detail: dict = {}
# worker_id -> asyncio.Task keeping the tunnel alive
_supervisors: dict = {}


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def gen_tag(is_master: bool = False) -> str:
    """A short unique worker tag like 'wk-a3f1'. The master gets 'master'."""
    if is_master:
        return "master"
    existing = {w["tag"] for w in db.list_workers()}
    for _ in range(200):
        tag = f"wk-{secrets.token_hex(2)}"
        if tag not in existing:
            return tag
    return f"wk-{secrets.token_hex(4)}"


# --------------------------------------------------------------------------- #
# Master-as-worker bootstrap
# --------------------------------------------------------------------------- #
def ensure_master_worker() -> dict | None:
    """Make sure a local 'master' worker row exists (runs jobs in-process)."""
    existing = db.get_master_worker()
    if existing:
        return existing
    if not config.MASTER_AS_WORKER:
        return None
    wid = db.add_worker(
        tag=gen_tag(is_master=True), ip="local", ssh_port=0, ssh_user="",
        ssh_pass_enc="", api_port=0, api_token_enc="", is_master=1,
    )
    return db.get_worker(wid)


def is_local(worker: dict) -> bool:
    return bool(worker and worker.get("is_master"))


# --------------------------------------------------------------------------- #
# Presentation helpers (shared by the owner panel's cards)
# --------------------------------------------------------------------------- #
def status_emoji(worker: dict) -> str:
    if not worker.get("file_ok"):
        return "🔴"
    ping = worker.get("ping_ms", -1)
    if ping is None or ping < 0:
        return "🟡"
    if ping <= config.PING_GREEN_MS:
        return "🟢"
    if ping <= config.PING_YELLOW_MS:
        return "🟡"
    return "🔴"


def ping_text(worker: dict) -> str:
    ping = worker.get("ping_ms")
    return f"{ping}ms" if (ping is not None and ping >= 0) else "—"


def route_label(worker: dict) -> str:
    """What the API answered last time. Not a Rubika reachability claim."""
    return "API ok" if worker.get("file_ok") else "No answer"


# --------------------------------------------------------------------------- #
# Low-level SSH helpers (asyncssh, lazy import)
# --------------------------------------------------------------------------- #
async def _ssh_connect(ip: str, port: int, user: str, password: str,
                       keepalive: bool = True):
    """Open an SSH connection with hard timeouts so a flaky server can never
    hang a caller forever.

    keepalive=True is for the persistent tunnel: it detects a dead link within
    about 45 seconds. keepalive=False is for one-shot admin operations
    (provision / update / restart / backup), because a long docker build on a
    loaded server would otherwise be dropped by a missed keepalive.
    """
    import asyncssh  # lazy
    base = dict(
        host=ip, port=int(port or 22), username=user, password=password,
        known_hosts=None,          # we own these servers; trust on first use
        login_timeout=8,
    )
    if keepalive:
        base["keepalive_interval"] = 15
        base["keepalive_count_max"] = 3

    # A one-shot admin operation gets a generous window; the health tunnel keeps a
    # tight one so a dead link is noticed quickly.
    #
    # Provisioning used to share the tunnel's 10 seconds, and that is how a
    # provision attempt died with a bare "TimeoutError": the server was still busy
    # from the previous build, SSH login took longer than ten seconds, and the
    # report named neither the step nor the limit.
    budget = config.SSH_CONNECT_TIMEOUT if keepalive \
        else config.SSH_ADMIN_CONNECT_TIMEOUT
    base["login_timeout"] = max(8, budget - 2)

    async def _do():
        try:
            return await asyncssh.connect(connect_timeout=max(8, budget - 2), **base)
        except TypeError:
            # older asyncssh without the connect_timeout kwarg
            return await asyncssh.connect(**base)

    try:
        return await asyncio.wait_for(_do(), timeout=budget)
    except asyncio.TimeoutError:
        # Say what timed out and what to check. A bare TimeoutError sent the owner
        # looking in the wrong place more than once.
        raise SSHStepTimeout(
            f"اتصال SSH به {ip}:{port} در {budget} ثانیه برقرار نشد.\n"
            f"معمولاً یعنی سرور تحت فشار است (ساخت قبلی هنوز در حال اجراست) یا "
            f"پورت/فایروال مسیر را بسته.\n"
            f"روی همان سرور بررسی کن:  uptime  و  docker ps") from None


class SSHStepTimeout(TimeoutError):
    """A timeout that knows which step it belongs to."""


# Patterns worth recognising, because each one has a different fix and guessing
# sends the owner to the wrong place. The first version of this message assumed
# "apt lock or no internet" for every failure; the real cause of one report was an
# EXPIRED ROOT PASSWORD, which has nothing to do with either.
_SETUP_HINTS = (
    (("password has expired", "password change required"),
     "پسورد root این سرور منقضی شده و لینوکس تا عوض نشود هیچ دستوری اجرا نمی‌کند.\n"
     "چون SSH ما ترمینال تعاملی ندارد، خودش نمی‌تواند عوضش کند.\n"
     "یک بار دستی وارد شو و پسورد را عوض کن، بعد «افزودن ورکر» را بزن:\n"
     "   ssh root@{ip}\n"
     "(سرورهای تازه‌ی Hetzner/OVH معمولاً همین حالت را دارند.)"),
    (("permission denied", "authentication failed"),
     "نام کاربری یا پسورد SSH درست نیست.\n"
     "اگر سرور فقط کلید قبول می‌کند، ورود با پسورد را فعال کن."),
    (("could not resolve", "temporary failure in name resolution",
      "network is unreachable"),
     "سرور به اینترنت وصل نیست یا DNS ندارد.\n"
     "روی همان سرور تست کن:  ping -c1 deb.debian.org"),
    (("no space left",),
     "دیسک سرور پر است.\n"
     "روی همان سرور:  df -h  و  docker system prune -af"),
    (("could not get lock", "dpkg was interrupted"),
     "قفل apt آزاد نشد (احتمالاً به‌روزرسانی خودکار در حال اجراست).\n"
     "چند دقیقه صبر کن و دوباره امتحان کن، یا روی سرور:\n"
     "   dpkg --configure -a"),
)


def _explain_setup_failure(out: str, err: str, ip: str = "") -> str:
    """Turn the server's own output into a diagnosis with a fix.

    A generic "probably the apt lock or no internet" was actively misleading: one
    report was an expired root password, and the owner was pointed at apt.
    """
    blob = f"{out or ''}\n{err or ''}"
    low = blob.lower()
    for needles, advice in _SETUP_HINTS:
        if any(n in low for n in needles):
            return advice.format(ip=ip or "SERVER") + \
                "\n\nخروجی سرور:\n" + blob.strip()[-400:]
    return ("نصب Docker روی سرور کامل نشد.\n"
            "روی همان سرور دستی بزن:\n"
            "   apt-get install -y docker.io && systemctl enable --now docker\n"
            "بعد دوباره «افزودن ورکر» را بزن.\n\n"
            "خروجی سرور:\n" + blob.strip()[-400:])


async def _run(conn, command: str, check: bool = False, timeout: float = None,
               label: str = ""):
    """Run a command over SSH -> (exit_status, out, err).

    `timeout` bounds a single command so one wedged step cannot hang provisioning
    forever, and `label` puts the step's name in the error instead of leaving the
    owner with an unattributed TimeoutError.
    """
    try:
        if timeout:
            res = await asyncio.wait_for(conn.run(command, check=check),
                                         timeout=timeout)
        else:
            res = await conn.run(command, check=check)
    except asyncio.TimeoutError:
        raise SSHStepTimeout(
            f"مرحله‌ی «{label or 'دستور'}» بعد از {int(timeout)} ثانیه تمام نشد.\n"
            f"احتمالاً سرور کم‌قدرت است یا شبکه‌اش کند. روی همان سرور نگاه کن:  "
            f"docker ps -a  و  df -h") from None
    return res.exit_status, (res.stdout or ""), (res.stderr or "")


async def _with_conn(worker: dict, keepalive: bool = True):
    return await _ssh_connect(
        worker["ip"], worker["ssh_port"], worker["ssh_user"],
        crypto_util.decrypt(worker["ssh_pass_enc"]),
        keepalive=keepalive,
    )


# --------------------------------------------------------------------------- #
# Provisioning: SSH in, install Docker, clone, build, run.
# --------------------------------------------------------------------------- #
async def provision_worker(ip: str, ssh_port: int, ssh_user: str, ssh_pass: str,
                           tag: str = None, on_progress=None) -> dict:
    """Build a brand-new worker. Returns {ok, tag, api_port, api_token, error}."""
    async def say(msg: str):
        if on_progress:
            try:
                await on_progress(msg)
            except Exception:
                pass

    api_port = config.WORKER_API_PORT
    api_token = secrets.token_urlsafe(24)
    tag = tag or gen_tag()

    try:
        import asyncssh  # noqa: F401  (fail early with a clear message)
    except ImportError:
        return {"ok": False,
                "error": "بسته‌ی asyncssh روی سرور اصلی نصب نیست (pip install asyncssh)."}

    conn = None
    try:
        await say("🔌 اتصال SSH به سرور ...")
        conn = await _ssh_connect(ip, ssh_port, ssh_user, ssh_pass, keepalive=False)

        # Check the account is actually usable BEFORE spending ten minutes on apt.
        # An expired root password lets SSH authenticate and then refuses every
        # command, so the first real failure surfaced ten minutes later disguised as
        # a Docker problem.
        code, out, err = await _run(conn, "echo READY", timeout=60,
                                    label="بررسی دسترسی")
        if "READY" not in (out or ""):
            return {"ok": False, "error": _explain_setup_failure(out, err, ip)}

        await say("🐳 بررسی و نصب Docker ...")
        # A fresh Ubuntu box runs unattended-upgrades right after boot and holds
        # the dpkg lock for minutes. Without waiting for that lock the docker
        # install silently fails and the later build dies with
        # "docker: command not found" — so: stop auto-upgrades for this run, tell
        # apt to WAIT for the lock, install from the distro repo, fall back to
        # get.docker.com, then VERIFY before continuing.
        install_script = (
            "export DEBIAN_FRONTEND=noninteractive\n"
            "systemctl stop unattended-upgrades >/dev/null 2>&1 || true\n"
            "if ! command -v docker >/dev/null 2>&1; then\n"
            "  apt-get -o DPkg::Lock::Timeout=180 update -qq || true\n"
            "  apt-get -o DPkg::Lock::Timeout=180 install -y -qq "
            "ca-certificates curl git docker.io || true\n"
            "fi\n"
            "command -v docker >/dev/null 2>&1 || "
            "{ curl -fsSL https://get.docker.com | sh; } || true\n"
            "command -v git >/dev/null 2>&1 || "
            "apt-get -o DPkg::Lock::Timeout=180 install -y -qq git || true\n"
            "systemctl enable --now docker >/dev/null 2>&1 || true\n"
            "if command -v docker >/dev/null 2>&1; then docker --version; "
            "echo DOCKER_OK; else echo DOCKER_MISSING; fi\n"
        )
        code, out, err = await _run(conn, install_script,
                                    timeout=config.SSH_STEP_TIMEOUT,
                                    label="نصب Docker")
        if "DOCKER_OK" not in (out or ""):
            return {"ok": False, "error": _explain_setup_failure(out, err, ip)}

        await say("📥 دریافت سورس ...")
        code, out, err = await _run(
            conn,
            f"rm -rf {REMOTE_DIR} && "
            f"git clone --depth 1 -b {config.GIT_BRANCH} "
            f"{config.GIT_REPO_URL} {REMOTE_DIR}",
            timeout=config.SSH_STEP_TIMEOUT, label="دریافت سورس",
        )
        if code != 0:
            return {"ok": False,
                    "error": f"git clone شکست خورد: {err[:200] or out[:200]}"}

        await say("📝 نوشتن تنظیمات ورکر ...")
        # A worker gets ONLY what it needs to execute work: no bot token, no
        # owner id, no customer data, no encryption key.
        env_lines = (
            "MODE=worker\n"
            f"WORKER_API_TOKEN={api_token}\n"
            f"WORKER_API_PORT={api_port}\n"
            # With host networking the API binds to the host's loopback, which
            # only the master's SSH tunnel can reach.
            "WORKER_BIND_HOST=127.0.0.1\n"
            f"TIMEZONE={config.TIMEZONE}\n"
        )
        await _run(conn, f"mkdir -p {REMOTE_DATA}")
        await _run(conn, f"cat > {REMOTE_DIR}/.env <<'ENVEOF'\n{env_lines}ENVEOF")

        await say("🏗 ساخت ایمیج Docker (چند دقیقه طول می‌کشه) ...")
        # The LEGACY builder, deliberately.
        #
        # I briefly forced DOCKER_BUILDKIT=1 to silence the "legacy builder is
        # deprecated" banner, and that broke provisioning outright: BuildKit needs
        # the buildx plugin, which a plain `apt install docker.io` does not ship,
        # so the build died with "BuildKit is enabled but the buildx component is
        # missing". The banner was only a WARNING — the build worked fine. Chasing
        # a warning and breaking the build was the wrong trade.
        #
        # --network=host lets build steps use the server's own DNS, avoiding the
        # common "build container cannot resolve PyPI" failure on fresh servers.
        code, out, err = await _run(
            conn, f"cd {REMOTE_DIR} && DOCKER_BUILDKIT=0 "
            f"docker build --network=host -t {IMAGE} .",
            timeout=config.SSH_BUILD_TIMEOUT, label="ساخت ایمیج Docker")
        if code != 0:
            # Tail of BOTH streams, and wide enough that a deprecation banner
            # cannot push the real cause (a missing apt package, a DNS failure)
            # out of the report.
            detail = (out + "\n" + err).strip()[-1500:]
            return {"ok": False, "error": f"docker build شکست خورد:\n{detail}"}

        await say("🚀 اجرای کانتینر ...")
        run_cmd = (
            f"docker rm -f {CONTAINER} 2>/dev/null; "
            f"docker run -d --name {CONTAINER} --restart always "
            f"--network=host "
            f"--env-file {REMOTE_DIR}/.env "
            f"-v {REMOTE_DATA}:/app/data {IMAGE}"
        )
        code, out, err = await _run(conn, run_cmd,
                                    timeout=config.SSH_STEP_TIMEOUT,
                                    label="اجرای کانتینر")
        if code != 0:
            return {"ok": False,
                    "error": f"docker run شکست خورد: {err[:200] or out[:200]}"}

        await say("✅ نصب کامل شد.")
        return {"ok": True, "tag": tag, "api_port": api_port,
                "api_token": api_token}
    except SSHStepTimeout as e:
        # These already explain themselves, including which step and what to
        # check. Do NOT reduce them to a type name.
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        # A bare `TimeoutError:` with nothing after it was a real report, and it
        # sent the owner looking in the wrong place. An exception with no message
        # gets a description of what was being attempted instead.
        detail = str(e).strip()
        if not detail:
            detail = ("بدون پیام. معمولاً یعنی اتصال SSH وسط کار قطع شد — "
                      "سرور تحت فشار یا شبکه ناپایدار. روی همان سرور: "
                      "uptime، free -h، docker ps -a")
        return {"ok": False, "error": f"{type(e).__name__}: {detail[:400]}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def register_provisioned(ip, ssh_port, ssh_user, ssh_pass, prov: dict) -> int:
    """Persist a provisioned worker, encrypting its credentials at rest."""
    return db.add_worker(
        tag=prov["tag"], ip=ip, ssh_port=int(ssh_port or 22), ssh_user=ssh_user,
        ssh_pass_enc=crypto_util.encrypt(ssh_pass),
        api_port=int(prov["api_port"]),
        api_token_enc=crypto_util.encrypt(prov["api_token"]),
        is_master=0,
    )


# --------------------------------------------------------------------------- #
# Remote lifecycle ops
# --------------------------------------------------------------------------- #
async def restart_worker(worker: dict) -> tuple:
    conn = await _with_conn(worker, keepalive=False)
    try:
        return await _run(conn, f"docker restart {CONTAINER}")
    finally:
        conn.close()


async def update_worker(worker: dict) -> tuple:
    """Move the worker onto the latest code of config.GIT_BRANCH and rebuild.

    The remote's origin is repointed first, so a worker cloned from an older
    repo is moved onto the active code with one click. That also means a stale
    GIT_REPO_URL would silently downgrade the whole fleet — which is why the
    owner panel shows the target repo before running this, and the settings
    validator checks it.
    """
    conn = await _with_conn(worker, keepalive=False)   # long build: no keepalive
    try:
        branch = config.GIT_BRANCH
        cmd = (
            f"cd {REMOTE_DIR} && "
            f"git remote set-url origin '{config.GIT_REPO_URL}' && "
            f"git fetch --depth 1 origin {branch} && "
            f"git checkout -B {branch} FETCH_HEAD && "
            f"DOCKER_BUILDKIT=0 docker build --network=host -t {IMAGE} . && "
            f"(docker rm -f {CONTAINER} 2>/dev/null || true) && "
            f"docker run -d --name {CONTAINER} --restart always --network=host "
            f"--env-file {REMOTE_DIR}/.env -v {REMOTE_DATA}:/app/data {IMAGE}"
        )
        return await _run(conn, cmd, timeout=config.SSH_BUILD_TIMEOUT,
                          label="به‌روزرسانی ورکر")
    finally:
        conn.close()


async def teardown_worker(worker: dict) -> None:
    """Stop and remove the container and checkout on the remote server."""
    await close_tunnel(worker["id"])
    try:
        conn = await _with_conn(worker, keepalive=False)
        try:
            await _run(conn,
                       f"docker rm -f {CONTAINER} 2>/dev/null; rm -rf {REMOTE_DIR}")
        finally:
            conn.close()
    except Exception:
        pass          # best-effort; the caller still removes the DB row


async def worker_code_version(worker: dict) -> str:
    """The short git revision a worker is actually running."""
    if is_local(worker):
        return master_code_version()
    try:
        res = await api_call(worker, "GET", "/ping", timeout=10)
        return str(res.get("version") or "?")
    except Exception:
        return "?"


def master_code_version() -> str:
    """The short git revision of this checkout."""
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip() or "?"
    except Exception:
        return "?"


# --------------------------------------------------------------------------- #
# SSH tunnel to the worker's loopback-only API
# --------------------------------------------------------------------------- #
def _lock_for(worker_id: int) -> asyncio.Lock:
    if worker_id not in _tunnel_locks:
        _tunnel_locks[worker_id] = asyncio.Lock()
    return _tunnel_locks[worker_id]


async def open_tunnel(worker: dict) -> int:
    """Open (or reuse) a local port-forward to the worker API."""
    wid = worker["id"]
    async with _lock_for(wid):
        existing = _tunnels.get(wid)
        if existing:
            return existing["local_port"]
        conn = await _with_conn(worker)
        listener = await conn.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", int(worker["api_port"]))
        local_port = listener.get_port()
        _tunnels[wid] = {"conn": conn, "listener": listener,
                         "local_port": local_port}
        return local_port


async def close_tunnel(worker_id: int) -> None:
    t = _tunnels.pop(worker_id, None)
    if not t:
        return
    for key in ("listener", "conn"):
        try:
            t[key].close()
        except Exception:
            pass


async def _supervisor_loop(worker_id: int) -> None:
    """Keep one worker's tunnel warm, rebuilding it with capped backoff.

    Without this every api_call pays a cold SSH connect, and the health loop
    reports false timeouts while those connects are still in flight.
    """
    backoff = [5, 15, 30, 60]
    idx = 0
    while True:
        try:
            w = db.get_worker(worker_id)
            if not w or is_local(w) or not w.get("enabled"):
                return                      # removed, disabled, or now master
            await open_tunnel(w)
            idx = 0                         # connected -> reset backoff
            t = _tunnels.get(worker_id)
            conn = t["conn"] if t else None
            if conn is None:
                raise RuntimeError("tunnel vanished right after opening")
            await conn.wait_closed()        # block until the link dies
            await close_tunnel(worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await close_tunnel(worker_id)
            base = backoff[min(idx, len(backoff) - 1)]
            idx += 1
            await asyncio.sleep(base + random.uniform(0, base * 0.3))


def start_supervisor(worker: dict) -> None:
    if not worker or is_local(worker) or not worker.get("enabled"):
        return
    wid = worker["id"]
    task = _supervisors.get(wid)
    if task and not task.done():
        return
    _supervisors[wid] = asyncio.create_task(_supervisor_loop(wid))


async def stop_supervisor(worker_id: int) -> None:
    task = _supervisors.pop(worker_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await close_tunnel(worker_id)


async def start_all_supervisors() -> None:
    for w in db.list_workers():
        if not is_local(w) and w.get("enabled"):
            start_supervisor(w)


async def prewarm_all() -> None:
    """Open every enabled remote tunnel in parallel, so the first health cycle
    after startup does not report cold connects as failures."""
    ws = [w for w in db.list_workers() if not is_local(w) and w.get("enabled")]
    if ws:
        await asyncio.gather(*[open_tunnel(w) for w in ws],
                             return_exceptions=True)


def snapshot_all() -> list:
    """Current in-memory health snapshots, without probing."""
    return list(_health_cache.values())


# --------------------------------------------------------------------------- #
# API client (master -> worker, through the tunnel)
# --------------------------------------------------------------------------- #
async def api_call(worker: dict, method: str, path: str, payload: dict = None,
                   timeout: int = 120) -> dict:
    """Call the worker API. Raises on transport or HTTP error.

    This is the single chokepoint for all master->worker traffic, which is why
    the credentials only ever get decrypted here.
    """
    import httpx  # lazy
    local_port = await open_tunnel(worker)
    token = crypto_util.decrypt(worker["api_token_enc"])
    url = f"http://127.0.0.1:{local_port}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=payload,
                                        headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        # a broken tunnel is the usual cause -> drop it so the next call reopens
        await close_tunnel(worker["id"])
        raise


# --------------------------------------------------------------------------- #
# Health checks (parallel, cached)
# --------------------------------------------------------------------------- #
async def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> int:
    """Latency in ms to open a TCP connection, or -1 on failure."""
    start = time.monotonic()
    try:
        fut = asyncio.open_connection(host, int(port or 22))
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return int((time.monotonic() - start) * 1000)
    except Exception:
        return -1


def _mk_summary(worker: dict, status: str, ping: int, api_ok: bool,
                detail=None) -> dict:
    """Persist and cache one worker's health snapshot.

    `file_ok` means "the worker API answered", NOT "the worker can reach
    Rubika". Rubika reachability was deliberately removed from the health path:
    a transient Rubika 503 used to mark every healthy worker as blocked.
    """
    wid = worker["id"]
    _health_detail[wid] = detail
    try:
        db.update_worker_health(wid, status, ping, api_ok)
    except Exception:
        pass
    summary = {"id": wid, "tag": worker["tag"], "ip": worker["ip"],
               "status": status, "ping_ms": ping, "file_ok": api_ok,
               "detail": detail, "ts": config.now_str(),
               "mono": time.monotonic()}
    _health_cache[wid] = summary
    return summary


async def check_worker(worker: dict, warm_only: bool = False) -> dict:
    """Measure one worker's health: SSH reachability plus the instant /ping.

    warm_only=True (the background loop) never opens a new tunnel: if the
    supervisor is still reconnecting, the worker is reported as reconnecting
    rather than forced through a cold connect.
    """
    wid = worker["id"]
    if is_local(worker):
        # the master runs jobs in-process: no SSH, no API
        return _mk_summary(worker, "ok", 1, True, None)

    ping = await _tcp_ping(worker["ip"], worker["ssh_port"], timeout=3.0)
    if ping < 0:
        return _mk_summary(worker, "down", ping, False, "ssh unreachable")

    if warm_only and wid not in _tunnels:
        return _mk_summary(worker, "blocked", ping, False, "reconnecting")

    try:
        await api_call(worker, "GET", "/ping", timeout=8)
        return _mk_summary(worker, "ok", ping, True, None)
    except Exception as e:  # noqa: BLE001
        return _mk_summary(worker, "blocked", ping, False,
                           f"api error: {type(e).__name__}: {str(e)[:120]}")


async def check_all(workers: list = None, warm_only: bool = False) -> list:
    """Health-check every ENABLED worker in parallel, each with its own deadline
    so one flaky server can never stall the whole cycle."""
    if workers is None:
        workers = db.list_workers()
    probe = [w for w in workers if w.get("enabled")]
    if not probe:
        return []

    async def _guarded(w):
        return await asyncio.wait_for(check_worker(w, warm_only=warm_only),
                                      timeout=15)

    results = await asyncio.gather(*[_guarded(w) for w in probe],
                                   return_exceptions=True)
    out = []
    for w, r in zip(probe, results):
        if isinstance(r, Exception):
            reason = ("timeout>15s" if isinstance(r, asyncio.TimeoutError)
                      else f"check crashed: {type(r).__name__}")
            out.append(_mk_summary(w, "down", -1, False, reason))
        else:
            out.append(r)
    return out


def health_detail(worker_id: int):
    return _health_detail.get(worker_id)


def cached_health(worker_id: int):
    return _health_cache.get(worker_id)


def is_healthy(worker: dict) -> bool:
    cached = _health_cache.get(worker["id"])
    if cached:
        return cached["status"] == "ok"
    return bool(worker.get("enabled"))     # never checked -> tentatively usable


# --------------------------------------------------------------------------- #
# Selection for a NEW account
# --------------------------------------------------------------------------- #
async def pick_worker_for_login(verify: bool = True, exclude_id=None) -> dict | None:
    """Pick the worker a new account should live on.

    Sequential round-robin over the healthy enabled pool, using the pointer
    persisted in the database. exclude_id lets a failed login retry land
    somewhere else; it is not a transfer feature (accounts stay put once they
    are placed).
    """
    ensure_master_worker()
    workers = db.list_enabled_workers()
    if not workers:
        return None

    remotes = [w for w in workers if not is_local(w)]
    if verify and remotes:
        await check_all(workers)
        workers = db.list_enabled_workers()      # reload with fresh health

    # the local master is always usable; remotes must have answered
    pool = [w for w in workers if (is_local(w) or w.get("status") == "ok")]
    if exclude_id is not None:
        pool = [w for w in pool if w["id"] != exclude_id]
    if not pool:
        return None

    pool.sort(key=lambda w: w["id"])
    return pool[db.fleet_rr_next(len(pool))]


def worker_for_account(account: dict) -> dict | None:
    """The worker that owns an account (session affinity).

    Sessions live on one worker's disk, so a job must always go back to the
    same one. Sessions are deliberately NOT replicated across the fleet: that
    would leave one customer's login material sitting on servers that serve
    other customers.
    """
    wid = account.get("worker_id")
    if wid:
        return db.get_worker(int(wid))
    return db.get_master_worker()


# --------------------------------------------------------------------------- #
# Backup hook: pull each remote worker's session files
# --------------------------------------------------------------------------- #
async def collect_worker_sessions(prefix: str = "rubika/workers") -> tuple:
    """Fetch every remote worker's session files IN PARALLEL.

    Returns ([(archive_name, bytes)], [unreachable_tags]). Sessions on a worker
    are stored under data/sessions/c<customer_id>/, and that layout is preserved
    in the archive so a restore puts each customer's sessions back in their own
    folder.
    """
    try:
        import asyncssh  # noqa: F401
    except ImportError:
        return [], ["asyncssh-missing"]

    remotes = [w for w in db.list_workers() if not is_local(w)]
    if not remotes:
        return [], []

    async def _one(w):
        files = []
        conn = None
        try:
            conn = await _with_conn(w, keepalive=False)
            sftp = await conn.start_sftp_client()
            safe_tag = str(w["tag"]).replace("#", "").replace("/", "_")
            root = f"{REMOTE_DATA}/sessions"
            try:
                customer_dirs = await sftp.listdir(root)
            except Exception:
                return w["tag"], files, True
            for cdir in customer_dirs:
                if cdir in (".", ".."):
                    continue
                cpath = f"{root}/{cdir}"
                try:
                    names = await sftp.listdir(cpath)
                except Exception:
                    continue
                for name in names:
                    if name in (".", ".."):
                        continue
                    try:
                        async with sftp.open(f"{cpath}/{name}", "rb") as fh:
                            data = await fh.read()
                        files.append((f"{prefix}/{safe_tag}/{cdir}/{name}", data))
                    except Exception:
                        continue
            return w["tag"], files, False
        except Exception:
            return w["tag"], files, True
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    results = await asyncio.gather(*[_one(w) for w in remotes],
                                   return_exceptions=True)
    collected, unreachable = [], []
    for item in results:
        if isinstance(item, Exception):
            unreachable.append("?")
            continue
        tag, files, failed = item
        collected.extend(files)
        if failed:
            unreachable.append(tag)
    return collected, unreachable


async def shutdown() -> None:
    """Cancel supervisors and close every tunnel (called on master shutdown)."""
    for wid in list(_supervisors.keys()):
        await stop_supervisor(wid)
    for wid in list(_tunnels.keys()):
        await close_tunnel(wid)
