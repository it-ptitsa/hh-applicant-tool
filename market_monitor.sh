#!/bin/bash
# Утренняя сводка по рынку фронтенд-вакансий → ТГ (спека openspec/specs/market-monitor).
# Крон хоста: 0 5 * * * (08:00 МСК). Бот — digest.env.
#
# Монитор работает от ОТДЕЛЬНОГО аккаунта hh (config/monitor/, вне git), не от кликера:
# 05.10.2026 тяжёлые выборки с токена кликера нагнали капчу на его аккаунт — карточки
# вакансий перестали отдаваться и кликеру. Токен монитора обновляем перед каждым запуском.
cd /opt/hh-applicant-tool
set -a; . /opt/hh-applicant-tool/digest.env; set +a
docker compose run --rm -T -u docker hh_applicant_tool \
  python -m hh_applicant_tool -c /app/config/monitor refresh-token
docker compose run --rm -T -u docker \
  -e CONFIG_DIR=/app/config/monitor \
  -e TELEGRAM_NOTIFY_BOT_TOKEN="$TELEGRAM_NOTIFY_BOT_TOKEN" \
  -e TELEGRAM_NOTIFY_CHAT_ID="$TELEGRAM_NOTIFY_CHAT_ID" \
  hh_applicant_tool python /app/market_monitor.py --db /app/config/market_v2.db "$@"
