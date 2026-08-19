"""
tg_panel.py — the Telegram section of the customer bot.
=====================================================

Placeholder shell for section 4. The section menu and the recognisable screens
exist so the two-section start card is never a dead end, but the sending engine,
the multi-account job runner and the photo export are built next.

The same rules as the Rubika side apply here and are enforced the same way:
every screen is scoped to the calling customer, and anything that opens a session
claims it in the busy registry first.
"""
from __future__ import annotations

from telethon import Button

import cards
import config
import db

_bot = None
_state: dict = {}
_gate = None
_safe_edit = None
_respond = None


def _back(target: bytes, label: str = "🔙 بازگشت") -> list:
    return [Button.inline(label, target)]


def menu_card(customer_id) -> str:
    counts = db.tg_count_accounts(customer_id)
    sent = sum(a.get("sent_total", 0) for a in db.tg_list_accounts(customer_id))
    content = db.tg_content_list(customer_id)
    return cards.card("✈️ Telegram", [
        cards.kv("Accounts", f"{counts['total']}  ({counts['healthy']} healthy)"),
        cards.kv("Total Sent", cards.num(sent)),
        cards.kv("Content", f"{len(content)} item(s)"),
        cards.kv("Speed", f"{db.get_float_setting(customer_id, 'tg_send_delay', config.TG_SEND_DELAY)}s"),
    ])


def menu_buttons() -> list:
    return [
        [Button.inline("🚀 ارسال", b"tgsend"),
         Button.inline("➕ افزودن اکانت", b"tgadd")],
        [Button.inline("👤 اکانت‌ها", b"tgaccs"),
         Button.inline("✍️ محتوا", b"tgcontent")],
        [Button.inline("📨 ارسال چنداکانتی", b"tgmulti"),
         Button.inline("📊 وضعیت ارسال‌ها", b"tgjobs")],
        [Button.inline("⚙️ سرعت", b"tgspeed")],
        [Button.inline("🏠 منوی اصلی", b"home")],
    ]


def setup(bot, state, gate, safe_edit, respond, register_steps) -> None:
    global _bot, _state, _gate, _safe_edit, _respond
    _bot, _state, _gate, _safe_edit, _respond = bot, state, gate, safe_edit, respond

    from telethon import events

    @bot.on(events.CallbackQuery(data=b"tg"))
    async def tg_home(event):
        if not await gate(event):
            return
        state.pop(event.sender_id, None)
        await safe_edit(event, menu_card(event.sender_id), buttons=menu_buttons())

    register_steps({})


async def restore_pending() -> None:
    """Nothing to resume yet; the job runner lands in section 4."""
    return None
