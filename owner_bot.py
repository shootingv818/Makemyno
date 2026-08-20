"""
owner_bot.py — the central panel. Only the owner ever talks to this bot.
=======================================================================

Everything a customer must never reach lives here and only here: the worker
fleet, the backup, the customer roster, the audit trail, the kill switch and the
anti-spam shield.

The isolation is structural rather than a permission check:

  * this is a SEPARATE process with a SEPARATE bot token, so a customer has no
    way to send it anything — they are not in the conversation at all;
  * the customer bot does not import central_db, and the owner-only screens
    below are not registered in that process, so there is no stale button in a
    customer's chat history that could reach them;
  * every handler still re-checks is_owner, because callback data is cheap to
    replay and defence in depth costs one comparison.

The owner bot cannot message customers directly (they never started it), so
anything addressed to a customer is queued in db.notifications and delivered by
the customer bot.
"""
from __future__ import annotations

import asyncio
import os
import time

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

import antispam
import backup
import cards
import central_db
import config
import db
import logbus
import ratelimit
import worker

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

bot = TelegramClient(os.path.join(DATA_DIR, "owner_bot"),
                     config.API_ID, config.API_HASH)

# One wizard at a time per owner: {"step": "...", ...}
state: dict = {}


# --------------------------------------------------------------------------- #
# Access + plumbing
# --------------------------------------------------------------------------- #
def is_owner(event) -> bool:
    return bool(config.OWNER_ID) and event.sender_id == config.OWNER_ID


async def safe_edit(event, text: str, buttons=None):
    """Edit in place, tolerating 'content not modified' and expired messages."""
    try:
        return await event.edit(text, buttons=buttons)
    except MessageNotModifiedError:
        return None
    except Exception:
        try:
            return await bot.send_message(event.sender_id, text, buttons=buttons)
        except Exception:
            return None


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


def _num(value) -> str:
    return cards.num(value)


# --------------------------------------------------------------------------- #
# Main menu + dashboard
# --------------------------------------------------------------------------- #
def main_menu() -> list:
    rows = [
        [Button.inline("📊 داشبورد", b"dash"),
         Button.inline("👥 مشتری‌ها", b"customers")],
        [Button.inline("🔎 جستجو", b"search"),
         Button.inline("🏆 رتبه‌بندی", b"ranking")],
        [Button.inline("➕ افزودن مشتری", b"addcust"),
         Button.inline("📣 پیام همگانی", b"broadcast")],
        [Button.inline("🛠 ورکرها", b"workers"),
         Button.inline("📈 آمار ورکرها", b"wstats")],
        [Button.inline("🔍 عیب‌یابی", b"diag"),
         Button.inline("💾 بکاپ", b"backupmenu")],
        [Button.inline("🩺 موتور سلامت", b"healthreport"),
         Button.inline("🧪 تست سلامت داخلی", b"selftest")],
        [Button.inline("🧰 حالت تعمیر", b"maint"),
         Button.inline("⏸ توقف اضطراری", b"freeze")],
        [Button.inline("🛡 سپر ضداسپم", b"shield"),
         Button.inline("⚙️ تنظیمات سرویس", b"settings")],
        [Button.inline("📋 لاگ ممیزی", b"audit")],
    ]
    open_tickets = 0
    try:
        open_tickets = db.owner_count_open_tickets()
    except Exception:
        pass
    label = "📨 تیکت‌ها" + (f" ({open_tickets})" if open_tickets else "")
    rows.insert(4, [Button.inline(label, b"tickets"),
                    Button.inline("😴 مشتری‌های بی‌کار", b"idle")])
    return rows


def dashboard_card() -> str:
    """The headline card: customers, accounts, sends, fleet, service state."""
    counts = db.owner_count_customers()
    totals = db.owner_account_totals()
    fleet = db.accounts_per_worker()
    healthy = sum(1 for w in fleet if w.get("status") == "ok")
    today = db.owner_usage_totals()
    shield = antispam.status()

    rows = [
        cards.kv("Customers", f"{counts['total']}  ({counts['active']} active, "
                              f"{counts['expired']} expired, "
                              f"{counts['blocked']} blocked)"),
        cards.kv("Rubika Acc", f"{_num(totals['rubika']['total'])}  "
                               f"({_num(totals['rubika']['healthy'])} healthy)"),
        cards.kv("TG Acc", f"{_num(totals['telegram']['total'])}  "
                           f"({_num(totals['telegram']['healthy'])} healthy)"),
        cards.kv("Sent Today", _num(today.get("send", 0))),
        cards.kv("Sent Total", _num(totals["rubika"]["sent"]
                                    + totals["telegram"]["sent"])),
        cards.LINE,
        cards.kv("Workers", f"{healthy}/{len(fleet)} healthy"),
        cards.kv("Service", "🟢 ONLINE" if shield["online"] else "🔴 OFFLINE"),
        cards.kv("Sends", "🔴 FROZEN" if db.are_sends_frozen() else "🟢 running"),
        cards.kv("Maintenance", "🧰 ON" if central_db.get_maintenance() else "off"),
        cards.kv("Last Backup", central_db.get_last_backup() or "—"),
    ]
    return cards.panel_card("🎛 - #owner_panel", rows)


@bot.on(events.NewMessage(pattern=r"^/start", func=lambda e: e.is_private))
async def start_handler(event):
    """`is_private` matters: this bot is a member of the log group, and without
    it a /start typed there would print the whole dashboard into the group."""
    if not is_owner(event):
        return                       # strangers get absolute silence
    state.pop(event.sender_id, None)
    await event.respond(dashboard_card(), buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, dashboard_card(), buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, dashboard_card(), buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"dash"))
async def dash_cb(event):
    """The dashboard plus a seven-day trend, so growth or decay is visible."""
    if not is_owner(event):
        return
    series = db.owner_usage_last_days(7, "send")
    peak = max([n for _d, n in series] or [0]) or 1
    rows = []
    for day, count in series:
        rows.append(f"{day[5:]}  {cards.bar(count, peak)}  {_num(count)}")
    if not rows:
        rows = ["— هنوز ارسالی ثبت نشده —"]
    probes = db.owner_usage_totals().get("probe", 0)
    body = [cards.kv("Sent today", _num(db.owner_usage_totals().get("send", 0))),
            cards.kv("Probes today", _num(probes)),
            cards.LINE] + rows
    text = dashboard_card() + "\n\n" + cards.card("📊 - #last_7_days", body)
    await safe_edit(event, text,
                    buttons=[[Button.inline("♻️ بروزرسانی", b"dash")], _back(b"home")])


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #
def _cust_mark(cust: dict) -> str:
    if cust.get("blocked"):
        return "⛔"
    left = db.seconds_left(cust["telegram_id"])
    if left <= 0:
        return "🔴"
    if left <= config.EXPIRY_WARN_DAYS * 86400:
        return "🟡"
    return "🟢"


def _cust_label(cust: dict) -> str:
    name = (cust.get("name") or "—")[:18]
    handle = f" (@{cust['username']})" if cust.get("username") else ""
    rb = db.count_accounts(cust["telegram_id"])["total"]
    tg = db.tg_count_accounts(cust["telegram_id"])["total"]
    if cust.get("blocked"):
        tail = "مسدود"
    else:
        left = db.days_left(cust["telegram_id"])
        tail = f"{left}d" if db.seconds_left(cust["telegram_id"]) > 0 else "منقضی"
    return f"{_cust_mark(cust)} {name}{handle} · 📱{rb + tg} · {tail}"


async def _render_customers(event, page: int = 0, order: str = "created"):
    customers = db.owner_list_customers(order)
    if not customers:
        await safe_edit(event, cards.card("👥 - #customers", [
            "هنوز مشتری‌ای ثبت نشده.",
            "با /start زدن یک کاربر در ربات مشتری، خودش اضافه می‌شود.",
        ]), buttons=[[Button.inline("➕ افزودن دستی", b"addcust")], _back(b"home")])
        return

    page_items, nav, page, total_pages = cards.paginate(
        customers, page, "cpage_", Button)
    counts = db.owner_count_customers()
    head = cards.card("👥 - #customers", [
        cards.kv("Total", counts["total"]),
        cards.kv("Active", f"{counts['active']}   Expired: {counts['expired']}   "
                           f"Blocked: {counts['blocked']}"),
        cards.kv("Page", f"{page + 1}/{total_pages}"),
    ])
    rows = [[Button.inline(_cust_label(c), f"cust_{c['telegram_id']}".encode())]
            for c in page_items]
    if nav:
        rows.append(nav)
    rows.append([Button.inline("🔎 جستجو", b"search"),
                 Button.inline("🏆 رتبه‌بندی", b"ranking")])
    rows.append(_back(b"home"))
    await safe_edit(event, head, buttons=rows)


@bot.on(events.CallbackQuery(data=b"customers"))
async def customers_cb(event):
    if not is_owner(event):
        return
    await _render_customers(event, 0)


@bot.on(events.CallbackQuery(pattern=rb"cpage_(\d+)"))
async def customers_page_cb(event):
    if not is_owner(event):
        return
    await _render_customers(event, int(event.pattern_match.group(1)))


def _profile_card(cust: dict) -> str:
    cid = cust["telegram_id"]
    rb = db.count_accounts(cid)
    tg = db.tg_count_accounts(cid)
    left = db.seconds_left(cid)
    if cust.get("blocked"):
        status = "⛔ BLOCKED"
    elif left > 0:
        status = "🟢 ACTIVE"
    else:
        status = "🔴 EXPIRED"

    worker_tags = []
    for acc in db.list_accounts(cid):
        w = worker.worker_for_account(acc)
        if w and w.get("tag") and w["tag"] not in worker_tags:
            worker_tags.append(w["tag"])

    rows = [
        cards.kv("Name", cust.get("name") or "—"),
        cards.kv("Username", f"@{cust['username']}" if cust.get("username") else "—"),
        cards.kv("ID", cid),
        cards.kv("Joined", (cust.get("created_at") or "—")[:10]),
        cards.kv("Status", status),
        cards.kv("Expires", (cust.get("expires_at") or "—")[:16]
                 + (f"  ({db.days_left(cid)} days left)" if left > 0 else "")),
        cards.LINE,
        cards.kv("Rubika Acc", f"{rb['total']}  ({rb['healthy']} healthy)"),
        cards.kv("TG Acc", f"{tg['total']}  ({tg['healthy']} healthy)"),
        cards.kv("Sent Total", _num(cust.get("total_sends", 0))),
        cards.kv("Last Seen", (cust.get("last_seen") or "—")[:16]),
        cards.kv("Workers", ", ".join(worker_tags) or "—"),
    ]
    if (cust.get("note") or "").strip():
        rows += [cards.LINE, cards.kv("Note", cust["note"][:120])]
    return cards.panel_card("👤 - #customer_profile", rows)


def _profile_buttons(cust: dict) -> list:
    cid = cust["telegram_id"]
    block_btn = (Button.inline("✅ رفع مسدودی", f"unblock_{cid}".encode())
                 if cust.get("blocked")
                 else Button.inline("⛔ مسدود کردن", f"block_{cid}".encode()))
    return [
        [Button.inline("➕ افزودن زمان", f"addt_{cid}".encode()),
         Button.inline("➖ کم کردن زمان", f"subt_{cid}".encode())],
        [block_btn, Button.inline("📝 یادداشت", f"note_{cid}".encode())],
        [Button.inline("💬 پیام مستقیم", f"msg_{cid}".encode()),
         Button.inline("📱 اکانت‌هاش", f"caccs_{cid}".encode())],
        [Button.inline("🗑 حذف مشتری", f"cdel_{cid}".encode())],
        [Button.inline("👥 لیست مشتری‌ها", b"customers"),
         Button.inline("🏠 خانه", b"home")],
    ]


async def _show_profile(event, cid):
    cust = db.get_customer(cid)
    if not cust:
        await event.answer("مشتری پیدا نشد.", alert=True)
        return
    await safe_edit(event, _profile_card(cust), buttons=_profile_buttons(cust))


@bot.on(events.CallbackQuery(pattern=rb"cust_(\d+)"))
async def customer_profile_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await _show_profile(event, int(event.pattern_match.group(1)))


# ---- grant / take back time ---------------------------------------------- #
_DAY_PRESETS = (3, 7, 30, 90)


@bot.on(events.CallbackQuery(pattern=rb"addt_(\d+)$"))
async def add_time_menu_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    rows = [[Button.inline(f"+{d} روز", f"addtd_{cid}_{d}".encode())
             for d in _DAY_PRESETS[:2]],
            [Button.inline(f"+{d} روز", f"addtd_{cid}_{d}".encode())
             for d in _DAY_PRESETS[2:]],
            [Button.inline("✏️ عدد دلخواه", f"addtc_{cid}".encode())],
            [Button.inline("🔙 پروفایل", f"cust_{cid}".encode())]]
    await safe_edit(event, cards.card("➕ - #add_time", [
        cards.kv("Customer", cid),
        cards.kv("Now", (db.get_customer(cid) or {}).get("expires_at") or "—"),
        "چند روز اضافه شود؟",
    ]), buttons=rows)


@bot.on(events.CallbackQuery(pattern=rb"subt_(\d+)$"))
async def sub_time_menu_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    rows = [[Button.inline(f"−{d} روز", f"addtd_{cid}_-{d}".encode())
             for d in _DAY_PRESETS[:2]],
            [Button.inline(f"−{d} روز", f"addtd_{cid}_-{d}".encode())
             for d in _DAY_PRESETS[2:]],
            [Button.inline("🔙 پروفایل", f"cust_{cid}".encode())]]
    await safe_edit(event, cards.card("➖ - #remove_time", [
        cards.kv("Customer", cid),
        cards.kv("Now", (db.get_customer(cid) or {}).get("expires_at") or "—"),
        "چند روز کم شود؟",
    ]), buttons=rows)


async def _grant_days(event, cid: int, days: float):
    cust = db.get_customer(cid)
    if not cust:
        await event.answer("مشتری پیدا نشد.", alert=True)
        return
    new_expiry = db.add_days(cid, days)
    central_db.audit("time" if days >= 0 else "time_removed",
                     f"{cid} {days:+g}d -> {new_expiry}")
    await logbus.event("⏱ - #time_changed", [
        cards.kv("Customer", cust.get("name") or cid),
        cards.kv("ID", cid),
        cards.kv("Change", f"{days:+g} days"),
        cards.kv("New expiry", new_expiry),
    ])
    # the owner bot cannot DM a customer, so this goes through the outbox
    verb = "افزایش" if days >= 0 else "کاهش"
    db.queue_notification(cid, cards.card("⏱ تغییر مدت دسترسی", [
        cards.kv("وضعیت", f"{verb} یافت", width=10),
        cards.kv("اعتبار تا", new_expiry[:16], width=10),
        cards.kv("روز باقی", db.days_left(cid), width=10),
    ]))
    await _show_profile(event, cid)


@bot.on(events.CallbackQuery(pattern=rb"addtd_(\d+)_(-?\d+)"))
async def add_time_apply_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    days = int(event.pattern_match.group(2))
    await _grant_days(event, cid, days)


@bot.on(events.CallbackQuery(pattern=rb"addtc_(\d+)"))
async def add_time_custom_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_days", "cid": cid}
    await safe_edit(event, cards.card("✏️ - #add_time", [
        cards.kv("Customer", cid),
        "تعداد روز را بفرست. عدد منفی زمان را کم می‌کند.",
        "مثال: 45  یا  -10",
    ]), buttons=[[Button.inline("🔙 انصراف", f"cust_{cid}".encode())]])


# ---- block / unblock ------------------------------------------------------ #
@bot.on(events.CallbackQuery(pattern=rb"block_(\d+)"))
async def block_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    db.set_blocked(cid, True)
    central_db.audit("block", str(cid))
    await logbus.event("⛔ - #customer_blocked", [
        cards.kv("ID", cid), cards.kv("By", "owner")])
    db.queue_notification(cid, cards.card("⛔ دسترسی مسدود شد", [
        "حساب شما توسط مدیر مسدود شده است.",
        "برای پیگیری با پشتیبانی تماس بگیرید.",
    ]))
    await _show_profile(event, cid)


@bot.on(events.CallbackQuery(pattern=rb"unblock_(\d+)"))
async def unblock_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    await ratelimit.unblock(cid, by="owner")
    central_db.audit("unblock", str(cid))
    db.queue_notification(cid, cards.card("✅ دسترسی برقرار شد", [
        "مسدودیت حساب شما برداشته شد.",
    ]))
    await _show_profile(event, cid)


# ---- note / direct message ----------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=rb"note_(\d+)"))
async def note_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_note", "cid": cid}
    current = (db.get_customer(cid) or {}).get("note") or "—"
    await safe_edit(event, cards.card("📝 - #note", [
        cards.kv("Customer", cid),
        cards.kv("Current", current[:150]),
        "یادداشت جدید را بفرست. برای پاک کردن، یک خط تیره بفرست: -",
    ]), buttons=[[Button.inline("🔙 انصراف", f"cust_{cid}".encode())]])


@bot.on(events.CallbackQuery(pattern=rb"msg_(\d+)"))
async def direct_msg_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_dm", "cid": cid}
    await safe_edit(event, cards.card("💬 - #direct_message", [
        cards.kv("Customer", cid),
        "متن پیام را بفرست. در پیوی خودش تحویل داده می‌شود.",
    ]), buttons=[[Button.inline("🔙 انصراف", f"cust_{cid}".encode())]])


# ---- a customer's accounts ------------------------------------------------ #
@bot.on(events.CallbackQuery(pattern=rb"caccs_(\d+)"))
async def customer_accounts_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    rb_accounts = db.list_accounts(cid)
    tg_accounts = db.tg_list_accounts(cid)
    rows = [cards.kv("Customer", cid), cards.LINE, "🟣 Rubika"]
    if rb_accounts:
        for acc in rb_accounts[:40]:
            w = worker.worker_for_account(acc)
            tag = (w or {}).get("tag", "—")
            mark = "🟢" if acc["status"] == "active" else "🔴"
            rows.append(f"   {mark} {acc['phone']} · {tag} · "
                        f"✉️{_num(acc.get('sent_total', 0))}")
    else:
        rows.append("   —")
    rows += [cards.LINE, "✈️ Telegram"]
    if tg_accounts:
        for acc in tg_accounts[:40]:
            mark = "🟢" if acc["status"] == "active" else "🔴"
            rows.append(f"   {mark} {acc['phone']} · "
                        f"✉️{_num(acc.get('sent_total', 0))}")
    else:
        rows.append("   —")
    await safe_edit(event, cards.panel_card("📱 - #customer_accounts", rows),
                    buttons=[[Button.inline("🔙 پروفایل", f"cust_{cid}".encode())]])


# ---- delete a customer (typed confirmation) ------------------------------- #
@bot.on(events.CallbackQuery(pattern=rb"cdel_(\d+)"))
async def customer_delete_cb(event):
    if not is_owner(event):
        return
    cid = int(event.pattern_match.group(1))
    cust = db.get_customer(cid) or {}
    rb = db.count_accounts(cid)["total"]
    tg = db.tg_count_accounts(cid)["total"]
    state[event.sender_id] = {"step": "await_del_confirm", "cid": cid}
    # A typed confirmation, not a second button: one mis-tap must not be able to
    # erase a customer and every account they own.
    await safe_edit(event, cards.card("🗑 - #delete_customer", [
        cards.kv("Customer", cust.get("name") or cid),
        cards.kv("ID", cid),
        cards.kv("Accounts", f"{rb} Rubika + {tg} Telegram"),
        cards.LINE,
        "این کار برگشت‌پذیر نیست.",
        f"برای تأیید، همین آیدی را بفرست: {cid}",
    ]), buttons=[[Button.inline("🔙 انصراف", f"cust_{cid}".encode())]])


# ---- add a customer by hand ---------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"addcust"))
async def add_customer_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_new_customer"}
    await safe_edit(event, cards.card("➕ - #add_customer", [
        "آیدی عددی مشتری را بفرست.",
        "می‌توانی روز را هم بعد از فاصله بنویسی.",
        "مثال:  774119203 30",
    ]), buttons=[_back(b"customers")])


# --------------------------------------------------------------------------- #
# Search / ranking / idle
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"search"))
async def search_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_search"}
    await safe_edit(event, cards.card("🔎 - #search", [
        "آیدی، یوزرنیم، نام یا شماره‌ی اکانت را بفرست.",
        "با شماره، مشتریِ صاحب آن اکانت پیدا می‌شود.",
    ]), buttons=[_back(b"customers")])


@bot.on(events.CallbackQuery(data=b"ranking"))
async def ranking_cb(event):
    if not is_owner(event):
        return
    await _render_ranking(event, "sends")


@bot.on(events.CallbackQuery(pattern=rb"rank_(\w+)"))
async def ranking_sort_cb(event):
    if not is_owner(event):
        return
    await _render_ranking(event, event.pattern_match.group(1).decode())


async def _render_ranking(event, order: str):
    customers = db.owner_list_customers("sends" if order == "sends" else "created")
    if order == "accounts":
        customers.sort(key=lambda c: db.count_accounts(c["telegram_id"])["total"]
                       + db.tg_count_accounts(c["telegram_id"])["total"],
                       reverse=True)
    rows = []
    for i, cust in enumerate(customers[:20], 1):
        cid = cust["telegram_id"]
        total = (db.count_accounts(cid)["total"]
                 + db.tg_count_accounts(cid)["total"])
        name = (cust.get("name") or str(cid))[:16]
        rows.append(f"{i:>2}. {name:<16} ✉️{_num(cust.get('total_sends', 0)):>9}  "
                    f"📱{total}")
    if not rows:
        rows = ["—"]
    labels = {"sends": "ارسال", "accounts": "تعداد اکانت", "created": "قدمت"}
    await safe_edit(event, cards.panel_card(
        f"🏆 - #ranking ({labels.get(order, order)})", rows), buttons=[
        [Button.inline("✉️ ارسال", b"rank_sends"),
         Button.inline("📱 اکانت", b"rank_accounts"),
         Button.inline("🕰 قدمت", b"rank_created")],
        _back(b"home"),
    ])


@bot.on(events.CallbackQuery(data=b"idle"))
async def idle_cb(event):
    """Customers who stopped using the service — the churn list."""
    if not is_owner(event):
        return
    idle = db.owner_customers_idle(10)
    rows = [cards.kv("Rule", "بدون فعالیت در ۱۰ روز گذشته"), cards.LINE]
    if idle:
        for cust in idle[:25]:
            rows.append(f"🟡 {(cust.get('name') or cust['telegram_id'])} · "
                        f"آخرین بازدید {(cust.get('last_seen') or '—')[:10]}")
    else:
        rows.append("همه فعالن. 👌")
    buttons = [[Button.inline(f"👤 {c.get('name') or c['telegram_id']}",
                              f"cust_{c['telegram_id']}".encode())]
               for c in idle[:8]]
    buttons.append(_back(b"home"))
    await safe_edit(event, cards.panel_card("😴 - #idle_customers", rows),
                    buttons=buttons)


# --------------------------------------------------------------------------- #
# Broadcast
# --------------------------------------------------------------------------- #
def _audience(kind: str) -> list:
    customers = db.owner_list_customers()
    if kind == "active":
        return [c for c in customers
                if not c.get("blocked") and db.seconds_left(c["telegram_id"]) > 0]
    if kind == "expired":
        return [c for c in customers
                if not c.get("blocked") and db.seconds_left(c["telegram_id"]) <= 0]
    if kind == "soon":
        return db.owner_customers_expiring(config.EXPIRY_WARN_DAYS)
    return customers


@bot.on(events.CallbackQuery(data=b"broadcast"))
async def broadcast_cb(event):
    if not is_owner(event):
        return
    last = (central_db.list_broadcasts(1) or [{}])[0]
    rows = [
        cards.kv("All", len(_audience("all"))),
        cards.kv("Active", len(_audience("active"))),
        cards.kv("Expired", len(_audience("expired"))),
        cards.kv("Expiring", len(_audience("soon"))),
    ]
    if last:
        rows += [cards.LINE,
                 cards.kv("Last", f"{(last.get('created_at') or '')[:16]} → "
                                  f"{last.get('queued', 0)} نفر")]
    rows.append("مخاطب را انتخاب کن:")
    await safe_edit(event, cards.card("📣 - #broadcast", rows), buttons=[
        [Button.inline("👥 همه", b"bcast_all"),
         Button.inline("🟢 فعال‌ها", b"bcast_active")],
        [Button.inline("🔴 منقضی‌ها", b"bcast_expired"),
         Button.inline("🟡 نزدیک انقضا", b"bcast_soon")],
        [Button.inline("📋 تاریخچه", b"bcast_hist")],
        _back(b"home"),
    ])


@bot.on(events.CallbackQuery(pattern=rb"bcast_(all|active|expired|soon)"))
async def broadcast_pick_cb(event):
    if not is_owner(event):
        return
    kind = event.pattern_match.group(1).decode()
    targets = _audience(kind)
    if not targets:
        await event.answer("این گروه مخاطب خالیه.", alert=True)
        return
    state[event.sender_id] = {"step": "await_broadcast", "kind": kind}
    await safe_edit(event, cards.card("📣 - #broadcast", [
        cards.kv("Audience", f"{kind} ({len(targets)} نفر)"),
        "متن پیام را بفرست.",
    ]), buttons=[_back(b"broadcast")])


@bot.on(events.CallbackQuery(data=b"bcast_hist"))
async def broadcast_hist_cb(event):
    if not is_owner(event):
        return
    rows = []
    for item in central_db.list_broadcasts(12):
        rows.append(f"{(item.get('created_at') or '')[:16]} · "
                    f"{item.get('audience')} · {item.get('queued')} نفر")
        rows.append(f"   «{(item.get('text') or '')[:60]}»")
    await safe_edit(event, cards.panel_card("📋 - #broadcast_history",
                                            rows or ["—"]),
                    buttons=[_back(b"broadcast")])



# --------------------------------------------------------------------------- #
# Worker fleet
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"workers"))
async def workers_cb(event):
    if not is_owner(event):
        return
    worker.ensure_master_worker()
    fleet = db.accounts_per_worker()
    healthy = sum(1 for w in fleet if w.get("status") == "ok")
    remotes = sum(1 for w in fleet if not w.get("is_master"))
    head = cards.card("🛠 - #workers", [
        cards.kv("Total", f"{len(fleet)}  (master: {len(fleet) - remotes} | "
                          f"remote: {remotes})"),
        cards.kv("Healthy", f"{healthy}/{len(fleet)}"),
        cards.kv("Accounts", _num(sum(w["total"] for w in fleet))),
        cards.kv("Sent today", _num(sum(w["sent_today"] for w in fleet))),
        cards.LINE,
        "برای جزئیات روی هر ورکر بزن:" if fleet else "— هنوز ورکری اضافه نشده —",
    ])
    rows = []
    for w in fleet:
        icon = worker.status_emoji(w)
        kind = "👑" if w.get("is_master") else "🖥"
        off = "" if w.get("enabled") else " ⏸"
        rows.append([Button.inline(
            f"{icon}{kind} {w['tag']} · 📱{w['total']} · 📶{worker.ping_text(w)}{off}",
            f"wk_{w['id']}".encode())])
    rows += [
        [Button.inline("➕ افزودن ورکر", b"wk_add"),
         Button.inline("🩺 بررسی همه", b"w_checkall")],
        [Button.inline("⬆️ آپدیت همه", b"w_updall"),
         Button.inline("📦 نسخه‌ها", b"w_versions")],
        [Button.inline("📈 آمار ورکرها", b"wstats")],
        _back(b"home"),
    ]
    await safe_edit(event, head, buttons=rows)


@bot.on(events.CallbackQuery(data=b"wstats"))
async def worker_stats_cb(event):
    """One screen answering 'which worker holds how many accounts, and how many
    of them are alive'."""
    if not is_owner(event):
        return
    fleet = db.accounts_per_worker()
    if not fleet:
        await safe_edit(event, cards.card("📈 - #worker_stats", ["— ورکری نیست —"]),
                        buttons=[_back(b"workers")])
        return

    rows = []
    for w in fleet:
        icon = worker.status_emoji(w)
        kind = "👑" if w.get("is_master") else "🖥"
        # An offline worker leaves its accounts UNCHECKED, which is not the same
        # as dead — showing them as dead would panic the owner for no reason.
        if w.get("status") == "ok":
            state_text = f"🟢{w['healthy']} 🔴{w['dead']}"
        else:
            state_text = f"⚪️{w['total']} (نامعلوم)"
        rows.append(f"{icon}{kind} {w['tag']:<10} 📱{w['total']:<4} {state_text:<20} "
                    f"✉️{_num(w['sent_today'])}")

    total = sum(w["total"] for w in fleet)
    healthy = sum(w["healthy"] for w in fleet if w.get("status") == "ok")
    checked = [w for w in fleet if w.get("status") == "ok"]
    fullest = max(fleet, key=lambda w: w["total"])
    emptiest = min(fleet, key=lambda w: w["total"])
    pct = int(round(100 * healthy / total)) if total else 0

    rows += [
        cards.LINE,
        cards.kv("Total", f"{_num(total)} accounts on {len(fleet)} workers"),
        cards.kv("Healthy", f"{_num(healthy)}  ({pct}%)"),
        cards.kv("Avg / worker", total // max(1, len(fleet))),
        cards.kv("Fullest", f"{fullest['tag']} ({fullest['total']})"),
        cards.kv("Emptiest", f"{emptiest['tag']} ({emptiest['total']})"),
    ]
    if len(checked) < len(fleet):
        rows.append(cards.kv("Note", f"{len(fleet) - len(checked)} worker(s) "
                                     f"unchecked — accounts unknown, not dead"))
    await safe_edit(event, cards.panel_card("📈 - #worker_stats", rows), buttons=[
        [Button.inline("♻️ بروزرسانی", b"wstats"),
         Button.inline("🩺 بررسی همه", b"w_checkall")],
        _back(b"workers"),
    ])


@bot.on(events.CallbackQuery(pattern=rb"wk_(\d+)$"))
async def worker_detail_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    stats = db.worker_account_stats(wid)
    detail = worker.health_detail(wid)
    rows = [
        cards.kv("Tag", w["tag"]),
        cards.kv("Type", "MASTER" if w.get("is_master") else "REMOTE"),
        cards.kv("State", f"{worker.status_emoji(w)} {str(w.get('status')).upper()}"
                          + ("" if w.get("enabled") else "  (disabled)")),
        cards.kv("Address", "local" if w.get("is_master")
                 else f"{w['ip']}:{w['ssh_port']}"),
        cards.kv("Ping", worker.ping_text(w)),
        cards.kv("Route", worker.route_label(w)),
        cards.kv("Last check", (w.get("last_checked") or "—")[:16]),
    ]
    if detail:
        rows.append(cards.kv("Detail", str(detail)[:90]))
    rows += [
        cards.LINE,
        cards.kv("Accounts", f"{stats['total']}  ({stats['healthy']} healthy, "
                             f"{stats['dead']} dead)"),
        cards.kv("Customers", stats["customers"]),
        cards.kv("Sent today", _num(db.worker_sent_today(wid))),
    ]
    toggle = (Button.inline("⏸ غیرفعال کردن", f"wktog_{wid}".encode())
              if w.get("enabled")
              else Button.inline("▶️ فعال کردن", f"wktog_{wid}".encode()))
    buttons = [[toggle, Button.inline("🩺 بررسی", f"wkchk_{wid}".encode())]]
    if not w.get("is_master"):
        buttons += [
            [Button.inline("🔄 ری‌استارت", f"wkrst_{wid}".encode()),
             Button.inline("⬆️ آپدیت", f"wkupd_{wid}".encode())],
            # The answer to "api error: Server disconnected" lives in the
            # container's own log, and reaching it used to mean opening an SSH
            # session by hand.
            [Button.inline("📜 لاگ ورکر", f"wklog_{wid}".encode())],
            [Button.inline("🗑 حذف ورکر", f"wkdel_{wid}".encode())],
        ]
    buttons.append([Button.inline("👥 مشتری‌های این ورکر", f"wcust_{wid}".encode())])
    buttons.append(_back(b"workers"))
    await safe_edit(event, cards.panel_card("🖥 - #worker_detail", rows),
                    buttons=buttons)


@bot.on(events.CallbackQuery(pattern=rb"wcust_(\d+)"))
async def worker_customers_cb(event):
    """Who is affected when this worker's IP gets throttled.

    Shared workers mean one customer's trouble becomes everyone's trouble on that
    box, so being able to warn exactly the right people before they open tickets
    is worth a screen of its own.
    """
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    rows = [cards.kv("Worker", w["tag"]), cards.LINE]
    listed = db.worker_customers(wid)
    if listed:
        for item in listed:
            name = item.get("name") or item["telegram_id"]
            handle = f" (@{item['username']})" if item.get("username") else ""
            rows.append(f"👤 {name}{handle} · 📱{item['accounts']}")
    else:
        rows.append("— هیچ اکانتی روی این ورکر نیست —")
    buttons = [[Button.inline(f"📣 هشدار به {len(listed)} مشتری",
                              f"wwarn_{wid}".encode())]] if listed else []
    buttons.append([Button.inline("🔙 ورکر", f"wk_{wid}".encode())])
    await safe_edit(event, cards.panel_card("👥 - #worker_customers", rows),
                    buttons=buttons)


@bot.on(events.CallbackQuery(pattern=rb"wwarn_(\d+)"))
async def worker_warn_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_worker_warn", "wid": wid}
    listed = db.worker_customers(wid)
    await safe_edit(event, cards.card("📣 - #warn_worker_customers", [
        cards.kv("Recipients", len(listed)),
        "متن هشدار را بفرست (مثلاً: اختلال موقت، در حال بررسی).",
    ]), buttons=[[Button.inline("🔙 انصراف", f"wcust_{wid}".encode())]])


@bot.on(events.CallbackQuery(pattern=rb"wktog_(\d+)"))
async def worker_toggle_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    new_state = not bool(w.get("enabled"))
    db.set_worker_enabled(wid, new_state)
    central_db.audit("worker_toggle", f"{w['tag']} -> "
                                      f"{'enabled' if new_state else 'disabled'}")
    fresh = db.get_worker(wid)
    if new_state:
        worker.start_supervisor(fresh)
    else:
        await worker.stop_supervisor(wid)
    await worker_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=rb"wkchk_(\d+)"))
async def worker_check_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    await event.answer("در حال بررسی ...")
    try:
        await worker.check_worker(w)
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="worker.check", notify=False)
    await worker_detail_cb(event)


@bot.on(events.CallbackQuery(data=b"w_checkall"))
async def worker_check_all_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال بررسی همه ...")
    try:
        await worker.check_all()
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context="worker.check_all", notify=False)
    await workers_cb(event)


@bot.on(events.CallbackQuery(pattern=rb"wkrst_(\d+)"))
async def worker_restart_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w.get("is_master"):
        return
    await event.answer("ری‌استارت ...")
    try:
        code, out, err = await worker.restart_worker(w)
        ok = code == 0
        await logbus.event("🔄 - #worker_restart", [
            cards.kv("Worker", w["tag"]),
            cards.kv("Result", "OK" if ok else f"failed: {(err or out)[:120]}"),
        ])
        central_db.audit("worker_restart", f"{w['tag']} ok={ok}")
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context=f"worker.restart {w['tag']}", notify=False)
    await worker_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=rb"wklog_(\d+)"))
async def worker_log_cb(event):
    """Show the worker container's own log, with a verdict where we can give one.

    "api error: RemoteProtocolError: Server disconnected without sending a
    response" means the tunnel opened and nothing was listening on the far side.
    The reason is always in the container log, and reaching it used to require an
    SSH session — so the panel could report a problem it could not explain.
    """
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w.get("is_master"):
        await event.answer("این ورکر لاگ کانتینر ندارد.", alert=True)
        return
    await event.answer("در حال خواندن لاگ ...")
    blob = await worker.worker_logs(w, tail=60)
    verdict = worker.explain_worker_log(blob)

    rows = [cards.kv("Worker", w["tag"]),
            cards.kv("Address", f"{w['ip']}:{w['ssh_port']}")]
    if verdict:
        rows += [cards.LINE, verdict]
    rows.append(cards.LINE)
    # Telegram caps a message near 4096 characters, so keep the tail and let the
    # card explain that it is a tail.
    tail = blob[-2400:]
    if len(blob) > 2400:
        rows.append("(انتهای لاگ)")
    head = cards.panel_card("\U0001F4DC - #worker_log", rows)
    buttons = [[Button.inline("\u267B\uFE0F \u062E\u0648\u0627\u0646\u062F\u0646 \u062F\u0648\u0628\u0627\u0631\u0647",
                              f"wklog_{wid}".encode())],
               [Button.inline("\U0001F504 \u0631\u06CC\u200C\u0627\u0633\u062A\u0627\u0631\u062A", f"wkrst_{wid}".encode()),
                Button.inline("\u2B06\uFE0F \u0622\u067E\u062F\u06CC\u062A", f"wkupd_{wid}".encode())],
               [Button.inline("\U0001F519 \u0648\u0631\u06A9\u0631", f"wk_{wid}".encode())]]
    try:
        await safe_edit(event, head + f"\n```\n{tail}\n```",
                        buttons=buttons)
    except Exception:
        await safe_edit(event, head, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=rb"wkupd_(\d+)"))
async def worker_update_cb(event):
    """Update one worker — after showing exactly which repo it will be moved to.

    A stale GIT_REPO_URL here would silently move the fleet onto older code, so
    the target is always displayed and confirmed rather than assumed.
    """
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w.get("is_master"):
        return
    await safe_edit(event, cards.card("⬆️ - #worker_update", [
        cards.kv("Worker", w["tag"]),
        cards.kv("Repo", config.GIT_REPO_URL),
        cards.kv("Branch", config.GIT_BRANCH),
        cards.LINE,
        "ورکر به این سورس منتقل و دوباره ساخته می‌شود.",
        "اگر آدرس سورس اشتباه باشد، ورکر به کد قدیمی برمی‌گردد.",
    ]), buttons=[
        [Button.inline("✅ آپدیت کن", f"wkupdy_{wid}".encode())],
        [Button.inline("🔙 انصراف", f"wk_{wid}".encode())],
    ])


@bot.on(events.CallbackQuery(pattern=rb"wkupdy_(\d+)"))
async def worker_update_apply_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w.get("is_master"):
        return
    await safe_edit(event, cards.card("⬆️ - #worker_update", [
        cards.kv("Worker", w["tag"]), "⏳ در حال آپدیت (چند دقیقه) ..."]))
    asyncio.create_task(_run_worker_update(event.sender_id, [w]))


@bot.on(events.CallbackQuery(data=b"w_updall"))
async def worker_update_all_cb(event):
    """Staged update: two workers first, so a bad build cannot take the fleet."""
    if not is_owner(event):
        return
    remotes = [w for w in db.list_workers() if not worker.is_local(w)]
    if not remotes:
        await event.answer("ورکر ریموتی برای آپدیت نیست.", alert=True)
        return
    await safe_edit(event, cards.card("⬆️ - #update_all", [
        cards.kv("Workers", len(remotes)),
        cards.kv("Repo", config.GIT_REPO_URL),
        cards.kv("Branch", config.GIT_BRANCH),
        cards.LINE,
        "پیشنهاد: اول ۲ ورکر را آپدیت کن، نتیجه را ببین، بعد بقیه.",
        "اگر نسخه‌ی جدید مشکل داشته باشد، کل ناوگان نمی‌خوابد.",
    ]), buttons=[
        [Button.inline("🐢 اول ۲ ورکر", b"w_upd_stage")],
        [Button.inline("⚡ همه با هم", b"w_upd_all_now")],
        _back(b"workers"),
    ])


@bot.on(events.CallbackQuery(data=b"w_upd_stage"))
async def worker_update_stage_cb(event):
    if not is_owner(event):
        return
    remotes = [w for w in db.list_workers() if not worker.is_local(w)][:2]
    await safe_edit(event, cards.card("⬆️ - #update_all", [
        cards.kv("Stage", f"{len(remotes)} worker(s)"), "⏳ شروع شد ..."]))
    asyncio.create_task(_run_worker_update(event.sender_id, remotes))


@bot.on(events.CallbackQuery(data=b"w_upd_all_now"))
async def worker_update_all_now_cb(event):
    if not is_owner(event):
        return
    remotes = [w for w in db.list_workers() if not worker.is_local(w)]
    await safe_edit(event, cards.card("⬆️ - #update_all", [
        cards.kv("Workers", len(remotes)), "⏳ شروع شد ..."]))
    asyncio.create_task(_run_worker_update(event.sender_id, remotes))


async def _run_worker_update(owner_id: int, workers: list) -> None:
    """Update workers one at a time and report a per-worker verdict.

    Sequential on purpose: parallel rebuilds on several boxes at once is how you
    end up with a fleet that is half old and half new with nobody knowing which.
    """
    done, failed = [], []
    for w in workers:
        try:
            code, out, err = await worker.update_worker(w)
            if code == 0:
                done.append(w["tag"])
            else:
                failed.append((w["tag"], (err or out)[-160:]))
        except Exception as exc:  # noqa: BLE001
            failed.append((w["tag"], f"{type(exc).__name__}: {str(exc)[:120]}"))

    # Health check after the update: knowing WHICH ones broke, before the
    # customers find out, is the whole point of doing this staged.
    await asyncio.sleep(20)
    try:
        await worker.check_all()
    except Exception:
        pass
    unhealthy = [w["tag"] for w in db.list_workers()
                 if not worker.is_local(w) and w.get("enabled")
                 and w.get("status") != "ok"]

    rows = [cards.kv("Updated", ", ".join(done) or "—")]
    if failed:
        rows.append(cards.kv("Failed", len(failed)))
        for tag, why in failed:
            rows.append(f"   ❌ {tag}: {why}")
    if unhealthy:
        rows += [cards.LINE,
                 cards.kv("⚠️ Unhealthy", ", ".join(unhealthy)),
                 "این ورکرها بعد از آپدیت جواب نمی‌دهند."]
    else:
        rows.append(cards.kv("Health", "✅ همه سالم"))
    central_db.audit("worker_update", f"ok={len(done)} failed={len(failed)}")
    card_text = cards.panel_card("⬆️ - #update_report", rows)
    await logbus.to_group(card_text)
    try:
        await bot.send_message(owner_id, card_text, buttons=[_back(b"workers")])
    except Exception:
        pass


@bot.on(events.CallbackQuery(data=b"w_versions"))
async def worker_versions_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال خواندن نسخه‌ها ...")
    master_version = worker.master_code_version()
    rows = [cards.kv("Master", f"{master_version}  (branch {config.GIT_BRANCH})"),
            cards.LINE]
    for w in db.list_workers():
        if worker.is_local(w):
            rows.append(f"👑 {w['tag']} : {master_version} ✅")
            continue
        version = await worker.worker_code_version(w)
        if version == "?":
            mark = "❔ بی‌جواب"
        elif version == master_version:
            mark = "✅ آخرین"
        else:
            mark = "⚠️ عقب‌مانده"
        rows.append(f"🖥 {w['tag']} : {version} {mark}")
    await safe_edit(event, cards.panel_card("📦 - #worker_versions", rows),
                    buttons=[[Button.inline("⬆️ آپدیت همه", b"w_updall")],
                             _back(b"workers")])


# ---- add worker wizard ---------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"wk_add"))
async def worker_add_cb(event):
    if not is_owner(event):
        return
    import crypto_util
    if not crypto_util.is_configured():
        await event.answer("اول WORKER_SECRET را در .env تنظیم کن.", alert=True)
        return
    state[event.sender_id] = {"step": "wk_ip", "wk": {}}
    await safe_edit(event, cards.card("➕ - #add_worker", [
        "آی‌پی سرور را بفرست.",
        "بعد پورت SSH، یوزر و پسورد پرسیده می‌شود.",
    ]), buttons=[_back(b"workers")])


@bot.on(events.CallbackQuery(pattern=rb"wkdel_(\d+)"))
async def worker_delete_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w.get("is_master"):
        return
    stats = db.worker_account_stats(wid)
    state[event.sender_id] = {"step": "await_wk_del", "wid": wid, "tag": w["tag"]}
    # Typed confirmation: deleting a worker detaches every account on it, and a
    # fat-fingered tap should not be able to do that to dozens of customers.
    await safe_edit(event, cards.card("🗑 - #delete_worker", [
        cards.kv("Worker", w["tag"]),
        cards.kv("Accounts", f"{stats['total']} (of {stats['customers']} customers)"),
        cards.LINE,
        "کانتینر حذف و اکانت‌ها از این ورکر جدا می‌شوند.",
        f"برای تأیید، تگ ورکر را بفرست: {w['tag']}",
    ]), buttons=[[Button.inline("🔙 انصراف", f"wk_{wid}".encode())]])


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"backupmenu"))
async def backup_menu_cb(event):
    if not is_owner(event):
        return
    stats = backup.stats()
    hours = stats["interval"] // 3600 if stats["interval"] else 0
    rows = [
        cards.kv("Content", "sessions only"),
        cards.kv("Rubika", _num(stats["rb_local"]) + " local"),
        cards.kv("Telegram", _num(stats["tg"])),
        cards.kv("Encryption", "🔐 ON" if stats["encrypted"]
                 else "⚠️ OFF — بکاپ ساخته نمی‌شود"),
        cards.kv("Last backup", stats["last"] or "—"),
        cards.kv("Automatic", f"every {hours}h" if hours else "off"),
        cards.LINE,
        "دیتابیس، تنظیمات و لاگ عمداً داخل بکاپ نیستند.",
    ]
    await safe_edit(event, cards.panel_card("💾 - #backup", rows), buttons=[
        [Button.inline("💾 بکاپ فوری", b"bk_now")],
        _back(b"home"),
    ])


@bot.on(events.CallbackQuery(data=b"bk_now"))
async def backup_now_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال ساخت بکاپ ...")
    await safe_edit(event, cards.card("💾 - #backup", ["⏳ در حال جمع‌آوری سشن‌ها ..."]))
    result = await backup.run_backup(to_owner=event.sender_id)
    if result["ok"]:
        central_db.audit("backup", "manual")
        rows = backup.summary_rows(result["meta"])
    elif result["error"] == "no-sessions":
        rows = ["سشنی برای بکاپ وجود ندارد."]
    else:
        rows = [cards.kv("Error", str(result["error"])[:150])]
    await safe_edit(event, cards.panel_card("💾 - #backup", rows),
                    buttons=[[Button.inline("💾 دوباره", b"bk_now")],
                             _back(b"backupmenu")])


# --------------------------------------------------------------------------- #
# Maintenance / emergency stop / shield
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"maint"))
async def maintenance_cb(event):
    if not is_owner(event):
        return
    on = central_db.get_maintenance()
    rows = [
        cards.kv("Mode", "🧰 ON" if on else "⚪️ OFF"),
        cards.kv("Notice", central_db.get_notice() or "—"),
        cards.LINE,
        "در حالت تعمیر، پنل مشتری‌ها قفل می‌شود.",
        "کارهای در جریان متوقف نمی‌شوند — برای آن «توقف اضطراری» هست.",
    ]
    toggle = (Button.inline("⚪️ خاموش کردن", b"maint_off") if on
              else Button.inline("🧰 روشن کردن", b"maint_on"))
    await safe_edit(event, cards.panel_card("🧰 - #maintenance", rows), buttons=[
        [toggle, Button.inline("✍️ متن اطلاع", b"maint_note")],
        _back(b"home"),
    ])


@bot.on(events.CallbackQuery(pattern=rb"maint_(on|off)"))
async def maintenance_toggle_cb(event):
    if not is_owner(event):
        return
    on = event.pattern_match.group(1).decode() == "on"
    central_db.set_maintenance(on)
    central_db.audit("maintenance", "on" if on else "off")
    await logbus.event("🧰 - #maintenance", [
        cards.kv("Mode", "ON" if on else "OFF")])
    await maintenance_cb(event)


@bot.on(events.CallbackQuery(data=b"maint_note"))
async def maintenance_note_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_maint_note"}
    await safe_edit(event, cards.card("✍️ - #maintenance_notice", [
        "متنی که در حالت تعمیر به مشتری نشان داده می‌شود را بفرست.",
        "مثال: سرویس در حال بروزرسانی است، ۱۰ دقیقه دیگر برگرد.",
    ]), buttons=[_back(b"maint")])


@bot.on(events.CallbackQuery(data=b"freeze"))
async def freeze_cb(event):
    """The emergency stop.

    Different from maintenance: maintenance closes the panel but lets running
    jobs finish, while this halts the sending itself. When the platform starts
    mass-banning, one tap has to stop everything instead of watching accounts
    burn one by one.
    """
    if not is_owner(event):
        return
    frozen = db.are_sends_frozen()
    state_row = db.get_bot_state()
    rows = [
        cards.kv("Sends", "🔴 FROZEN" if frozen else "🟢 running"),
        cards.kv("Since", (state_row.get("frozen_at") or "—")[:16]),
        cards.LINE,
        "با توقف اضطراری، همه‌ی ارسال‌های همه‌ی مشتری‌ها می‌ایستد.",
        "پنل باز می‌ماند و کارها بعد از رفع توقف ادامه‌پذیرند.",
    ]
    toggle = (Button.inline("▶️ رفع توقف", b"freeze_off") if frozen
              else Button.inline("⏸ توقف همه‌ی ارسال‌ها", b"freeze_on"))
    await safe_edit(event, cards.panel_card("⏸ - #emergency_stop", rows),
                    buttons=[[toggle], _back(b"home")])


@bot.on(events.CallbackQuery(pattern=rb"freeze_(on|off)"))
async def freeze_toggle_cb(event):
    if not is_owner(event):
        return
    on = event.pattern_match.group(1).decode() == "on"
    db.set_sends_frozen(on)
    central_db.audit("freeze", "on" if on else "off")
    await logbus.event("⏸ - #emergency_stop", [
        cards.kv("Sends", "FROZEN" if on else "running"),
        cards.kv("By", "owner")])
    await freeze_cb(event)


@bot.on(events.CallbackQuery(data=b"shield"))
async def shield_cb(event):
    if not is_owner(event):
        return
    status = antispam.status()
    rows = [
        cards.kv("Bot", "🟢 ONLINE" if status["online"] else "🔴 OFFLINE"),
        cards.kv("Recent starts", f"{status['recent_starts']} in "
                                  f"{status['window']}s (limit {status['limit']})"),
    ]
    if not status["online"]:
        rows += [cards.kv("Taken down by", status["by"] or "—"),
                 cards.kv("At", (status["at"] or "—")[:16]),
                 cards.kv("Reason", status["note"] or "—")]
    rows += [
        cards.LINE,
        "اگر بیش از حد مجاز کاربر تازه در بازه استارت بزنند،",
        "ربات خودش آفلاین می‌شود تا با صد اکانت جعلی از کار نیفتد.",
    ]
    toggle = (Button.inline("▶️ آنلاین کن", b"shield_up") if not status["online"]
              else Button.inline("🛡 آفلاین کن", b"shield_down"))
    await safe_edit(event, cards.panel_card("🛡 - #antispam_shield", rows),
                    buttons=[[toggle], _back(b"home")])


@bot.on(events.CallbackQuery(pattern=rb"shield_(up|down)"))
async def shield_toggle_cb(event):
    if not is_owner(event):
        return
    if event.pattern_match.group(1).decode() == "up":
        await antispam.lift(by="owner")
        central_db.audit("shield", "lifted")
    else:
        await antispam.lower(by="owner", note="manual")
        central_db.audit("shield", "lowered")
    await shield_cb(event)


# --------------------------------------------------------------------------- #
# Diagnostics: look up a phone number
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"healthreport"))
async def health_report_cb(event):
    """What the last health sweep found.

    The engine itself runs in the CUSTOMER process, because the busy registry it
    depends on is in memory where the jobs are. It parks its result in the shared
    bot_state row, which is what this screen reads — the owner bot never runs a
    sweep of its own.
    """
    if not is_owner(event):
        return
    import health
    report = db.get_health_report()
    rows = []
    if report.get("dead_accounts"):
        rows = [cards.LINE, "آخرین اکانت‌های سوخته:"] + [
            f"🔴 {d}" for d in report["dead_accounts"][:12]]
    await safe_edit(event, health.report_card() + ("\n".join(rows) if rows else ""),
                    buttons=[[Button.inline("🔄 بازخوانی", b"healthreport")],
                             _back(b"home")])


@bot.on(events.CallbackQuery(data=b"selftest"))
async def selftest_cb(event):
    """Run the safety-critical invariants against the live system and report.

    This exists because the owner cannot exercise these by hand: session-collision
    protection needs two operations racing for one account, worker distribution
    needs several workers, tenant isolation needs several customers. The checks
    simulate all of that with throwaway data and touch no real account, worker, or
    customer.
    """
    if not is_owner(event):
        return
    import selftest
    await safe_edit(event, cards.card("🧪 - #selftest", [
        "در حال اجرای بررسی‌های ایمنی ..."]))
    results = await selftest.run()
    s = selftest.summary(results)

    rows = [cards.kv("Result", f"{s['passed']}/{s['total']} سبز"),
            cards.LINE]
    for name, ok, detail in results:
        rows.append(f"{'✅' if ok else '❌'} {name}")
        if not ok:
            rows.append(f"     ↳ {detail}")
    rows.append(cards.LINE)
    if s["all_ok"]:
        rows.append("همه‌ی زیرسیستم‌های منطقی سالم‌اند.")
        rows.append("توجه: این‌ها منطق را می‌سنجند؛ تماس واقعی با روبیکا/تلگرام")
        rows.append("را فقط یک ارسال واقعی ثابت می‌کند.")
    else:
        rows.append("⚠️ موارد قرمز را برای پشتیبانی بفرست.")
    await safe_edit(event, cards.panel_card("🧪 - #selftest", rows),
                    buttons=[[Button.inline("🔄 اجرای دوباره", b"selftest")],
                             _back(b"home")])


@bot.on(events.CallbackQuery(data=b"diag"))
async def diag_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_diag"}
    await safe_edit(event, cards.card("🔍 - #diagnose", [
        "شماره‌ی اکانت را بفرست.",
        "می‌گوید مال کدام مشتری است، روی کدام ورکر، سشن سالم است یا نه،",
        "و آخرین وضعیتش چه بوده.",
    ]), buttons=[_back(b"home")])


async def _diagnose(phone: str) -> str:
    import busy
    found = db.owner_locate_phone(phone)
    if not found:
        return cards.card("🔍 - #diagnose", [
            cards.kv("Phone", phone),
            "هیچ اکانتی با این شماره پیدا نشد.",
        ])
    blocks = []
    for row in found:
        cid = row.get("customer_id")
        platform = row.get("platform")
        rows = [
            cards.kv("Phone", row.get("phone")),
            cards.kv("Platform", platform),
            cards.kv("Customer", f"{row.get('customer_name') or '—'} "
                                 f"({cid})"),
        ]
        if cid:
            cust = db.get_customer(cid) or {}
            left = db.seconds_left(cid)
            rows.append(cards.kv("Subscription",
                                 "⛔ blocked" if cust.get("blocked")
                                 else (f"🟢 {db.days_left(cid)}d left" if left > 0
                                       else "🔴 expired")))
        if platform == "rubika":
            w = worker.worker_for_account(row)
            if w:
                rows.append(cards.kv("Worker", f"{w['tag']} "
                                               f"{worker.status_emoji(w)} "
                                               f"{str(w.get('status')).upper()}"))
            key = busy.key_for(row.get("phone"), customer_id=cid)
            holder = busy.who(key)
            if holder:
                held = int(time.time() - float(holder.get("since") or 0))
                busy_text = f"{busy.label(holder.get('what'))} ({held}s)"
            else:
                busy_text = "free"
            rows.append(cards.kv("Busy", busy_text))
        status = row.get("status")
        rows.append(cards.kv("Session",
                             "🟢 active" if status == "active" else f"🔴 {status}"))
        rows.append(cards.kv("Sent", _num(row.get("sent_total", 0))))
        rows.append(cards.kv("Added", (row.get("added_at") or "—")[:16]))
        if status != "active":
            rows += [cards.LINE,
                     "علت احتمالی: سشن باطل شده — مشتری باید دوباره لاگین کند."]
        blocks.append(cards.panel_card("🔍 - #diagnose", rows))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"tickets"))
async def tickets_cb(event):
    if not is_owner(event):
        return
    open_tickets = db.owner_list_tickets(only_open=True, limit=20)
    if not open_tickets:
        await safe_edit(event, cards.card("📨 - #tickets", ["تیکت بازی نیست. 👌"]),
                        buttons=[[Button.inline("📋 آرشیو", b"tk_all")],
                                 _back(b"home")])
        return
    rows = [cards.kv("Open", len(open_tickets))]
    buttons = []
    for ticket in open_tickets[:10]:
        cust = db.get_customer(ticket["customer_id"]) or {}
        name = cust.get("name") or ticket["customer_id"]
        rows.append(f"• #{ticket['id']} {name}: "
                    f"«{(ticket.get('text') or '')[:45]}»")
        buttons.append([Button.inline(f"📨 #{ticket['id']} — {name}",
                                      f"tk_{ticket['id']}".encode())])
    buttons.append(_back(b"home"))
    await safe_edit(event, cards.panel_card("📨 - #tickets", rows), buttons=buttons)


@bot.on(events.CallbackQuery(pattern=rb"tk_(\d+)"))
async def ticket_detail_cb(event):
    if not is_owner(event):
        return
    tid = int(event.pattern_match.group(1))
    ticket = db.owner_get_ticket(tid)
    if not ticket:
        await event.answer("تیکت پیدا نشد.", alert=True)
        return
    cid = ticket["customer_id"]
    cust = db.get_customer(cid) or {}
    rb = db.count_accounts(cid) if cid else {"total": 0, "healthy": 0}
    tg = db.tg_count_accounts(cid) if cid else {"total": 0, "healthy": 0}
    left = db.seconds_left(cid) if cid else 0
    rows = [
        cards.kv("From", f"{cust.get('name') or '—'} ({cid})"),
        cards.kv("Status", "⛔ blocked" if cust.get("blocked")
                 else (f"🟢 {db.days_left(cid)}d" if left > 0 else "🔴 expired")),
        cards.kv("Accounts", f"{rb['total']} Rubika ({rb['healthy']} healthy) | "
                             f"{tg['total']} Telegram"),
        cards.kv("At", (ticket.get("created_at") or "—")[:16]),
        cards.LINE,
        f"«{ticket.get('text') or ''}»",
    ]
    if ticket.get("answered"):
        rows += [cards.LINE, cards.kv("Answer", ticket.get("answer") or "")]
    buttons = []
    if not ticket.get("answered"):
        buttons.append([Button.inline("💬 پاسخ", f"tkr_{tid}".encode())])
    buttons.append([Button.inline("👤 پروفایل", f"cust_{cid}".encode()),
                    Button.inline("🔙 تیکت‌ها", b"tickets")])
    await safe_edit(event, cards.panel_card("📨 - #ticket", rows), buttons=buttons)


@bot.on(events.CallbackQuery(pattern=rb"tkr_(\d+)"))
async def ticket_reply_cb(event):
    if not is_owner(event):
        return
    tid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_ticket_reply", "tid": tid}
    await safe_edit(event, cards.card("💬 - #ticket_reply", [
        cards.kv("Ticket", f"#{tid}"),
        "متن پاسخ را بفرست. در پیوی مشتری تحویل داده می‌شود.",
    ]), buttons=[[Button.inline("🔙 انصراف", f"tk_{tid}".encode())]])


@bot.on(events.CallbackQuery(data=b"tk_all"))
async def tickets_all_cb(event):
    if not is_owner(event):
        return
    rows = []
    for ticket in db.owner_list_tickets(only_open=False, limit=20):
        mark = "✅" if ticket.get("answered") else "📨"
        rows.append(f"{mark} #{ticket['id']} · {ticket['customer_id']} · "
                    f"{(ticket.get('created_at') or '')[:10]}")
    await safe_edit(event, cards.panel_card("📋 - #ticket_archive", rows or ["—"]),
                    buttons=[_back(b"tickets")])


# --------------------------------------------------------------------------- #
# Service settings + audit log
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"settings"))
async def settings_cb(event):
    if not is_owner(event):
        return
    rows = [
        cards.kv("Rate limit", f"{config.RATE_LIMIT_MAX} actions / "
                               f"{config.RATE_LIMIT_WINDOW}s"),
        cards.kv("Auto-block", "🟢 ON" if config.RATE_LIMIT_AUTOBLOCK else "off"),
        cards.kv("Shield", f"{config.START_FLOOD_MAX} new /start / "
                           f"{config.START_FLOOD_WINDOW}s"),
        cards.kv("Trial", f"{config.TRIAL_DAYS} days"),
        cards.kv("Probe cap", f"{_num(config.PROBE_DAILY_CAP)} / customer / day"),
        cards.kv("Settle delay", f"{config.SESSION_SETTLE_SEC}s"),
        cards.kv("Health engine", f"every {config.HEALTH_ENGINE_INTERVAL // 3600}h"),
        cards.kv("PDF concurrency", config.PV_EXPORT_MAX_CONCURRENT),
        cards.LINE,
        "این‌ها از .env خوانده می‌شوند؛ برای تغییر، فایل را ویرایش",
        "و پروسه را ری‌استارت کن.",
    ]
    await safe_edit(event, cards.panel_card("⚙️ - #service_settings", rows),
                    buttons=[_back(b"home")])


@bot.on(events.CallbackQuery(data=b"audit"))
async def audit_cb(event):
    if not is_owner(event):
        return
    rows = []
    for item in central_db.list_audit(30):
        when = (item.get("created_at") or "")[11:16]
        rows.append(f"{when}  {item.get('action'):<16} {item.get('detail', '')[:40]}")
    await safe_edit(event, cards.panel_card("📋 - #audit_log", rows or ["—"]),
                    buttons=[[Button.inline("♻️ بروزرسانی", b"audit")],
                             _back(b"home")])



# --------------------------------------------------------------------------- #
# Text router — every wizard step lands here
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage(func=lambda e: e.is_private))
async def text_router(event):
    if not is_owner(event):
        return                       # strangers get absolute silence
    if (event.raw_text or "").startswith("/"):
        return
    st = state.get(event.sender_id)
    if not st:
        return
    step = st.get("step")
    handler = _STEPS.get(step)
    if not handler:
        state.pop(event.sender_id, None)
        return
    try:
        await handler(event, st)
    except Exception as exc:  # noqa: BLE001
        state.pop(event.sender_id, None)
        code = await logbus.error(exc, context=f"owner step {step}", notify=False)
        await event.respond(cards.card("⚠️ خطا", [
            cards.kv("Step", step),
            cards.kv("Code", code),
        ]), buttons=[_back(b"home")])


async def _step_days(event, st):
    cid = st["cid"]
    state.pop(event.sender_id, None)
    try:
        days = float((event.raw_text or "").strip())
    except ValueError:
        await event.respond("عدد نامعتبر بود. دوباره از پروفایل امتحان کن.",
                            buttons=[[Button.inline("👤 پروفایل",
                                                    f"cust_{cid}".encode())]])
        return
    new_expiry = db.add_days(cid, days)
    central_db.audit("time", f"{cid} {days:+g}d -> {new_expiry}")
    db.queue_notification(cid, cards.card("⏱ تغییر مدت دسترسی", [
        cards.kv("اعتبار تا", new_expiry[:16], width=10),
        cards.kv("روز باقی", db.days_left(cid), width=10),
    ]))
    await event.respond(_profile_card(db.get_customer(cid)),
                        buttons=_profile_buttons(db.get_customer(cid)))


async def _step_note(event, st):
    cid = st["cid"]
    state.pop(event.sender_id, None)
    text = (event.raw_text or "").strip()
    db.set_note(cid, "" if text == "-" else text)
    central_db.audit("note", f"{cid}")
    await event.respond(_profile_card(db.get_customer(cid)),
                        buttons=_profile_buttons(db.get_customer(cid)))


async def _step_dm(event, st):
    cid = st["cid"]
    state.pop(event.sender_id, None)
    text = (event.raw_text or "").strip()
    if not text:
        await event.respond("متن خالی بود.")
        return
    db.queue_notification(cid, cards.card("💬 پیام از مدیر", [text]))
    central_db.audit("direct_message", f"{cid}")
    await logbus.event("💬 - #direct_message", [
        cards.kv("To", cid), cards.kv("Text", text[:120])])
    await event.respond(cards.card("✅ ارسال شد", [
        cards.kv("Customer", cid),
        "پیام در صف تحویل قرار گرفت و توسط ربات مشتری فرستاده می‌شود.",
    ]), buttons=[[Button.inline("👤 پروفایل", f"cust_{cid}".encode())]])


async def _step_del_confirm(event, st):
    cid = st["cid"]
    typed = "".join(ch for ch in (event.raw_text or "") if ch.isdigit())
    if typed != str(cid):
        state.pop(event.sender_id, None)
        await event.respond("تأیید نشد — چیزی حذف نشد.",
                            buttons=[[Button.inline("👤 پروفایل",
                                                    f"cust_{cid}".encode())]])
        return
    state.pop(event.sender_id, None)
    cust = db.get_customer(cid) or {}
    rb = db.count_accounts(cid)["total"]
    tg = db.tg_count_accounts(cid)["total"]
    db.delete_customer(cid)
    central_db.audit("delete_customer", f"{cid} ({rb}+{tg} accounts)")
    await logbus.event("🗑 - #customer_deleted", [
        cards.kv("Customer", cust.get("name") or cid),
        cards.kv("ID", cid),
        cards.kv("Accounts removed", f"{rb} Rubika + {tg} Telegram"),
    ])
    await event.respond(cards.card("🗑 حذف شد", [
        cards.kv("Customer", cid),
        cards.kv("Accounts removed", f"{rb} + {tg}"),
    ]), buttons=[_back(b"customers")])


async def _step_new_customer(event, st):
    state.pop(event.sender_id, None)
    parts = (event.raw_text or "").split()
    digits = "".join(ch for ch in (parts[0] if parts else "") if ch.isdigit())
    if not digits:
        await event.respond("آیدی عددی نامعتبر بود.", buttons=[_back(b"customers")])
        return
    cid = int(digits)
    days = 0.0
    if len(parts) > 1:
        try:
            days = float(parts[1])
        except ValueError:
            days = 0.0
    db.ensure_customer(cid, "", "")
    if days:
        db.add_days(cid, days)
    central_db.audit("add_customer", f"{cid} +{days:g}d")
    await logbus.event("➕ - #customer_added", [
        cards.kv("ID", cid), cards.kv("Days", days or "trial")])
    await event.respond(_profile_card(db.get_customer(cid)),
                        buttons=_profile_buttons(db.get_customer(cid)))


async def _step_search(event, st):
    state.pop(event.sender_id, None)
    term = (event.raw_text or "").strip()
    results = db.owner_search_customers(term)
    if not results:
        await event.respond(cards.card("🔎 - #search", [
            cards.kv("Term", term), "چیزی پیدا نشد."]),
            buttons=[[Button.inline("🔎 دوباره", b"search")], _back(b"customers")])
        return
    rows = [cards.kv("Term", term), cards.kv("Found", len(results))]
    buttons = [[Button.inline(_cust_label(c), f"cust_{c['telegram_id']}".encode())]
               for c in results[:12]]
    buttons.append(_back(b"customers"))
    await event.respond(cards.card("🔎 - #search", rows), buttons=buttons)


async def _step_broadcast(event, st):
    kind = st.get("kind", "all")
    state.pop(event.sender_id, None)
    text = (event.raw_text or "").strip()
    if not text:
        await event.respond("متن خالی بود.", buttons=[_back(b"broadcast")])
        return
    targets = _audience(kind)
    body = cards.card("📣 اطلاعیه", [text])
    for cust in targets:
        db.queue_notification(cust["telegram_id"], body)
    central_db.record_broadcast(text, kind, len(targets))
    central_db.audit("broadcast", f"{kind} -> {len(targets)}")
    await logbus.event("📣 - #broadcast", [
        cards.kv("Audience", kind),
        cards.kv("Queued", len(targets)),
        cards.kv("Text", text[:150]),
    ])
    await event.respond(cards.card("📣 - #broadcast", [
        cards.kv("Audience", kind),
        cards.kv("Queued", len(targets)),
        "تحویل توسط ربات مشتری انجام می‌شود.",
    ]), buttons=[_back(b"home")])


async def _step_worker_warn(event, st):
    wid = st["wid"]
    state.pop(event.sender_id, None)
    text = (event.raw_text or "").strip()
    if not text:
        await event.respond("متن خالی بود.")
        return
    listed = db.worker_customers(wid)
    body = cards.card("⚠️ اطلاعیه‌ی فنی", [text])
    for item in listed:
        db.queue_notification(item["telegram_id"], body)
    w = db.get_worker(wid) or {}
    central_db.audit("worker_warn", f"{w.get('tag')} -> {len(listed)}")
    await logbus.event("📣 - #worker_warning", [
        cards.kv("Worker", w.get("tag")),
        cards.kv("Recipients", len(listed)),
        cards.kv("Text", text[:120]),
    ])
    await event.respond(cards.card("✅ ارسال شد", [
        cards.kv("Recipients", len(listed))]),
        buttons=[[Button.inline("🔙 ورکر", f"wk_{wid}".encode())]])


async def _step_maint_note(event, st):
    state.pop(event.sender_id, None)
    central_db.set_notice((event.raw_text or "").strip())
    if central_db.get_maintenance():
        central_db.set_maintenance(True)      # rewrite the flag file contents
    await event.respond(cards.card("✅ ثبت شد", [
        cards.kv("Notice", central_db.get_notice() or "—")]),
        buttons=[_back(b"maint")])


async def _step_ticket_reply(event, st):
    tid = st["tid"]
    state.pop(event.sender_id, None)
    text = (event.raw_text or "").strip()
    ticket = db.owner_get_ticket(tid)
    if not ticket or not text:
        await event.respond("تیکت یا متن نامعتبر بود.", buttons=[_back(b"tickets")])
        return
    db.owner_answer_ticket(tid, text)
    db.queue_notification(ticket["customer_id"], cards.card("💬 پاسخ پشتیبانی", [
        cards.kv("درباره", f"«{(ticket.get('text') or '')[:60]}»", width=8),
        cards.LINE,
        text,
    ]))
    central_db.audit("ticket_reply", f"#{tid} -> {ticket['customer_id']}")
    await event.respond(cards.card("✅ پاسخ ارسال شد", [
        cards.kv("Ticket", f"#{tid}")]), buttons=[_back(b"tickets")])


async def _step_diag(event, st):
    state.pop(event.sender_id, None)
    phone = (event.raw_text or "").strip()
    await event.respond(await _diagnose(phone), buttons=[
        [Button.inline("🔍 شماره‌ی دیگر", b"diag")], _back(b"home")])


async def _step_wk_del(event, st):
    wid, tag = st["wid"], st["tag"]
    typed = (event.raw_text or "").strip()
    if typed != tag:
        state.pop(event.sender_id, None)
        await event.respond("تأیید نشد — ورکر حذف نشد.",
                            buttons=[[Button.inline("🔙 ورکر",
                                                    f"wk_{wid}".encode())]])
        return
    state.pop(event.sender_id, None)
    w = db.get_worker(wid)
    if not w:
        await event.respond("ورکر پیدا نشد.", buttons=[_back(b"workers")])
        return
    stats = db.worker_account_stats(wid)
    await event.respond("⏳ در حال حذف کانتینر و آزادسازی اکانت‌ها ...")
    await worker.stop_supervisor(wid)
    try:
        await worker.teardown_worker(w)
    except Exception as exc:  # noqa: BLE001
        await logbus.error(exc, context=f"worker.teardown {tag}", notify=False)
    db.delete_worker(wid)
    central_db.audit("worker_delete", f"{tag} ({stats['total']} accounts detached)")
    await logbus.event("🗑 - #worker_deleted", [
        cards.kv("Worker", tag),
        cards.kv("Accounts detached", stats["total"]),
    ])
    await event.respond(cards.card("🗑 ورکر حذف شد", [
        cards.kv("Worker", tag),
        cards.kv("Accounts detached", stats["total"]),
        "اکانت‌ها در لاگین بعدی روی ورکر دیگری قرار می‌گیرند.",
    ]), buttons=[_back(b"workers")])


# ---- add-worker wizard ---------------------------------------------------- #
async def _step_wk_ip(event, st):
    st["wk"]["ip"] = (event.raw_text or "").strip()
    st["step"] = "wk_port"
    await event.respond("پورت SSH را بفرست (معمولاً 22).")


async def _step_wk_port(event, st):
    raw = "".join(ch for ch in (event.raw_text or "") if ch.isdigit())
    st["wk"]["port"] = int(raw or 22)
    st["step"] = "wk_user"
    await event.respond("یوزر SSH را بفرست (معمولاً root).")


async def _step_wk_user(event, st):
    st["wk"]["user"] = (event.raw_text or "").strip() or "root"
    st["step"] = "wk_pass"
    await event.respond("پسورد SSH را بفرست.\n"
                        "بعد از ساخت ورکر، این پیام را از چت پاک کن.")


async def _step_wk_pass(event, st):
    wk = st["wk"]
    wk["password"] = (event.raw_text or "").strip()
    state.pop(event.sender_id, None)
    msg = await event.respond(cards.card("🏗 - #provision_worker", [
        cards.kv("Server", f"{wk['ip']}:{wk['port']}"),
        "⏳ شروع نصب ...",
    ]))
    asyncio.create_task(_provision(event.sender_id, msg, wk))


async def _provision(owner_id: int, msg, wk: dict) -> None:
    tag = worker.gen_tag()
    done: list = []          # completed steps, kept as history
    live: list = []          # the step in progress, REPLACED on every update
    last = [""]

    async def on_progress(text: str):
        """Render the card: finished steps as history, the current one live.

        Appending every update would turn a ten-minute build with a progress bar
        into a hundred stacked copies of itself, so the live block is replaced
        rather than added to. A multi-line update (the build's bar and percentage)
        stays multi-line.
        """
        block = [ln for ln in str(text).splitlines() if ln.strip()]
        if not block:
            return
        # A new headline means the previous step finished: promote it to history.
        if live and live[0] != block[0]:
            done.append(live[0])
        live[:] = block

        body = [
            cards.kv("Server", f"{wk['ip']}:{wk['port']}"),
            cards.kv("Tag", tag),
            cards.LINE,
        ] + done[-5:] + live
        text_out = cards.card("🏗 - #provision_worker", body)
        if text_out == last[0]:
            return                       # Telegram rejects an identical edit
        last[0] = text_out
        try:
            await msg.edit(text_out)
        except Exception:
            pass

    result = await worker.provision_worker(
        wk["ip"], wk["port"], wk["user"], wk["password"],
        tag=tag, on_progress=on_progress)

    if not result.get("ok"):
        # The error is often several lines: which step failed, and what to check
        # on the server. cards.kv() collapses that into one truncated line, which
        # is how a report arrived saying only "TimeoutError:" — so the lines are
        # kept as lines.
        detail = str(result.get("error") or "خطای نامشخص").strip()
        body = [cards.kv("Server", wk["ip"]), cards.LINE] + \
            detail.splitlines()[:12]
        await logbus.event("❌ - #worker_provision_failed", body)
        try:
            await msg.edit(cards.card("❌ - #provision_failed", body),
                           buttons=[[Button.inline("🔁 تلاش دوباره", b"wk_add")],
                                    _back(b"workers")])
        except Exception:
            pass
        return

    wid = worker.register_provisioned(wk["ip"], wk["port"], wk["user"],
                                      wk["password"], result)
    await asyncio.sleep(20)                    # let the container settle
    fresh = db.get_worker(wid)
    try:
        await worker.check_worker(fresh)
    except Exception:
        pass
    worker.start_supervisor(db.get_worker(wid))
    fresh = db.get_worker(wid)
    central_db.audit("worker_add", f"{fresh['tag']} {wk['ip']}")
    rows = [
        cards.kv("Tag", fresh["tag"]),
        cards.kv("Address", f"{fresh['ip']}:{fresh['ssh_port']}"),
        cards.kv("State", f"{worker.status_emoji(fresh)} "
                          f"{str(fresh.get('status')).upper()}"),
        cards.kv("Ping", worker.ping_text(fresh)),
    ]
    await logbus.event("✅ - #worker_added", rows)
    try:
        await msg.edit(cards.panel_card("✅ - #worker_added", rows), buttons=[
            [Button.inline("🖥 جزئیات", f"wk_{wid}".encode())],
            _back(b"workers"),
        ])
    except Exception:
        pass


_STEPS = {
    "await_days": _step_days,
    "await_note": _step_note,
    "await_dm": _step_dm,
    "await_del_confirm": _step_del_confirm,
    "await_new_customer": _step_new_customer,
    "await_search": _step_search,
    "await_broadcast": _step_broadcast,
    "await_worker_warn": _step_worker_warn,
    "await_maint_note": _step_maint_note,
    "await_ticket_reply": _step_ticket_reply,
    "await_diag": _step_diag,
    "await_wk_del": _step_wk_del,
    "wk_ip": _step_wk_ip,
    "wk_port": _step_wk_port,
    "wk_user": _step_wk_user,
    "wk_pass": _step_wk_pass,
}


# --------------------------------------------------------------------------- #
# Background loops
# --------------------------------------------------------------------------- #
async def worker_snapshot_loop() -> None:
    """Watch the fleet: alert on a worker going bad, post the full card
    periodically. Uses warm-only checks so a reconnecting tunnel is not
    reported as a failure."""
    last_status: dict = {}
    last_card = 0.0
    while True:
        await asyncio.sleep(25)
        try:
            workers = db.list_workers()
            if not workers:
                continue
            await worker.check_all(workers, warm_only=True)
            for w in db.list_workers():
                previous = last_status.get(w["id"])
                current = w.get("status")
                if previous == "ok" and current != "ok":
                    await logbus.warn("worker_unhealthy", [
                        cards.kv("Worker", w["tag"]),
                        cards.kv("State", str(current).upper()),
                        cards.kv("Detail",
                                 str(worker.health_detail(w["id"]) or "—")[:120]),
                        cards.kv("Accounts", db.count_accounts_on_worker(w["id"])),
                    ])
                elif previous and previous != "ok" and current == "ok":
                    await logbus.event("✅ - #worker_recovered", [
                        cards.kv("Worker", w["tag"])])
                last_status[w["id"]] = current

            now = time.monotonic()
            if now - last_card >= config.HEALTH_INTERVAL:
                last_card = now
                fleet = db.accounts_per_worker()
                rows = [f"{worker.status_emoji(w)} {w['tag']:<10} "
                        f"📱{w['total']:<4} 📶{worker.ping_text(w)}"
                        for w in fleet]
                await logbus.to_group(cards.panel_card("🛠 - #fleet_status", rows))
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="worker_snapshot_loop", notify=False)


async def expiry_watch_loop() -> None:
    """Warn customers a couple of days before their access ends, once each."""
    while True:
        await asyncio.sleep(3600)
        try:
            for cust in db.owner_customers_expiring(config.EXPIRY_WARN_DAYS):
                cid = cust["telegram_id"]
                left = db.days_left(cid)
                db.queue_notification(cid, cards.card("🟡 نزدیک پایان دسترسی", [
                    cards.kv("روز باقی", left, width=10),
                    cards.kv("اعتبار تا", (cust.get("expires_at") or "")[:16],
                             width=10),
                    "برای تمدید با پشتیبانی تماس بگیر.",
                ]))
                db.set_warned(cid, True)
                await logbus.event("🟡 - #expiry_warning", [
                    cards.kv("Customer", cust.get("name") or cid),
                    cards.kv("ID", cid),
                    cards.kv("Days left", left),
                ])
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="expiry_watch_loop", notify=False)


async def clock_watch_loop() -> None:
    """Notice a backwards server clock, which would otherwise silently extend
    every subscription."""
    while True:
        await asyncio.sleep(1800)
        try:
            db.monotonic_now()
            if db.clock_tampered():
                await logbus.warn("clock_tampered", [
                    "ساعت سرور عقب‌تر از بالاترین زمان دیده‌شده است.",
                    "محاسبه‌ی انقضا از زمان ذخیره‌شده استفاده می‌کند.",
                ])
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
async def amain() -> None:
    problems = config.validate_owner()
    if problems:
        raise SystemExit("تنظیمات ناقص است (.env): " + ", ".join(problems))

    db.init()
    central_db.init()

    await bot.start(bot_token=config.OWNER_BOT_TOKEN)
    logbus.bind(bot, role="owner")

    worker.ensure_master_worker()
    await worker.start_all_supervisors()
    asyncio.create_task(worker.prewarm_all())

    asyncio.create_task(worker_snapshot_loop())
    asyncio.create_task(expiry_watch_loop())
    asyncio.create_task(clock_watch_loop())
    asyncio.create_task(backup.backup_loop())

    fleet = db.accounts_per_worker()
    await logbus.event("🎛 - #owner_online", [
        cards.kv("Version", config.VERSION),
        cards.kv("Customers", db.owner_count_customers()["total"]),
        cards.kv("Workers", len(fleet)),
        cards.kv("Service", "🟢 ONLINE" if db.is_bot_online() else "🔴 OFFLINE"),
    ])
    print("owner bot running")
    try:
        await bot.run_until_disconnected()
    finally:
        await worker.shutdown()
