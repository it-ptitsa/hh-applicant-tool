# scheduled-applying Specification

## Purpose
Рассылать отклики по расписанию хоста одним предсказуемым сценарием.

## Requirements

### Requirement: Единая точка рассылки

Отклики по расписанию SHALL отправляться только через `apply3.sh` из крона хоста (будни 06:00 UTC). `startup.sh` контейнера SHALL NOT запускать `apply-vacancies`. Параметры поиска и пауз SHALL задаваться в `apply3.sh`, id резюме — в `apply.env`.

#### Scenario: Перезапуск контейнера

- **WHEN** контейнер стартует через startup.sh
- **THEN** выполняются только refresh-token и update-resumes, отклики не отправляются

#### Scenario: Нет apply.env

- **WHEN** apply3.sh запущен без apply.env или без HH_RESUME_ID
- **THEN** скрипт завершается с ошибкой до обращения к hh

### Requirement: Дайджест чатов

`chat_digest.sh` SHALL присылать в Telegram сводку по чатам с работодателями дважды в день и SHALL предупреждать, если веб-сессия протухла. Токен и chat id SHALL браться из `digest.env`.

#### Scenario: Протухшая сессия

- **WHEN** web_session_alive() возвращает false
- **THEN** в сообщении есть явное предупреждение о протухшей веб-сессии
