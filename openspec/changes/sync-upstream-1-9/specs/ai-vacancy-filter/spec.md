## ADDED Requirements

### Requirement: Режим custom ИИ-фильтра

`--ai-filter` SHALL принимать режимы `heavy`, `light` и `custom`. Режим `custom` SHALL требовать `--ai-filter-prompt`, использовать его как инструкции, добавлять анализ резюме блоком «Кандидат» и оценивать вакансии как `heavy`. Поведение совпадает с оригиналом; проверяется `tests/test_ai_filter_prompt.py` (тест оригинала).

#### Scenario: custom с промптом

- **WHEN** запуск с `--ai-filter custom --ai-filter-prompt "..."`
- **THEN** системный промпт фильтра начинается с переданного текста и содержит блок «Кандидат»

#### Scenario: custom без промпта

- **WHEN** запуск с `--ai-filter custom` без `--ai-filter-prompt`
- **THEN** ошибка с упоминанием `--ai-filter-prompt` до начала откликов
