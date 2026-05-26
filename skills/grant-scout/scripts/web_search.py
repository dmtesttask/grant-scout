"""
web_search.py — Пошук через пошукові системи (Serper.dev або DuckDuckGo)

- serper: Google Search API через Serper.dev (потрібен SERPER_API_KEY в .env)
- duckduckgo: Безкоштовний пошук через DuckDuckGo (без ключів)
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
import requests

logger = logging.getLogger(__name__)

SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "duckduckgo").lower()
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

STATE_DIR = Path(os.environ.get("GRANT_SCOUT_STATE", Path.home() / ".grant-scout"))
STATE_FILE = STATE_DIR / "web_search_state.json"


def _load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "count": 0}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_remaining_quota(daily_limit: int) -> int:
    state = _load_state()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state.get("date") != today:
        _save_state({"date": today, "count": 0})
        return daily_limit
    return max(0, daily_limit - state.get("count", 0))


def _increment_counter(n: int = 1) -> None:
    state = _load_state()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    state["count"] = state.get("count", 0) + n
    _save_state(state)


def _search_serper(query: str, max_results: int = 10) -> list[dict]:
    """Виконати пошук по всьому інтернету через Serper.dev (Google Search API)."""
    if not SERPER_API_KEY:
        logger.warning("Serper API не налаштований (немає SERPER_API_KEY в .env)")
        return []

    url = "https://google.serper.dev/search"
    payload = {
        "q": query,
        "num": min(max_results, 20),
        "gl": "ua",
        "hl": "uk",  # Мова результатів
        "date": "w"  # Останній тиждень
    }
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _increment_counter(1)
    except requests.exceptions.RequestException as e:
        logger.error(f"Serper API помилка: {e}")
        return []

    results = []
    for item in data.get("organic", []):
        link = item.get("link", "")
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        if not link or not title:
            continue
        results.append({
            "title": title,
            "url": link,
            "url_hash": hashlib.md5(link.encode()).hexdigest(),
            "snippet": snippet,
            "source_name": "Google Search (Serper)",
            "source_id": "serper",
            "date_text": "",
            "scraped_at": datetime.utcnow().isoformat(),
        })

    return results


def _search_duckduckgo(query: str, max_results: int = 10) -> list[dict]:
    """Виконати пошук по всьому інтернету через безкоштовний DuckDuckGo."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.error("Бібліотека duckduckgo_search не встановлена. Додайте її до requirements.txt")
        return []

    results = []
    try:
        with DDGS() as ddgs:
            ddg_results = ddgs.text(
                keywords=query,
                region="ua-uk",
                safesearch="moderate",
                timelimit="w",
                max_results=max_results
            )
            for r in ddg_results:
                href = r.get("href", "")
                title = r.get("title", "")
                body = r.get("body", "")
                if not href or not title:
                    continue
                results.append({
                    "title": title,
                    "url": href,
                    "url_hash": hashlib.md5(href.encode()).hexdigest(),
                    "snippet": body,
                    "source_name": "DuckDuckGo Search",
                    "source_id": "duckduckgo",
                    "date_text": "",
                    "scraped_at": datetime.utcnow().isoformat(),
                })
    except Exception as e:
        logger.error(f"DuckDuckGo пошук помилка: {e}")

    return results


def search_google(query: str, max_results: int = 10) -> list[dict]:
    """Виконати один пошуковий запит за допомогою обраного провайдера."""
    provider = SEARCH_PROVIDER
    if provider == "serper":
        if SERPER_API_KEY:
            return _search_serper(query, max_results)
        else:
            logger.warning("SEARCH_PROVIDER встановлено в serper, але SERPER_API_KEY відсутній. Перемикаємось на duckduckgo.")
            return _search_duckduckgo(query, max_results)
    else:
        return _search_duckduckgo(query, max_results)


def search_all_topics(config: dict) -> list[dict]:
    """
    Виконати пошук для всіх активних тем.
    """
    web_config = config.get("sources", {}).get("web_search", {})
    if not web_config.get("enabled", True):
        logger.info("Пошук по вебу вимкнено в конфігу")
        return []

    max_results = web_config.get("max_results_per_query", 10)
    daily_limit = web_config.get("daily_limit", 90)

    provider = SEARCH_PROVIDER
    if provider == "serper" and not SERPER_API_KEY:
        provider = "duckduckgo"

    if provider == "serper":
        remaining = _get_remaining_quota(daily_limit)
        if remaining <= 0:
            logger.warning(f"Вичерпано денний ліміт запитів для {provider}")
            return []
    else:
        remaining = 999999

    all_results = []
    seen_hashes = set()
    queries_used = 0

    for topic in config.get("topics", []):
        if not topic.get("enabled", True):
            continue

        topic_name = topic["name"]
        keywords = topic.get("keywords_uk", []) + topic.get("keywords_en", [])

        for keyword in keywords:
            if queries_used >= remaining:
                logger.warning(f"Досягнуто денний ліміт запитів для {provider}")
                return all_results

            logger.info(f"Пошук ({provider}): «{keyword}» (тема: {topic_name})")
            results = search_google(keyword, max_results)
            queries_used += 1
            
            for r in results:
                h = r["url_hash"]
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_results.append(r)
            
            # Невелика затримка для збереження лімітів
            time.sleep(1.0 if provider == "serper" else 1.5)

    return all_results
