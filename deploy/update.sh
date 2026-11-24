#!/usr/bin/env bash
# Обновление FunPay Helper Bot на сервере (git pull + перезапуск сервиса).
# Использование: sudo bash /opt/funpay-helper/deploy/update.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/funpay-helper}"
SERVICE_USER="${SERVICE_USER:-funpay}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash $APP_DIR/deploy/update.sh"
[[ -d "$APP_DIR/.git" ]] || die "В $APP_DIR нет git-репозитория"

log "1/4 Получение обновлений"
git -C "$APP_DIR" fetch --all --tags
git -C "$APP_DIR" pull --ff-only

log "2/4 Зависимости"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

log "3/4 systemd-юниты"
for unit in funpay-helper.service funpay-healthcheck.service funpay-healthcheck.timer \
            funpay-backup.service funpay-backup.timer; do
  install -m 644 "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

log "4/4 Перезапуск сервиса"
systemctl restart funpay-helper.service
systemctl status funpay-helper.service --no-pager -l || true
printf '\nОбновление завершено. Логи: journalctl -u funpay-helper -f\n'
