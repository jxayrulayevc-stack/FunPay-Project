#!/usr/bin/env bash
# Установка FunPay Helper Bot на чистую Ubuntu 22.04/24.04 (x86_64 или aarch64).
# Использование: sudo bash deploy/install.sh [URL_РЕПОЗИТОРИЯ]
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/funpay-helper}"
SERVICE_USER="${SERVICE_USER:-funpay}"
ADMIN_ID="${ADMIN_ID:-8875725946}"
REPO_URL="${1:-${REPO_URL:-}}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash deploy/install.sh <URL репозитория>"

log "1/9 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl sqlite3 ufw unattended-upgrades

log "2/9 Swap, если RAM < 2 ГБ"
mem_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
if (( mem_mb < 2048 )) && [[ ! -f /swapfile ]]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10
fi

log "3/9 Сервисный пользователь $SERVICE_USER"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$SERVICE_USER"

log "4/9 Код проекта"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only
elif [[ -n "$REPO_URL" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  die "Укажите URL репозитория: sudo bash deploy/install.sh https://github.com/USER/funpay-helper.git"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

log "5/9 Каталоги данных"
sudo -u "$SERVICE_USER" mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/backups"
chmod 700 "$APP_DIR/data"

log "6/9 Файл .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
  sudo -u "$SERVICE_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  read -rsp 'Введите токен Telegram-бота (символы не видны на экране): ' BOT_TOKEN
  echo
  [[ -n "$BOT_TOKEN" ]] || die "Токен не введён"
  sudo -u "$SERVICE_USER" env BOT_TOKEN="$BOT_TOKEN" ADMIN_ID="$ADMIN_ID" python3 - "$APP_DIR/.env" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
for line in lines:
    if line.startswith("ADMIN_ID="):
        updated.append(f"ADMIN_ID={os.environ['ADMIN_ID']}")
    elif line.startswith("BOT_TOKEN="):
        updated.append(f"BOT_TOKEN={os.environ['BOT_TOKEN']}")
    else:
        updated.append(line)
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
  chmod 600 "$APP_DIR/.env"
  echo "Создан $APP_DIR/.env с ADMIN_ID=$ADMIN_ID"
else
  echo "Существующий $APP_DIR/.env не трогаем"
fi

log "7/9 Виртуальное окружение"
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

log "8/9 systemd-юниты"
chmod +x "$APP_DIR/deploy/backup.sh"
for unit in funpay-helper.service funpay-healthcheck.service funpay-healthcheck.timer \
            funpay-backup.service funpay-backup.timer; do
  install -m 644 "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now funpay-helper.service funpay-healthcheck.timer funpay-backup.timer

log "9/9 Файрвол и автообновления"
bash "$APP_DIR/deploy/firewall.sh"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

systemctl status funpay-helper.service --no-pager -l || true
printf '\nГотово. Отправьте боту /start, затем /cookie с golden_key FunPay.\n'
printf 'Проверка: journalctl -u funpay-helper -f\n'
