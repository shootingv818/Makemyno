"""
backup.py — fast, SESSION-ONLY, encrypted backup. Owner side only.
==================================================================

WHAT IS IN THE ARCHIVE, AND WHY SO LITTLE
-----------------------------------------
Only what is needed to restore accounts:

    rubika/local/c<customer>/<files>            master's own sessions
    rubika/workers/<tag>/c<customer>/<files>    each remote worker's sessions
    telegram/c<customer>/acc_<id>_<phone>.session

The databases, the .env, the logs and the source are deliberately EXCLUDED. The
base project's backup zipped the whole deployment — which meant the file also
contained every worker's SSH password. A backup that leaks the infrastructure it
is meant to protect is worse than no backup, so the fix here is to shrink the
contents rather than to guard the button.

The finished archive is encrypted with Fernet before it leaves the machine,
because a session is equivalent to the account itself. If no key is configured
the build FAILS rather than silently producing a plaintext archive.

Worker sessions are fetched in parallel, and the layout keeps each customer's
sessions in their own folder so a restore cannot merge two customers who happen
to own the same phone number.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile

import cards
import central_db
import config
import crypto_util
import db
import logbus
import rubika_client as rb

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# One backup at a time: the manual button and the periodic loop must not build
# the same archive twice concurrently.
_lock = asyncio.Lock()


class BackupError(RuntimeError):
    """Raised when an archive cannot be produced safely."""


def _add_dir(zf: zipfile.ZipFile, src_dir: str, arc_prefix: str) -> int:
    if not os.path.isdir(src_dir):
        return 0
    count = 0
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, src_dir)
            try:
                zf.write(full, arcname=os.path.join(arc_prefix, rel))
                count += 1
            except OSError:
                continue
    return count


async def build_archive() -> tuple:
    """Build the session-only archive. Returns (path, meta) or (None, meta)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    meta = {"rb_local": 0, "rb_workers": 0, "tg": 0, "unreachable": []}

    # 1) remote worker sessions, fetched in parallel (network-bound)
    worker_files = []
    try:
        import worker
        worker_files, meta["unreachable"] = await worker.collect_worker_sessions(
            "rubika/workers")
    except Exception as exc:  # noqa: BLE001
        meta["unreachable"].append(
            f"collect_worker_sessions raised: {type(exc).__name__}: "
            f"{str(exc)[:140]}")
        await logbus.warn("backup_partial", [
            cards.kv("Detail", f"worker sessions incomplete: {repr(exc)[:150]}")])

    # 2) Telegram StringSessions straight from the database rows
    tg_rows = []
    for cust in db.owner_list_customers():
        cid = cust["telegram_id"]
        for acc in db.tg_list_accounts(cid):
            if acc.get("session"):
                tg_rows.append((cid, acc))

    local_has = os.path.isdir(rb.SESSIONS_DIR) and any(os.scandir(rb.SESSIONS_DIR))
    if not (worker_files or tg_rows or local_has):
        return None, meta

    fd, zip_path = tempfile.mkstemp(prefix="sessions_", suffix=".zip", dir=DATA_DIR)
    os.close(fd)
    # compresslevel=1: session files are small, so favour speed over ratio.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=1) as zf:
        meta["rb_local"] = _add_dir(zf, rb.SESSIONS_DIR, "rubika/local")
        for arcname, data in worker_files:
            try:
                zf.writestr(arcname, data)
                meta["rb_workers"] += 1
            except Exception:
                continue
        for cid, acc in tg_rows:
            safe = "".join(ch for ch in str(acc.get("phone") or acc["id"])
                           if ch.isalnum())
            try:
                zf.writestr(f"telegram/c{cid}/acc_{acc['id']}_{safe}.session",
                            acc["session"])
                meta["tg"] += 1
            except Exception:
                continue
    return zip_path, meta


def _encrypt(zip_path: str) -> str:
    """Encrypt the archive in place and return the new path.

    Fails loudly when no key is configured: a plaintext bundle of live sessions
    must never be produced by accident.
    """
    if not crypto_util.is_configured():
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise BackupError(
            "WORKER_SECRET تنظیم نشده؛ بکاپ بدون رمزگذاری ساخته نمی‌شود.")
    with open(zip_path, "rb") as fh:
        data = fh.read()
    token = crypto_util.encrypt_bytes(data)
    out_path = zip_path + ".enc"
    with open(out_path, "wb") as fh:
        fh.write(token)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return out_path


def summary_rows(meta: dict) -> list:
    rows = [
        cards.kv("Rubika (master)", cards.num(meta.get("rb_local", 0))),
        cards.kv("Rubika (workers)", cards.num(meta.get("rb_workers", 0))),
        cards.kv("Telegram", cards.num(meta.get("tg", 0))),
        cards.kv("Encryption", "🔐 ON"),
    ]
    problems = meta.get("unreachable") or []
    if problems:
        rows.append(cards.kv("State", f"⚠️ partial — {len(problems)} "
                                      f"worker(s) not collected"))
        # The reason, not just the count. "⚠️ partial — 1 worker(s) unreachable"
        # on its own is unactionable: a wrong SSH password, a rebuilt worker with
        # no sessions yet and a firewalled port all printed that same line, and
        # the only way to tell them apart was to SSH in.
        for problem in problems[:5]:
            rows.append(cards.kv("Why", str(problem)[:160]))
        if len(problems) > 5:
            rows.append(cards.kv("More", f"+{len(problems) - 5}"))
    else:
        rows.append(cards.kv("State", "✅ complete"))
    return rows


async def run_backup(to_owner: int = None) -> dict:
    """Build and ship an encrypted session backup.

    Returns {"ok": bool, "meta": dict, "error": str}. The archive is sent to the
    log group, and privately to `to_owner` when given.
    """
    async with _lock:
        path = None
        try:
            try:
                path, meta = await build_archive()
            except Exception as exc:  # noqa: BLE001
                code = await logbus.error(exc, context="backup.build",
                                          notify=False)
                return {"ok": False, "meta": {}, "error": code}

            if not path:
                return {"ok": False, "meta": meta, "error": "no-sessions"}

            try:
                path = _encrypt(path)
            except BackupError as exc:
                await logbus.warn("backup_refused", [cards.kv("Reason", str(exc))])
                return {"ok": False, "meta": meta, "error": str(exc)}

            caption = cards.card("💾 - #backup", summary_rows(meta)
                                 + [f"🕒 {cards.now()}"])
            await logbus.to_group_file(path, caption=caption)
            if to_owner:
                try:
                    await logbus._client.send_file(          # noqa: SLF001
                        int(to_owner), path, caption=caption, force_document=True)
                except Exception:
                    pass
            central_db.set_last_backup()
            return {"ok": True, "meta": meta, "error": ""}
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


async def backup_loop() -> None:
    """Periodic automatic backup (owner process only)."""
    interval = int(config.BACKUP_INTERVAL or 0)
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            await run_backup()
        except Exception as exc:  # noqa: BLE001
            await logbus.error(exc, context="backup.loop", notify=False)


def stats() -> dict:
    """Counts for the owner's backup screen, without building anything."""
    rb_local = 0
    if os.path.isdir(rb.SESSIONS_DIR):
        for _root, _dirs, files in os.walk(rb.SESSIONS_DIR):
            rb_local += len(files)
    tg = 0
    for cust in db.owner_list_customers():
        tg += sum(1 for a in db.tg_list_accounts(cust["telegram_id"])
                  if a.get("session"))
    return {
        "rb_local": rb_local,
        "tg": tg,
        "encrypted": crypto_util.is_configured(),
        "last": central_db.get_last_backup(),
        "interval": int(config.BACKUP_INTERVAL or 0),
    }
