## ADDED Requirements

### Requirement: Стек фронт-вакансий

Для каждой вакансии стек (react, vue, angular, svelte) SHALL определяться по названию вместе с
фрагментом требований из выдачи поиска (`snippet.requirement`), без дополнительных запросов.
Сводка SHALL показывать для фронтенда число вакансий с React, Vue, Angular, Svelte, с несколькими
фреймворками и без указанного фреймворка.

#### Scenario: Фреймворк только в требованиях

- **WHEN** вакансия называется «Frontend-разработчик», а во фрагменте требований есть «Vue 3»
- **THEN** её стек — vue

#### Scenario: Фреймворк не указан

- **WHEN** ни в названии, ни во фрагменте требований нет React, Vue, Angular и Svelte
- **THEN** вакансия учитывается как «не указан»

### Requirement: Fullstack с Node.js

Fullstack-вакансия SHALL отмечаться как «с Node», если Node, Nest или Express есть в названии или
фрагменте требований, либо если она найдена отдельным запросом «fullstack в названии И node/nest/
express в тексте». Сводка SHALL показывать fullstack всего и сколько из них с Node.

#### Scenario: Node только в полном тексте вакансии

- **WHEN** во фрагменте требований Node нет, но вакансия найдена запросом fullstack + node
- **THEN** она отмечена как fullstack с Node

### Requirement: AI ближе к фронту

Монитор SHALL отдельным запросом собирать AI-вакансии (AI/ИИ-инженер и разработчик, LLM,
AI-native, AI-first, vibe coding, prompt engineer) по ролям IT-категории hh. AI-вакансия без
фронт- и fullstack-признаков в названии SHALL получать категорию `ai_js`, если рядом JS/TS/Node/
web/fullstack в названии или фрагменте требований, иначе `ai`. Категория `ai_js` SHALL входить
в JS-рынок. Фронт-, fullstack-, бэкенд- и веб-вакансии с AI в названии SHALL сохранять свою
категорию и получать флаг AI. Сводка SHALL показывать блок «AI ближе к фронту»: фронт с AI,
fullstack с AI, AI-инженеры на JS/TS — и общее число AI-вакансий в IT для масштаба.

#### Scenario: AI-инженер на Python

- **WHEN** вакансия «Senior AI developer (Python)» без JS/TS в названии и требованиях
- **THEN** её категория ai, в JS-рынок она не входит

#### Scenario: Fullstack AI Engineer

- **WHEN** вакансия называется «Fullstack AI Engineer»
- **THEN** её категория fullstack с флагом AI, и она учитывается в «AI ближе к фронту»
