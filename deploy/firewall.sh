#!/usr/bin/env bash
# Минимальный файрвол: только входящий SSH, всё остальное запрещено.
# ВАЖНО: перед выполнением убедитесь, что текущий SSH-порт разрешён.
set -euo pipefail

die() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash deploy/firewall.sh"

SSH_PORT="$(awk '/^[[:space:]]*Port[[:space:]]+/ {print $2; exit}' /etc/ssh/sshd_config 2>/dev/null || true)"
SSH_PORT="${SSH_PORT:-22}"

echo "==> ufw: по умолчанию запрещаем входящие, разрешаем исходящие"
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp" comment 'SSH'

echo "==> Включение ufw (порт SSH: $SSH_PORT)"
ufw --force enable
ufw status verbose
