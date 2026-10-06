-- Схема и образец прод-базы market_v2.db (06.10.2026, v3): проверка миграции на v4.
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL, window_start TEXT NOT NULL, found INTEGER,
    front_total INTEGER NOT NULL, js_total INTEGER NOT NULL,
    russia INTEGER, moscow INTEGER, spb INTEGER, other_countries INTEGER, remote INTEGER,
    new_front INTEGER, bumped_front INTEGER, reopened_front INTEGER, closed_front INTEGER, hidden INTEGER
, slice_version INTEGER);
CREATE TABLE snapshot (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    vacancy_id TEXT NOT NULL,
    name TEXT, employer TEXT, category TEXT, area_id TEXT, country_id TEXT, remote INTEGER,
    published_at TEXT, salary_from INTEGER, salary_to INTEGER, currency TEXT, stack TEXT, node INTEGER, ai INTEGER,
    PRIMARY KEY (run_id, vacancy_id)
);
CREATE TABLE vacancy_initial (
    vacancy_id TEXT PRIMARY KEY,
    initial_created_at TEXT NOT NULL
);
CREATE TABLE events (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    vacancy_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('new', 'bumped', 'reopened', 'closed', 'hidden')),
    category TEXT,
    age_days INTEGER
);
INSERT INTO runs (id,run_at,window_start,found,front_total,js_total,russia,moscow,spb,other_countries,remote,new_front,bumped_front,reopened_front,closed_front,hidden,slice_version) VALUES (1,'2026-10-06T08:00:02.040204+03:00','2026-10-05T08:00:02.040204+03:00',883,217,500,168,100,15,49,115,11,5,2,0,0,NULL);
INSERT INTO snapshot (run_id,vacancy_id,name,employer,category,area_id,country_id,remote,published_at,salary_from,salary_to,currency,stack,node,ai) VALUES (1,'127249684','Frontend-разработчик Vue.js','«UZUM TECHNOLOGIES»','front','2759','97',0,'2026-09-07T10:22:07+03:00',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO snapshot (run_id,vacancy_id,name,employer,category,area_id,country_id,remote,published_at,salary_from,salary_to,currency,stack,node,ai) VALUES (1,'128839585','Lead/Senior Game Developer (Pixi.JS) / Ведущий разработчик общего цикла','ROCKSTONE','other','2','113',1,'2026-09-13T18:00:40+03:00',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO snapshot (run_id,vacancy_id,name,employer,category,area_id,country_id,remote,published_at,salary_from,salary_to,currency,stack,node,ai) VALUES (1,'128966075','Frontend-разработчик','Аптечная сеть Ваша №1 х Таблетка.ру','front','1','113',0,'2026-09-12T17:17:36+03:00',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO snapshot (run_id,vacancy_id,name,employer,category,area_id,country_id,remote,published_at,salary_from,salary_to,currency,stack,node,ai) VALUES (1,'129947738','QA Fullstack Java специалист','ГК Орбита','qa','1','113',1,'2026-09-14T16:10:29+03:00',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO snapshot (run_id,vacancy_id,name,employer,category,area_id,country_id,remote,published_at,salary_from,salary_to,currency,stack,node,ai) VALUES (1,'130639502','Frontend-разработчик (React, Офис)','Диджитал Сектор Поддержка','front','53','113',0,'2026-09-13T11:30:43+03:00',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO vacancy_initial (vacancy_id,initial_created_at) VALUES ('138125101','2026-10-05T13:18:35+03:00');
INSERT INTO vacancy_initial (vacancy_id,initial_created_at) VALUES ('138039374','2026-10-02T08:59:33+03:00');
INSERT INTO vacancy_initial (vacancy_id,initial_created_at) VALUES ('137067528','2026-09-07T15:49:52+03:00');
