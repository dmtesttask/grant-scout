"""
analyzer.py — LLM-аналіз знахідок через OpenRouter

- Класифікація типу (грант / конференція / стипендія / програма)
- Витягування дедлайну, суми фінансування
- Генерація опису українською
- Оцінка релевантності (0–100)
"""

import json
import logging
import os
import re
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SITE_URL = "https://github.com/grant-scout"  # для OpenRouter HTTP-Referer
# OPENROUTER_API_KEY читаємо динамічно всередині функцій (не на рівні модуля)


ANALYSIS_PROMPT = """\
Ти — асистент для аналізу наукових грантів та конференцій. Проаналізуй наступний текст та поверни JSON.

Текст:
---
Назва: {title}
URL: {url}
Фрагмент: {snippet}
---

Поверни ТІЛЬКИ валідний JSON без пояснень:
{{
  "type": "<одне з: Грант, Конференція, Стипендія, Програма обміну, Невизначено>",
  "topics": ["<теми з переліку: Освіта, Мистецтво, Музика, EdTech, Наука, Інше>"],
  "deadline": "<дедлайн у форматі YYYY-MM-DD або null якщо не знайдено>",
  "funding": "<сума/умови фінансування або null>",
  "summary_uk": "<короткий опис українською 2-3 речення>",
  "relevance": <число від 0 до 100, де 100 = максимально релевантно для українських науковців>,
  "is_ukraine_relevant": <true якщо стосується України або відкрито для українців, false якщо ні>
}}"""


def analyze_item(item: dict, config: dict) -> dict:
    """
    Аналізує один елемент через LLM.
    Повертає збагачений словник з полями аналізу.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY не встановлено — пропускаємо LLM аналіз")
        return _fallback_analysis(item)

    model = config.get("llm", {}).get("model", "google/gemma-3-27b-it:free")
    max_tokens = config.get("llm", {}).get("max_tokens", 500)
    temperature = config.get("llm", {}).get("temperature", 0.1)

    prompt = ANALYSIS_PROMPT.format(
        title=item.get("title", ""),
        url=item.get("url", ""),
        snippet=item.get("snippet", item.get("title", ""))[:500],
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": SITE_URL,
        "Content-Type": "application/json",
    }

    # Затримки для retry: звичайні помилки 1с/2с, 429 Rate Limit — 15с/30с
    RETRY_DELAYS = [1, 2]
    RATE_LIMIT_DELAYS = [15, 30]

    for attempt in range(3):
        try:
            resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)

            # Окремо обробляємо 429 — рат ліміт потребує довшу паузу
            if resp.status_code == 429:
                wait = RATE_LIMIT_DELAYS[attempt] if attempt < len(RATE_LIMIT_DELAYS) else 60
                logger.warning(f"LLM спроба {attempt+1}/3: 429 Rate Limit — очікуємо {wait}с...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content")

            if not content:
                logger.warning(f"LLM спроба {attempt+1}/3: порожня відповідь (content=None)")
                time.sleep(RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 4)
                continue

            analysis = json.loads(content)
            item.update(_normalize_analysis(analysis))
            return item

        except (requests.exceptions.RequestException, json.JSONDecodeError,
                KeyError, TypeError, ValueError) as e:
            wait = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 4
            logger.warning(f"LLM спроба {attempt+1}/3: {e}")
            if attempt < 2:
                time.sleep(wait)

    # Якщо LLM не відповів — використати fallback
    logger.info("Використовуємо fallback аналіз (без LLM)")
    return _fallback_analysis(item)


def _normalize_analysis(analysis: dict) -> dict:
    """Нормалізувати та валідувати відповідь LLM."""
    valid_types = {"Грант", "Конференція", "Стипендія", "Програма обміну", "Невизначено"}
    item_type = analysis.get("type", "Невизначено")
    if item_type not in valid_types:
        item_type = "Невизначено"

    # Дедлайн — перевірити формат
    deadline = analysis.get("deadline")
    if deadline:
        try:
            datetime.strptime(deadline, "%Y-%m-%d")
        except (ValueError, TypeError):
            deadline = None

    relevance = analysis.get("relevance", 50)
    if not isinstance(relevance, (int, float)):
        relevance = 50
    relevance = max(0, min(100, int(relevance)))

    return {
        "type": item_type,
        "topics_detected": analysis.get("topics", []),
        "deadline": deadline,
        "funding": analysis.get("funding"),
        "summary_uk": analysis.get("summary_uk", ""),
        "relevance": relevance,
        "is_ukraine_relevant": bool(analysis.get("is_ukraine_relevant", True)),
    }


def _fallback_analysis(item: dict) -> dict:
    """Базовий аналіз без LLM — на основі ключових слів."""
    title_lower = item.get("title", "").lower()

    if any(w in title_lower for w in ["конференція", "conference", "симпозіум", "форум"]):
        item_type = "Конференція"
    elif any(w in title_lower for w in ["стипендія", "scholarship", "fellowship"]):
        item_type = "Стипендія"
    elif any(w in title_lower for w in ["обмін", "exchange", "erasmus", "msca"]):
        item_type = "Програма обміну"
    else:
        item_type = "Грант"

    item.update({
        "type": item_type,
        "topics_detected": [item.get("topic_hint", "Інше")],
        "deadline": None,
        "funding": None,
        "summary_uk": item.get("title", ""),
        "relevance": 50,
        "is_ukraine_relevant": True,
    })
    return item


def analyze_batch(items: list[dict], config: dict) -> list[dict]:
    """
    Аналізує список елементів.
    Фільтрує за мінімальною релевантністю після аналізу.
    """
    min_relevance = config.get("telegram", {}).get("notifications", {}).get("min_relevance_score", 60)
    analyzed = []

    for i, item in enumerate(items):
        logger.info(f"Аналіз {i+1}/{len(items)}: {item.get('title', '')[:60]}…")
        result = analyze_item(item, config)
        if result.get("relevance", 0) >= min_relevance and result.get("is_ukraine_relevant", True):
            analyzed.append(result)
        # Пауза між запитами до API (важливо для free-tier моделей з жорстким rate limit)
        if i < len(items) - 1:
            delay = config.get("llm", {}).get("request_delay_sec", 3.0)
            time.sleep(delay)

    logger.info(f"Після аналізу: {len(analyzed)}/{len(items)} релевантних позицій")
    return analyzed


if __name__ == "__main__":
    import sys
    from config_manager import load_config

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = load_config()

    # Тест з одним прикладом
    test_item = {
        "title": "Конкурс наукових грантів НФДУ 2025 — освіта та мистецтво",
        "url": "https://nfdu.gov.ua/news/konkurs-2025",
        "snippet": "НФДУ оголошує конкурс грантів для підтримки наукових досліджень...",
        "source_name": "НФДУ",
    }
    result = analyze_item(test_item, config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
