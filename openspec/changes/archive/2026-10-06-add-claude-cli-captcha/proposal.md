## Why

Распознавание каптчи сейчас требует ключ OpenRouter с балансом. У учеников ключа часто нет, а бесплатные модели для каптчи не годятся. При этом агент ученика — Claude Code с подпиской: `claude -p --model opus` распознал 4/4 образца каптчи hh (~10 с), без денег на OpenRouter.

## What Changes

- Второй способ распознавания: `openai_captcha.provider = claude-cli` — картинка во временный файл, вызов `claude -p --model <model>` (по умолчанию `opus`), ответ нормализуется.
- Защита от отказа/мусора: ответ принимается, только если это два слова кириллицей; иначе попытка считается неудачной, а при настроенном OpenRouter (`base_url` + `api_key`) берётся его ответ.
- `check-captcha` понимает оба способа; если секции нет, первым предлагает `claude-cli`, когда `claude` есть в PATH.
- Нормализация снимает markdown-звёздочки (`**слово слово**`).
- Скилл `hh-clicker` и `GUIDE.md`: алгоритм «есть Claude Code → подписка, нет → OpenRouter».

## Capabilities

### Modified Capabilities
- `captcha-handling`: распознавание через подписку Claude, выбор провайдера, проверка формы ответа.

## Impact

`captcha.py`, `operations/apply_vacancies.py`, `operations/check_captcha.py`; тесты с настоящим подпроцессом (fake-бинарь `claude`). Сервер не меняется (остаётся OpenRouter до установки CLI в образ).
