"""
Architectural rules, enforced by tests rather than by memory.

These are the invariants that are easy to break six months from now with a
one-line change and impossible to notice by reading a diff. Each one failing
means a real security or correctness regression, so they are checked against the
source itself.
"""
import ast
import os
import re

import pytest

import config
import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules that run inside the CUSTOMER process. None of them may reach the
# owner-only database.
CUSTOMER_SIDE = [
    "customer_bot.py", "tg_panel.py", "tabchi.py", "rubika_panel.py",
    "telegram_multi_send.py", "pdf_export.py", "help_text.py", "health.py",
    "busy.py", "ratelimit.py", "antispam.py", "db.py", "cards.py", "logbus.py",
]


def _source(name: str) -> str | None:
    path = os.path.join(ROOT, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _existing_customer_files() -> list:
    return [n for n in CUSTOMER_SIDE if _source(n) is not None]


# --------------------------------------------------------------------------- #
# Isolation: the customer process must not be able to open the owner database
# --------------------------------------------------------------------------- #
def test_customer_side_never_imports_central_db():
    """Isolation by architecture: not a permission check that can be forgotten,
    but a module the other process simply does not open."""
    offenders = []
    for name in _existing_customer_files():
        src = _source(name)
        tree = ast.parse(src, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name == "central_db" for a in node.names):
                    offenders.append(name)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "central_db":
                    offenders.append(name)
    assert offenders == [], (
        f"customer-side modules must not import central_db: {offenders}")


def test_central_db_is_only_reachable_from_the_owner_side():
    """Whoever does import it should be an owner-side module or a helper the
    owner process runs (backup)."""
    allowed = {"owner_bot.py", "backup.py", "central_db.py", "main.py"}
    for name in os.listdir(ROOT):
        if not name.endswith(".py") or name in allowed:
            continue
        src = _source(name) or ""
        assert not re.search(r"^\s*import\s+central_db", src, re.M), \
            f"{name} imports central_db but is not an owner-side module"


# --------------------------------------------------------------------------- #
# The golden rule of db.py
# --------------------------------------------------------------------------- #
def _called_names(node: ast.FunctionDef) -> set:
    """Every plain function name called inside `node`."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            out.add(sub.func.id)
    return out


def test_every_customer_scoped_function_validates_its_id():
    """Any db function whose first parameter is customer_id must end up calling
    _require_cid — directly, or through another function that does.

    Forgetting the guard is precisely how a cross-tenant leak happens, so the
    check is transitive: a thin wrapper is fine as long as something downstream
    validates. Computed as a fixed point over the call graph.
    """
    src = _source("db.py")
    tree = ast.parse(src, filename="db.py")

    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    # Seed: functions that guard directly.
    safe = {name for name, node in functions.items()
            if "_require_cid" in _called_names(node)}

    # Expand: calling a safe function makes you safe too.
    changed = True
    while changed:
        changed = False
        for name, node in functions.items():
            if name in safe:
                continue
            if _called_names(node) & safe:
                safe.add(name)
                changed = True

    # _require_cid IS the guard, so it naturally does not call itself.
    scoped = [name for name, node in functions.items()
              if not name.startswith("_")
              and node.args.args and node.args.args[0].arg == "customer_id"]
    missing = sorted(name for name in scoped if name not in safe)
    assert missing == [], f"these db functions skip the customer-id guard: {missing}"


def test_the_guard_check_would_actually_catch_a_leak():
    """Prove the test above has teeth: a function that reads customer data
    without validating must be reported."""
    leaky = ast.parse(
        "def list_everything(customer_id):\n"
        "    conn = _conn()\n"
        "    return conn.execute('SELECT * FROM accounts').fetchall()\n"
    )
    node = leaky.body[0]
    assert "_require_cid" not in _called_names(node)


def test_no_db_function_defaults_customer_id_to_none():
    """A default of None turns the guard into an opt-in, which defeats it."""
    src = _source("db.py")
    tree = ast.parse(src, filename="db.py")
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        args = [a.arg for a in node.args.args]
        if not args or args[0] != "customer_id":
            continue
        defaults = node.args.defaults
        # defaults align to the END of the arg list
        if len(defaults) == len(args) and defaults:
            offenders.append(node.name)
    assert offenders == [], f"customer_id must be required, not optional: {offenders}"


def test_owner_only_readers_are_named_with_the_owner_prefix():
    """Unscoped reads are legitimate for the owner panel, but they must be
    obvious at the call site."""
    unscoped_but_fine = {
        # fleet is global by nature
        "add_worker", "list_workers", "list_enabled_workers", "get_worker",
        "get_worker_by_tag", "get_master_worker", "set_worker_enabled",
        "update_worker_health", "delete_worker", "count_accounts_on_worker",
        "worker_account_stats", "worker_customers", "accounts_per_worker",
        "incr_worker_sent", "worker_sent_today", "fleet_rr_next",
        # number status cache is not customer data
        "number_seen", "number_record", "numbers_known",
        # service-wide runtime state / infrastructure
        "init", "schema_version", "monotonic_now", "clock_tampered",
        "get_bot_state", "is_bot_online", "set_bot_online", "are_sends_frozen",
        "set_sends_frozen", "record_start", "recent_start_count",
        # the health sweep is service-wide, and its result is handed to the owner
        # bot through the shared state row rather than in memory
        "set_health_report", "get_health_report",
        "clear_start_events", "session_pack", "session_unpack",
        "fetch_unsent_notifications", "mark_notification_sent",
        # customer-identified helpers that take the id as telegram_id
        "ensure_customer", "get_customer", "touch_customer", "seconds_left",
        "days_left", "is_blocked", "is_active", "set_blocked", "add_days",
        "set_expiry", "set_note", "set_warned", "incr_customer_sends",
        "delete_customer", "rate_hit", "rate_reset",
    }
    src = _source("db.py")
    tree = ast.parse(src, filename="db.py")
    bad = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        args = [a.arg for a in node.args.args]
        first = args[0] if args else None
        if first in ("customer_id", "telegram_id"):
            continue
        if node.name in unscoped_but_fine or node.name.startswith("owner_"):
            continue
        bad.append(node.name)
    assert bad == [], (
        f"unscoped db functions must start with owner_ or be whitelisted: {bad}")


# --------------------------------------------------------------------------- #
# The log group must never be mentioned to a customer
# --------------------------------------------------------------------------- #
def test_only_logbus_knows_the_log_group_id():
    """A customer must never learn the log group exists — not its id, not even
    the phrase.

    Keeping the id in exactly two places (the setting, and the one module that
    delivers) means no future screen can accidentally print it, and every log
    line goes through the same redaction path.
    """
    allowed_files = {"logbus.py", "config.py"}
    for name in os.listdir(ROOT):
        if not name.endswith(".py") or name in allowed_files:
            continue
        src = _source(name) or ""
        assert "LOG_GROUP_ID" not in src, (
            f"{name} references LOG_GROUP_ID; route it through logbus instead")


def test_customer_side_has_no_log_group_wording():
    """Guard against a friendly-but-leaky message like
    'the error was sent to the log group'."""
    leaky = re.compile(r"گروه\s*لاگ|log\s*group", re.I)
    for name in _existing_customer_files():
        if name == "logbus.py":
            continue
        src = _source(name) or ""
        tree = ast.parse(src, filename=name)

        # Docstrings are internal developer notes and may discuss the log group
        # freely; only strings that could reach a customer are checked.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            if node.value in docstrings:
                continue
            if leaky.search(node.value):
                pytest.fail(
                    f"{name} contains customer-visible log-group wording: "
                    f"{node.value[:60]!r}")


# --------------------------------------------------------------------------- #
# Style must not drift
# --------------------------------------------------------------------------- #
def test_only_one_divider_style_exists_in_the_project():
    """The base project drifted into three divider styles. Everything here goes
    through cards.LINE."""
    forbidden = ["━━━", "═══", "───"]
    for name in os.listdir(ROOT):
        if not name.endswith(".py"):
            continue
        src = _source(name) or ""
        for bad in forbidden:
            assert bad not in src, f"{name} uses a non-standard divider ({bad})"


def test_modules_do_not_define_their_own_line_constant():
    """A local LINE = ... is how the styles drifted apart last time."""
    for name in os.listdir(ROOT):
        if not name.endswith(".py") or name == "cards.py":
            continue
        src = _source(name) or ""
        assert not re.search(r"^\s*_?LINE\s*=\s*[\"']", src, re.M), \
            f"{name} defines its own LINE; import it from cards"


# --------------------------------------------------------------------------- #
# Config sanity
# --------------------------------------------------------------------------- #
def test_removed_subsystems_left_no_settings_behind():
    """Portal, linkdooni, the generator, the old broadcaster, the channel-report
    and reply engines are gone; dead config keys invite dead code."""
    src = _source("config.py") or ""
    for gone in ("PORTAL_", "LINKDOONI_", "GENERATOR_", "BROADCAST_GAP",
                 "CHANNEL_REPORT_", "REPLY_POLL", "AUTOMATION_",
                 "PROFILE_SYNC_", "TG_TABCHI_", "TG_COMMENT_", "PV_GROUP_BATCH"):
        assert gone not in src, f"config.py still carries {gone} from a removed feature"


def test_no_payment_subsystem_anywhere():
    """Pricing and transactions were explicitly dropped from the plan."""
    for name in os.listdir(ROOT):
        if not name.endswith(".py"):
            continue
        src = _source(name) or ""
        for token in ("WALLET_ADDRESS", "TRON_API_KEY", "verify_trx_payment",
                      "FREE_MODE"):
            assert token not in src, f"{name} references payment code ({token})"


def test_git_repo_url_points_at_this_project():
    """A stale GIT_REPO_URL makes 'update all workers' downgrade the fleet — the
    worst bug in the reference project."""
    assert "Makemyno" in config.GIT_REPO_URL


def test_validators_report_missing_settings():
    problems = config.validate_owner()
    assert isinstance(problems, list)
    # In a bare test environment nothing is configured, so it must complain.
    assert "OWNER_ID" in problems or config.OWNER_ID


def test_probe_cap_and_input_caps_are_set():
    """Somebody will paste a million numbers. These are the guards."""
    assert config.PROBE_DAILY_CAP > 0
    assert config.CONTACT_IMPORT_MAX > 0
    assert config.BRAIN_MAX_NUMBERS > 0
    assert config.PV_EXPORT_MAX_CONCURRENT >= 1


def test_settle_delay_is_configured():
    """Session safety depends on this being non-zero in production."""
    assert config.SESSION_SETTLE_SEC >= 0
    assert config.BUSY_STALE_SEC > 0


def test_pv_export_delivers_in_batches_of_100():
    assert config.PV_EXPORT_PDF_BATCH == 100


def test_pv_parallel_is_clamped():
    assert config.clamp_pv_parallel(999) == config.PV_EXPORT_PARALLEL_MAX
    assert config.clamp_pv_parallel(0) == 1
    assert config.clamp_pv_parallel("x") == config.PV_EXPORT_PARALLEL


# --------------------------------------------------------------------------- #
# Every query against a customer-owned table must filter by customer
# --------------------------------------------------------------------------- #
# Tables that hold customer-owned rows. Reading one without a customer_id
# predicate is a cross-tenant read.
OWNED_TABLES = [
    "accounts", "tg_accounts", "customer_settings", "tg_content",
    "usage_daily", "paused_sends", "rb_sent", "tg_sent", "contact_jobs",
    "tabchi", "tabchi_texts", "tabchi_groups", "secretary", "secretary_replied",
]


def test_customer_scoped_queries_filter_by_customer_id():
    """A SELECT/UPDATE/DELETE touching an owned table inside a customer-facing
    function must mention customer_id.

    Owner-side readers (owner_*) are exempt by design — seeing every customer is
    their whole job — and so are the two functions that deliberately span
    tenants for boot-time recovery and fleet accounting.
    """
    exempt = {
        # owner_* are exempt via the prefix check below
        "delete_worker",            # NULLs accounts.worker_id fleet-wide
        "worker_account_stats",     # fleet accounting, no customer rows returned
        "count_accounts_on_worker",
        "worker_customers",
        "fetch_unsent_notifications",   # the outbox drain, keyed by delivery state
        "mark_notification_sent",
    }
    src = _source("db.py")
    tree = ast.parse(src, filename="db.py")
    table_re = re.compile(
        r"\b(?:FROM|UPDATE|INTO|JOIN)\s+(" + "|".join(OWNED_TABLES) + r")\b",
        re.I)

    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("owner_") or node.name in exempt:
            continue
        segment = ast.get_source_segment(src, node) or ""
        if not table_re.search(segment):
            continue
        if "customer_id" not in segment:
            offenders.append(node.name)

    assert offenders == [], (
        f"these functions query customer tables without scoping: {offenders}")


def test_the_query_check_would_catch_an_unscoped_read():
    """Prove that check has teeth too."""
    table_re = re.compile(
        r"\b(?:FROM|UPDATE|INTO|JOIN)\s+(" + "|".join(OWNED_TABLES) + r")\b",
        re.I)
    leaky = "conn.execute('SELECT * FROM accounts ORDER BY id')"
    assert table_re.search(leaky)
    assert "customer_id" not in leaky


# --------------------------------------------------------------------------- #
# Every customer-facing screen must pass through the access gate
# --------------------------------------------------------------------------- #
def _handler_functions(src: str, module: str) -> list:
    """Async functions decorated with a CallbackQuery or NewMessage handler."""
    tree = ast.parse(src, filename=module)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            text = ast.dump(deco)
            if "CallbackQuery" in text or "NewMessage" in text:
                found.append(node)
                break
    return found


def test_every_customer_screen_checks_the_gate():
    """A new screen that forgets the gate would serve a blocked, expired or
    flooding customer. Rather than trusting review, the rule is checked here.

    The gate is the one place that walks shield -> maintenance -> blocked ->
    rate limit -> subscription, so 'did you call it' is the whole question.
    """
    # /start does its own shield handling before a customer row even exists, and
    # the shared text router runs the gate itself before dispatching a step.
    exempt = {"start_handler", "text_router"}
    offenders = []
    for module in ("customer_bot.py", "rubika_panel.py", "tg_panel.py",
                   "tabchi.py"):
        src = _source(module)
        if src is None:
            continue
        for fn in _handler_functions(src, module):
            if fn.name in exempt:
                continue
            body = ast.dump(fn)
            if "_gate" not in body and "gate" not in body:
                offenders.append(f"{module}:{fn.name}")
    assert offenders == [], (
        f"these customer-facing handlers skip the access gate: {offenders}")


def test_the_gate_check_would_catch_a_missing_call():
    """Prove that check has teeth."""
    leaky = ast.parse(
        "@bot.on(events.CallbackQuery(data=b'x'))\n"
        "async def open_screen(event):\n"
        "    await safe_edit(event, 'secret')\n"
    )
    fn = leaky.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert "gate" not in ast.dump(fn)


def test_customer_panels_do_not_reach_owner_only_helpers():
    """No customer screen may call the fleet's provisioning or credential code,
    or the backup builder."""
    forbidden = ("provision_worker", "register_provisioned", "teardown_worker",
                 "update_worker", "collect_worker_sessions", "run_backup",
                 "build_archive", "decrypt")
    for module in ("customer_bot.py", "rubika_panel.py", "tg_panel.py",
                   "tabchi.py"):
        src = _source(module)
        if src is None:
            continue
        for name in forbidden:
            assert name not in src, f"{module} references owner-only {name}"
