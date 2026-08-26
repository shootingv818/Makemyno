"""
cards.py — the ONE place that defines how every card in this project looks.
==========================================================================

The panel style is inherited verbatim from the base project and must not drift:

    LINE = 31 dashes
    card(title, rows)              ->  title / LINE / rows
    panel_card(tag, rows, footer)  ->  | tag / LINE / rows / LINE / footer

Every screen and every log card in every role goes through these helpers, so the
whole product (customer bot, owner bot, log group) reads the same. There is
deliberately no second divider style anywhere in the codebase.
"""
from __future__ import annotations

import config

# The project-wide divider: exactly 31 dashes. Do not change.
LINE = "-------------------------------"


def now() -> str:
    """Timestamp in the configured timezone — used in every card footer."""
    try:
        return config.now_str()
    except Exception:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def card(title: str, rows: list) -> str:
    """Standard screen/log card:  title + divider + rows."""
    return f"{title}\n{LINE}\n" + "\n".join(str(r) for r in rows)


def panel_card(tag: str, rows: list, footer: str = None) -> str:
    """Log-card shell:  `| <emoji> - #<tag>` + dividers + rows + optional footer."""
    out = f"| {tag}\n{LINE}\n" + "\n".join(str(r) for r in rows) + f"\n{LINE}"
    if footer:
        out += f"\n{footer}"
    return out


def kv(key: str, value, width: int = 16) -> str:
    """A `• Key            : Value` row, key padded so columns line up."""
    return f"• {str(key).ljust(width)}: {value}"


def section(title: str, rows: list) -> list:
    """An indented sub-block for the two-section start card."""
    out = [title]
    out.extend(f"   {r}" for r in rows)
    return out


def bar(done: int, total: int, width: int = 10) -> str:
    """Text progress bar: ██████░░░░"""
    try:
        total = max(1, int(total))
        done = max(0, min(int(done), total))
    except (TypeError, ValueError):
        return "░" * width
    filled = int(round(width * done / total))
    return "█" * filled + "░" * (width - filled)


def body(text, limit: int = 700) -> list:
    """A multi-line value rendered as its OWN rows, not squeezed into a kv line.

    The send card printed the customer's advert as kv("Content", text[:80]) and 80
    characters lands in the middle of a sentence or, worse, in the middle of a URL —
    so the owner saw "...خوشحال شی  https://t" and could not tell what was actually
    being sent. A kv line is for one short value; a paragraph needs rows.

    Long text is cut at `limit`, but the cut is ANNOUNCED with the real length, so
    nobody mistakes a truncated preview for the whole message.
    """
    text = "" if text is None else str(text)
    if not text.strip():
        return ["—"]
    shown = text[:limit]
    rows = [line for line in shown.splitlines() if line.strip()]
    if len(text) > limit:
        rows.append(f"… (کل {len(text)} کاراکتر)")
    return rows


def num(value) -> str:
    """1234567 -> 1,234,567 (never raises)."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def dot(ok) -> str:
    """Status dot used everywhere: True -> green, False -> red, None -> white."""
    if ok is None:
        return "⚪️"
    return "🟢" if ok else "🔴"


def paginate(items, page: int, cb_prefix: str, Button, per_page: int = None):
    """Return (page_items, nav_row, page, total_pages) for a long button list.

    Every long list in the project (accounts, customers, workers) uses this, so
    pagination looks and behaves identically everywhere. The first page has no
    "Prev" and the last page has no "Next".
    """
    per_page = int(per_page or config.ACC_PAGE_SIZE)
    per_page = max(1, per_page)
    items = list(items)
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    page_items = items[start:start + per_page]
    nav = []
    if page > 0:
        nav.append(Button.inline("◀️ صفحه قبل", f"{cb_prefix}{page - 1}".encode()))
    if page < total_pages - 1:
        nav.append(Button.inline("صفحه بعد ▶️", f"{cb_prefix}{page + 1}".encode()))
    return page_items, nav, page, total_pages
