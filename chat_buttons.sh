#!/bin/bash
# Ответы роботу-рекрутеру кнопками (крон, после reply-employers) + сводка отправленного в Telegram.
set -uo pipefail
cd /opt/hh-applicant-tool
set -a; . ./digest.env; set +a
out=$(/usr/bin/docker compose run --rm -T -u docker -e PYTHONPATH=/app/src:/app hh_applicant_tool python -m hh_applicant_tool answer-buttons --send 2>&1 | grep -vE "^time=|Container")
echo "=== $(date "+%F %T")"; echo "$out"
lines=$(grep -E "^(✅|⏸|   ⚠️)" <<<"$out" | head -30)
[ -z "$lines" ] && exit 0
msg=$(printf "🤖 Ответы роботу-рекрутеру кнопками\n\n%s" "$lines")
python3 -c "import json,sys; print(json.dumps({\"chat_id\": sys.argv[1], \"text\": sys.argv[2], \"disable_web_page_preview\": True}))" "$TELEGRAM_NOTIFY_CHAT_ID" "$msg" \
  | curl -s -o /dev/null -X POST -H "Content-Type: application/json" -d @- "https://api.telegram.org/bot${TELEGRAM_NOTIFY_BOT_TOKEN}/sendMessage"
