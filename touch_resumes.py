#!/usr/bin/env python3
"""Ежедневная мелкая правка каждого опубликованного резюме — правило активности аккаунта.

Правило Александра (12.10.2026): каждый день в каждом резюме должно быть исправление. Ставим или
убираем точку в конце «О себе» (поле skills): текст не разрастается, смысл не меняется. Проверено на
живом резюме: PUT 204, статус остаётся published, модерация не срабатывает.
Запуск в контейнере кликера: python /app/touch_resumes.py (аккаунт — CONFIG_DIR, по умолчанию кликер).
"""

from __future__ import annotations

import json
import os
import sys


def toggle_dot(text: str) -> str:
    return text[:-1] if text.endswith(".") else text + "."


def touch_all(api) -> list[str]:
    done = []
    for item in api.get("/resumes/mine")["items"]:
        if (item.get("status") or {}).get("id") != "published":
            continue
        resume = api.get(f"/resumes/{item['id']}")
        api.put(f"/resumes/{item['id']}", {"skills": toggle_dot(resume.get("skills") or "")})
        done.append(item["id"])
    return done


class Api:
    def __init__(self, token: str):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "HH-User-Agent": "hh-applicant-tool/touch (sashapticin@gmail.com)"})

    def get(self, path):
        r = self.s.get("https://api.hh.ru" + path, timeout=30)
        r.raise_for_status()
        return r.json()

    def put(self, path, body):
        r = self.s.put("https://api.hh.ru" + path, json=body, timeout=30)
        if r.status_code != 204:
            raise RuntimeError(f"PUT {path}: {r.status_code} {r.text[:200]}")


def main() -> None:
    cfg = os.path.join(os.getenv("CONFIG_DIR", "/app/config"), "config.json")
    token = json.load(open(cfg))["token"]["access_token"]
    done = touch_all(Api(token))
    print(f"✏️ Правка резюме: {len(done)} шт. — " + ", ".join(i[:8] for i in done), flush=True)


if __name__ == "__main__":
    sys.exit(main())
