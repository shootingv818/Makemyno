"""
customer_bot.py — the shared bot every customer talks to.
=========================================================

One process serves all customers. Which customer is acting is decided by the
Telegram sender id, and every database call is scoped to it, so nobody can see
or touch anybody else's accounts.

WHAT THIS FILE IS RESPONSIBLE FOR
---------------------------------
The shell around the two platform sections: access control, the start screen,
support, help, and the loop that delivers whatever the owner queued. The actual
work lives in rubika_panel.py and tg_panel.py.

THINGS THAT ARE DELIBERATELY ABSENT
-----------------------------------
  * central_db is never imported. Maintenance mode is read from a flag file
    instead, so this process has no way to open the owner's database.
  * No worker screen, no backup, no customer roster, no service settings. They
    are not hidden behind a permission check — they are not registered in this
    process at all, so a stale button in an old chat cannot reach them.
  * The log group is never named. Customers see friendly text plus an error code;
    the full trace goes to the owner.

ACCESS IS CHECKED IN ONE PLACE
------------------------------
Every handler starts with `await _gate(event)`, which walks the whole chain in
order: shield, maintenance, blocked, rate limit, subscription. Putting it in one
function is what stops a new screen from quietly skipping a check.
"""
from __future__ import annotations

import asyncio
import os

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

import antispam
import busy
import cards
import config
import db
import forcedjoin
import logbus
import ratelimit

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

bot = TelegramClient(os.path.join(DATA_DIR, "customer_bot"),
                     config.API_ID, config.API_HASH)

# Wizard state per customer: {"step": "...", ...}
state: dict = {}


# --------------------------------------------------------------------------- #
# Maintenance, read WITHOUT touching the owner's database
# --------------------------------------------------------------------------- #
def _maintenance_flag_path() -> str:
    return os.path.join(DATA_DIR, "maintenance.flag")


def maintenance_on() -> bool:
    return os.path.exists(_maintenance_flag_path())


def maintenance_notice() -> str:
    try:
        with open(_maintenance_flag_path(), encoding="utf-8") as fh:
            text = fh.read().strip()
        return "" if text == "1" else text
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #
async def safe_edit(event, text: str, buttons=None):
    try:
        return await event.edit(text, buttons=buttons)
    except MessageNotModifiedError:
        return None
    except Exception:
        try:
            return await bot.send_message(event.sender_id, text, buttons=buttons)
        except Exception:
            return None


async def respond(event, text: str, buttons=None):
    """Reply whether the event is a message or a button press."""
    try:
        if isinstance(event, events.CallbackQuery.Event):
            return await event.edit(text, buttons=buttons)
        return await event.respond(text, buttons=buttons)
    except Exception:
        try:
            return await bot.send_message(event.sender_id, text, buttons=buttons)
        except Exception:
            return None


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


# --------------------------------------------------------------------------- #
# The single access gate
# --------------------------------------------------------------------------- #
async def _gate(event, *, need_active: bool = True,
                count_action: bool = True) -> bool:
    """Decide whether this customer may proceed, and explain it if not.

    Order matters: the cheapest and most protective checks come first, so a
    flood costs us an indexed lookup rather than a subscription calculation.
    """
    uid = event.sender_id

    # 1. A blocked customer gets absolute silence. Answering a flooder is doing
    #    their work for them: every reply is an API call we pay for.
    if db.is_blocked(uid):
        return False

    # 2. The anti-spam shield: while it is up, strangers are not served.
    if not db.is_bot_online():
        known = db.get_customer(uid) is not None
        if not known:
            return False
        await respond(event, cards.card("🛡 سرویس موقتاً بسته است", [
            "به‌دلیل ترافیک غیرعادی، ثبت‌نام و ورود موقتاً متوقف شده.",
            "کمی بعد دوباره امتحان کن.",
        ]))
        return False

    # 3. Maintenance.
    if maintenance_on():
        notice = maintenance_notice() or "سرویس در حال بروزرسانی است."
        await respond(event, cards.card("🧰 حالت تعمیر", [notice]))
        return False

    # 4. Rate limit (which may auto-block and then stay silent).
    if count_action and not await ratelimit.guard(uid, action="panel"):
        return False

    db.touch_customer(uid)

    # 5. Sponsor channels. AFTER the rate limit (the check is a network call, so a
    #    flooder must not be able to make us spend one per press) and BEFORE the
    #    subscription check, so a customer whose time has run out still sees the
    #    join prompt rather than two contradictory refusals at once.
    if forcedjoin.is_active() and not await forcedjoin.enforce(
            bot, event, respond=respond):
        return False

    # 6. Subscription.
    if need_active and not db.is_active(uid):
        left_note = ("زمان دسترسی شما تمام شده."
                     if db.get_customer(uid) else "دسترسی فعال نیست.")
        await respond(event, cards.card("🔴 دسترسی غیرفعال", [
            left_note,
            "برای تمدید با پشتیبانی تماس بگیر.",
        ]), buttons=[[Button.inline("🆘 پشتیبانی", b"support")]])
        return False

    return True


# --------------------------------------------------------------------------- #
# Start screen: two sections
# --------------------------------------------------------------------------- #
def start_card(customer_id) -> str:
    """The card the owner specified: one block per platform, then the total."""
    rb = db.count_accounts(customer_id)
    tg = db.tg_count_accounts(customer_id)
    rb_sent = sum(a.get("sent_total", 0) for a in db.list_accounts(customer_id))
    tg_sent = sum(a.get("sent_total", 0) for a in db.tg_list_accounts(customer_id))

    rows = []
    rows += cards.section("🟣 Rubika", [
        f"👤 Accounts: {rb['total']}  ({rb['healthy']} healthy)",
        f"→ Total Sent: {cards.num(rb_sent)}",
    ])
    rows.append("")
    rows += cards.section("✈️ Telegram", [
        f"👤 Accounts: {tg['total']}  ({tg['healthy']} healthy)",
        f"→ Total Sent: {cards.num(tg_sent)}",
    ])
    rows.append(cards.LINE)
    rows.append(f"▪ Total Sent: {cards.num(rb_sent + tg_sent)}")

    left = db.days_left(customer_id)
    if db.seconds_left(customer_id) > 0:
        rows.append(f"▪ Access: {left} روز باقی")
    rows.append("")
    rows.append("Rubika , Telegram")
    rows.append("Which section do you want to open?")
    return cards.card("🤖 Bot Panel", rows)


def start_menu() -> list:
    return [
        [Button.inline("🟣 Rubika", b"rb"), Button.inline("✈️ Telegram", b"tg")],
        [Button.inline("🧰 ابزارها", b"tools")],
        [Button.inline("📖 راهنما", b"help"), Button.inline("🆘 پشتیبانی", b"support")],
    ]


# --------------------------------------------------------------------------- #
# Tools — ported from the reference project's «🧰 ابزارها» section.
#
# Pure utilities that need no account and no platform connection, so they stay
# usable when a session is broken or a subscription has lapsed.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"tools"))
async def tools_cb(event):
    # need_active=False: a number list is useful to somebody deciding whether to
    # renew, and there is no cost to us in generating one.
    if not await _gate(event, need_active=False):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, cards.card("🧰 ابزارها", [
        "🔢 ساخت شماره ایران — بر اساس پیش‌شماره، لیست شماره می‌سازد و "
        "اپراتور و استان آن را می‌گوید.",
        "📦 APK → ZIP — فایل APK را داخل یک زیپ سالم می‌گذارد؛ "
        "بعد از استخراج دقیقاً همان فایل اول برمی‌گردد.",
        cards.LINE,
        f"سقف هر بار: {config.NUMGEN_MAX} شماره",
    ]), buttons=[
        [Button.inline("🔢 ساخت شماره ایران", b"tool_numgen")],
        [Button.inline("📦 APK → ZIP", b"tool_apkzip")],
        [Button.inline("🔙 منوی اصلی", b"home")],
    ])


@bot.on(events.CallbackQuery(data=b"tool_numgen"))
async def tool_numgen_cb(event):
    if not await _gate(event, need_active=False):
        return
    state[event.sender_id] = {"step": "await_numgen"}
    await safe_edit(event, cards.card("🔢 ساخت شماره ایران", [
        "پیش‌شماره و تعداد را در یک پیام بفرست:",
        "`0913 500`",
        cards.LINE,
        "پیش‌شمارهٔ کامل‌تر هم می‌شود — `0913613 200` فقط رقم‌های باقی‌مانده را "
        "پر می‌کند.",
        f"حداکثر {config.NUMGEN_MAX} شماره در هر بار.",
    ]), buttons=[[Button.inline("🔙 ابزارها", b"tools")]])


async def _step_numgen(event, st):
    import iran_numbers

    uid = event.sender_id
    state.pop(uid, None)
    parts = (event.raw_text or "").split()
    prefix = iran_numbers.clean_prefix(parts[0] if parts else "")
    try:
        count = int(parts[1]) if len(parts) > 1 else 100
    except ValueError:
        count = 0

    if not prefix or not iran_numbers.is_valid_prefix(prefix):
        await respond(event, cards.card("پیش‌شماره شناخته نشد", [
            "یک پیش‌شمارهٔ موبایل ایران بفرست، مثل 0913 یا 0921.",
            "مثال کامل: `0913 500`",
        ]), buttons=[[Button.inline("🔙 ابزارها", b"tools")]])
        return
    if count < 1:
        await respond(event, "تعداد را درست بفرست. مثال: `0913 500`",
                      buttons=[[Button.inline("🔙 ابزارها", b"tools")]])
        return
    # Capped, and the cap is REPORTED rather than silently applied: a customer who
    # asked for 50000 and got 5000 with no word would treat the file as complete.
    asked = count
    count = min(count, config.NUMGEN_MAX)

    operator, region = iran_numbers.detect(prefix)
    numbers = iran_numbers.gen_unique(prefix, count)
    if not numbers:
        await respond(event, "با این پیش‌شماره چیزی ساخته نشد.",
                      buttons=[[Button.inline("🔙 ابزارها", b"tools")]])
        return

    # config has no DATA_DIR — backup.py defines its own from __file__, and that
    # is the pattern here too. Writing to a name that does not exist would have
    # raised AttributeError on the first customer who pressed the button.
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"numbers_{uid}_{prefix}.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(numbers) + "\n")
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="numgen write", customer=uid)
        return

    rows = [
        cards.kv("Prefix", prefix),
        cards.kv("Operator", operator or "—"),
        cards.kv("Region", region or "—"),
        cards.kv("Count", cards.num(len(numbers))),
    ]
    if asked > count:
        rows.append(f"⚠️ {cards.num(asked)} خواستی، سقف {cards.num(count)} است.")
    # Fewer than asked can also happen legitimately: a long prefix leaves few
    # remaining digits, so the search space itself is smaller than the request.
    elif len(numbers) < count:
        rows.append(f"⚠️ با این پیش‌شماره فقط {cards.num(len(numbers))} شمارهٔ "
                    "یکتا امکان‌پذیر بود.")
    rows.append("⚠️ این شماره‌ها تصادفی ساخته شده‌اند — یعنی وجود داشتنشان "
                "تضمینی نیست. برای پیدا کردن شماره‌های واقعی از «افزودن مخاطب» "
                "استفاده کن.")

    try:
        await _bot_send_file(uid, path, cards.card("🔢 شماره‌ها آماده شد", rows))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# APK -> ZIP, ported from the reference project.
#
# The point is not compression, it is getting an APK past a filter that blocks the
# extension. So the one thing that MUST hold is that what comes out of the zip is
# byte-for-byte what went in: a "helpful" tool that quietly corrupts an installer
# is worse than no tool. The archive is therefore verified after writing —
# testzip(), the stored size, and a SHA-256 of the extracted bytes against the
# source — and the hash is shown so the customer can check it themselves.
# --------------------------------------------------------------------------- #
def _tools_dir(uid) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "tools", str(uid))
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name: str, ext: str) -> str:
    """A file name safe to write, forced to `ext`.

    basename first: a caller-supplied name must never be able to escape the
    directory with an absolute path or a ../ traversal.
    """
    import re
    raw = (name or "").strip()
    # Both separators, before basename. On Linux os.path.basename does NOT treat a
    # backslash as a separator, so "..\\..\\win.ini" survived it whole and the
    # cleanup below left "....win" — dots and all. Normalising both separators
    # first means a Windows-style traversal is split like any other path.
    raw = raw.replace("\\", "/")
    base = os.path.basename(raw)
    base = re.sub(r"[^\w.\- ]+", "", base, flags=re.UNICODE).strip() or "file"
    base = re.sub(r"\s+", "_", base)
    # A name made only of dots is not a name; it is the traversal that is left
    # once the slashes are gone.
    if not base.strip("."):
        base = "file"
    root = (base[:-len(ext)] if base.lower().endswith(ext.lower())
            else base.rsplit(".", 1)[0])
    return ((root or "file")[:60]) + ext


def _human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sha256_file(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@bot.on(events.CallbackQuery(data=b"tool_apkzip"))
async def tool_apkzip_cb(event):
    if not await _gate(event, need_active=False):
        return
    state[event.sender_id] = {"step": "await_apk"}
    await safe_edit(event, cards.card("📦 APK → ZIP", [
        "فایل APK را به‌صورت فایل (Document) بفرست.",
        cards.LINE,
        f"• حداکثر حجم: {config.APK_ZIP_MAX_MB} مگابایت",
        "• فایل اصلی دست نمی‌خورد — فقط داخل یک زیپ گذاشته می‌شود.",
        "• سالم بودن زیپ بعد از ساخت بررسی می‌شود.",
    ]), buttons=[[Button.inline("🔙 ابزارها", b"tools")]])


async def _step_apk(event, st):
    uid = event.sender_id
    if not event.document:
        await respond(event, "لطفاً خود فایل APK را بفرست (نه متن).")
        return
    handle = event.file
    name = ((getattr(handle, "name", None) or "").strip()) if handle else ""
    size = int((getattr(handle, "size", 0) or 0)) if handle else 0
    if size and size > config.APK_ZIP_MAX_MB * 1024 * 1024:
        state.pop(uid, None)
        await respond(event, cards.card("❌ فایل بزرگ است", [
            cards.kv("Size", _human_size(size)),
            cards.kv("Max", f"{config.APK_ZIP_MAX_MB} MB"),
        ]), buttons=[[Button.inline("🔙 ابزارها", b"tools")]])
        return
    if name and not name.lower().endswith(".apk"):
        await respond(event, "فقط فایل با پسوند .apk قبول است.")
        return

    msg = await respond(event, "⏳ در حال دریافت فایل ...")
    target = os.path.join(_tools_dir(uid), f"src_{os.urandom(6).hex()}.apk")
    try:
        path = await event.download_media(file=target)
    except Exception as exc:  # noqa: BLE001
        state.pop(uid, None)
        await logbus.error(exc, context="apkzip download", customer=uid,
                          notify=False)
        await _edit_or_send(msg, event, cards.card("❌ دریافت نشد", [
            logbus.humanize_error(exc)]))
        return

    st["apk_path"] = path
    st["apk_name"] = _safe_name(name or "app.apk", ".apk")
    st["step"] = "await_apk_zipname"
    await _edit_or_send(msg, event, cards.card("📦 APK → ZIP", [
        cards.kv("File", st["apk_name"]),
        cards.kv("Size", _human_size(os.path.getsize(path))),
        cards.LINE,
        "حالا یک اسم برای فایل زیپ بفرست (مانند my_app).",
    ]))


async def _step_apk_zipname(event, st):
    import zipfile

    uid = event.sender_id
    apk_path = st.get("apk_path")
    apk_name = st.get("apk_name") or "app.apk"
    state.pop(uid, None)
    if not apk_path or not os.path.exists(apk_path):
        await respond(event, "فایل منبع پیدا نشد. دوباره از ابزارها شروع کن.",
                      buttons=[[Button.inline("🔙 ابزارها", b"tools")]])
        return

    zip_path = os.path.join(os.path.dirname(apk_path),
                            _safe_name((event.raw_text or "").strip() or "app",
                                       ".zip"))
    msg = await respond(event, "⏳ در حال ساخت زیپ ...")
    try:
        source_hash = _sha256_file(apk_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(apk_path, arcname=apk_name)

        # VERIFY before handing it over. An installer that arrives corrupted is
        # worse than a tool that refuses: the customer would blame the app.
        with zipfile.ZipFile(zip_path, "r") as archive:
            if archive.testzip() is not None:
                raise RuntimeError("zip integrity check failed")
            entry = archive.getinfo(apk_name)
            if entry.file_size != os.path.getsize(apk_path):
                raise RuntimeError("size mismatch after zip")
            import hashlib
            digest = hashlib.sha256()
            with archive.open(apk_name, "r") as inner:
                for chunk in iter(lambda: inner.read(65536), b""):
                    digest.update(chunk)
            if digest.hexdigest() != source_hash:
                raise RuntimeError("hash mismatch after zip")

        await bot.send_file(
            int(uid), zip_path, force_document=True,
            caption=cards.card("📦 زیپ آماده شد", [
                cards.kv("Inside", apk_name),
                cards.kv("Size", _human_size(os.path.getsize(zip_path))),
                cards.kv("SHA-256", source_hash[:16] + "…"),
                cards.LINE,
                "بعد از استخراج دقیقاً همان APK اول برمی‌گردد — بررسی شد.",
            ]),
            buttons=[[Button.inline("🔙 ابزارها", b"tools")]])
        try:
            await msg.delete()
        except Exception:      # noqa: BLE001
            pass
        await logbus.customer_action(db.get_customer(uid), "apk_zip", [
            cards.kv("File", apk_name),
            cards.kv("Size", _human_size(os.path.getsize(zip_path))),
        ])
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="apkzip build", customer=uid,
                          notify=False)
        await _edit_or_send(msg, event, cards.card("❌ زیپ ساخته نشد", [
            logbus.humanize_error(exc),
        ]))
    finally:
        # Both copies go, always. These are whole APKs; leaving them behind fills
        # the disk one customer at a time.
        for leftover in (apk_path, zip_path):
            try:
                os.remove(leftover)
            except OSError:
                pass


async def _edit_or_send(msg, event, text: str) -> None:
    """Edit the progress message, or send a new one if editing is not possible."""
    if msg is not None:
        try:
            await msg.edit(text)
            return
        except Exception:      # noqa: BLE001
            pass
    await respond(event, text)


async def _bot_send_file(uid, path: str, caption: str) -> None:
    await bot.send_file(int(uid), path, caption=caption,
                        buttons=[[Button.inline("🔙 ابزارها", b"tools")]])


@bot.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    uid = event.sender_id

    # A blocked customer is ignored entirely — no row touched, no reply sent.
    if db.is_blocked(uid):
        return

    is_new = db.get_customer(uid) is None

    # The shield counts DISTINCT NEW users: one curious person pressing /start
    # ten times is not an attack, a hundred fresh accounts are.
    if not await antispam.note_start(uid, is_new=is_new):
        if is_new:
            return                                   # stranger during a flood
        await respond(event, cards.card("🛡 سرویس موقتاً بسته است", [
            "به‌دلیل ترافیک غیرعادی، سرویس موقتاً متوقف شده.",
        ]))
        return

    sender = await event.get_sender()
    cust = db.ensure_customer(uid,
                              getattr(sender, "first_name", "") or "",
                              getattr(sender, "username", "") or "")
    state.pop(uid, None)

    if is_new:
        await logbus.customer_action(cust, "customer_start", [
            cards.kv("New", "yes"),
            cards.kv("Trial", f"{config.TRIAL_DAYS} days"
                     if config.TRIAL_DAYS else "none"),
        ])
    else:
        await logbus.customer_action(cust, "customer_start", [
            cards.kv("New", "no"),
            cards.kv("Days left", db.days_left(uid)),
        ])

    if maintenance_on():
        await respond(event, cards.card("🧰 حالت تعمیر", [
            maintenance_notice() or "سرویس در حال بروزرسانی است."]))
        return

    if not db.is_active(uid):
        await respond(event, cards.card("🔴 دسترسی غیرفعال", [
            "برای فعال‌سازی با پشتیبانی تماس بگیر.",
            cards.kv("آیدی شما", uid, width=10),
        ]), buttons=[[Button.inline("🆘 پشتیبانی", b"support")]])
        return

    await respond(event, start_card(uid), buttons=start_menu())


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not await _gate(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, start_card(event.sender_id), buttons=start_menu())


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    if not await _gate(event, count_action=False):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, start_card(event.sender_id), buttons=start_menu())


@bot.on(events.CallbackQuery(data=b"fj_check"))
async def fj_check_cb(event):
    """"I have joined — check me".

    This deliberately does NOT go through _gate. _gate is what shows the join
    prompt, so routing the prompt's own button through it would answer a customer
    who has just joined with the same prompt again, forever. It runs the membership
    check directly, and only then hands them the normal menu.

    The cached PASS is dropped first: the customer joined seconds ago, so any
    remembered verdict is stale by definition.
    """
    uid = event.sender_id
    if db.is_blocked(uid):
        return
    if not await ratelimit.guard(uid, action="panel"):
        return
    forcedjoin.clear_cache(uid)
    missing = await forcedjoin.missing_for(bot, uid)
    if missing:
        text, buttons = forcedjoin.prompt(missing, Button)
        await event.answer("هنوز عضو همهٔ کانال‌ها نشدی.", alert=True)
        await safe_edit(event, text, buttons=buttons)
        return
    db.touch_customer(uid)
    await event.answer("✅ عضویت تأیید شد.")
    await safe_edit(event, start_card(uid), buttons=start_menu())


# --------------------------------------------------------------------------- #
# Support tickets
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"support"))
async def support_cb(event):
    # need_active=False on purpose: an expired customer is exactly the person who
    # needs to reach support.
    if not await _gate(event, need_active=False):
        return
    uid = event.sender_id
    open_count = db.customer_open_tickets(uid)
    rows = [
        cards.kv("آیدی شما", uid, width=12),
        cards.kv("تیکت باز", open_count, width=12),
    ]
    recent = db.customer_tickets(uid, 3)
    if recent:
        rows.append(cards.LINE)
        for ticket in recent:
            mark = "✅" if ticket.get("answered") else "⏳"
            rows.append(f"{mark} «{(ticket.get('text') or '')[:40]}»")
            if ticket.get("answered"):
                rows.append(f"   ↳ {(ticket.get('answer') or '')[:60]}")
    buttons = []
    if open_count < 3:
        buttons.append([Button.inline("✍️ ارسال پیام به پشتیبانی", b"tk_new")])
    else:
        rows.append("سه تیکت باز داری؛ تا پاسخ یکی صبر کن.")
    if config.SUPPORT_URL:
        buttons.append([Button.url("💬 چت مستقیم", config.SUPPORT_URL)])
    buttons.append(_back(b"home"))
    await safe_edit(event, cards.card("🆘 پشتیبانی", rows), buttons=buttons)


@bot.on(events.CallbackQuery(data=b"tk_new"))
async def ticket_new_cb(event):
    if not await _gate(event, need_active=False):
        return
    state[event.sender_id] = {"step": "await_ticket"}
    await safe_edit(event, cards.card("✍️ پیام به پشتیبانی", [
        "مشکل یا سوالت را در یک پیام بنویس.",
        "اگر کد خطا داری، همان را هم بفرست.",
    ]), buttons=[_back(b"support")])


# How much of a support message fits in the owner card alongside the context
# rows. The REMAINDER is posted as a follow-up message, never dropped: the old
# card cut at 400 characters with no marker, so a customer who wrote three
# paragraphs had two vanish and neither side knew.
TICKET_CARD_BUDGET = 900


async def _step_ticket(event, st):
    uid = event.sender_id
    state.pop(uid, None)
    text = (event.raw_text or "").strip()
    if len(text) < 5:
        await respond(event, "متن خیلی کوتاه بود. دوباره امتحان کن.",
                      buttons=[_back(b"support")])
        return
    # Store the whole message, and if it genuinely will not fit, SAY so instead of
    # cutting it in silence. The old code stored text[:2000] and the owner's card
    # showed text[:400] with no marker at all, so a customer who wrote three
    # paragraphs had two of them vanish and neither side knew — the customer
    # believed they had explained the problem and the owner saw a sentence that
    # stopped mid-word.
    trimmed = len(text) > config.TICKET_MAX
    tid = db.add_ticket(uid, text[:config.TICKET_MAX])
    cust = db.get_customer(uid) or {}
    rb = db.count_accounts(uid)
    tg = db.tg_count_accounts(uid)
    # The owner gets the message WITH context attached, so support does not
    # start with "who are you and what do you have".
    head = [
        cards.kv("Ticket", f"#{tid}"),
        cards.kv("From", f"{cust.get('name') or '—'} ({uid})"),
        cards.kv("Access", f"{db.days_left(uid)}d left"
                 if db.seconds_left(uid) > 0 else "expired"),
        cards.kv("Accounts", f"{rb['total']} Rubika ({rb['healthy']} healthy) | "
                             f"{tg['total']} Telegram"),
        cards.LINE,
        f"«{text[:TICKET_CARD_BUDGET]}»",
    ]
    if len(text) > TICKET_CARD_BUDGET:
        # Never drop it silently. The remainder goes out as its own message, so
        # the whole thing stays readable in the log group.
        head.append(cards.LINE)
        head.append(f"⬇️ ادامهٔ متن در پیام بعدی (کل {len(text)} کاراکتر)")
    await logbus.event("📨 - #ticket", head)
    if len(text) > TICKET_CARD_BUDGET:
        await logbus.to_group(
            f"📨 #{tid} — ادامه:\n\n{text[TICKET_CARD_BUDGET:]}")

    rows = [
        cards.kv("شماره تیکت", f"#{tid}", width=12),
        "پاسخ در همین چت برایت می‌آید.",
    ]
    if trimmed:
        rows.append(
            f"⚠️ پیامت طولانی بود و تا {config.TICKET_MAX} کاراکتر ثبت شد. "
            "اگر چیزی جا افتاد، در یک تیکت دیگر بفرست.")
    await respond(event, cards.card("✅ ثبت شد", rows),
                  buttons=[_back(b"home")])


# --------------------------------------------------------------------------- #
# Help
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"help"))
async def help_cb(event):
    if not await _gate(event, need_active=False):
        return
    import help_text
    await safe_edit(event, help_text.index_card(), buttons=help_text.index_menu())


@bot.on(events.CallbackQuery(pattern=rb"help_(\w+)"))
async def help_topic_cb(event):
    if not await _gate(event, need_active=False, count_action=False):
        return
    import help_text
    topic = event.pattern_match.group(1).decode()
    text, buttons = help_text.topic(topic)
    await safe_edit(event, text, buttons=buttons)


# --------------------------------------------------------------------------- #
# Text router — the panels register their own steps here
# --------------------------------------------------------------------------- #
_STEPS: dict = {"await_ticket": _step_ticket,
                "await_numgen": _step_numgen,
                "await_apk": _step_apk,
                "await_apk_zipname": _step_apk_zipname}


def register_steps(steps: dict) -> None:
    """Let a panel module add its wizard steps to the shared router."""
    _STEPS.update(steps)


@bot.on(events.NewMessage(func=lambda e: e.is_private))
async def text_router(event):
    if (event.raw_text or "").startswith("/"):
        return
    uid = event.sender_id
    st = state.get(uid)
    if not st:
        return
    step = st.get("step")
    handler = _STEPS.get(step)
    if not handler:
        state.pop(uid, None)
        return
    # A file upload is a feature (numbers list, media), so the gate runs before
    # anything is read off the wire.
    if not await _gate(event, count_action=False):
        return
    try:
        await handler(event, st)
    except db.ScopeError as exc:
        # This means a caller forgot the customer id. The guard did its job:
        # loud error, no cross-tenant read.
        state.pop(uid, None)
        code = await logbus.error(exc, context=f"scope leak in step {step}",
                                  customer=uid)
        await respond(event, cards.card("⚠️ مشکلی پیش آمد", [
            cards.kv("کد خطا", code, width=8)]), buttons=[_back(b"home")])
    except Exception as exc:  # noqa: BLE001
        state.pop(uid, None)
        code = await logbus.error(exc, context=f"customer step {step}",
                                  customer=uid)
        await respond(event, cards.card("⚠️ مشکلی پیش آمد", [
            cards.kv("کد خطا", code, width=8),
            "این کد را برای پشتیبانی بفرست تا بررسی شود.",
        ]), buttons=[[Button.inline("🆘 پشتیبانی", b"support")], _back(b"home")])


# --------------------------------------------------------------------------- #
# Background loops
# --------------------------------------------------------------------------- #
async def notification_loop() -> None:
    """Deliver whatever the owner queued.

    The owner bot cannot message a customer directly — they never started it —
    so it writes to db.notifications and this loop hands them over. A customer
    who blocked the bot simply fails; the row is still marked done so one dead
    recipient cannot wedge the queue.
    """
    while True:
        try:
            for note in db.fetch_unsent_notifications(50):
                try:
                    await bot.send_message(int(note["customer_id"]), note["text"])
                except Exception:
                    pass
                db.mark_notification_sent(note["id"])
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="notification_loop", notify=False)
        await asyncio.sleep(10)


async def expiry_notice_loop() -> None:
    """Tell a customer the moment their access lapses, once."""
    seen_expired: set = set()
    while True:
        await asyncio.sleep(1800)
        try:
            for cust in db.owner_list_customers():
                uid = cust["telegram_id"]
                if cust.get("blocked") or db.seconds_left(uid) > 0:
                    seen_expired.discard(uid)
                    continue
                if uid in seen_expired or not (cust.get("expires_at") or ""):
                    continue
                seen_expired.add(uid)
                db.queue_notification(uid, cards.card("🔴 دسترسی تمام شد", [
                    "مدت دسترسی شما به پایان رسید.",
                    "برای تمدید با پشتیبانی تماس بگیر.",
                ]))
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="expiry_notice_loop", notify=False)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
async def amain() -> None:
    problems = config.validate_customer()
    if problems:
        raise SystemExit("تنظیمات ناقص است (.env): " + ", ".join(problems))

    db.init()
    _assert_db_api()

    import account_conn
    import health
    import pool
    import rubika_panel
    import tabchi
    import tg_panel
    import worker

    await bot.start(bot_token=config.CUSTOMER_BOT_TOKEN)
    logbus.bind(bot, role="customer")

    account_conn.set_invalid_auth_handler(_on_invalid_auth)
    account_conn.start_janitor()

    worker.ensure_master_worker()
    await worker.start_all_supervisors()

    rubika_panel.setup(bot, state, _gate, safe_edit, respond, register_steps)
    tg_panel.setup(bot, state, _gate, safe_edit, respond, register_steps)
    tabchi.setup(bot, state, _gate, safe_edit, respond, register_steps)
    pool.setup(bot, state, _gate, safe_edit, respond, register_steps)

    # Resume interrupted work AND re-register it in the busy registry. Without
    # the second half a resumed job is invisible, and the next health pass opens
    # a second connection on top of it and kills the account.
    asyncio.create_task(rubika_panel.restore_pending())
    asyncio.create_task(tg_panel.restore_pending())
    # A pool job can represent hundreds of already-spent probes, so an unfinished
    # one is resumed rather than abandoned.
    asyncio.create_task(pool.restore_pending())
    # Tabchi and the secretary are always-on features: a customer who switched
    # them on expects them to survive a restart, and silently not resuming is
    # indistinguishable from the feature being broken.
    asyncio.create_task(tabchi.restore_engines())

    asyncio.create_task(notification_loop())
    asyncio.create_task(expiry_notice_loop())

    # The health engine runs HERE, in the process that owns the jobs, because the
    # busy registry is in memory. Running it in the owner bot would give it no
    # view of what is mid-send — which is exactly how the base project's engine
    # killed the sessions it was meant to be protecting.
    health.start(notify=_on_invalid_auth)

    counts = db.owner_count_customers()
    await logbus.event("🤖 - #customer_bot_online", [
        cards.kv("Version", config.VERSION),
        cards.kv("Customers", f"{counts['total']} ({counts['active']} active)"),
        cards.kv("Service", "🟢 ONLINE" if db.is_bot_online() else "🔴 OFFLINE"),
        cards.kv("Maintenance", "🧰 ON" if maintenance_on() else "off"),
    ])
    print("customer bot running")
    try:
        await bot.run_until_disconnected()
    finally:
        await health.stop()
        await tabchi.stop_all()
        await account_conn.close_all()


def _assert_db_api() -> None:
    """Fail at BOOT if any database entry point is missing or not callable.

    A production report showed `db.add_account(...)` raising
    "'NoneType' object is not callable" while a fresh import of the same file
    proved it was a healthy function — the classic signature of stale bytecode or
    a half-imported module. Chasing that took several rounds because the only
    evidence arrived after a customer had already hit it.

    This turns that whole class of problem into one loud line at startup, before
    any customer can reach it, and names the exact attribute that is broken
    instead of leaving a mystery in a traceback.
    """
    required = [
        "init", "add_account", "get_account", "list_accounts", "delete_account",
        "get_account_by_phone", "set_status", "set_session_blob",
        "set_account_contacts", "incr_account_sent", "count_accounts",
        "ensure_customer", "get_customer", "is_active", "is_blocked",
        "seconds_left", "touch_customer",
        "get_setting", "set_setting", "get_marker", "get_max_errors",
        "mark_sent", "was_sent", "sent_targets", "usage_incr",
        "probe_budget_left", "probe_spend",
        "tabchi_get", "tabchi_set", "secretary_get", "secretary_set",
        "pool_create_job", "pool_lease_block", "pool_add_contact",
        "tgm_create_job", "tgm_get_job", "tgm_update_job",
        "is_bot_online", "are_sends_frozen", "add_ticket",
    ]
    broken = [name for name in required
              if not callable(getattr(db, name, None))]
    if broken:
        raise SystemExit(
            "db API ناقص است: " + ", ".join(broken) + "\n"
            f"فایل بارگذاری‌شده: {getattr(db, '__file__', '?')}\n"
            "معمولاً یعنی bytecode کهنه است. این را بزن:\n"
            "  find /opt/makemyno -name '__pycache__' -type d "
            "-exec rm -rf {} +\n"
            "  systemctl restart makemyno-customer")


async def _on_invalid_auth(customer_id, phone: str) -> None:
    """A session died. Mark it, tell the owner, and tell the CUSTOMER.

    The base project only told the owner, so from the customer's side an account
    silently stopped working and the service looked broken. Turning a silent
    failure into one actionable message is the cheapest support win available.
    """
    acc = None
    try:
        acc = db.get_account_by_phone(customer_id, phone)
    except db.ScopeError:
        pass
    if acc:
        db.set_status(customer_id, acc["id"], "quarantined")

    await logbus.event("🔴 - #account_down", [
        cards.kv("Customer", customer_id),
        cards.kv("Phone", phone),
        cards.kv("Reason", "session invalid"),
    ])

    if not config.NOTIFY_CUSTOMER_ON_DEAD:
        return
    buttons = None
    if acc:
        buttons = [[Button.inline("🔑 ورود مجدد",
                                  f"rbrelogin_{acc['id']}".encode())]]
    try:
        await bot.send_message(int(customer_id), cards.card("⚠️ اکانت از کار افتاد", [
            cards.kv("شماره", phone, width=8),
            cards.kv("وضعیت", "خارج شده از روبیکا", width=8),
            cards.LINE,
            "برای فعال‌سازی، دوباره وارد شو.",
        ]), buttons=buttons)
    except Exception:
        pass


def busy_snapshot() -> list:
    """Exposed for diagnostics from the owner side."""
    return busy.snapshot()
