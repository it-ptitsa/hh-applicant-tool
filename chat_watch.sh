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
# Ответы через подписку Claude (provider claude-cli): если на хосте есть claude и токен
# от `claude setup-token`, отдаём их контейнеру. Токен — через окружение, не в аргументах (видны в ps).
CLAUDE_BIN=${CLAUDE_BIN:-/usr/bin/claude}
CLAUDE_TOKEN_FILE=${CLAUDE_TOKEN_FILE:-/root/.claude_oauth_token}
if [ -z "${HH_CMD:-}" ] && [ -x "$CLAUDE_BIN" ] && [ -r "$CLAUDE_TOKEN_FILE" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN=$(cat "$CLAUDE_TOKEN_FILE")
  HH="docker compose run --rm -T -u docker -v $CLAUDE_BIN:/usr/local/bin/claude:ro -e CLAUDE_CODE_OAUTH_TOKEN -e HOME=/tmp hh_applicant_tool python -m hh_applicant_tool"
fi
# Второй аккаунт: HH_PROFILE=acc5 ACCOUNT_LABEL=Синицын bash chat_watch.sh — свой профиль и свой замок
[ -n "${HH_PROFILE:-}" ] && HH="$HH --profile-id $HH_PROFILE"
exec 9>/tmp/hh_chat_watch${HH_PROFILE:+_$HH_PROFILE}.lock
flock -n 9 || exit 0   # прошлый запуск ещё отвечает

$HH watch-chats >/dev/null 2>&1 || exit 0
echo "=== $(date '+%F %T') новое сообщение — отвечаю"
replies=$($HH reply-employers --ai --only-new --system-prompt "$(cat reply_system_prompt.txt)" 2>&1 | grep -vE "^time=|Container")
buttons=$($HH answer-buttons --send 2>&1 | grep -vE "^time=|Container")
echo "$replies"; echo "$buttons"
$HH watch-chats --commit >/dev/null 2>&1

DIGEST_ENV=${DIGEST_ENV:-digest.env}
[ -f "$DIGEST_ENV" ] || exit 0
set -a; . "$DIGEST_ENV"; set +a
[ -n "${TELEGRAM_NOTIFY_BOT_TOKEN:-}" ] || exit 0
# Несколько резюме на аккаунте = по чату на резюме, и одно событие вакансии приходит N раз.
# chat_notify.py склеивает строки по вакансии и не повторяет то, о чём уже писал за сутки.
printf "%s\n%s\n" "$replies" "$buttons" | python3 chat_notify.py --label "${ACCOUNT_LABEL:-}" \
  --state "${CHAT_NOTIFY_STATE:-.chat_notify_seen${HH_PROFILE:+_$HH_PROFILE}.json}"
