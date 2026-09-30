#!/bin/bash
# Сводка по чатам с работодателями → ТГ (бот-ассистент).
cd /opt/hh-applicant-tool
set -a; . /opt/hh-applicant-tool/digest.env; set +a
docker compose run --rm -T -u docker \
  -e TELEGRAM_NOTIFY_BOT_TOKEN="$TELEGRAM_NOTIFY_BOT_TOKEN" \
  -e TELEGRAM_NOTIFY_CHAT_ID="$TELEGRAM_NOTIFY_CHAT_ID" \
  hh_applicant_tool python /app/chat_digest.py --hours "${1:-24}"
