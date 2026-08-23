"""
config.py — every setting is read from the environment / .env (never hard-coded).
================================================================================

One codebase, THREE roles selected by MODE:

    MODE=owner     -> owner_bot.py     (the central panel — only the owner)
    MODE=customer  -> customer_bot.py  (the shared customer bot)
    MODE=worker    -> worker_api.py    (headless sending node)

The owner bot and the customer bot are SEPARATE processes with SEPARATE tokens.
Workers are provisioned by the owner panel over SSH+Docker and never hold a bot
token, a customer id, or a database.

IMPORTANT (multi-tenant): the customer bot must never be able to read anything
that belongs to the owner. That is enforced structurally — the customer process
does not import central_db at all.
"""
import os


def _load_env(path: str = ".env") -> None:
    """Load .env into the environment.

    python-dotenv is used when available; otherwise a small parser handles the
    same `KEY=value` format. Keeping this dependency soft means the module (and
    therefore the test suite and any tooling) imports cleanly on a machine that
    has not installed the runtime requirements yet.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return
    except ImportError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (path, os.path.join(here, path)):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    # real environment variables always win over the file
                    os.environ.setdefault(key, value)
        except OSError:
            pass
        return


_load_env()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    return (os.getenv(name, str(default)).strip().lower()
            in ("1", "true", "yes", "on"))


# --------------------------------------------------------------------------- #
# Role
# --------------------------------------------------------------------------- #
MODE = (os.getenv("MODE", "owner") or "owner").strip().lower()

# --------------------------------------------------------------------------- #
# Telegram API + the two bot tokens
# --------------------------------------------------------------------------- #
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")

OWNER_BOT_TOKEN = os.getenv("OWNER_BOT_TOKEN", "").strip()
CUSTOMER_BOT_TOKEN = os.getenv("CUSTOMER_BOT_TOKEN", "").strip()

# The single human who owns the service.
OWNER_ID = _int("OWNER_ID")

# The PRIVATE log group. Only the owner is ever in it. Nothing in the customer
# bot's UI may ever mention that it exists.
LOG_GROUP_ID = _int("LOG_GROUP_ID")

# --------------------------------------------------------------------------- #
# Subscription (no payment subsystem at all — the owner grants time by hand)
# --------------------------------------------------------------------------- #
# Days automatically granted the first time a brand-new customer sends /start.
# 0 disables the trial (a new customer then has no access until the owner
# grants time).
TRIAL_DAYS = _int("TRIAL_DAYS", 3)

# Warn the customer this many days before their access expires.
EXPIRY_WARN_DAYS = _int("EXPIRY_WARN_DAYS", 2)

# If someone moves the server clock backwards by more than this many seconds we
# ignore it and hold the last time we saw (anti-tamper for expiry).
CLOCK_BACKWARD_TOLERANCE = _int("CLOCK_BACKWARD_TOLERANCE", 300)

# --------------------------------------------------------------------------- #
# Anti-abuse: per-customer rate limit -> automatic block
# --------------------------------------------------------------------------- #
# More than RATE_LIMIT_MAX rate-limited actions inside RATE_LIMIT_WINDOW seconds
# blocks the customer automatically and logs it. A blocked customer is then
# ignored completely (no reply, no processing) so abuse costs us nothing.
RATE_LIMIT_MAX = _int("RATE_LIMIT_MAX", 35)
RATE_LIMIT_WINDOW = _int("RATE_LIMIT_WINDOW", 120)
RATE_LIMIT_AUTOBLOCK = _bool("RATE_LIMIT_AUTOBLOCK", True)

# --------------------------------------------------------------------------- #
# Anti-spam shield: automatic offline on a /start flood
# --------------------------------------------------------------------------- #
# If more than START_FLOOD_MAX DISTINCT new users press /start inside
# START_FLOOD_WINDOW seconds, the customer bot puts itself OFFLINE (a shield) so
# a competitor cannot drown it with hundreds of fake accounts. Only the owner
# can lift the shield, from the owner panel.
START_FLOOD_MAX = _int("START_FLOOD_MAX", 20)
START_FLOOD_WINDOW = _int("START_FLOOD_WINDOW", 120)
START_FLOOD_SHIELD = _bool("START_FLOOD_SHIELD", True)

# --------------------------------------------------------------------------- #
# Session safety (the single most important correctness rule in this project)
# --------------------------------------------------------------------------- #
# Rubika allows exactly ONE live connection per session. A second connection is
# answered with AUTH_FROM_ANOTHER and the session is revoked ("the account gets
# shot"). Two defences:
#   1) busy.py — one global registry every feature must consult AND join.
#   2) the settle delay below — even a fast SEQUENTIAL reconnect on the same
#      session can be treated as a conflict, so we always wait after closing
#      before opening again.
SESSION_SETTLE_SEC = _float("SESSION_SETTLE_SEC", 5.0)

# How long a single feature may hold an account before the registry considers
# the entry stale and reclaims it (protects against a crashed task leaving an
# account marked busy forever).
BUSY_STALE_SEC = _int("BUSY_STALE_SEC", 7200)

# --------------------------------------------------------------------------- #
# Rubika sending
# --------------------------------------------------------------------------- #
MIN_DELAY = 0.2
MAX_DELAY = 10.0
DEFAULT_DELAY = _float("SEND_DELAY", 1.0)

# Marker at the end of the caption of the message in the account's Saved Messages.
FORWARD_MARKER = os.getenv("FORWARD_MARKER", "کد135").strip()

# Stop a round after this many CONSECUTIVE failed sends (reset on success).
MAX_ERRORS = _int("MAX_ERRORS", 5)

# Per-send timeout so one stuck send can never hang a whole run.
SEND_TIMEOUT = _int("SEND_TIMEOUT", 60)

# Post a progress card every this-many successful sends.
SEND_LOG_EVERY = _int("SEND_LOG_EVERY", 50)

# ---- auto-resume after an error burst ----
RESUME_WAIT = _int("RESUME_WAIT", 300)
RESUME_MAX_RETRIES = _int("RESUME_MAX_RETRIES", 2)
RESUME_UNLIMITED = _bool("RESUME_UNLIMITED", True)
RESUME_MAX_DEAD_ROUNDS = _int("RESUME_MAX_DEAD_ROUNDS", 3)

# ---- channel-style send ----
CHANNEL_MEMBER_TARGET = _int("CHANNEL_MEMBER_TARGET", 300)
CHANNEL_ADD_BATCH = _int("CHANNEL_ADD_BATCH", 80)
CHANNEL_ADD_DELAY = _float("CHANNEL_ADD_DELAY", 2.0)

# ---- campaign: on every new login, auto create channel -> forward -> send ----
CAMPAIGN_STEP_DELAY = _float("CAMPAIGN_STEP_DELAY", 5.0)

# --------------------------------------------------------------------------- #
# Contacts: import from a txt + prefix discovery
# --------------------------------------------------------------------------- #
CONTACT_MIN_DELAY = 0.1
CONTACT_MAX_DELAY = 10.0
CONTACT_ADD_DELAY = _float("CONTACT_ADD_DELAY", 1.0)
CONTACT_LOG_EVERY = _int("CONTACT_LOG_EVERY", 100)
CONTACT_DEFAULT_FIRST = os.getenv("CONTACT_DEFAULT_FIRST", "Friend").strip()
CONTACT_PROGRESS_EVERY = _float("CONTACT_PROGRESS_EVERY", 4.0)
CONTACT_REMOTE_CHUNK = _int("CONTACT_REMOTE_CHUNK", 25)

# Consecutive-error brake for contact adding, ported from the reference project.
# Rubika answers a burst of add_address_book calls with errors long before it
# revokes anything: the cure is to PAUSE and carry on, not to abandon the batch.
# Without these two knobs the worker had no brake at all, so a rate-limit burst
# was counted as N individual failures and the customer got "0 added" with no
# explanation for a list that was perfectly fine.
# ---- sponsor-channel lock ---------------------------------------------------- #
# How long a PASSED membership check is trusted. _gate runs on every button press
# and get_permissions is a network call, so without this a customer walking five
# menus pays five round-trips per channel. Only passes are cached: a user who has
# not joined is re-checked every time, because they are about to join.
# ---- tools -------------------------------------------------------------------- #
# Ceiling on one number-generation request. Generating is cheap, but a file of a
# million lines is not something anyone can use and the upload alone would time
# out — and the cap is reported to the customer rather than applied silently.
NUMGEN_MAX = _int("NUMGEN_MAX", 5000)

FORCED_JOIN_CACHE_SEC = _int("FORCED_JOIN_CACHE_SEC", 300)

CONTACT_MAX_ERRORS = _int("CONTACT_MAX_ERRORS", 5)      # consecutive errors
CONTACT_RESUME_WAIT = _int("CONTACT_RESUME_WAIT", 60)   # pause after the brake

# HARD input cap: the biggest numbers file we will accept in one go, so nobody
# can hand us a million lines and take the service down.
CONTACT_IMPORT_MAX = _int("CONTACT_IMPORT_MAX", 20000)

# ---- prefix discovery engine ----
DISCOVERY_TARGET = _int("DISCOVERY_TARGET", 150)
DISCOVERY_MAX_ATTEMPTS = _int("DISCOVERY_MAX_ATTEMPTS", 8000)
DISCOVERY_PROBE_DELAY = _float("DISCOVERY_PROBE_DELAY", 0.7)

# DAILY probe budget per customer, shared by discovery + brain + pool. Probing
# is what actually hammers Rubika, so this is the real throttle: once a customer
# has probed this many numbers today, number-building stops until tomorrow.
PROBE_DAILY_CAP = _int("PROBE_DAILY_CAP", 2000)

# --------------------------------------------------------------------------- #
# Brain / Pool (split a numbers file across accounts, add, then send)
# --------------------------------------------------------------------------- #
BRAIN_SEND_CAP = _int("BRAIN_SEND_CAP", 150)
# Hard cap on how many numbers one brain job may accept.
BRAIN_MAX_NUMBERS = _int("BRAIN_MAX_NUMBERS", 20000)

# --------------------------------------------------------------------------- #
# PV photo archive -> PDF
# --------------------------------------------------------------------------- #
PV_EXPORT_MAX_CHATS = _int("PV_EXPORT_MAX_CHATS", 1000)
PV_EXPORT_MAX_PHOTOS = _int("PV_EXPORT_MAX_PHOTOS", 2000)

# Only this many photo exports may run at once across the WHOLE service (the
# job is memory-heavy; everyone else queues).
PV_EXPORT_MAX_CONCURRENT = _int("PV_EXPORT_MAX_CONCURRENT", 1)

# Deliver a cumulative PDF every this-many photos.
PV_EXPORT_PDF_BATCH = _int("PV_EXPORT_PDF_BATCH", 100)

# Each photo is decoded+downscaled+re-encoded EXACTLY ONCE (prepare_image), so
# rebuilding the cumulative PDF stays cheap.
PV_EXPORT_PDF_QUALITY = _int("PV_EXPORT_PDF_QUALITY", 45)
PV_EXPORT_PDF_MAX_EDGE = _int("PV_EXPORT_PDF_MAX_EDGE", 1000)

# ---- the two collection modes ----
# "parallel": several downloads in flight AT ONCE over the SAME connection
#             (never a second connection — that would revoke the session).
# "safe"    : strictly one download at a time (the original behaviour).
# "auto" picks parallel and falls back to safe on trouble.
PV_EXPORT_MODE_DEFAULT = os.getenv("PV_EXPORT_MODE_DEFAULT", "auto").strip().lower()
PV_EXPORT_PARALLEL = _int("PV_EXPORT_PARALLEL", 4)
PV_EXPORT_PARALLEL_MAX = _int("PV_EXPORT_PARALLEL_MAX", 8)
# Consecutive download failures in parallel mode before falling back to safe.
PV_EXPORT_FALLBACK_ERRORS = _int("PV_EXPORT_FALLBACK_ERRORS", 5)

# Remote (worker-owned) exports are streamed by polling instead of returning
# one giant base64 response.
PV_EXPORT_POLL_SEC = _float("PV_EXPORT_POLL_SEC", 2.0)
PV_EXPORT_MAX_POLL_FAILS = _int("PV_EXPORT_MAX_POLL_FAILS", 8)

# --------------------------------------------------------------------------- #
# Tabchi (group engine) + Secretary (lives inside Tabchi)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Pool brain — several accounts working one number space in parallel
# --------------------------------------------------------------------------- #
# Numbers leased per round. Small on purpose: the target is shared, so a large
# block means the final round overshoots and probes numbers nobody needed — and
# probes are the metered resource.
POOL_BLOCK = _int("POOL_BLOCK", 50)
# A ceiling on what one job may ask for, so a mistyped target cannot queue a
# job that outlives the subscription.
POOL_MAX_TARGET = _int("POOL_MAX_TARGET", 20000)
# A hard ceiling on rounds per account per job, independent of the probe budget.
# Every other exit from the leech loop is a correctness check; this is the
# backstop for when one of those is broken, so a miscounted budget cannot become
# an account walking ten million numbers.
#
# Derived from the daily cap rather than picked out of the air: spending the whole
# allowance takes PROBE_DAILY_CAP / POOL_BLOCK rounds, so twice that is generous
# for any legitimate job and still tight enough to actually bound a runaway. A
# fixed large number would be fifty times looser than the real limit and would
# not bound anything worth bounding.
POOL_MAX_ROUNDS = _int("POOL_MAX_ROUNDS",
                       max(20, (PROBE_DAILY_CAP // max(1, POOL_BLOCK)) * 2))

# --------------------------------------------------------------------------- #
# SSH timeouts — two budgets, because the two uses are nothing alike
# --------------------------------------------------------------------------- #
# The health tunnel: tight, so a dead link is noticed within seconds.
SSH_CONNECT_TIMEOUT = _int("SSH_CONNECT_TIMEOUT", 10)
# One-shot admin work (provision / update / backup): generous. A server that is
# mid-docker-build takes far longer than ten seconds to answer an SSH login, and
# sharing the tunnel's budget is how provisioning died with a bare TimeoutError.
SSH_ADMIN_CONNECT_TIMEOUT = _int("SSH_ADMIN_CONNECT_TIMEOUT", 60)
# Per-step ceilings, so one wedged command cannot hang provisioning forever.
SSH_STEP_TIMEOUT = _int("SSH_STEP_TIMEOUT", 600)          # apt, git clone
# A docker build on a small VPS legitimately takes many minutes.
SSH_BUILD_TIMEOUT = _int("SSH_BUILD_TIMEOUT", 2400)       # 40 minutes
# How often the provisioning card may be redrawn. A docker build prints thousands
# of lines and Telegram rate-limits edits, so progress is throttled rather than
# streamed line by line.
PROVISION_REPORT_EVERY = _float("PROVISION_REPORT_EVERY", 4.0)
# Free megabytes a server needs before a build is worth attempting. The base image
# is ~150MB, the wheels a few hundred more, and pip's temp files a few hundred
# again. Below this the build dies deep inside pip with "[Errno 28] No space left
# on device", where the real cause is easy to miss.
WORKER_MIN_DISK_MB = _int("WORKER_MIN_DISK_MB", 2500)
# Docker Hub answers 502 often enough to matter. A transient registry failure is
# not a broken server, so the build is retried rather than reported as a defeat.
WORKER_BUILD_ATTEMPTS = _int("WORKER_BUILD_ATTEMPTS", 3)

TABCHI_MIN_INTERVAL = _int("TABCHI_MIN_INTERVAL", 10)
TABCHI_MAX_INTERVAL = _int("TABCHI_MAX_INTERVAL", 86400)
TABCHI_DEFAULT_INTERVAL = _int("TABCHI_DEFAULT_INTERVAL", 1800)
# Small random pause between groups so one pass isn't a burst.
TABCHI_GROUP_DELAY_MIN = _float("TABCHI_GROUP_DELAY_MIN", 0.5)
TABCHI_GROUP_DELAY_MAX = _float("TABCHI_GROUP_DELAY_MAX", 2.0)
# Stagger between accounts, so several accounts never post into the same group
# at the same moment.
TABCHI_ACCOUNT_STAGGER = _float("TABCHI_ACCOUNT_STAGGER", 30.0)
# Mute a group after this many consecutive failures.
TABCHI_GROUP_MAX_FAILS = _int("TABCHI_GROUP_MAX_FAILS", 3)
# Pause between joining each group from the link list.
GROUP_JOIN_DELAY = _float("GROUP_JOIN_DELAY", 3.0)
# How often the per-customer tabchi summary card is posted.
TABCHI_SUMMARY_INTERVAL = _int("TABCHI_SUMMARY_INTERVAL", 1200)

SECRETARY_INTERVAL = _int("SECRETARY_INTERVAL", 600)
SECRETARY_MIN_INTERVAL = _int("SECRETARY_MIN_INTERVAL", 60)
SECRETARY_MAX_INTERVAL = _int("SECRETARY_MAX_INTERVAL", 3600)
SECRETARY_REPLY_DELAY = _float("SECRETARY_REPLY_DELAY", 2.0)

# --------------------------------------------------------------------------- #
# Telegram section
# --------------------------------------------------------------------------- #
TG_FLOOD_MAX_WAIT = _int("TG_FLOOD_MAX_WAIT", 300)
TG_SEND_DELAY_MIN = _float("TG_SEND_DELAY_MIN", 0.2)
TG_SEND_DELAY_MAX = _float("TG_SEND_DELAY_MAX", 1.0)
TG_SEND_DELAY = _float("TG_SEND_DELAY", 0.2)
# Human-like "typing…" before each message. OFF by default: it used to default
# to 0.4–2.0s and was ADDED ON TOP of the send delay, so a customer who set the
# speed to 0.2s still saw one message roughly every three seconds and thought the
# bot was broken. The speed setting is now the real gap. Turn this on only if you
# want the extra camouflage and accept that it slows sending.
TG_TYPING_MIN = _float("TG_TYPING_MIN", 0.0)
TG_TYPING_MAX = _float("TG_TYPING_MAX", 0.0)
TG_STATS_REFRESH = _float("TG_STATS_REFRESH", 5.0)
# Accounts shown per page in every paginated account list.
ACC_PAGE_SIZE = _int("ACC_PAGE_SIZE", 15)

# --------------------------------------------------------------------------- #
# Health / self-heal engine
# --------------------------------------------------------------------------- #
HEALTH_ENGINE_ENABLED = _bool("HEALTH_ENGINE_ENABLED", True)
HEALTH_ENGINE_INTERVAL = _int("HEALTH_ENGINE_INTERVAL", 10800)   # 3 hours
HEALTH_ENGINE_AUTODISABLE_DEAD = _bool("HEALTH_ENGINE_AUTODISABLE_DEAD", True)
# Never sweep at boot: resumed jobs are still being re-registered in the busy
# registry, and a sweep that beats them there probes accounts that are mid-job.
HEALTH_ENGINE_WARMUP = _int("HEALTH_ENGINE_WARMUP", 300)
# Pause between accounts, so a sweep is a trickle rather than a spike.
HEALTH_ACCOUNT_GAP = _float("HEALTH_ACCOUNT_GAP", 1.5)
# Tell the customer, in their own PV, when one of their accounts dies.
NOTIFY_CUSTOMER_ON_DEAD = _bool("NOTIFY_CUSTOMER_ON_DEAD", True)
# Alert the owner when this many accounts die inside DEAD_BURST_WINDOW seconds
# (an early warning that a worker's IP is being throttled).
DEAD_BURST_MAX = _int("DEAD_BURST_MAX", 10)
DEAD_BURST_WINDOW = _int("DEAD_BURST_WINDOW", 3600)

# --------------------------------------------------------------------------- #
# Workers / distributed mode
# --------------------------------------------------------------------------- #
# Fernet key that encrypts worker SSH passwords, API tokens and portable
# sessions at rest. Generate once with:
#   python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()

# The repo a worker is built from. MUST point at THIS project — pointing it at
# an older repo makes "update all workers" silently downgrade the fleet.
GIT_REPO_URL = os.getenv(
    "GIT_REPO_URL", "https://github.com/shootingv818/Makemyno").strip()
# The branch a worker clones. NOT "main": main carries only a README, so a worker
# built from it would clone a repository with no Dockerfile and no code — a silent
# way to make every provision fail for a reason that looks like anything else.
# Whatever branch the master is running is the branch its workers must run.
GIT_BRANCH = os.getenv("GIT_BRANCH", "feat/multi-tenant-foundation").strip()

WORKER_API_PORT = _int("WORKER_API_PORT", 8765)
WORKER_BIND_HOST = os.getenv("WORKER_BIND_HOST", "0.0.0.0").strip()
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "").strip()
MASTER_AS_WORKER = _bool("MASTER_AS_WORKER", True)

HEALTH_URL = os.getenv(
    "HEALTH_URL", "https://upmessenger490.iranlms.ir/UploadFile.ashx").strip()
HEALTH_TIMEOUT = _int("HEALTH_TIMEOUT", 15)
HEALTH_INTERVAL = _int("HEALTH_INTERVAL", 1800)

PING_GREEN_MS = _int("PING_GREEN_MS", 800)
PING_YELLOW_MS = _int("PING_YELLOW_MS", 2000)

# Close an account's warm connection after this many idle seconds.
CONN_IDLE_CLOSE_SEC = _int("CONN_IDLE_CLOSE_SEC", 600)

# --------------------------------------------------------------------------- #
# Backup (owner only — sessions ONLY, encrypted)
# --------------------------------------------------------------------------- #
# 0 disables the periodic backup.
BACKUP_INTERVAL = _int("BACKUP_INTERVAL", 43200)   # 12 hours

# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()
VERSION = os.getenv("VERSION", "V1")
MAINTENANCE_DEFAULT = _bool("MAINTENANCE_DEFAULT", False)
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()


# --------------------------------------------------------------------------- #
# Clamps
# --------------------------------------------------------------------------- #
def clamp_delay(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY
    return max(MIN_DELAY, min(MAX_DELAY, value))


def clamp_contact_delay(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return CONTACT_ADD_DELAY
    return max(CONTACT_MIN_DELAY, min(CONTACT_MAX_DELAY, value))


def clamp_discovery_delay(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DISCOVERY_PROBE_DELAY
    return max(0.1, min(10.0, v))


def clamp_tabchi_interval(value) -> int:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return TABCHI_DEFAULT_INTERVAL
    return max(TABCHI_MIN_INTERVAL, min(TABCHI_MAX_INTERVAL, v))


def clamp_secretary_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return SECRETARY_INTERVAL
    return max(SECRETARY_MIN_INTERVAL, min(SECRETARY_MAX_INTERVAL, value))


def clamp_tg_delay(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return TG_SEND_DELAY
    return max(TG_SEND_DELAY_MIN, min(TG_SEND_DELAY_MAX, v))


def clamp_pv_parallel(value) -> int:
    """Keep the photo-download concurrency inside [1, PV_EXPORT_PARALLEL_MAX]."""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return PV_EXPORT_PARALLEL
    return max(1, min(PV_EXPORT_PARALLEL_MAX, v))


# --------------------------------------------------------------------------- #
# Validation per role
# --------------------------------------------------------------------------- #
def _common_problems() -> list:
    problems = []
    if not API_ID:
        problems.append("API_ID")
    if not API_HASH:
        problems.append("API_HASH")
    if not OWNER_ID:
        problems.append("OWNER_ID")
    if not LOG_GROUP_ID:
        problems.append("LOG_GROUP_ID")
    return problems


def validate_owner() -> list:
    problems = _common_problems()
    if not OWNER_BOT_TOKEN:
        problems.append("OWNER_BOT_TOKEN")
    if not WORKER_SECRET:
        problems.append("WORKER_SECRET")
    return problems


def validate_customer() -> list:
    problems = _common_problems()
    if not CUSTOMER_BOT_TOKEN:
        problems.append("CUSTOMER_BOT_TOKEN")
    if not WORKER_SECRET:
        problems.append("WORKER_SECRET")
    return problems


def validate_worker() -> list:
    return [] if WORKER_API_TOKEN else ["WORKER_API_TOKEN"]


# --------------------------------------------------------------------------- #
# Timezone-aware "now" — every card timestamp uses it, so a worker on a foreign
# server still prints Iran time.
# --------------------------------------------------------------------------- #
def _tzinfo():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        try:
            import pytz
            return pytz.timezone(TIMEZONE)
        except Exception:
            return None


def now_dt():
    from datetime import datetime
    tz = _tzinfo()
    return datetime.now(tz) if tz else datetime.now()


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return now_dt().strftime("%Y-%m-%d")
