#!/bin/bash
# Утренняя сводка по рынку фронтенд-вакансий → ТГ (спека openspec/specs/market-monitor).
# Крон хоста: 0 5 * * * (08:00 МСК). Токен hh — config/config.json, бот — digest.env.
cd /opt/hh-applicant-tool
set -a; . /opt/hh-applicant-tool/digest.env; set +a
docker compose run --rm -T -u docker \
  -e TELEGRAM_NOTIFY_BOT_TOKEN="$TELEGRAM_NOTIFY_BOT_TOKEN" \
  -e TELEGRAM_NOTIFY_CHAT_ID="$TELEGRAM_NOTIFY_CHAT_ID" \
  hh_applicant_tool python /app/market_monitor.py "$@"
