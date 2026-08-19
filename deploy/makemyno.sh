#!/usr/bin/env bash
#
# Makemyno — day-to-day control.
#
#   bash makemyno.sh start | stop | restart | status | logs | update | check
#
# `logs` follows both bots at once, which is what you actually want: a customer
# action shows up in one and its consequences in the other.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICES=(makemyno-owner makemyno-customer)

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m  %s\n' "$*"; }
bad()  { printf '    \033[1;31mxx\033[0m  %s\n' "$*"; }
warn() { printf '    \033[1;33m!!\033[0m  %s\n' "$*"; }

need_root() {
    [ "$(id -u)" -eq 0 ] || { echo "با sudo اجرا کن"; exit 1; }
}

case "${1:-status}" in

start)   need_root; systemctl start "${SERVICES[@]}";   say "شروع شد"; exec "$0" status ;;
stop)    need_root; systemctl stop  "${SERVICES[@]}";   say "متوقف شد" ;;
restart) need_root; systemctl restart "${SERVICES[@]}"; sleep 3; exec "$0" status ;;

status)
    say "سرویس‌ها"
    for s in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$s"; then
            since="$(systemctl show -p ActiveEnterTimestamp --value "$s" | cut -d' ' -f2-3)"
            ok "$s — از $since"
        else
            bad "$s — خوابیده"
        fi
    done

    say "پایگاه داده"
    for f in customer central; do
        p="$APP_DIR/data/$f.db"
        [ -f "$p" ] && ok "$f.db — $(du -h "$p" | cut -f1)" || warn "$f.db هنوز ساخته نشده"
    done

    say "سشن‌ها"
    d="$APP_DIR/data/sessions"
    if [ -d "$d" ]; then
        ok "$(find "$d" -mindepth 1 -maxdepth 1 -type d | wc -l) مشتری، $(find "$d" -name 'acc_*' | wc -l) اکانت"
    else
        warn "هنوز اکانتی اضافه نشده"
    fi

    say "خطاهای اخیر"
    n="$(journalctl -u makemyno-owner -u makemyno-customer --since '1 hour ago' \
         --no-pager 2>/dev/null | grep -ci 'Traceback\|CRITICAL' || true)"
    [ "$n" = "0" ] && ok "در یک ساعت گذشته چیزی نبود" || bad "$n خطا در یک ساعت گذشته"
    echo
    ;;

logs)
    # Both units in one stream, so cause and effect stay next to each other.
    exec journalctl -u makemyno-owner -u makemyno-customer -f -n 100 --no-pager
    ;;

logs-owner)    exec journalctl -u makemyno-owner    -f -n 100 --no-pager ;;
logs-customer) exec journalctl -u makemyno-customer -f -n 100 --no-pager ;;

update)
    need_root
    say "به‌روزرسانی"
    cd "$APP_DIR"
    before="$(git rev-parse --short HEAD)"
    branch="$(git rev-parse --abbrev-ref HEAD)"
    git fetch --quiet origin "$branch"
    git reset --hard --quiet "origin/$branch"
    ok "$before → $(git rev-parse --short HEAD)"

    ./.venv/bin/pip install --quiet -r requirements.txt
    ok "کتابخانه‌ها بررسی شد"

    # Check the new code loads BEFORE restarting into it. A syntax error found
    # here is a five-second annoyance; found by systemd it is a flapping service.
    if ! MODE=owner ./.venv/bin/python -c 'import config, owner_bot, customer_bot' 2>/dev/null; then
        bad "کد جدید ایمپورت نمی‌شود — ری‌استارت نمی‌کنم، نسخه‌ی قبلی همچنان بالاست"
        MODE=owner ./.venv/bin/python -c 'import config, owner_bot, customer_bot' || true
        exit 1
    fi
    ok "کد جدید سالم است"

    # The schema migrates itself on init (_ensure_columns), so a new column on an
    # existing database is not a manual step.
    systemctl restart "${SERVICES[@]}"
    sleep 3
    exec "$0" status
    ;;

check)
    say "بررسی تنظیمات"
    cd "$APP_DIR"
    MODE=owner ./.venv/bin/python - <<'PY'
import config
miss = set(config.validate_owner()) | set(config.validate_customer())
if miss:
    print("    xx  ناقص:", ", ".join(sorted(miss)))
    raise SystemExit(1)
print("    ok  تنظیمات کامل است")
print("    ok  OWNER_ID     =", config.OWNER_ID)
print("    ok  LOG_GROUP_ID =", config.LOG_GROUP_ID)
print("    ok  VERSION      =", config.VERSION)
print("    ok  PROBE_DAILY_CAP =", config.PROBE_DAILY_CAP)
PY
    say "اتصال به تلگرام"
    # Cheap end-to-end proof: resolve both tokens and post one line to the log
    # group. If the bots are not in that group, this is where you find out —
    # rather than wondering later why the log group is empty.
    MODE=owner ./.venv/bin/python - <<'PY'
import asyncio, config
from telethon import TelegramClient

async def probe(name, token):
    c = TelegramClient(f"data/_check_{name}", config.API_ID, config.API_HASH)
    await c.start(bot_token=token)
    me = await c.get_me()
    print(f"    ok  {name}: @{me.username}")
    try:
        await c.send_message(config.LOG_GROUP_ID, f"✅ health check — {name}")
        print(f"    ok  {name}: ارسال به گروه لاگ موفق")
    except Exception as e:
        print(f"    xx  {name}: ارسال به گروه لاگ نشد — {type(e).__name__}: {e}")
        print("        ربات را به گروه اضافه کن و اجازه‌ی ارسال بده")
    await c.disconnect()

async def main():
    await probe("owner", config.OWNER_BOT_TOKEN)
    await probe("customer", config.CUSTOMER_BOT_TOKEN)

asyncio.run(main())
PY
    rm -f "$APP_DIR"/data/_check_*.session
    echo
    ;;

*)
    cat <<EOF

  bash makemyno.sh <دستور>

    start          روشن کردن هر دو ربات
    stop           خاموش کردن
    restart        ری‌استارت
    status         وضعیت سرویس‌ها، دیتابیس، سشن‌ها، خطاهای یک ساعت اخیر
    logs           لاگ زنده‌ی هر دو ربات
    logs-owner     فقط ربات مالک
    logs-customer  فقط ربات مشتری
    update         git pull + بررسی سالم بودن کد + ری‌استارت
    check          بررسی .env و تست واقعی اتصال به تلگرام و گروه لاگ

EOF
    ;;
esac
