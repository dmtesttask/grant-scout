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
You are an assistant for analyzing scientific grants and conferences. Analyze the text below and return a JSON object.

IMPORTANT LANGUAGE RULE: The "summary_uk" field MUST be written in Ukrainian (uk-UA). All other fields follow their specified formats.

Text to analyze:
---
Title: {title}
URL: {url}
Snippet: {snippet}
---

Return ONLY valid JSON with no explanations or extra text:
{{
  "type": "<one of: Грант, Конференція, Стипендія, Програма обміну, Невизначено>",
  "topics": ["<topics from: Освіта, Мистецтво, Музика, EdTech, Наука, Інше>"],
  "deadline": "<deadline in YYYY-MM-DD format, or null if not found>",
  "funding": "<funding amount/conditions, or null>",
  "summary_uk": "<short description IN UKRAINIAN, 2-3 sentences>",
  "relevance": <integer 0-100, where 100 = maximally relevant for Ukrainian researchers>,
  "is_ukraine_relevant": <true if related to Ukraine or open to Ukrainians, false otherwise>
}}"""


def _extract_json(text: str) -> str | None:
    """Витягнути JSON-рядок з відповіді LLM.
    Моделі без response_format часто загортають JSON у ```json ... ```
    або додають пояснення до/після — знаходимо перший валідний {...}.
    """
    if not text:
        return None

    # Спробувати 1: зняти markdown-огортку ```json ... ``` або ``` ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)

    # Спробувати 2: знайти перший { і відповідний } (жадібний пошук)
    start = text.find("{")
    if start == -1:
        return None
    # Йдемо по тексту, рахуємо дужки
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


def analyze_item(item: dict, config: dict) -> dict:
    """
    Аналізує один елемент через LLM.
    Повертає збагачений словник з полями аналізу.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY не встановлено — пропускаємо LLM аналіз")
        return _fallback_analysis(item)

    llm_cfg = config.get("llm", {})
    preset = llm_cfg.get("preset", "").strip()
    if preset:
        model = f"@preset/{preset}"
        logger.info(f"Використовуємо OpenRouter пресет: {model}")
    else:
        model = llm_cfg.get("model")
        if not model:
            logger.warning("llm.model не задано в config.yaml — використовуємо fallback аналіз")
            return _fallback_analysis(item)
    max_tokens = llm_cfg.get("max_tokens", 500)
    temperature = llm_cfg.get("temperature", 0.1)

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
        # response_format НЕ використовуємо — більшість free-tier моделей
        # не підтримують json_object і повертають content=None
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

            # Модель може загорнути JSON у ```json ... ``` або додати текст перед/після
            json_str = _extract_json(content)
            if not json_str:
                logger.warning(f"LLM спроба {attempt+1}/3: JSON не знайдено у відповіді")
                time.sleep(RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 4)
                continue

            analysis = json.loads(json_str)
            if not isinstance(analysis, dict):
                logger.warning(f"LLM спроба {attempt+1}/3: відповідь не є словником")
                continue
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
    """Нормалізувати та валідувати відповідь LLM.
    LLM може повернути будь-який тип у будь-якому полі —
    кожне поле явно приводимо до очікуваного типу.
    """
    valid_types = {"Грант", "Конференція", "Стипендія", "Програма обміну", "Невизначено"}

    # type: очікуємо рядок; якщо список — беремо перший елемент
    raw_type = analysis.get("type", "Невизначено")
    if isinstance(raw_type, list):
        raw_type = raw_type[0] if raw_type else "Невизначено"
    item_type = str(raw_type) if raw_type else "Невизначено"
    if item_type not in valid_types:
        item_type = "Невизначено"

    # deadline: очікуємо рядок YYYY-MM-DD або null
    deadline = analysis.get("deadline")
    if isinstance(deadline, list):
        deadline = deadline[0] if deadline else None
    if deadline:
        try:
            datetime.strptime(str(deadline), "%Y-%m-%d")
            deadline = str(deadline)
        except (ValueError, TypeError):
            deadline = None

    # relevance: очікуємо число 0–100
    relevance = analysis.get("relevance", 50)
    if isinstance(relevance, list):
        relevance = relevance[0] if relevance else 50
    try:
        relevance = max(0, min(100, int(relevance)))
    except (ValueError, TypeError):
        relevance = 50

    # topics: очікуємо список рядків
    raw_topics = analysis.get("topics", [])
    if isinstance(raw_topics, str):
        raw_topics = [raw_topics]
    elif not isinstance(raw_topics, list):
        raw_topics = []
    # Якщо елементи є списками — вирівнюємо
    topics = []
    for t in raw_topics:
        if isinstance(t, list):
            topics.extend(str(x) for x in t if x)
        elif t:
            topics.append(str(t))

    # funding: очікуємо рядок або null
    funding = analysis.get("funding")
    if isinstance(funding, list):
        funding = ", ".join(str(x) for x in funding if x) or None
    elif funding is not None:
        funding = str(funding) or None

    # summary_uk: очікуємо рядок
    summary = analysis.get("summary_uk", "")
    if isinstance(summary, list):
        summary = " ".join(str(x) for x in summary if x)
    summary = str(summary) if summary else ""

    return {
        "type": item_type,
        "topics_detected": topics,
        "deadline": deadline,
        "funding": funding,
        "summary_uk": summary,
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
