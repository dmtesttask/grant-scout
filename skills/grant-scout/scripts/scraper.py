"""
scraper.py — Веб-скрапінг українських сайтів

Підтримує:
- CSS-селектори з конфігу
- Fallback: витягує всі посилання з сторінки
- Retry з exponential backoff
- UTF-8 кирилиця
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Ключові слова для фільтрації релевантних сторінок
GRANT_KEYWORDS = [
    "грант", "конкурс", "стипендія", "програма", "фінансування",
    "проєкт", "конференція", "симпозіум", "захід", "форум",
    "grant", "scholarship", "funding", "conference", "fellowship",
    "program", "competition", "call", "opportunity",
]


def _fetch(url: str, retries: int = 3, timeout: int = 15) -> str | None:
    """HTTP GET з retry та exponential backoff."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in (403, 404):
                logger.warning(f"[{url}] HTTP {e.response.status_code} — пропускаємо")
                return None
            logger.warning(f"[{url}] Спроба {attempt+1}/{retries}: {e}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"[{url}] Спроба {attempt+1}/{retries}: {e}")

        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s, 4s

    logger.error(f"[{url}] Не вдалося отримати сторінку після {retries} спроб")
    return None


def _is_relevant(text: str) -> bool:
    """Перевірити чи містить текст ключові слова грантів/конференцій."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in GRANT_KEYWORDS)


def _extract_items_css(soup: BeautifulSoup, base_url: str, selectors: dict) -> list[dict]:
    """Витягнути елементи за допомогою CSS-селекторів."""
    items = []
    containers = soup.select(selectors.get("container", "article"))

    for container in containers[:30]:
        title_el = container.select_one(selectors.get("title", "h2, h3"))
        link_el = container.select_one(selectors.get("link", "a"))
        date_el = container.select_one(selectors.get("date", "time, .date"))

        title = title_el.get_text(strip=True) if title_el else ""
        href = link_el.get("href", "") if link_el else ""
        date_text = date_el.get_text(strip=True) if date_el else ""

        if not title or not href:
            continue
        if not _is_relevant(title):
            continue

        full_url = urljoin(base_url, href)
        items.append({
            "title": title,
            "url": full_url,
            "url_hash": hashlib.md5(full_url.encode()).hexdigest(),
            "date_text": date_text,
            "source_id": urlparse(base_url).netloc,
        })

    return items


def _extract_items_fallback(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Fallback: витягує всі посилання зі сторінки та фільтрує за ключовими словами.
    Використовується якщо CSS-селектори не дали результатів.
    """
    items = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True)
        href = a_tag["href"]

        if len(text) < 20 or len(text) > 300:
            continue
        if not _is_relevant(text):
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        if not full_url.startswith("http"):
            continue

        seen_urls.add(full_url)
        items.append({
            "title": text,
            "url": full_url,
            "url_hash": hashlib.md5(full_url.encode()).hexdigest(),
            "date_text": "",
            "source_id": urlparse(base_url).netloc,
        })

    return items[:20]


def scrape_site(site_config: dict) -> list[dict]:
    """
    Скрапінг одного сайту.
    Спочатку намагається CSS-селектори, потім fallback.
    """
    url = site_config["url"]
    name = site_config.get("name", url)
    selectors = site_config.get("selectors", {})

    logger.info(f"Скрапінг: {name} ({url})")
    html = _fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")

    # Спочатку CSS-селектори
    items = _extract_items_css(soup, url, selectors)

    # Fallback якщо CSS не дав результатів
    if not items:
        logger.info(f"[{name}] CSS-селектори не спрацювали, використовуємо fallback")
        items = _extract_items_fallback(soup, url)

    # Додати метадані сайту
    for item in items:
        item["source_name"] = name
        item["scraped_at"] = datetime.utcnow().isoformat()

    logger.info(f"[{name}] Знайдено {len(items)} релевантних посилань")
    return items


def scrape_all_sites(config: dict) -> list[dict]:
    """Скрапінг всіх сайтів з конфігу."""
    all_items = []
    websites = config.get("sources", {}).get("websites", [])

    for site in websites:
        if not site.get("enabled", True):
            continue
        try:
            items = scrape_site(site)
            all_items.extend(items)
        except Exception as e:
            logger.error(f"Помилка при скрапінгу {site.get('name', site.get('url'))}: {e}")

        # Пауза між сайтами щоб не перевантажувати
        time.sleep(1)

    logger.info(f"Загалом зібрано {len(all_items)} позицій з усіх сайтів")
    return all_items


if __name__ == "__main__":
    import json
    import sys
    from config_manager import load_config

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = load_config()
    results = scrape_all_sites(config)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nЗнайдено: {len(results)} позицій")
