"""
notion_db.py — Клієнт для роботи з Notion API

- Автоматичне створення бази даних при першому запуску
- Дедуплікація за MD5-хешем URL
- Запис нових знахідок
- Запит записів з близьким дедлайном
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from notion_client import Client
from notion_client.errors import APIResponseError

logger = logging.getLogger(__name__)

# Читаємо динамічно всередині функцій — не на рівні модуля,
# щоб .env, завантажений runner.py, вже був доступний.

# Кеш хешів для поточного сеансу (щоб не запитувати Notion щоразу)
_url_hash_cache: set[str] = set()
_cache_loaded = False

STATE_DIR = Path(os.environ.get("GRANT_SCOUT_STATE", Path.home() / ".grant-scout"))
DB_ID_FILE = STATE_DIR / "notion_db_id.txt"


def _get_client() -> Client:
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        raise RuntimeError("NOTION_API_KEY не встановлено")
    # Явно фіксуємо версію API Notion для стабільної роботи та сумісності з ендпоінтами
    return Client(auth=api_key, notion_version="2022-06-28")


def _query_database(client: Client, database_id: str, **kwargs) -> dict:
    """
    Універсальний метод запиту до бази даних Notion.
    Забезпечує сумісність між різними версіями notion-client (v2.x та v3.x).
    """
    try:
        # Спробувати стандартний метод (v2.x та старіші версії)
        return client.databases.query(database_id=database_id, **kwargs)
    except AttributeError:
        # Якщо методу query немає (версії v3.x), виконуємо прямий HTTP-запит до API Notion.
        # Це працює на всіх версіях клієнта, оскільки сам endpoint на сервері Notion незмінний.
        try:
            return client.request(path=f"databases/{database_id}/query", method="POST", body=kwargs)
        except TypeError:
            # На випадок зміни сигнатури методу request у майбутніх версіях SDK (наприклад, positional arguments)
            return client.request(method="POST", path=f"databases/{database_id}/query", json=kwargs)


def _get_or_create_database(client: Client, config: dict) -> str:
    """
    Отримати ID бази даних Notion або створити нову.
    ID кешується у файлі notion_db_id.txt.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Перевірити кеш
    if DB_ID_FILE.exists():
        db_id = DB_ID_FILE.read_text().strip()
        if db_id:
            logger.debug(f"Використовуємо існуючу Notion БД: {db_id}")
            return db_id

    logger.info("Створення нової Notion бази даних…")
    db_name = config.get("notion", {}).get("database_name", "Grant Scout — Знахідки")

    page_id = os.environ.get("NOTION_PAGE_ID", "").replace("-", "")
    if not page_id:
        raise RuntimeError("NOTION_PAGE_ID не встановлено — потрібен ID батьківської сторінки")

    # Очистити ID від дефісів (Notion приймає обидва формати)

    database = client.databases.create(
        parent={"type": "page_id", "page_id": page_id},
        title=[{"type": "text", "text": {"content": db_name}}],
        properties={
            "Назва": {"title": {}},
            "Тип": {
                "select": {
                    "options": [
                        {"name": "Грант", "color": "green"},
                        {"name": "Конференція", "color": "blue"},
                        {"name": "Стипендія", "color": "purple"},
                        {"name": "Програма обміну", "color": "orange"},
                        {"name": "Невизначено", "color": "gray"},
                    ]
                }
            },
            "Тематика": {
                "multi_select": {
                    "options": [
                        {"name": "Освіта", "color": "yellow"},
                        {"name": "Мистецтво", "color": "pink"},
                        {"name": "Музика", "color": "red"},
                        {"name": "EdTech", "color": "blue"},
                        {"name": "Наука", "color": "green"},
                        {"name": "Інше", "color": "gray"},
                    ]
                }
            },
            "Джерело": {
                "select": {
                    "options": [
                        {"name": "НФДУ", "color": "blue"},
                        {"name": "МОН України", "color": "yellow"},
                        {"name": "ЄвроОсвіта", "color": "green"},
                        {"name": "УкрІНТЕІ", "color": "orange"},
                        {"name": "Google Search", "color": "red"},
                    ]
                }
            },
            "Дедлайн": {"date": {}},
            "Посилання": {"url": {}},
            "Опис": {"rich_text": {}},
            "Фінансування": {"rich_text": {}},
            "Дата знахідки": {"date": {}},
            "Статус": {
                "select": {
                    "options": [
                        {"name": "🆕 Нове", "color": "green"},
                        {"name": "👀 Переглянуто", "color": "yellow"},
                        {"name": "📝 Подано", "color": "blue"},
                        {"name": "📦 Архів", "color": "gray"},
                    ]
                }
            },
            "Релевантність": {"number": {"format": "percent"}},
            "URL Hash": {"rich_text": {}},
        },
    )

    db_id = database["id"]
    DB_ID_FILE.write_text(db_id)
    logger.info(f"✅ Notion БД створено: {db_id}")
    return db_id


def _load_hash_cache(client: Client, db_id: str) -> None:
    """Завантажити всі існуючі URL-хеші з Notion у кеш."""
    global _url_hash_cache, _cache_loaded
    if _cache_loaded:
        return

    logger.info("Завантаження кешу хешів з Notion…")
    has_more = True
    start_cursor = None

    while has_more:
        query_args = {"page_size": 100}
        if start_cursor:
            query_args["start_cursor"] = start_cursor

        response = _query_database(
            client,
            db_id,
            **query_args,
            filter={"property": "URL Hash", "rich_text": {"is_not_empty": True}},
        )

        for page in response.get("results", []):
            props = page.get("properties", {})
            hash_prop = props.get("URL Hash", {}).get("rich_text", [])
            if hash_prop:
                _url_hash_cache.add(hash_prop[0]["text"]["content"])

        has_more = response.get("has_more", False)
        start_cursor = response.get("next_cursor")

    _cache_loaded = True
    logger.info(f"Кеш завантажено: {len(_url_hash_cache)} унікальних записів")


def is_duplicate(url_hash: str) -> bool:
    """Перевірити чи запис вже є в базі (за хешем URL)."""
    return url_hash in _url_hash_cache


def save_item(item: dict, config: dict) -> bool:
    """
    Зберегти знахідку в Notion.
    Повертає True якщо запис створено, False якщо дублікат або помилка.
    """
    if not os.environ.get("NOTION_API_KEY", ""):
        logger.warning("NOTION_API_KEY не встановлено — пропускаємо запис")
        return False

    client = _get_client()
    db_id = _get_or_create_database(client, config)
    _load_hash_cache(client, db_id)

    url_hash = item.get("url_hash", "")
    if is_duplicate(url_hash):
        logger.debug(f"Дублікат: {item.get('title', '')[:50]}")
        return False

    # Підготувати властивості
    props = {
        "Назва": {"title": [{"text": {"content": item.get("title", "Без назви")[:2000]}}]},
        "Тип": {"select": {"name": item.get("type", "Невизначено")}},
        "Джерело": {"select": {"name": item.get("source_name", "Інше")[:100]}},
        "Посилання": {"url": item.get("url", "")},
        "Опис": {"rich_text": [{"text": {"content": item.get("summary_uk", "")[:2000]}}]},
        "Статус": {"select": {"name": "🆕 Нове"}},
        "Дата знахідки": {"date": {"start": datetime.utcnow().strftime("%Y-%m-%d")}},
        "URL Hash": {"rich_text": [{"text": {"content": url_hash}}]},
        "Релевантність": {"number": item.get("relevance", 50) / 100},
    }

    # Тематика
    topics = item.get("topics_detected", [])
    if topics:
        props["Тематика"] = {"multi_select": [{"name": t} for t in topics[:5]]}

    # Дедлайн
    deadline = item.get("deadline")
    if deadline:
        props["Дедлайн"] = {"date": {"start": deadline}}

    # Фінансування
    funding = item.get("funding")
    if funding:
        props["Фінансування"] = {"rich_text": [{"text": {"content": str(funding)[:500]}}]}

    try:
        client.pages.create(parent={"database_id": db_id}, properties=props)
        _url_hash_cache.add(url_hash)
        logger.info(f"✅ Збережено: {item.get('title', '')[:60]}")
        return True
    except APIResponseError as e:
        logger.error(f"Notion API помилка при збереженні: {e}")
        return False


def get_upcoming_deadlines(days_ahead: int = 7) -> list[dict]:
    """
    Отримати записи з дедлайном у найближчі N днів.
    Повертає список словників для Telegram-нагадувань.
    """
    if not os.environ.get("NOTION_API_KEY", ""):
        return []

    client = _get_client()
    db_id = _get_or_create_database(client, {})

    today = datetime.utcnow().strftime("%Y-%m-%d")
    future = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    try:
        response = _query_database(
            client,
            db_id,
            filter={
                "and": [
                    {"property": "Дедлайн", "date": {"on_or_after": today}},
                    {"property": "Дедлайн", "date": {"on_or_before": future}},
                    {
                        "property": "Статус",
                        "select": {"does_not_equal": "📦 Архів"},
                    },
                ]
            },
            sorts=[{"property": "Дедлайн", "direction": "ascending"}],
        )
    except APIResponseError as e:
        logger.error(f"Помилка запиту дедлайнів: {e}")
        return []

    results = []
    for page in response.get("results", []):
        props = page.get("properties", {})
        title_prop = props.get("Назва", {}).get("title", [])
        deadline_prop = props.get("Дедлайн", {}).get("date", {})
        url_prop = props.get("Посилання", {}).get("url", "")
        type_prop = props.get("Тип", {}).get("select", {})

        title = title_prop[0]["text"]["content"] if title_prop else "Без назви"
        deadline = deadline_prop.get("start") if deadline_prop else None
        item_type = type_prop.get("name", "") if type_prop else ""

        if deadline:
            results.append({
                "title": title,
                "deadline": deadline,
                "url": url_prop,
                "type": item_type,
                "notion_url": page.get("url", ""),
            })

    return results


def get_weekly_stats() -> dict:
    """Статистика за останній тиждень для дайджесту."""
    if not os.environ.get("NOTION_API_KEY", ""):
        return {}

    client = _get_client()
    try:
        db_id = _get_or_create_database(client, {})
    except Exception:
        return {}

    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        response = _query_database(
            client,
            db_id,
            filter={"property": "Дата знахідки", "date": {"on_or_after": week_ago}},
        )
    except APIResponseError:
        return {}

    stats = {"total": 0, "by_type": {}, "by_topic": {}, "items": []}
    for page in response.get("results", []):
        props = page.get("properties", {})
        stats["total"] += 1

        type_name = (props.get("Тип", {}).get("select") or {}).get("name", "Невизначено")
        stats["by_type"][type_name] = stats["by_type"].get(type_name, 0) + 1

        for t in (props.get("Тематика", {}).get("multi_select") or []):
            tname = t.get("name", "")
            stats["by_topic"][tname] = stats["by_topic"].get(tname, 0) + 1

        title_prop = props.get("Назва", {}).get("title", [])
        deadline_prop = (props.get("Дедлайн", {}).get("date") or {})
        stats["items"].append({
            "title": title_prop[0]["text"]["content"] if title_prop else "",
            "type": type_name,
            "deadline": deadline_prop.get("start"),
            "url": props.get("Посилання", {}).get("url", ""),
        })

    return stats


def test_connection() -> bool:
    """Перевірити підключення до Notion."""
    try:
        client = _get_client()
        client.users.me()
        logger.info("✅ Notion підключення успішне")
        return True
    except Exception as e:
        logger.error(f"❌ Notion підключення помилка: {e}")
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if "--test-connection" in sys.argv:
        ok = test_connection()
        sys.exit(0 if ok else 1)
    else:
        deadlines = get_upcoming_deadlines(7)
        print(f"Дедлайни наступних 7 днів: {len(deadlines)}")
        for d in deadlines:
            print(f"  • {d['deadline']} — {d['title'][:60]}")
