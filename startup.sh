#!/bin/bash
echo "[$(date)] Running startup tasks..."

# echo "Current user: $(whoami)"
# echo "$CONFIG_DIR"

# Выполняем цепочку
/usr/local/bin/python -m hh_applicant_tool refresh-token
/usr/local/bin/python -m hh_applicant_tool update-resumes
# apply-vacancies отключён — рассылка только через apply3.sh (крон 9:00 будни)

echo "[$(date)] Startup tasks finished."
