#!/bin/bash
# Утренняя рассылка откликов (крон хоста, будни 06:00 UTC).
# Данные аккаунта (id резюме) — в apply.env рядом со скриптом, вне git; образец — apply.env.example.
set -euo pipefail
cd /opt/hh-applicant-tool
set -a; . ./apply.env; set +a
docker compose run --rm -T -u docker hh_applicant_tool python -m hh_applicant_tool -v apply-vacancies \
  --resume-id "$HH_RESUME_ID" \
  --ai -f -L /app/letter.txt \
  --system-prompt "$(cat /opt/hh-applicant-tool/cover_letter_prompt.txt)" \
  --message-prompt "Адаптируй базовое письмо ниже под эту вакансию по правилам из системного промпта." \
  --search '(NAME:(frontend OR "frontend developer" OR фронтенд OR react OR typescript OR "next.js")) AND NOT NAME:("full stack" OR fullstack OR backend OR python OR golang OR QA OR "Team Lead" OR vue OR angular)' \
  --per-page 100 --total-pages 5 \
  --order-by publication_time --period 2 \
  --apply-delay-min 30 --apply-delay-max 80
