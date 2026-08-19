"""
tg_panel.py — the Telegram section of the customer bot.
=====================================================

Same rules as the Rubika side, enforced the same way: every screen is scoped to
the calling customer, and anything that opens a session claims it in the busy
registry first. Telegram revokes a session on a second connection just as Rubika
does, so a photo export, a contact export and a send can never overlap on one
account.

What differs from Rubika: content is an ORDERED LIST (several texts and media in
one send) rather than a single marked message, and the multi-account job engine
lives in telegram_multi_send.py because it is persistent and resumable.
"""
from __future__ import annotations

import asyncio
import os

from telethon import Button

import busy
import cards
import config
import db
import logbus
import telegram_client as tg
import telegram_multi_send as multi

_bot = None
_state: dict = {}
_gate = None
_safe_edit = None
_respond = None

# Live single-send controls: account_id -> ctl
_jobs: dict = {}

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "tg_media")
os.makedirs(MEDIA_DIR, exist_ok=True)


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


def _key(customer_id, phone: str) -> str:
    return busy.key_for(phone, customer_id=customer_id, platform="tg")


async def _busy_answer(event, key: str) -> None:
    await event.answer(busy.reason(key) or "این اکانت الان مشغول است.", alert=True)


def _speed(customer_id) -> float:
    return config.clamp_tg_delay(
        db.get_float_setting(customer_id, "tg_send_delay", config.TG_SEND_DELAY))


def _target_mode(customer_id) -> str:
    return db.get_setting(customer_id, "tg_target") or "both"


_TARGET_LABELS = {"both": "دوطرفه‌ها + گروه‌ها", "contacts": "فقط مخاطبین",
                  "groups": "فقط گروه‌ها"}


# --------------------------------------------------------------------------- #
# Section menu
# --------------------------------------------------------------------------- #
def menu_card(customer_id) -> str:
    counts = db.tg_count_accounts(customer_id)
    sent = sum(a.get("sent_total", 0) for a in db.tg_list_accounts(customer_id))
    content = db.tg_content_list(customer_id)
    return cards.card("✈️ Telegram", [
        cards.kv("Accounts", f"{counts['total']}  ({counts['healthy']} healthy)"),
        cards.kv("Total Sent", cards.num(sent)),
        cards.kv("Content", _content_summary(content)),
        cards.kv("Speed", f"{_speed(customer_id)}s"),
        cards.kv("Target", _TARGET_LABELS.get(_target_mode(customer_id))),
    ])


def menu_buttons() -> list:
    return [
        [Button.inline("🚀 ارسال", b"tgsend"),
         Button.inline("➕ افزودن اکانت", b"tgadd")],
        [Button.inline("👤 اکانت‌ها", b"tgaccs"),
         Button.inline("✍️ محتوا", b"tgcontent")],
        [Button.inline("📨 ارسال چنداکانتی", b"tgmulti"),
         Button.inline("📊 وضعیت ارسال‌ها", b"tgjobs")],
        [Button.inline("🎯 مقصد ارسال", b"tgtarget"),
         Button.inline("⚙️ سرعت", b"tgspeed")],
        [Button.inline("🏠 منوی اصلی", b"home")],
    ]


def _content_summary(content: list) -> str:
    if not content:
        return "خالی"
    texts = sum(1 for c in content if (c.get("kind") or "text") == "text")
    media = len(content) - texts
    parts = []
    if texts:
        parts.append(f"✍️{texts} متن")
    if media:
        parts.append(f"🖼{media} فایل")
    return " + ".join(parts)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def _acc_label(acc: dict) -> str:
    mark = "🟢" if acc["status"] == "active" else "🔴"
    return (f"{mark} {acc['phone']} · 👥{acc.get('mutuals', 0)}"
            f"/{acc.get('contacts', 0)} · ✉️{cards.num(acc.get('sent_total', 0))}")


async def _render_accounts(event, page: int = 0):
    uid = event.sender_id
    accounts = db.tg_list_accounts(uid)
    if not accounts:
        await _safe_edit(event, cards.card("👤 اکانت‌های تلگرام", [
            "هنوز اکانتی اضافه نکردی."]), buttons=[
            [Button.inline("➕ افزودن اکانت", b"tgadd")], _back(b"tg")])
        return
    page_items, nav, page, total = cards.paginate(accounts, page, "tgapage_",
                                                 Button)
    counts = db.tg_count_accounts(uid)
    head = cards.card("👤 اکانت‌های تلگرام", [
        cards.kv("Total", f"{counts['total']}  ({counts['healthy']} healthy)"),
        cards.kv("Page", f"{page + 1}/{total}"),
    ])
    rows = [[Button.inline(_acc_label(a), f"tgacc_{a['id']}".encode())]
            for a in page_items]
    if nav:
        rows.append(nav)
    rows.append([Button.inline("➕ افزودن اکانت", b"tgadd")])
    rows.append(_back(b"tg"))
    await _safe_edit(event, head, buttons=rows)


def _account_card(customer_id, acc: dict) -> str:
    key = _key(customer_id, acc["phone"])
    holder = busy.who(key)
    rows = [
        cards.kv("Phone", acc["phone"]),
        cards.kv("Name", acc.get("name") or "—"),
        cards.kv("Username", f"@{acc['username']}" if acc.get("username") else "—"),
        cards.kv("Status", "🟢 active" if acc["status"] == "active"
                 else f"🔴 {acc['status']}"),
        cards.kv("Contacts", cards.num(acc.get("contacts", 0))),
        cards.kv("Mutual", cards.num(acc.get("mutuals", 0))),
        cards.kv("Groups", cards.num(acc.get("groups", 0))),
        cards.kv("Sent", cards.num(acc.get("sent_total", 0))),
    ]
    if holder:
        rows.append(cards.kv("Busy", busy.label(holder.get("what"))))
    return cards.panel_card("📱 - #tg_account", rows)


def _account_buttons(acc: dict) -> list:
    aid = acc["id"]
    rows = [
        [Button.inline("🚀 ارسال", f"tgrun_{aid}".encode()),
         Button.inline("📥 گرفتن مخاطبین", f"tgexport_{aid}".encode())],
        [Button.inline("🔄 ریست لیست ارسال", f"tgreset_{aid}".encode())],
    ]
    if acc["status"] != "active":
        rows.insert(0, [Button.inline("🔑 ورود مجدد", f"tgrelogin_{aid}".encode())])
    rows.append([Button.inline("🗑 حذف اکانت", f"tgdel_{aid}".encode()),
                 Button.inline("🔙 اکانت‌ها", b"tgaccs")])
    return rows


# --------------------------------------------------------------------------- #
# Single-account send
# --------------------------------------------------------------------------- #
async def _run_single(customer_id, acc: dict, msg=None) -> None:
    """Send the configured content to one account's own recipients."""
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    content = db.tg_content_list(customer_id)
    delay = _speed(customer_id)
    mode = _target_mode(customer_id)

    ctl = {"stop": False, "pause": False, "sent": 0, "failed": 0,
           "skipped": 0, "total": 0, "phone": phone, "state": "running",
           "last_error": ""}
    _jobs[aid] = ctl

    async with busy.hold(key, "send", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            _jobs.pop(aid, None)
            if msg:
                try:
                    await msg.edit(cards.card("🚀 ارسال", [busy.reason(key)]),
                                   buttons=[_back(b"tgaccs")])
                except Exception:
                    pass
            return

        await logbus.customer_action(db.get_customer(customer_id), "tg_send_start", [
            cards.kv("Phone", phone),
            cards.kv("Content", _content_summary(content)),
            cards.kv("Target", _TARGET_LABELS.get(mode)),
            cards.kv("Speed", f"{delay}s"),
        ], platform="Telegram")

        progress = None
        try:
            client = await tg.get_client(customer_id, aid)
            # Enumerate here rather than through the job engine: this is the
            # simple single-account path and does not need persistence.
            mutuals, others = await tg.get_contacts_ordered(client)
            recipients = [(u, True) for u in mutuals] + [(u, False) for u in others]
            if mode == "groups":
                recipients = []
            if mode in ("both", "groups"):
                recipients += [(g, False)
                               for g in await tg.get_group_entities(client)]
            already = db.sent_targets(customer_id, aid, platform="tg")
            recipients = [(e, m) for e, m in recipients
                          if str(getattr(e, "id", e)) not in already]
            ctl["total"] = len(recipients)

            if msg is not None:
                progress = asyncio.create_task(_progress_loop(aid, ctl, msg))

            consecutive = 0
            max_errors = db.get_max_errors(customer_id)
            for entity, _mutual in recipients:
                if ctl["stop"]:
                    ctl["state"] = "stopped"
                    break
                while ctl["pause"] and not ctl["stop"]:
                    await asyncio.sleep(1)
                if db.are_sends_frozen():
                    ctl["state"] = "frozen"
                    break
                uid_key = str(getattr(entity, "id", entity))
                try:
                    await multi._deliver(client,
                                         {"kind": "user", "id": entity},
                                         content, delay)
                    db.mark_sent(customer_id, aid, uid_key, platform="tg")
                    ctl["sent"] += 1
                    consecutive = 0
                except Exception as exc:  # noqa: BLE001
                    wait = multi._is_flood(exc)
                    if wait:
                        ctl["last_error"] = f"floodwait {wait}s"
                        await asyncio.sleep(wait)
                        continue
                    if multi._is_fatal_account_error(exc):
                        ctl["state"] = "auth_failed"
                        db.tg_set_status(customer_id, aid, "dead")
                        break
                    if multi._is_permanent_recipient_error(exc):
                        ctl["skipped"] += 1
                        continue
                    ctl["failed"] += 1
                    consecutive += 1
                    ctl["last_error"] = type(exc).__name__
                    if consecutive >= max_errors:
                        ctl["state"] = "error_burst"
                        break
                await asyncio.sleep(delay)
            if ctl["state"] == "running":
                ctl["state"] = "done"
        except Exception as exc:  # noqa: BLE001
            ctl["state"] = "failed"
            ctl["last_error"] = await logbus.error(
                exc, context=f"tg send {phone}", customer=customer_id)
        finally:
            if progress:
                progress.cancel()
            _jobs.pop(aid, None)

    if ctl["sent"]:
        db.tg_incr_sent(customer_id, aid, ctl["sent"])
        db.incr_customer_sends(customer_id, ctl["sent"])
        db.usage_incr(customer_id, "send", ctl["sent"])

    labels = {"done": "🏁 پایان", "stopped": "⛔ متوقف شد",
              "error_burst": "⚠️ خطاهای پشت‌سرهم — متوقف شد",
              "auth_failed": "🔴 اکانت از کار افتاد",
              "frozen": "⏸ ارسال موقتاً متوقف است", "failed": "⚠️ خطا"}
    rows = [
        cards.kv("Phone", phone),
        cards.kv("Sent", cards.num(ctl["sent"])),
        cards.kv("Failed", cards.num(ctl["failed"])),
        cards.kv("Skipped", cards.num(ctl["skipped"])),
        cards.kv("Result", labels.get(ctl["state"], ctl["state"])),
    ]
    await logbus.customer_action(db.get_customer(customer_id), "tg_send_finished",
                                rows, platform="Telegram")
    buttons = [_back(b"tgaccs")]
    if ctl["state"] == "auth_failed":
        buttons = [[Button.inline("🔑 ورود مجدد", f"tgrelogin_{aid}".encode())],
                   _back(b"tgaccs")]
    text = cards.panel_card("🏁 - #tg_send_report", rows)
    if msg is not None:
        try:
            await msg.edit(text, buttons=buttons)
            return
        except Exception:
            pass
    try:
        await _bot.send_message(int(customer_id), text, buttons=buttons)
    except Exception:
        pass


def _progress_card(ctl: dict) -> str:
    total = max(1, int(ctl.get("total") or 1))
    done = ctl["sent"] + ctl["failed"] + ctl["skipped"]
    rows = [
        cards.kv("Phone", ctl["phone"]),
        cards.kv("Progress", f"{cards.bar(done, total)}  {done}/{total}"),
        cards.kv("Sent", cards.num(ctl["sent"])),
        cards.kv("Failed", cards.num(ctl["failed"])),
        cards.kv("Skipped", cards.num(ctl["skipped"])),
        cards.kv("State", ctl.get("state", "running")),
    ]
    if ctl.get("last_error"):
        rows.append(cards.kv("Note", str(ctl["last_error"])[:60]))
    return cards.panel_card("🚀 - #tg_send", rows)


def _ctl_buttons(account_id, paused: bool = False) -> list:
    return [[Button.inline("▶️ ادامه" if paused else "⏸ مکث",
                           f"{'tgresume' if paused else 'tgpause'}_"
                           f"{account_id}".encode()),
             Button.inline("⛔ توقف", f"tgstop_{account_id}".encode())]]


async def _progress_loop(account_id, ctl, msg) -> None:
    last = ""
    try:
        while True:
            await asyncio.sleep(config.TG_STATS_REFRESH)
            text = _progress_card(ctl)
            if text != last:
                last = text
                try:
                    await msg.edit(text,
                                   buttons=_ctl_buttons(account_id, ctl["pause"]))
                except Exception:
                    pass
            if ctl.get("state") != "running":
                return
    except asyncio.CancelledError:
        return


# --------------------------------------------------------------------------- #
# Contact export
# --------------------------------------------------------------------------- #
async def _run_export(customer_id, acc: dict, msg=None) -> None:
    aid, phone = acc["id"], acc["phone"]
    key = _key(customer_id, phone)
    path = os.path.join(MEDIA_DIR,
                        f"tgcontacts_{customer_id}_{aid}_{os.urandom(4).hex()}.txt")
    numbers = []
    async with busy.hold(key, "export", customer_id=customer_id,
                         extra={"account_id": aid}) as held:
        if not held.ok:
            if msg:
                try:
                    await msg.edit(cards.card("📥 گرفتن مخاطبین",
                                              [busy.reason(key)]),
                                   buttons=[_back(b"tgaccs")])
                except Exception:
                    pass
            return
        try:
            client = await tg.get_client(customer_id, aid)
            users = await tg.get_contacts(client)
            seen = set()
            for user in users:
                digits = "".join(ch for ch in (getattr(user, "phone", "") or "")
                                 if ch.isdigit())
                if digits and digits not in seen:
                    seen.add(digits)
                    numbers.append(digits)
        except Exception as exc:  # noqa: BLE001
            code = await logbus.error(exc, context=f"tg export {phone}",
                                      customer=customer_id, notify=False)
            if msg:
                try:
                    await msg.edit(cards.card("⚠️ مشکلی پیش آمد", [
                        cards.kv("کد خطا", code, width=8)]),
                        buttons=[_back(b"tgaccs")])
                except Exception:
                    pass
            return

    if not numbers:
        if msg:
            try:
                await msg.edit(cards.card("📥 گرفتن مخاطبین", [
                    cards.kv("Phone", phone),
                    "شماره‌ای پیدا نشد. (تلگرام شماره‌ی همه‌ی مخاطبین را نمی‌دهد)",
                ]), buttons=[_back(b"tgaccs")])
            except Exception:
                pass
        return

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(numbers))
    caption = cards.panel_card("📥 - #tg_contacts_export", [
        cards.kv("Phone", phone),
        cards.kv("Numbers", cards.num(len(numbers))),
        cards.LINE,
        "این فایل را می‌توانی در بخش روبیکا هم استفاده کنی.",
    ])
    await logbus.customer_action(db.get_customer(customer_id), "tg_contacts_export",
                                [cards.kv("Phone", phone),
                                 cards.kv("Numbers", len(numbers))],
                                platform="Telegram")
    try:
        await _bot.send_file(int(customer_id), path, caption=caption,
                             force_document=True)
        if msg is not None:
            await msg.delete()
    except Exception:
        pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Multi-account selector
# --------------------------------------------------------------------------- #
async def _render_multi(event):
    uid = event.sender_id
    accounts = [a for a in db.tg_list_accounts(uid) if a["status"] == "active"]
    chosen = _state.setdefault(uid, {}).setdefault("tgmulti", [])
    valid = {a["id"] for a in accounts}
    chosen[:] = [aid for aid in chosen if aid in valid]

    page_items, nav, page, total = cards.paginate(accounts, 0, "tgmpage_", Button)
    rows = [[Button.inline(f"{'✅' if a['id'] in chosen else '▫️'} {a['phone']}"
                           f" · 👥{a.get('mutuals', 0)}",
                           f"tgmsel_{a['id']}".encode())]
            for a in page_items]
    if nav:
        rows.append(nav)
    if chosen:
        rows.append([Button.inline(f"🚀 شروع با {len(chosen)} اکانت", b"tgmgo")])
    rows.append([Button.inline("📊 وضعیت ارسال‌ها", b"tgjobs")])
    rows.append(_back(b"tg"))
    body = [
        cards.kv("Selected", len(chosen)),
        cards.kv("Content", _content_summary(db.tg_content_list(uid))),
        cards.kv("Target", _TARGET_LABELS.get(_target_mode(uid))),
        cards.LINE,
        "هر اکانت فقط به مخاطبان خودش می‌فرستد و اکانت‌ها",
        "یکی‌یکی اجرا می‌شوند. دوطرفه‌ها اول.",
        "اگر دو اکانت مخاطب مشترک داشته باشند، فقط یک بار پیام می‌رود.",
    ]
    if not accounts:
        body = ["اکانت فعالی نداری."]
    assert total >= 1
    await _safe_edit(event, cards.card("📨 ارسال چنداکانتی", body), buttons=rows)


async def _render_jobs(event):
    uid = event.sender_id
    jobs = db.tgm_list_jobs(uid, 8)
    rows = []
    buttons = []
    if not jobs:
        rows.append("هنوز ارسال چنداکانتی ثبت نشده.")
    for job in jobs:
        labels = {"queued": "در صف", "running": "▶️ در اجرا",
                  "stop_requested": "در حال توقف", "stopped": "⛔ متوقف",
                  "paused": "⏸ نیمه‌کاره", "done": "✅ پایان",
                  "failed": "🔴 خطا", "frozen": "⏸ متوقف"}
        rows.append(f"• {job['job_id']} | {labels.get(job['state'], job['state'])}"
                    f" | ✉️{cards.num(job['sent_count'])}/{job['total']}"
                    f" | ❌{job['failed_count']}")
        row = [Button.inline(f"📊 {job['job_id'][:6]}",
                             f"tgjob_{job['job_id']}".encode())]
        if job["state"] in ("running", "queued", "stop_requested"):
            row.append(Button.inline("⛔ توقف", f"tgjstop_{job['job_id']}".encode()))
        elif job["state"] in ("paused", "stopped", "failed"):
            row.append(Button.inline("▶️ ادامه", f"tgjres_{job['job_id']}".encode()))
        buttons.append(row)
    buttons.append([Button.inline("♻️ بروزرسانی", b"tgjobs")])
    buttons.append(_back(b"tg"))
    await _safe_edit(event, cards.panel_card("📊 - #tg_jobs", rows), buttons=buttons)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
async def _start_login(event, phone: str) -> None:
    uid = event.sender_id
    await _respond(event, cards.card("⏳ در حال اتصال", [
        cards.kv("Phone", phone), "ارسال کد ورود ..."]))
    try:
        ctx = await tg.start_login(phone)
    except Exception as exc:  # noqa: BLE001
        _state.pop(uid, None)
        code = await logbus.error(exc, context=f"tg login start {phone}",
                                  customer=uid)
        await _respond(event, cards.card("⚠️ ارسال کد ناموفق بود", [
            cards.kv("کد خطا", code, width=8)]), buttons=[_back(b"tg")])
        return
    st = _state.setdefault(uid, {})
    st.update({"step": "tg_code", "phone": phone, "ctx": ctx})
    await _respond(event, cards.card("📩 کد ورود", [
        cards.kv("Phone", phone),
        "کدی که تلگرام فرستاد را بفرست.",
    ]), buttons=[_back(b"tg")])


async def _finish_login(event, st: dict) -> None:
    uid = event.sender_id
    ctx = st.get("ctx")
    try:
        info = await tg.commit_login(uid, ctx)
    except Exception as exc:  # noqa: BLE001
        code = await logbus.error(exc, context="tg commit login", customer=uid)
        await _respond(event, cards.card("⚠️ ثبت اکانت ناموفق بود", [
            cards.kv("کد خطا", code, width=8)]), buttons=[_back(b"tg")])
        return
    _state.pop(uid, None)
    aid = info.get("account_id")
    rows = [
        cards.kv("Status", "SUCCESS"),
        cards.kv("Phone", st["phone"]),
        cards.kv("Name", info.get("name") or "—"),
        cards.kv("Username", f"@{info['username']}" if info.get("username")
                 else "—"),
        cards.kv("Contacts", cards.num(info.get("contacts", 0))),
        cards.kv("Mutual", cards.num(info.get("mutuals", 0))),
        cards.kv("Groups", cards.num(info.get("groups", 0))),
        cards.kv("Session Saved", "YES"),
        cards.kv("Time", cards.now()),
    ]
    footer = "--| ✈️ - Platform : Telegram"
    await logbus.event("✅ - #telegram_login", rows + [cards.kv("Customer", uid)],
                       footer=footer)
    await _respond(event, cards.panel_card("✅ - #telegram_login", rows,
                                          footer=footer), buttons=[
        [Button.inline("🚀 ارسال", f"tgrun_{aid}".encode())],
        [Button.inline("✍️ تنظیم محتوا", b"tgcontent")],
        _back(b"tg"),
    ])


# --------------------------------------------------------------------------- #
# Handler registration
# --------------------------------------------------------------------------- #
def setup(bot, state, gate, safe_edit, respond, register_steps) -> None:
    global _bot, _state, _gate, _safe_edit, _respond
    _bot, _state, _gate, _safe_edit, _respond = bot, state, gate, safe_edit, respond
    multi.bind(bot)

    from telethon import events

    @bot.on(events.CallbackQuery(data=b"tg"))
    async def tg_home(event):
        if not await gate(event):
            return
        state.pop(event.sender_id, None)
        await safe_edit(event, menu_card(event.sender_id), buttons=menu_buttons())

    # ---- accounts ------------------------------------------------------ #
    @bot.on(events.CallbackQuery(data=b"tgaccs"))
    async def tg_accounts(event):
        if not await gate(event):
            return
        await _render_accounts(event, 0)

    @bot.on(events.CallbackQuery(pattern=rb"tgapage_(\d+)"))
    async def tg_accounts_page(event):
        if not await gate(event, count_action=False):
            return
        await _render_accounts(event, int(event.pattern_match.group(1)))

    @bot.on(events.CallbackQuery(pattern=rb"tgacc_(\d+)"))
    async def tg_account(event):
        if not await gate(event):
            return
        acc = db.tg_get_account(event.sender_id,
                                int(event.pattern_match.group(1)))
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        await safe_edit(event, _account_card(event.sender_id, acc),
                        buttons=_account_buttons(acc))

    @bot.on(events.CallbackQuery(data=b"tgadd"))
    async def tg_add(event):
        if not await gate(event):
            return
        state[event.sender_id] = {"step": "tg_phone"}
        await safe_edit(event, cards.card("➕ افزودن اکانت تلگرام", [
            "شماره را با کد کشور بفرست.",
            "مثال: +989123456789",
        ]), buttons=[_back(b"tg")])

    @bot.on(events.CallbackQuery(pattern=rb"tgrelogin_(\d+)"))
    async def tg_relogin(event):
        if not await gate(event):
            return
        acc = db.tg_get_account(event.sender_id,
                                int(event.pattern_match.group(1)))
        if not acc:
            return
        await _start_login(event, acc["phone"])

    @bot.on(events.CallbackQuery(pattern=rb"tgdel_(\d+)"))
    async def tg_delete(event):
        if not await gate(event):
            return
        aid = int(event.pattern_match.group(1))
        acc = db.tg_get_account(event.sender_id, aid)
        if not acc:
            return
        await safe_edit(event, cards.card("🗑 حذف اکانت", [
            cards.kv("Phone", acc["phone"]),
            "سشن و لیست ارسال این اکانت پاک می‌شود.",
        ]), buttons=[[Button.inline("✅ حذف کن", f"tgdely_{aid}".encode())],
                     [Button.inline("🔙 انصراف", f"tgacc_{aid}".encode())]])

    @bot.on(events.CallbackQuery(pattern=rb"tgdely_(\d+)"))
    async def tg_delete_yes(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.tg_get_account(uid, aid)
        if not acc:
            return
        await tg.drop_client(uid, acc["phone"])
        db.tg_delete_account(uid, aid)
        await logbus.customer_action(db.get_customer(uid), "tg_account_deleted",
                                    [cards.kv("Phone", acc["phone"])],
                                    platform="Telegram")
        await _render_accounts(event, 0)

    @bot.on(events.CallbackQuery(pattern=rb"tgreset_(\d+)"))
    async def tg_reset(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        cleared = db.reset_sent(uid, aid, platform="tg")
        await event.answer(f"{cleared} مخاطب از لیست ارسال پاک شد.", alert=True)
        acc = db.tg_get_account(uid, aid)
        if acc:
            await safe_edit(event, _account_card(uid, acc),
                            buttons=_account_buttons(acc))

    # ---- content ------------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"tgcontent"))
    async def tg_content(event):
        if not await gate(event):
            return
        uid = event.sender_id
        items = db.tg_content_list(uid)
        rows = [cards.kv("Items", _content_summary(items)), cards.LINE]
        if items:
            for i, item in enumerate(items, 1):
                if (item.get("kind") or "text") == "text":
                    rows.append(f"{i}. ✍️ «{(item.get('text') or '')[:50]}»")
                else:
                    rows.append(f"{i}. 🖼 {item.get('file_name') or 'file'}"
                                + (f" — «{item['text'][:30]}»"
                                   if item.get("text") else ""))
        else:
            rows.append("هنوز محتوایی تنظیم نشده.")
        rows += [cards.LINE, "همه‌ی موارد به‌ترتیب برای هر مخاطب فرستاده می‌شوند."]
        await safe_edit(event, cards.panel_card("✍️ - #tg_content", rows),
                        buttons=[
            [Button.inline("➕ افزودن متن", b"tgctext"),
             Button.inline("🖼 افزودن فایل", b"tgcmedia")],
            [Button.inline("🗑 پاک کردن همه", b"tgcclear")],
            _back(b"tg"),
        ])

    @bot.on(events.CallbackQuery(data=b"tgctext"))
    async def tg_content_text(event):
        if not await gate(event):
            return
        state[event.sender_id] = {"step": "tg_add_text"}
        await safe_edit(event, cards.card("✍️ افزودن متن", [
            "متن را بفرست. به انتهای لیست محتوا اضافه می‌شود."]),
            buttons=[_back(b"tgcontent")])

    @bot.on(events.CallbackQuery(data=b"tgcmedia"))
    async def tg_content_media(event):
        if not await gate(event):
            return
        state[event.sender_id] = {"step": "tg_add_media"}
        await safe_edit(event, cards.card("🖼 افزودن فایل", [
            "عکس یا فایل را بفرست. کپشن هم اگر بگذاری ذخیره می‌شود.",
            "نام واقعی فایل حفظ می‌شود.",
        ]), buttons=[_back(b"tgcontent")])

    @bot.on(events.CallbackQuery(data=b"tgcclear"))
    async def tg_content_clear(event):
        if not await gate(event):
            return
        uid = event.sender_id
        removed = db.tg_content_clear(uid)
        await logbus.customer_action(db.get_customer(uid), "tg_content_cleared",
                                    [cards.kv("Files removed", removed)],
                                    platform="Telegram")
        await tg_content(event)

    # ---- target + speed ------------------------------------------------ #
    @bot.on(events.CallbackQuery(data=b"tgtarget"))
    async def tg_target(event):
        if not await gate(event):
            return
        uid = event.sender_id
        current = _target_mode(uid)
        rows = [cards.kv("Current", _TARGET_LABELS.get(current)), cards.LINE,
                "مخاطبین: افراد لیست مخاطبان اکانت (دوطرفه‌ها اول).",
                "گروه‌ها: گروه‌هایی که اکانت عضوشان است."]
        await safe_edit(event, cards.card("🎯 مقصد ارسال", rows), buttons=[
            [Button.inline(("✅ " if current == "both" else "") + "هردو",
                           b"tgtset_both")],
            [Button.inline(("✅ " if current == "contacts" else "") + "فقط مخاطبین",
                           b"tgtset_contacts")],
            [Button.inline(("✅ " if current == "groups" else "") + "فقط گروه‌ها",
                           b"tgtset_groups")],
            _back(b"tg"),
        ])

    @bot.on(events.CallbackQuery(pattern=rb"tgtset_(both|contacts|groups)"))
    async def tg_target_set(event):
        if not await gate(event):
            return
        mode = event.pattern_match.group(1).decode()
        db.set_setting(event.sender_id, "tg_target", mode)
        await tg_target(event)

    @bot.on(events.CallbackQuery(data=b"tgspeed"))
    async def tg_speed(event):
        if not await gate(event):
            return
        uid = event.sender_id
        rows = [cards.kv("Current", f"{_speed(uid)}s"),
                cards.LINE,
                f"مجاز: {config.TG_SEND_DELAY_MIN} تا "
                f"{config.TG_SEND_DELAY_MAX} ثانیه.",
                "کندتر = امن‌تر."]
        presets = [0.2, 0.4, 0.6, 0.8, 1.0]
        await safe_edit(event, cards.card("⚙️ سرعت ارسال تلگرام", rows), buttons=[
            [Button.inline(f"{p}s", f"tgsset_{p}".encode()) for p in presets[:3]],
            [Button.inline(f"{p}s", f"tgsset_{p}".encode()) for p in presets[3:]],
            _back(b"tg"),
        ])

    @bot.on(events.CallbackQuery(pattern=rb"tgsset_([\d.]+)"))
    async def tg_speed_set(event):
        if not await gate(event):
            return
        value = config.clamp_tg_delay(event.pattern_match.group(1).decode())
        db.set_setting(event.sender_id, "tg_send_delay", value)
        await tg_speed(event)

    # ---- single send --------------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"tgsend"))
    async def tg_send_menu(event):
        if not await gate(event):
            return
        uid = event.sender_id
        accounts = [a for a in db.tg_list_accounts(uid) if a["status"] == "active"]
        if not accounts:
            await safe_edit(event, cards.card("🚀 ارسال", ["اکانت فعالی نداری."]),
                            buttons=[[Button.inline("➕ افزودن اکانت", b"tgadd")],
                                     _back(b"tg")])
            return
        if not db.tg_content_list(uid):
            await safe_edit(event, cards.card("🚀 ارسال", [
                "اول محتوا را تنظیم کن."]), buttons=[
                [Button.inline("✍️ تنظیم محتوا", b"tgcontent")], _back(b"tg")])
            return
        rows = [[Button.inline(f"🚀 {a['phone']}", f"tgrun_{a['id']}".encode())]
                for a in accounts[:config.ACC_PAGE_SIZE]]
        rows.append([Button.inline("📨 ارسال چنداکانتی", b"tgmulti")])
        rows.append(_back(b"tg"))
        await safe_edit(event, cards.card("🚀 ارسال", [
            cards.kv("Accounts", len(accounts)),
            cards.kv("Content", _content_summary(db.tg_content_list(uid))),
            cards.kv("Target", _TARGET_LABELS.get(_target_mode(uid))),
        ]), buttons=rows)

    @bot.on(events.CallbackQuery(pattern=rb"tgrun_(\d+)"))
    async def tg_run(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.tg_get_account(uid, aid)
        if not acc:
            await event.answer("اکانت پیدا نشد.", alert=True)
            return
        if not db.tg_content_list(uid):
            await event.answer("اول محتوا را تنظیم کن.", alert=True)
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        msg = await safe_edit(event, cards.card("🚀 ارسال", [
            cards.kv("Phone", acc["phone"]), "⏳ آماده‌سازی مخاطبین ..."]))
        asyncio.create_task(_run_single(uid, acc, msg))

    @bot.on(events.CallbackQuery(pattern=rb"tgpause_(\d+)"))
    async def tg_pause(event):
        if not await gate(event, count_action=False):
            return
        ctl = _jobs.get(int(event.pattern_match.group(1)))
        if ctl:
            ctl["pause"] = True
        await event.answer("مکث شد.")

    @bot.on(events.CallbackQuery(pattern=rb"tgresume_(\d+)"))
    async def tg_resume(event):
        if not await gate(event, count_action=False):
            return
        ctl = _jobs.get(int(event.pattern_match.group(1)))
        if ctl:
            ctl["pause"] = False
        await event.answer("ادامه یافت.")

    @bot.on(events.CallbackQuery(pattern=rb"tgstop_(\d+)"))
    async def tg_stop(event):
        if not await gate(event, count_action=False):
            return
        ctl = _jobs.get(int(event.pattern_match.group(1)))
        if ctl:
            ctl["stop"] = True
            ctl["pause"] = False
        await event.answer("درخواست توقف ثبت شد.")

    @bot.on(events.CallbackQuery(pattern=rb"tgexport_(\d+)"))
    async def tg_export(event):
        if not await gate(event):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        acc = db.tg_get_account(uid, aid)
        if not acc:
            return
        key = _key(uid, acc["phone"])
        if busy.is_busy(key):
            await _busy_answer(event, key)
            return
        msg = await safe_edit(event, cards.card("📥 گرفتن مخاطبین", [
            cards.kv("Phone", acc["phone"]), "⏳ در حال خواندن ..."]))
        asyncio.create_task(_run_export(uid, acc, msg))

    # ---- multi-account send -------------------------------------------- #
    @bot.on(events.CallbackQuery(data=b"tgmulti"))
    async def tg_multi(event):
        if not await gate(event):
            return
        await _render_multi(event)

    @bot.on(events.CallbackQuery(pattern=rb"tgmpage_(\d+)"))
    async def tg_multi_page(event):
        if not await gate(event, count_action=False):
            return
        await _render_multi(event)

    @bot.on(events.CallbackQuery(pattern=rb"tgmsel_(\d+)"))
    async def tg_multi_select(event):
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        aid = int(event.pattern_match.group(1))
        chosen = _state.setdefault(uid, {}).setdefault("tgmulti", [])
        if aid in chosen:
            chosen.remove(aid)
        else:
            chosen.append(aid)
        await _render_multi(event)

    @bot.on(events.CallbackQuery(data=b"tgmgo"))
    async def tg_multi_go(event):
        if not await gate(event):
            return
        uid = event.sender_id
        chosen = list(_state.get(uid, {}).get("tgmulti") or [])
        if not chosen:
            await event.answer("حداقل یک اکانت انتخاب کن.", alert=True)
            return
        content = db.tg_content_list(uid)
        if not content:
            await event.answer("اول محتوا را تنظیم کن.", alert=True)
            return
        _state.get(uid, {}).pop("tgmulti", None)
        await safe_edit(event, cards.card("📨 ارسال چنداکانتی", [
            cards.kv("Accounts", len(chosen)),
            "⏳ در حال خواندن مخاطبان هر اکانت ...",
        ]))
        asyncio.create_task(_launch_multi(uid, chosen, content, event))

    @bot.on(events.CallbackQuery(data=b"tgjobs"))
    async def tg_jobs(event):
        if not await gate(event):
            return
        await _render_jobs(event)

    @bot.on(events.CallbackQuery(pattern=rb"tgjob_([a-f0-9]+)"))
    async def tg_job_detail(event):
        if not await gate(event, count_action=False):
            return
        uid = event.sender_id
        job_id = event.pattern_match.group(1).decode()
        job = db.tgm_get_job(uid, job_id)
        if not job:
            await event.answer("جاب پیدا نشد.", alert=True)
            return
        buttons = []
        if job["state"] in ("running", "queued", "stop_requested"):
            buttons.append([Button.inline("⛔ توقف", f"tgjstop_{job_id}".encode())])
        elif job["state"] in ("paused", "stopped", "failed"):
            buttons.append([Button.inline("▶️ ادامه", f"tgjres_{job_id}".encode())])
        buttons.append([Button.inline("♻️ بروزرسانی", f"tgjob_{job_id}".encode())])
        buttons.append(_back(b"tgjobs"))
        await safe_edit(event, multi.progress_card(uid, job_id), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"tgjstop_([a-f0-9]+)"))
    async def tg_job_stop(event):
        if not await gate(event):
            return
        uid = event.sender_id
        job_id = event.pattern_match.group(1).decode()
        await event.answer("در حال توقف ...")
        try:
            await multi.stop(uid, job_id)
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="tg multi stop", customer=uid,
                               notify=False)
        await _render_jobs(event)

    @bot.on(events.CallbackQuery(pattern=rb"tgjres_([a-f0-9]+)"))
    async def tg_job_resume(event):
        if not await gate(event):
            return
        uid = event.sender_id
        job_id = event.pattern_match.group(1).decode()
        try:
            await multi.resume(uid, job_id)
            await event.answer("ادامه یافت.")
        except Exception as exc:  # noqa: BLE001
            code = await logbus.error(exc, context="tg multi resume", customer=uid)
            await event.answer(f"ادامه ناموفق بود ({code}).", alert=True)
        await _render_jobs(event)

    register_steps(_STEPS)


async def _launch_multi(customer_id, account_ids: list, content: list,
                        event) -> None:
    try:
        job = await multi.create_job(customer_id, account_ids, content,
                                     target_mode=_target_mode(customer_id))
    except Exception as exc:  # noqa: BLE001
        code = await logbus.error(exc, context="tg multi create",
                                  customer=customer_id)
        try:
            await _bot.send_message(int(customer_id), cards.card(
                "⚠️ شروع ارسال ناموفق بود", [cards.kv("کد خطا", code, width=8)]),
                buttons=[_back(b"tg")])
        except Exception:
            pass
        return
    job_id = job["job_id"]
    if not job.get("total"):
        db.tgm_update_job(customer_id, job_id, state="failed",
                          last_error="no recipients")
        try:
            await _bot.send_message(int(customer_id), cards.card(
                "📨 ارسال چنداکانتی", ["مخاطبی برای ارسال پیدا نشد."]),
                buttons=[_back(b"tg")])
        except Exception:
            pass
        return
    msg = None
    try:
        msg = await _bot.send_message(int(customer_id),
                                      multi.progress_card(customer_id, job_id),
                                      buttons=[[Button.inline(
                                          "📊 وضعیت",
                                          f"tgjob_{job_id}".encode())]])
    except Exception:
        msg = None
    # The message id must be stored BEFORE the job starts: the engine's live card
    # edits that message, and a job that finishes fast would otherwise have
    # nothing to edit.
    if msg is not None:
        db.tgm_update_job(customer_id, job_id, msg_id=msg.id)
    # multi.start also launches the live card. It lives in the engine rather than
    # here so that resuming a job, and reviving one after a restart, get a live
    # card too — neither of which goes through this function.
    await multi.start(customer_id, job_id)


# --------------------------------------------------------------------------- #
# Wizard steps
# --------------------------------------------------------------------------- #
def _normalize_phone(text: str) -> str:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        digits = "98" + digits[1:]
    return "+" + digits if digits else ""


async def _step_phone(event, st):
    uid = event.sender_id
    phone = _normalize_phone(event.raw_text)
    if not phone or len(phone) < 11:
        await _respond(event, "شماره خوانده نشد. با کد کشور بفرست، مثل +989123456789.")
        return
    if db.tg_get_by_phone(uid, phone.lstrip("+")) or db.tg_get_by_phone(uid, phone):
        _state.pop(uid, None)
        await _respond(event, cards.card("این شماره از قبل هست", [
            cards.kv("Phone", phone)]), buttons=[_back(b"tgaccs")])
        return
    await _start_login(event, phone)


async def _step_code(event, st):
    uid = event.sender_id
    code = "".join(ch for ch in (event.raw_text or "") if ch.isdigit())
    if not code:
        await _respond(event, "کد فقط عدد است. دوباره بفرست.")
        return
    ctx = st.get("ctx")
    try:
        await tg.finish_login(ctx, code)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "SessionPasswordNeededError":
            st["step"] = "tg_password"
            await _respond(event, cards.card("🔐 رمز دو مرحله‌ای", [
                "این اکانت رمز دومرحله‌ای دارد. رمز را بفرست."]),
                buttons=[_back(b"tg")])
            return
        err = await logbus.error(exc, context="tg login code", customer=uid)
        await _respond(event, cards.card("⚠️ کد پذیرفته نشد", [
            cards.kv("کد خطا", err, width=8),
            "کد را دوباره بفرست یا از اول شروع کن.",
        ]), buttons=[_back(b"tg")])
        return
    await _finish_login(event, st)


async def _step_password(event, st):
    uid = event.sender_id
    try:
        await tg.finish_password(st.get("ctx"), (event.raw_text or "").strip())
    except Exception as exc:  # noqa: BLE001
        err = await logbus.error(exc, context="tg 2fa", customer=uid)
        await _respond(event, cards.card("⚠️ رمز پذیرفته نشد", [
            cards.kv("کد خطا", err, width=8)]), buttons=[_back(b"tg")])
        return
    await _finish_login(event, st)


async def _step_add_text(event, st):
    uid = event.sender_id
    _state.pop(uid, None)
    text = (event.raw_text or "").strip()
    if not text:
        await _respond(event, "متن خالی بود.")
        return
    db.tg_content_add(uid, "text", text=text)
    await logbus.customer_action(db.get_customer(uid), "tg_content_added", [
        cards.kv("Kind", "text"), cards.kv("Text", text[:120])],
        platform="Telegram")
    await _respond(event, cards.card("✅ اضافه شد", [
        cards.kv("Items", _content_summary(db.tg_content_list(uid)), width=8)]),
        buttons=[_back(b"tgcontent")])


async def _step_add_media(event, st):
    """Store an uploaded file, keeping its real name.

    The real filename matters: a document that arrives as 'file_0.bin' looks
    broken to the recipient.
    """
    uid = event.sender_id
    if event.file is None:
        await _respond(event, "فایلی پیدا نشد. عکس یا فایل بفرست.")
        return
    _state.pop(uid, None)
    name = getattr(event.file, "name", None) or f"file_{os.urandom(3).hex()}"
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "._-") or "file"
    path = os.path.join(MEDIA_DIR, f"{uid}_{os.urandom(4).hex()}_{safe}")
    try:
        await event.download_media(path)
    except Exception as exc:  # noqa: BLE001
        code = await logbus.error(exc, context="tg media download", customer=uid)
        await _respond(event, cards.card("⚠️ دریافت فایل ناموفق بود", [
            cards.kv("کد خطا", code, width=8)]), buttons=[_back(b"tgcontent")])
        return
    caption = (event.raw_text or "").strip()
    db.tg_content_add(uid, "media", text=caption, file_path=path, file_name=safe)
    await logbus.customer_action(db.get_customer(uid), "tg_content_added", [
        cards.kv("Kind", "media"), cards.kv("File", safe),
        cards.kv("Caption", caption[:80] or "—")], platform="Telegram")
    await _respond(event, cards.card("✅ اضافه شد", [
        cards.kv("File", safe, width=8),
        cards.kv("Items", _content_summary(db.tg_content_list(uid)), width=8)]),
        buttons=[_back(b"tgcontent")])


_STEPS = {
    "tg_phone": _step_phone,
    "tg_code": _step_code,
    "tg_password": _step_password,
    "tg_add_text": _step_add_text,
    "tg_add_media": _step_add_media,
}


async def restore_pending() -> None:
    """Hand restart recovery to the job engine."""
    await multi.restore_pending()
