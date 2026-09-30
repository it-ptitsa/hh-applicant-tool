## 1. Слияние

- [x] 1.1 Ветка `sync/upstream-1.9`, `git merge upstream/main` (6c51d79)
- [x] 1.2 Конфликт `apply_vacancies.py` (импорты, письма) — сторона оригинала
- [x] 1.3 Наши правки на месте: `store_browser_cookies`, `find_key(vacancyTests)`

## 2. Восстановление потерь

- [x] 2.1 Запуск тестов оригинала на `upstream/main` (116 passed) — эталон
- [x] 2.2 `--ai-filter custom` + `--ai-filter-prompt` (блок идентичен оригиналу)
- [x] 2.3 Проверка `--max-responses` в цикле откликов
- [x] 2.4 Значения по умолчанию паузы + `tests/test_human_delay.py`
- [x] 2.5 Объединение `authorize.py` с `48ea34b` — вынесено в отдельное изменение, записано в fork-maintenance (нужен живой вход)

## 3. Проверка и выкладка

- [x] 3.1 `pytest -q tests` — 129 passed
- [x] 3.2 Пуш в `it-ptitsa`, `git pull` на сервере, импорт в контейнере
