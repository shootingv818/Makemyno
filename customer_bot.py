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
        [Button.inline("📖 راهنما", b"help"), Button.inline("🆘 پشتیبانی", b"support")],
    ]


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


async def _step_ticket(event, st):
    uid = event.sender_id
    state.pop(uid, None)
    text = (event.raw_text or "").strip()
    if len(text) < 5:
        await respond(event, "متن خیلی کوتاه بود. دوباره امتحان کن.",
                      buttons=[_back(b"support")])
        return
    tid = db.add_ticket(uid, text[:2000])
    cust = db.get_customer(uid) or {}
    rb = db.count_accounts(uid)
    tg = db.tg_count_accounts(uid)
    # The owner gets the message WITH context attached, so support does not
    # start with "who are you and what do you have".
    await logbus.event("📨 - #ticket", [
        cards.kv("Ticket", f"#{tid}"),
        cards.kv("From", f"{cust.get('name') or '—'} ({uid})"),
        cards.kv("Access", f"{db.days_left(uid)}d left"
                 if db.seconds_left(uid) > 0 else "expired"),
        cards.kv("Accounts", f"{rb['total']} Rubika ({rb['healthy']} healthy) | "
                             f"{tg['total']} Telegram"),
        cards.LINE,
        f"«{text[:400]}»",
    ])
    await respond(event, cards.card("✅ ثبت شد", [
        cards.kv("شماره تیکت", f"#{tid}", width=12),
        "پاسخ در همین چت برایت می‌آید.",
    ]), buttons=[_back(b"home")])


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
_STEPS: dict = {"await_ticket": _step_ticket}


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
