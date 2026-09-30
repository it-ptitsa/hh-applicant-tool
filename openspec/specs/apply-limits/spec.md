# apply-limits Specification

## Purpose
TBD - created by archiving change sync-upstream-1-9. Update Purpose after archive.

## Requirements

### Requirement: Лимит откликов за запуск

При заданном `--max-responses N` цикл откликов SHALL остановиться, как только отправлено N откликов, и SHALL NOT делать запросы отклика сверх N. Проверяется `tests/test_apply_limit_shutdown.py` (тест оригинала).

#### Scenario: Лимит 5 при 20 подходящих вакансиях

- **WHEN** запуск с `--max-responses 5` и 20 подходящими вакансиями
- **THEN** отправлено ровно 5 откликов и в логе «Достигнут лимит откликов --max-responses»

#### Scenario: Лимит не задан

- **WHEN** `--max-responses` не передан
- **THEN** отклики идут по всем подходящим вакансиям

### Requirement: Пауза между откликами

При заданных `--apply-delay-min/--apply-delay-max` между откликами SHALL выдерживаться случайная пауза в этом диапазоне (границы в любом порядке). Без флагов паузы SHALL NOT быть, в том числе у Operation, собранного без парсера. Проверяется `tests/test_human_delay.py`.

#### Scenario: Диапазон 30–80

- **WHEN** запуск с `--apply-delay-min 30 --apply-delay-max 80`
- **THEN** каждая пауза в пределах 30–80 секунд

#### Scenario: Operation без флагов паузы

- **WHEN** Operation создан без парсера и без атрибутов паузы
- **THEN** `_human_delay()` не спит и не падает
