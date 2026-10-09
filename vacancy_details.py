#!/usr/bin/env python3
"""Полные карточки вакансий (описание, key_skills) для подбора групп резюме.

Пилот 09.10.2026 (идея Александра: 5 резюме под группы вакансий): фронтенд с основным стеком
React или Vue из последнего снимка монитора. Почему так:
- сайт hh без входа вакансии не показывает («Вам недоступна эта вакансия») — только API с токеном;
- карточки API ловят капчу на пару «аккаунт + IP»: 09.10 решённая с сервера капча с мака не
  действовала. Поэтому сбор живёт на сервере, капчу решаем тем же путём, что кликер (CaptchaFlow +
  голосование модели), а без решения — останавливаемся, не помечая вакансии ушедшими;
- паузы 4–9 с: 05–06.10 капчу нагоняли сотни карточек подряд.

Запуск: market_monitor.sh --fetch-details N (через market_monitor.py) или напрямую.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import market_monitor as mm

PAUSE = (4.0, 9.0)
PILOT_STACKS = ("react", "vue")
CAPTCHA_ATTEMPTS = 5


class CaptchaUnsolved(RuntimeError):
    """Капча не решена — дальше карточки не отдадут, прогон остановлен."""


_BLOCK = re.compile(r"</?(p|div|li|ul|ol|br|h\d|tr)\b[^>]*>", re.I)


def html_to_text(s: str) -> str:
    s = _BLOCK.sub("\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines())
    return "\n".join(line for line in lines if line)


def targets(store: mm.Store, stacks=PILOT_STACKS) -> list[str]:
    last = store.last_run()
    if last is None:
        return []
    done = {r[0] for r in store.db.execute("SELECT vacancy_id FROM vacancy_detail")}
    out = []
    for row in store.snapshot_rows(last.id):
        cat = mm.recategorize(row["name"], row["category"], row["it_role"])
        primary = (row["stack"] or "").split(",")[0]
        if cat == "front" and primary in stacks and row["vacancy_id"] not in done:
            out.append(row["vacancy_id"])
    return sorted(out, key=int)


def _captcha_url(status: int, body: dict) -> str | None:
    if status != 403:
        return None
    for e in (body or {}).get("errors") or []:
        if e.get("value") == "captcha_required":
            return e.get("captcha_url")
    return None


def collect(store: mm.Store, get: Callable[[str], tuple[int, dict]], solve: Callable[[str], bool],
            sleep=time.sleep, now: datetime | None = None, limit: int = 60, log=print) -> dict:
    stats = {"ok": 0, "gone": 0, "captcha_solved": 0}
    for vid in targets(store)[:limit]:
        status, body = get(vid)
        url = _captcha_url(status, body)
        if url:
            if not solve(url):
                raise CaptchaUnsolved(f"капча не решена на вакансии {vid}; собрано {stats['ok']}")
            stats["captcha_solved"] += 1
            status, body = get(vid)
            if _captcha_url(status, body):
                raise CaptchaUnsolved(f"после решения капча снова на {vid}")
        t = now or datetime.now(mm.MSK)
        if status == 200:
            store.save_detail(vid, 200, body, t)
            stats["ok"] += 1
        elif status in (403, 404):
            store.save_detail(vid, status, None, t)
            stats["gone"] += 1
        else:
            raise RuntimeError(f"HTTP {status} на вакансии {vid}: {str(body)[:200]}")
        sleep(random.uniform(*PAUSE))
    log(f"карточки: {stats}")
    return stats


# ── боевые get/solve: токен и куки аккаунта из CONFIG_DIR ────────────────


def live_get_and_solve():
    import requests
    from hh_applicant_tool.api.captcha import CaptchaFlow
    from hh_applicant_tool.main import HHApplicantTool

    tool = HHApplicantTool()
    tool.config_dir = tool.profile_id = tool.proxy_url = tool.openai_proxy_url = None
    token = tool.config["token"]["access_token"]
    headers = {"Authorization": f"Bearer {token}", "HH-User-Agent": "hh-market/1.0 (sashapticin@gmail.com)"}

    def get(vid: str) -> tuple[int, dict]:
        r = requests.get(f"https://api.hh.ru/vacancies/{vid}", headers=headers, timeout=25)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def solve(url: str) -> bool:
        ai = tool.get_captcha_ai()
        flow = CaptchaFlow(tool.session, url, language="EN")
        flow.prime()
        for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
            image = flow.fetch()
            text = ai.solve_captcha_consensus(image.image, language="EN")
            if not text:
                continue
            if flow.submit(text, image.key).accepted:
                tool.save_cookies()
                print(f"капча решена с попытки {attempt}", flush=True)
                return True
            time.sleep(5)
        return False

    return get, solve


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Сбор полных карточек вакансий пилота (React/Vue фронт)")
    ap.add_argument("--db", type=Path, default=mm.CONFIG_DIR / "market_v2.db")
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args(argv)
    get, solve = live_get_and_solve()
    try:
        collect(mm.Store(args.db), get, solve, limit=args.limit, log=lambda m: print(m, flush=True))
    except CaptchaUnsolved as ex:
        mm.send(f"⚠️ <b>Сбор карточек остановлен</b>: {ex}. Собранное сохранено.")
        raise


if __name__ == "__main__":
    main()
