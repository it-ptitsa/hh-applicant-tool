#!/bin/bash
# Оперативные ответы в чатах (13.10.2026). Крон: каждые 5 минут в рабочее время, например
#   */5 6-17 * * * bash /путь/chat_watch.sh >> chat_watch.log 2>&1      (UTC → 09:00–21:00 МСК)
# 1. watch-chats — один лёгкий запрос: написал ли работодатель что-то новое. Нет — выходим.
# 2. Есть — reply-employers --only-new (нейронка в чатах с новыми сообщениями по reply_system_prompt.txt)
#    и answer-buttons --send (кнопки робота-рекрутера), затем watch-chats --commit.
# 3. Если есть digest.env с TELEGRAM_NOTIFY_* — сводка того, что ответили и что оставлено человеку.
# Команда кликера: HH_CMD (по умолчанию docker compose на сервере; локально — ".venv/bin/python -m hh_applicant_tool").
set -uo pipefail
cd "$(dirname "$0")"
HH=${HH_CMD:-"docker compose run --rm -T -u docker hh_applicant_tool python -m hh_applicant_tool"}
exec 9>/tmp/hh_chat_watch.lock
flock -n 9 || exit 0   # прошлый запуск ещё отвечает

$HH watch-chats >/dev/null 2>&1 || exit 0
echo "=== $(date '+%F %T') новое сообщение — отвечаю"
replies=$($HH reply-employers --ai --only-new --system-prompt "$(cat reply_system_prompt.txt)" 2>&1 | grep -vE "^time=|Container")
buttons=$($HH answer-buttons --send 2>&1 | grep -vE "^time=|Container")
echo "$replies"; echo "$buttons"
$HH watch-chats --commit >/dev/null 2>&1

[ -f digest.env ] || exit 0
set -a; . ./digest.env; set +a
[ -n "${TELEGRAM_NOTIFY_BOT_TOKEN:-}" ] || exit 0
lines=$(printf "%s\n%s\n" "$replies" "$buttons" | grep -E "^(📨|✅|⏸|📋|   ⚠️)" | head -30)
[ -z "$lines" ] && exit 0
msg=$(printf "💬 Ответы в чатах hh\n\n%s" "$lines")
python3 -c 'import json,sys; print(json.dumps({"chat_id": sys.argv[1], "text": sys.argv[2], "disable_web_page_preview": True}))' \
  "$TELEGRAM_NOTIFY_CHAT_ID" "$msg" \
  | curl -s -o /dev/null -X POST -H "Content-Type: application/json" -d @- \
    "https://api.telegram.org/bot${TELEGRAM_NOTIFY_BOT_TOKEN}/sendMessage"
