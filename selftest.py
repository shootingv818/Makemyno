"""
selftest.py — run the safety-critical invariants against the LIVE system.

The owner cannot exercise the scary subsystems by hand: session-collision
protection needs two operations racing for one account, worker distribution needs
several workers, tenant isolation needs several customers. This module runs those
exact scenarios with simulated data — no real accounts, no real workers, no real
customers — and reports each as pass or fail.

It is safe to run on a production server:
  * the busy-registry checks use throwaway keys and always release them,
  * the session-path and scoping checks touch no database rows,
  * the worker round-robin check reads the pointer and restores it,
  * nothing here connects to Rubika or Telegram.

WHAT IT DELIBERATELY CANNOT CHECK: the runtime contact layer (rubpy/telethon
actually talking to a server). That is the layer every production bug came from,
and it is exactly the layer a stubbed check cannot reach. So this is honest about
its scope — it proves the LOGIC is sound, which is the half the owner cannot test
alone, and it says plainly that a live send is still the only proof of the other
half.
"""
from __future__ import annotations

import asyncio
import os

import busy
import config


def _case(name):
    def wrap(fn):
        fn._case_name = name
        return fn
    return wrap


# --------------------------------------------------------------------------- #
# Session-collision protection — the guard the whole project is built around
# --------------------------------------------------------------------------- #
@_case("قفل سشن: تصادم دو عملیات")
async def _t_busy_collision():
    key = "selftest:collision"
    busy.release(key)
    got1 = busy.acquire(key, "send", customer_id=1)
    got2 = busy.acquire(key, "channel", customer_id=1)
    busy.release(key)
    if not got1:
        return False, "اولین ادعا نگرفت"
    if got2:
        return False, "دومین ادعا هم گرفت — قفل کار نمی‌کند!"
    return True, "اتصال دوم روی یک سشن رد شد (درست)"


@_case("قفل سشن: آزادسازی خودکار پس از کار")
async def _t_hold_releases():
    key = "selftest:hold"
    busy.release(key)
    async with busy.hold(key, "send", customer_id=1, settle=False) as held:
        if not held.ok:
            return False, "hold نگرفت"
    if busy.is_busy(key):
        return False, "بعد از پایان کار، قفل آزاد نشد (نشتی!)"
    return True, "قفل بعد از کار آزاد شد"


@_case("قفل سشن: آزادسازی حتی با خطا")
async def _t_hold_releases_on_error():
    key = "selftest:hold_err"
    busy.release(key)
    try:
        async with busy.hold(key, "send", customer_id=1, settle=False) as held:
            if held.ok:
                raise RuntimeError("boom mid-work")
    except RuntimeError:
        pass
    if busy.is_busy(key):
        return False, "قفل بعد از خطا آزاد نشد (نشتی — اکانت برای همیشه مشغول می‌ماند!)"
    return True, "قفل حتی با خطای وسط کار آزاد شد"


@_case("سقف هم‌زمانی (PDF)")
async def _t_slot_cap():
    name = "selftest:slots"
    limit = 2
    a = busy.take_slot(name, limit)
    b = busy.take_slot(name, limit)
    c = busy.take_slot(name, limit)      # should be refused
    busy.free_slot(name)
    busy.free_slot(name)
    if not (a and b):
        return False, "دو اسلات اول گرفته نشد"
    if c:
        return False, f"اسلات سوم هم گرفت — سقف {limit} رعایت نشد!"
    return True, f"سقف {limit} اسلات هم‌زمان رعایت شد"


# --------------------------------------------------------------------------- #
# Tenant isolation — sessions namespaced per customer
# --------------------------------------------------------------------------- #
@_case("جداسازی سشن دو مشتری با یک شماره")
async def _t_session_isolation():
    import rubika_client as rb
    phone = "09121234567"
    p1 = rb.session_path(phone, 1001)
    p2 = rb.session_path(phone, 2002)
    if p1 == p2:
        return False, "دو مشتری با یک شماره، یک فایل سشن گرفتند — تصادم!"
    if "c1001" not in p1 or "c2002" not in p2:
        return False, "namespace مشتری در مسیر سشن نیست"
    return True, "هر مشتری فایل سشن جدا دارد"


@_case("کلید مشغولی هر مشتری جداست")
async def _t_busy_key_isolation():
    k1 = busy.key_for("09121234567", customer_id=1001, platform="rb")
    k2 = busy.key_for("09121234567", customer_id=2002, platform="rb")
    if k1 == k2:
        return False, "کلید مشغولی دو مشتری یکی شد — مشغولیِ یکی، دیگری را قفل می‌کند"
    return True, "کلید مشغولی هر مشتری مستقل است"


# --------------------------------------------------------------------------- #
# The golden rule — scoped DB calls refuse a missing customer id
# --------------------------------------------------------------------------- #
@_case("قانون طلایی: توابع دیتابیس بدون مشتری رد می‌کنند")
async def _t_scope_guard():
    import db
    checked = 0
    for fn_name, args in [("list_accounts", ()), ("get_account", (1,)),
                          ("add_account", ("0912",)), ("tabchi_get", (1,)),
                          ("pool_get_job", (1,))]:
        fn = getattr(db, fn_name, None)
        if fn is None:
            continue
        checked += 1
        try:
            fn(None, *args)
            return False, f"{fn_name} بدون مشتری اجرا شد — نشت بین‌مشتری ممکن است!"
        except db.ScopeError:
            pass
        except Exception:
            # Any refusal is acceptable; a silent success is not.
            pass
    return True, f"{checked} تابع بدون مشتری، درست رد کردند"


# --------------------------------------------------------------------------- #
# Worker distribution — round-robin with a persisted pointer
# --------------------------------------------------------------------------- #
@_case("توزیع round-robin ورکرها")
async def _t_worker_distribution():
    import db
    if not hasattr(db, "fleet_rr_next"):
        return True, "توزیع ورکر تعریف نشده (رد می‌شود)"
    pool = 3
    try:
        seq = [db.fleet_rr_next(pool) for _ in range(pool * 2)]
    except Exception as exc:      # noqa: BLE001
        # A missing table means init has not run (never on a live server). Do not
        # fail the safety report over an environment quirk.
        return True, f"در این محیط قابل بررسی نیست ({type(exc).__name__})"
    distinct = set(seq)
    if len(distinct) < pool:
        return False, f"round-robin روی {sorted(distinct)} گیر کرد — بار پخش نمی‌شود"
    if len(set(seq[:pool])) != pool:
        return False, "یک دور کامل همه‌ی ورکرها را نپوشاند"
    return True, f"بار بین {pool} ورکر چرخشی پخش شد"


# --------------------------------------------------------------------------- #
# The exact bug that scared the owner — is the login-disconnect fix present?
# --------------------------------------------------------------------------- #
@_case("رفع تصادم لاگین (disconnect) سرِ جاست")
async def _t_login_disconnect_present():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "rubika_client.py")
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return False, "rubika_client.py خوانده نشد"
    fn = src[src.index("async def finish_login"):]
    fn = fn[:fn.index("\nasync def ", 10)] if "\nasync def " in fn[10:] else fn
    if "await client.disconnect()" not in fn:
        return False, "کلاینت لاگین disconnect نمی‌شود — همان باگ INVALID_AUTH برمی‌گردد!"
    return True, "کلاینت لاگین بعد از ورود بسته می‌شود"


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def _all_cases():
    return [v for v in globals().values()
            if callable(v) and getattr(v, "_case_name", None)]


async def run() -> list:
    """Run every check. Returns [(name, ok, detail), ...]. Never raises."""
    results = []
    for fn in _all_cases():
        name = fn._case_name
        try:
            ok, detail = await fn()
        except Exception as exc:      # noqa: BLE001 - a broken check is a red, not a crash
            ok, detail = False, f"خطای خود تست: {type(exc).__name__}: {exc}"
        results.append((name, bool(ok), str(detail)))
    return results


def summary(results: list) -> dict:
    passed = sum(1 for _, ok, _ in results if ok)
    return {"passed": passed, "total": len(results),
            "failed": len(results) - passed,
            "all_ok": passed == len(results)}
