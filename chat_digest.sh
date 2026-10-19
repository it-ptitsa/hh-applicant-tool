#!/bin/bash
# Сводка по чатам с работодателями → ТГ (бот-ассистент).
# Аргументы: [часы=24] [профиль] [подпись в отчёте]. Без профиля — основной аккаунт кликера.
cd /opt/hh-applicant-tool
set -a; . /opt/hh-applicant-tool/digest.env; set +a
docker compose run --rm -T -u docker \
  -e TELEGRAM_NOTIFY_BOT_TOKEN="$TELEGRAM_NOTIFY_BOT_TOKEN" \
  -e TELEGRAM_NOTIFY_CHAT_ID="$TELEGRAM_NOTIFY_CHAT_ID" \
  ${2:+-e CONFIG_DIR=/app/config/$2} -e ACCOUNT_LABEL="${3:-}" \
  hh_applicant_tool python /app/chat_digest.py --hours "${1:-24}"
