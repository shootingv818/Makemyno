#!/usr/bin/env bash
#
# Makemyno — bootstrap a bare Debian/Ubuntu server.
#
# Run once, as root, on a fresh machine:
#
#     bash install.sh
#
# It installs the system packages, creates a virtualenv, installs the Python
# dependencies, writes a .env if there is not one already, and installs two
# systemd services so both bots survive a reboot and restart themselves after a
# crash.
#
# Safe to run again: every step checks before acting, so re-running it after a
# `git pull` just refreshes the dependencies and restarts the services.
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/makemyno}"
REPO_URL="${REPO_URL:-https://github.com/shootingv818/Makemyno.git}"
BRANCH="${BRANCH:-feat/multi-tenant-foundation}"
PY_MIN_MINOR=10          # the source uses `dict | None`, which needs 3.10+

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[1;31mxx  %s\033[0m\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "با root اجرا کن:  sudo bash install.sh"

# Check the assumptions BEFORE touching anything. Without this the script fails
# halfway through with "apt-get: command not found", which says nothing about
# what is actually wrong.
command -v apt-get >/dev/null \
    || die "این اسکریپت برای Debian/Ubuntu است (apt-get پیدا نشد).
    اگر سرورت CentOS/Alma/Rocky است بگو، نسخه‌ی dnf را می‌نویسم."
command -v systemctl >/dev/null \
    || die "systemd پیدا نشد. اگر داخل کانتینر اجرا می‌کنی از docker-compose.yml
    استفاده کن؛ این اسکریپت برای یک سرور واقعی است."

# --------------------------------------------------------------------------- #
say "بسته‌های سیستم"
# --------------------------------------------------------------------------- #
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3-dev + build-essential + the libjpeg/zlib/freetype headers are here as a
# fallback: Pillow ships manylinux wheels for the usual CPython versions, but on
# an unusual interpreter pip falls back to building from source and then it needs
# these. Installing them up front turns a confusing build failure into nothing.
apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    build-essential pkg-config \
    libjpeg-dev zlib1g-dev libfreetype6-dev \
    git curl ca-certificates tzdata sqlite3 >/dev/null
ok "نصب شد"

PY_MINOR="$(python3 -c 'import sys;print(sys.version_info[1])')"
[ "$PY_MINOR" -ge "$PY_MIN_MINOR" ] \
    || die "پایتون 3.$PY_MINOR قدیمی است. حداقل 3.$PY_MIN_MINOR لازم است."
ok "پایتون 3.$PY_MINOR"

say "منطقه‌ی زمانی"
timedatectl set-timezone Asia/Tehran 2>/dev/null || warn "نشد؛ ادامه می‌دهم"
ok "$(date '+%Y-%m-%d %H:%M %Z')"

# --------------------------------------------------------------------------- #
say "کد پروژه"
# --------------------------------------------------------------------------- #
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" checkout --quiet "$BRANCH"
    git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"
    ok "به‌روزرسانی شد: $(git -C "$APP_DIR" rev-parse --short HEAD)"
elif [ -f "$(dirname "$0")/../main.py" ]; then
    # Running from inside an already-uploaded copy: use it where it is.
    APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    ok "از نسخه‌ی موجود استفاده می‌کنم: $APP_DIR"
else
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    ok "کلون شد: $(git -C "$APP_DIR" rev-parse --short HEAD)"
fi
cd "$APP_DIR"

# --------------------------------------------------------------------------- #
say "محیط مجازی و کتابخانه‌ها"
# --------------------------------------------------------------------------- #
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip wheel setuptools
./.venv/bin/pip install --quiet --prefer-binary --retries 10 --timeout 180 \
    -r requirements.txt
ok "$(./.venv/bin/pip list 2>/dev/null | wc -l) بسته نصب است"

# Prove the imports actually resolve before we hand the machine to systemd.
# Without this the first sign of a broken install is a service that flaps.
./.venv/bin/python - <<'PY' || die "کتابخانه‌ها درست نصب نشدند"
import importlib
for m in ("telethon", "rubpy", "dotenv", "cryptography", "fastapi",
          "uvicorn", "aiohttp", "reportlab", "PIL", "asyncssh"):
    importlib.import_module(m)
print("    ok  همه‌ی ایمپورت‌ها سالم")
PY

# --------------------------------------------------------------------------- #
say "فایل .env"
# --------------------------------------------------------------------------- #
if [ -f .env ]; then
    ok ".env از قبل هست، دست نمی‌زنم"
    # ...with one exception. A MODE line here used to override the per-service
    # role (systemd lets EnvironmentFile win over Environment), which started the
    # owner bot twice and the customer bot never. Existing installs carry that
    # line, so it is removed on upgrade.
    if grep -q '^MODE=' .env; then
        sed -i '/^MODE=/d' .env
        warn "خط MODE از .env حذف شد — باعث می‌شد هر دو سرویس ربات مالک را اجرا کنند"
    fi
else
    [ -f deploy/env.template ] || die "deploy/env.template پیدا نشد"
    cp deploy/env.template .env
    # One Fernet key per installation, generated here so it never lives in git.
    # It encrypts worker credentials, portable session tokens and backups: if it
    # is lost, existing backups can never be restored, so it is generated ONCE
    # and then left alone.
    KEY="$(./.venv/bin/python -c \
        'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
    sed -i "s|^WORKER_SECRET=.*|WORKER_SECRET=$KEY|" .env
    ok ".env ساخته شد و کلید رمزنگاری تولید شد"
    warn "این کلید را جایی نگه دار — بدون آن بکاپ‌های قبلی باز نمی‌شوند"
fi
chmod 600 .env
mkdir -p data && chmod 700 data

# Refuse to continue with a half-filled .env: a bot that starts and immediately
# exits with "تنظیمات ناقص" is harder to diagnose than a clear failure here.
say "بررسی تنظیمات"
MODE=owner ./.venv/bin/python - <<'PY' || die "تنظیمات ناقص است؛ .env را کامل کن"
import config
missing = set(config.validate_owner()) | set(config.validate_customer())
if missing:
    raise SystemExit("    xx  ناقص: " + ", ".join(sorted(missing)))
print("    ok  همه‌ی کلیدهای لازم پر است")
print("    ok  OWNER_ID     =", config.OWNER_ID)
print("    ok  LOG_GROUP_ID =", config.LOG_GROUP_ID)
PY

# --------------------------------------------------------------------------- #
say "سرویس‌های systemd"
# --------------------------------------------------------------------------- #
for role in owner customer; do
    sed -e "s|{{APP_DIR}}|$APP_DIR|g" -e "s|{{ROLE}}|$role|g" \
        deploy/makemyno.service.template > "/etc/systemd/system/makemyno-$role.service"
done
systemctl daemon-reload
systemctl enable --quiet makemyno-owner makemyno-customer
ok "makemyno-owner و makemyno-customer فعال شدند"

say "راه‌اندازی"
systemctl restart makemyno-owner makemyno-customer
sleep 4
for role in owner customer; do
    if systemctl is-active --quiet "makemyno-$role"; then
        ok "makemyno-$role در حال اجرا"
    else
        warn "makemyno-$role بالا نیامد — لاگ:"
        journalctl -u "makemyno-$role" -n 25 --no-pager || true
    fi
done

cat <<EOF

$(printf '\033[1;32m%s\033[0m' "تمام شد.")

  وضعیت      :  bash $APP_DIR/deploy/makemyno.sh status
  لاگ زنده   :  bash $APP_DIR/deploy/makemyno.sh logs
  ری‌استارت  :  bash $APP_DIR/deploy/makemyno.sh restart
  به‌روزرسانی :  bash $APP_DIR/deploy/makemyno.sh update

  حالا در تلگرام به ربات مالک /start بزن.

EOF
