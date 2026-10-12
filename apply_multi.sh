#!/bin/bash
# Эксперимент «все резюме на каждую вакансию» (12.10.2026, крон хоста вместо apply3.sh).
#
# 1. Основное резюме (HH_RESUME_ID) откликается на свежие вакансии — как apply3.sh.
# 2. Остальные резюме (HH_EXTRA_RESUME_IDS) переоткликаются на вакансии, где аккаунт откликнулся
#    за последние сутки и где этим резюме отклика ещё нет. hh показывает работодателю отклики разных
#    резюме отдельными карточками — так сравниваем варианты резюме на одной и той же вакансии.
# Порядок дополнительных резюме сдвигается каждый день, чтобы ни одно не было всегда первым.
# На «Лимит откликов hh.ru исчерпан» цепочка останавливается.
set -uo pipefail
cd /opt/hh-applicant-tool
set -a; . ./apply.env; set +a

run() {
  local out
  out=$(bash apply3.sh "$@" 2>&1); echo "$out"
  grep -q "Лимит откликов hh.ru исчерпан" <<<"$out" && { echo "⛔ лимит — цепочка остановлена"; exit 0; }
  return 0
}

echo "=== $(date '+%F %T') основное резюме"
run

read -ra IDS <<<"${HH_EXTRA_RESUME_IDS:-}"
n=${#IDS[@]}
[ "$n" -eq 0 ] && exit 0
shift_by=$(( 10#$(date +%j) % n ))
for ((i = 0; i < n; i++)); do
  rid=${IDS[$(( (i + shift_by) % n ))]}
  echo "=== $(date '+%F %T') переотклик резюме ${rid:0:8}"
  RESUME_ID="$rid" run --reapply-rejected --reapply-states response discard invitation --reapply-since-days 1
done
