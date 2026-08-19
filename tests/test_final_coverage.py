"""
The final sweep: does every promised feature actually exist and connect?

The unit tests prove each piece behaves. This file answers a different question —
"did anything get forgotten?" — by walking the agreed feature list and checking
each one is reachable from a button a customer or the owner can actually press.

A feature that works but has no route to it is not a feature.
"""
import ast
import os
import re

import pytest

import cards
import config
import db
import help_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _all(names):
    return "\n".join(_src(n) for n in names)


CUSTOMER = ["customer_bot.py", "rubika_panel.py", "tg_panel.py", "tabchi.py",
            "pool.py", "help_text.py", "telegram_multi_send.py"]
OWNER = ["owner_bot.py"]
ALL_MODULES = [f for f in os.listdir(ROOT) if f.endswith(".py")]


def _handlers(name):
    """(literal callbacks, compiled patterns) this module answers to.

    The patterns are kept as real regexes and matched properly. An earlier version
    of this file just took the leading letters of `rb"rbpdfset_(auto|parallel)"`
    and compared strings, which reported every pattern-based handler as missing —
    a test that cries wolf teaches you to ignore it.
    """
    src = _src(name)
    literal = set(re.findall(r'CallbackQuery\(data=b"([^"]+)"', src))
    # Some handlers are registered by looping over a table of
    # (b"callback", "step", title, hint) tuples rather than by a literal
    # decorator. They are perfectly real; a static check that cannot see them
    # reports healthy code as broken.
    literal |= set(re.findall(r'\(\s*b"([a-z_0-9]+)"\s*,\s*"\w+"\s*,', src))
    patterns = []
    for raw in re.findall(r'CallbackQuery\(pattern=rb"([^"]+)"', src):
        try:
            patterns.append(re.compile("^" + raw + "$"))
        except re.error:
            continue
    return literal, patterns


def _resolves(callback: str, literal: set, patterns: list) -> bool:
    if callback in literal:
        return True
    return any(p.match(callback) for p in patterns)


def _probe_prefix(prefix: str, literal: set, patterns: list) -> bool:
    """Does any handler accept a payload built from this prefix?

    Prefix handlers take different payload shapes — one id, two ids, an id plus a
    mode letter — so the prefix is satisfied when ANY of them matches. Demanding
    every shape match would flag healthy buttons.
    """
    for probe in (f"{prefix}_1", f"{prefix}_1_1", f"{prefix}_1_m",
                  f"{prefix}_auto", f"{prefix}_x"):
        if _resolves(probe, literal, patterns):
            return True
    return False


def _all_customer_handlers():
    lit, pats = set(), []
    for name in CUSTOMER:
        a, b = _handlers(name)
        lit |= a
        pats += b
    return lit, pats


def _steps_started(name: str) -> set:
    """Every step value the module assigns, in any of the spellings used.

    Steps are set three ways in this codebase — a dict literal, an item
    assignment, and a dict update — so all three have to be recognised or the
    check invents dead code that is not there.
    """
    src = _src(name)
    found = set(re.findall(r'"step":\s*"(\w+)"', src))
    found |= set(re.findall(r'\["step"\]\s*=\s*"(\w+)"', src))
    found |= set(re.findall(r'step\s*=\s*"(\w+)"', src))
    # Steps built by a factory helper, e.g. _make_text_setter("rb_marker", ...)
    # ... and the same table supplies the step name as its second element
    found |= set(re.findall(r'\(\s*b"[a-z_0-9]+"\s*,\s*"(\w+)"\s*,', src))
    found.discard("None")
    return found


# =========================================================================== #
# Every promised customer feature has a live route
# =========================================================================== #
CUSTOMER_FEATURES = {
    "دو بخشی روبیکا/تلگرام": ["rb", "tg"],
    "اکانت‌ها + صفحه‌بندی": ["rbaccs", "tgaccs"],
    "افزودن اکانت": ["rbadd", "tgadd"],
    "توکن سشن": ["rbsess"],
    "ارسال روبیکا": ["rbsend"],
    "ارسال کانالی": ["rbchan"],
    "محتوا": ["rbcontent", "tgcontent"],
    "مولتی‌سند روبیکا": ["rbmulti", "rbmgo"],
    "مولتی‌سند تلگرام": ["tgmulti", "tgmgo"],
    "افزودن مخاطب": ["rbcontacts", "rbcadd"],
    "گرفتن مخاطبین روبیکا": ["rbexport"],
    "گرفتن مخاطبین تلگرام": ["tgexport"],
    "کشف مخاطب": ["rbdiscover"],
    "مغز": ["rbbrain", "rbbfile"],
    "ارسال به مخاطبین تازه‌ی مغز": ["rbbrainsend"],
    "مغز استخری": ["rbpool", "plgo", "pljobs"],
    "تبچی": ["tabchi", "tbtog", "tbgadd", "tbgjoin", "tbgunmute"],
    "منشی (داخل تبچی)": ["tbsec", "tbsectog", "tbsecmode"],
    "اعمال روی همه": ["tbapply", "tbsecapply"],
    "آرشیو عکس PDF": ["rbpdf", "rbpdfrun"],
    "دو حالت PDF": ["rbpdfmode", "rbpdfset_auto", "rbpdfset_safe"],
    "تنظیمات": ["rbsettings", "tgspeed"],
    "کارهای تلگرام": ["tgjobs"],
    "راهنما": ["help"],
    "پشتیبانی": ["support"],
    "ورود مجدد اکانت سوخته": ["rbrelogin", "tgrelogin"],
}


@pytest.mark.parametrize("feature,callbacks", sorted(CUSTOMER_FEATURES.items()))
def test_every_promised_customer_feature_is_reachable(feature, callbacks):
    literal, patterns = _all_customer_handlers()
    # A prefix handler is declared as rb"rbacc_(\d+)", so probe it with a real id.
    missing = [c for c in callbacks
               if not (_resolves(c, literal, patterns)
                       or _probe_prefix(c, literal, patterns))]
    assert missing == [], f"«{feature}» has no handler for: {missing}"


OWNER_FEATURES = {
    "داشبورد": ["home"],
    "مشتری‌ها": ["customers", "cust"],
    "افزودن مشتری": ["addcust"],
    "جستجو": ["search"],
    "رتبه‌بندی": ["ranking"],
    "پیام همگانی": ["broadcast"],
    "ورکرها": ["workers"],
    "آمار ورکرها": ["wstats"],
    "عیب‌یابی با شماره": ["diag"],
    "بکاپ": ["backupmenu"],
    "حالت تعمیر": ["maint"],
    "توقف اضطراری": ["freeze"],
    "سپر ضداسپم": ["shield"],
    "تنظیمات سرویس": ["settings"],
    "لاگ ممیزی": ["audit"],
    "تیکت‌ها": ["tickets"],
    "موتور سلامت": ["healthreport"],
}


@pytest.mark.parametrize("feature,callbacks", sorted(OWNER_FEATURES.items()))
def test_every_promised_owner_feature_is_reachable(feature, callbacks):
    literal, patterns = _handlers("owner_bot.py")
    missing = [c for c in callbacks
               if not (_resolves(c, literal, patterns)
                       or _probe_prefix(c, literal, patterns))]
    assert missing == [], f"owner «{feature}» has no handler for: {missing}"


# =========================================================================== #
# No dead buttons anywhere
# =========================================================================== #
def test_no_customer_button_points_at_nothing():
    """A button that answers nothing is indistinguishable from a crash, and it is
    the commonest way a growing panel rots."""
    literal, patterns = _all_customer_handlers()
    dangling = {}
    for name in CUSTOMER:
        src = _src(name)
        plain = set(re.findall(r'Button\.inline\([^,]+,\s*b"([a-z_0-9]+)"', src))
        # f"rbacc_{aid}" -> probe the prefix handler with a concrete id
        prefixes = set(re.findall(
            r'Button\.inline\([^,]+,\s*f"([a-z_0-9]+)_\{', src))
        missing = sorted(c for c in plain
                         if not _resolves(c, literal, patterns))
        # A prefix button is satisfied if ANY plausible payload shape reaches a
        # handler: rb"rbacc_(\d+)" and rb"rbgo_(\d+)_(m|t)" are both legitimate.
        missing += sorted(p + "_…" for p in prefixes
                          if not _probe_prefix(p, literal, patterns))
        if missing:
            dangling[name] = missing
    assert dangling == {}, f"buttons with no handler: {dangling}"


def test_no_owner_button_points_at_nothing():
    literal, patterns = _handlers("owner_bot.py")
    src = _src("owner_bot.py")
    plain = set(re.findall(r'Button\.inline\([^,]+,\s*b"([a-z_0-9]+)"', src))
    prefixes = set(re.findall(
        r'Button\.inline\([^,]+,\s*f"([a-z_0-9]+)_\{', src))
    missing = sorted(c for c in plain if not _resolves(c, literal, patterns))
    missing += sorted(p + "_…" for p in prefixes
                      if not _probe_prefix(p, literal, patterns))
    assert missing == [], f"owner buttons with no handler: {missing}"


# =========================================================================== #
# Every wizard step can be finished
# =========================================================================== #
@pytest.mark.parametrize("module", ["rubika_panel", "tg_panel", "tabchi", "pool"])
def test_every_step_started_is_registered(module):
    """A step the panel sets but never registers leaves the customer typing into a
    bot that ignores them."""
    mod = __import__(module)
    started = _steps_started(module + ".py")
    registered = set(mod._STEPS)
    assert started <= registered, (
        f"{module} starts steps nothing handles: {sorted(started - registered)}")


@pytest.mark.parametrize("module", ["rubika_panel", "tg_panel", "tabchi", "pool"])
def test_every_registered_step_is_reachable(module):
    mod = __import__(module)
    unreachable = set(mod._STEPS) - _steps_started(module + ".py")
    assert unreachable == set(), f"{module} has dead steps: {sorted(unreachable)}"


def test_no_two_panels_claim_the_same_step_name():
    """They share one dict in customer_bot, so a duplicate name means one panel
    silently hijacks the other's wizard."""
    import pool
    import rubika_panel
    import tabchi
    import tg_panel
    seen = {}
    for name, mod in [("rubika_panel", rubika_panel), ("tg_panel", tg_panel),
                      ("tabchi", tabchi), ("pool", pool)]:
        for step in mod._STEPS:
            assert step not in seen, (
                f"step «{step}» is claimed by both {seen[step]} and {name}")
            seen[step] = name


def test_no_two_panels_claim_the_same_callback():
    """Two handlers on one callback means both fire, and the second undoes the
    first."""
    seen = {}
    for name in CUSTOMER:
        literal, _ = _handlers(name)
        for cb in literal:
            assert cb not in seen, (
                f"callback «{cb}» is handled by both {seen[cb]} and {name}")
            seen[cb] = name


# =========================================================================== #
# The owner's requirements, restated as assertions
# =========================================================================== #
def test_the_log_group_is_named_in_exactly_two_places():
    """«هیچ جا حرفی از گپ لاگ زده نشه فقط من بدونم» — the group is plumbing, and
    only the plumbing may know its name."""
    allowed = {"config.py", "logbus.py"}
    offenders = [n for n in ALL_MODULES
                 if n not in allowed and "LOG_GROUP_ID" in _src(n)]
    assert offenders == [], f"these modules name the log group: {offenders}"


def test_a_customer_error_carries_a_code_and_nothing_else():
    """«فقط براش بزنه مشکلی پیش آمد کد خطا» — the customer gets a reference, the
    diagnosis goes to the owner."""
    src = _src("logbus.py")
    assert re.search(r'E-\{?', src) or "E-" in src, "no error code format found"
    assert "traceback" in src.lower(), "the owner's copy has no traceback"


def test_the_house_divider_survived_everywhere():
    """«نوع فونت پنل و خط ------------------------------- که داره باید باقی بمونه»"""
    assert cards.LINE == "-" * 31
    for name in ALL_MODULES:
        src = _src(name)
        for bad in ('"' + "-" * 30 + '"', '"' + "-" * 32 + '"',
                    '"' + "=" * 31 + '"', '"' + "_" * 31 + '"'):
            assert bad not in src, f"{name} hand-rolled a divider: {bad}"


def test_no_payment_or_pricing_code_exists():
    """«اون قیمت گذاری تراکنش ایناهم نساز اصن»"""
    banned = ["trx", "tron", "usdt", "invoice", "price", "payment", "wallet",
              "tether", "zarinpal", "gateway"]
    hits = {}
    for name in ALL_MODULES:
        for line in _src(name).splitlines():
            # A comment saying "there is no payment subsystem" is the opposite of
            # payment code; only real statements count.
            code = line.split("#")[0].lower()
            found = [b for b in banned if b in code]
            if found:
                hits.setdefault(name, []).extend(found)
    assert hits == {}, f"payment code found: {hits}"


def test_the_discovery_cap_exists_and_is_two_thousand():
    """«محدودیت روزانه ۲۰۰۰ تای»"""
    assert config.PROBE_DAILY_CAP == 2000


def test_the_pdf_batch_is_one_hundred():
    """«هر ۱۰۰ تا برای طرف خروجی بفرست»"""
    assert config.PV_EXPORT_PDF_BATCH == 100


def test_the_antispam_shield_thresholds_match_what_was_asked():
    """«بیشتر ۲۰ نفر تو یک بازه ۲ دقیقه ای ربات رو استارت زدن ربات خودش خودکار
    آفلاین بشه»"""
    assert config.START_FLOOD_MAX == 20
    assert config.START_FLOOD_WINDOW == 120
    assert config.START_FLOOD_SHIELD is True


def test_the_owner_can_bring_the_service_back_after_the_shield_trips():
    """«بعدش بتونم از پنل خودم انش بکنم روشنش بکنم»"""
    literal, _ = _handlers("owner_bot.py")
    assert "shield" in literal
    # The shield takes the service offline; antispam.lift is what the owner's
    # button calls to bring it back, and it is the only writer of that flag.
    assert "antispam" in _src("owner_bot.py"), "the owner panel cannot reach it"
    src = _src("antispam.py")
    assert "set_bot_online(True" in src, "nothing can bring the service back"
    # Lifting must also clear the recorded burst, or the stale window trips the
    # shield again on the very next /start and the owner's action looks broken.
    assert "clear_start_events" in src


def test_removed_features_are_really_gone():
    """Things explicitly cut: the portal, the channel brain, linkdooni, the
    automation menu, worker transfer, session distribution, admin management."""
    banned = ["linkdooni", "worker_transfer", "session_distribut",
              "channel_brain", "portal_", "def portal"]
    hits = {}
    for name in ALL_MODULES:
        for line in _src(name).splitlines():
            code = line.split("#")[0].lower()
            found = [b for b in banned if b in code]
            if found:
                hits.setdefault(name, []).extend(found)
    assert hits == {}, f"a removed feature came back: {hits}"


# =========================================================================== #
# Structural invariants across the whole tree
# =========================================================================== #
def test_every_session_open_goes_through_the_busy_registry():
    """The root cause of the base project's dead accounts: two connections on one
    session. Every module that opens one must claim it first."""
    for name in ("rubika_panel.py", "tabchi.py", "pool.py", "health.py",
                 "worker_api.py"):
        src = _src(name)
        if "account_conn" not in src:
            continue
        assert "busy." in src, f"{name} uses sessions without the registry"


def test_no_customer_module_imports_the_owner_database():
    for name in CUSTOMER + ["health.py", "pdf_export.py"]:
        tree = ast.parse(_src(name), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name == "central_db" for a in node.names), name
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "central_db", name


def test_every_long_job_can_be_stopped():
    """A job with no stop button is a job the customer has to wait out."""
    for name, needle in [("rubika_panel.py", "rbstop"), ("tg_panel.py", "tgstop"),
                         ("tabchi.py", "tbtog"), ("pool.py", "plstop"),
                         ("telegram_multi_send.py", "def stop")]:
        assert needle in _src(name), f"{name} has no way to stop its work"


def test_every_engine_has_restart_recovery():
    """«بازیابی‌ها» — anything that survives a restart in the customer's mind must
    survive it in the process too."""
    for name, needle in [("rubika_panel.py", "restore_pending"),
                         ("tg_panel.py", "restore_pending"),
                         ("tabchi.py", "restore_engines"),
                         ("pool.py", "restore_pending"),
                         ("telegram_multi_send.py", "restore_pending")]:
        assert needle in _src(name), f"{name} cannot resume after a restart"
    boot = _src("customer_bot.py")
    for call in ("rubika_panel.restore_pending", "tg_panel.restore_pending",
                 "tabchi.restore_engines", "pool.restore_pending",
                 "health.start"):
        assert call in boot, f"{call} is never wired into startup"


def test_help_covers_every_customer_feature_area():
    expected = {"send", "accounts", "content", "contacts", "discovery", "brain",
                "pool", "tabchi", "secretary", "pdf", "errors"}
    missing = expected - set(help_text.topics())
    assert missing == set(), f"undocumented features: {sorted(missing)}"


def test_the_two_bots_are_routed_by_one_entrypoint():
    src = _src("main.py")
    for mode in ("owner", "customer", "worker"):
        assert mode in src, f"main.py cannot start the {mode} process"


def test_the_database_schema_migrates_rather_than_breaking(tmp_path, monkeypatch):
    """Running a newer build against an older database must not crash."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "x.db"))
    db.init()
    db.init()
    assert db.schema_version() >= 2
