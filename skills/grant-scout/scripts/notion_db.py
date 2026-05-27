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
import sys
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


def _get_properties_mapping(config: dict) -> dict:
    """Отримати мапінг назв властивостей Notion з конфігу або значень за замовчуванням."""
    default_props = {
        "title": "Назва",
        "summary": "Опис",
        "url": "Посилання",
        "source": "Джерело",
        "type": "Тип",
        "topics": "Тематика",
        "deadline": "Дедлайн",
        "found_date": "Дата знахідки",
        "url_hash": "URL Hash"
    }
    if not config:
        return default_props
    config_props = config.get("notion", {}).get("properties", {})
    props = {}
    for key, val in default_props.items():
        props[key] = config_props.get(key) or val
    return props


def _get_client() -> Client:
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        logger.error("Відсутній NOTION_API_KEY у .env")
        sys.exit(1)
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
        # Для цього ми створюємо сумісний клієнт із зафіксованою версією API "2022-06-28",
        # яка підтримує запити до баз даних, щоб уникнути помилки InvalidRequestURL.
        auth = client.options.auth if hasattr(client, "options") and hasattr(client.options, "auth") else os.environ.get("NOTION_API_KEY", "")
        compat_client = Client(auth=auth, notion_version="2022-06-28")
        try:
            return compat_client.request(path=f"databases/{database_id}/query", method="POST", body=kwargs)
        except TypeError:
            # На випадок зміни сигнатури методу request у майбутніх версіях SDK (наприклад, positional arguments)
            return compat_client.request(method="POST", path=f"databases/{database_id}/query", json=kwargs)


def _get_or_create_database(client: Client, config: dict) -> str:
    """
    Отримати ID бази даних Notion або створити нову.
    ID кешується у файлі notion_db_id.txt.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    props = _get_properties_mapping(config)

    # Перевірити кеш
    if DB_ID_FILE.exists():
        db_id = DB_ID_FILE.read_text().strip()
        if db_id:
            logger.debug(f"Використовуємо існуючу Notion БД: {db_id}")
            try:
                client.databases.update(
                    database_id=db_id,
                    properties={props["url_hash"]: {"rich_text": {}}}
                )
            except Exception as e:
                logger.debug(f"Не вдалося оновити властивості БД (можливо вони вже існують): {e}")
            return db_id

    # Створюємо базу даних
    logger.info("Створення нової Notion бази даних…")
    
    db_name = config.get("notion", {}).get("database_name", "Grant Scout — Знахідки")
    page_id = os.environ.get("NOTION_PAGE_ID", "").replace("-", "")
    if not page_id:
        raise RuntimeError("NOTION_PAGE_ID не встановлено — потрібен ID батьківської сторінки")

    database = client.databases.create(
        parent={"type": "page_id", "page_id": page_id},
        title=[{"type": "text", "text": {"content": db_name}}],
        properties={
            props["title"]: {"title": {}},
            props["summary"]: {"rich_text": {}},
            props["url"]: {"url": {}},
            props["source"]: {
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
            props["type"]: {
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
            props["topics"]: {
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
            props["deadline"]: {"date": {}},
            props["found_date"]: {"date": {}},
            props["url_hash"]: {"rich_text": {}},
        }
    )
    
    db_id = database["id"]
    DB_ID_FILE.write_text(db_id)
    logger.info(f"✅ Notion БД створено: {db_id}")
    return db_id


def _load_hash_cache(client: Client, db_id: str, config: dict) -> None:
    """Завантажити всі існуючі URL-хеші з Notion у кеш."""
    global _url_hash_cache, _cache_loaded
    if _cache_loaded:
        return

    logger.info("Завантаження кешу хешів з Notion…")
    props_mapping = _get_properties_mapping(config)
    url_hash_name = props_mapping["url_hash"]
    
    # Отримати ID властивості url_hash_name, щоб уникнути помилки "Could not find property with name or id"
    try:
        db_info = client.databases.retrieve(database_id=db_id)
        props = db_info.get("properties", {})
        url_hash_prop = props.get(url_hash_name)
        if not url_hash_prop:
            logger.warning(f"Властивість '{url_hash_name}' не знайдена у базі даних! Наявні властивості: {list(props.keys())}")
            return
        prop_id = url_hash_prop.get("id")
    except Exception as e:
        logger.error(f"Помилка отримання схеми БД: {e}")
        prop_id = url_hash_name

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
            filter={"property": prop_id, "rich_text": {"is_not_empty": True}},
        )

        for page in response.get("results", []):
            page_props = page.get("properties", {})
            # Отримуємо значення за іменем або ID
            hash_prop = None
            for key, val in page_props.items():
                if key == url_hash_name or page_props[key].get("id") == prop_id:
                    hash_prop = val.get("rich_text", [])
                    break
                    
            if hash_prop and len(hash_prop) > 0 and "text" in hash_prop[0]:
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
    _load_hash_cache(client, db_id, config)

    url_hash = item.get("url_hash", "")
    if is_duplicate(url_hash):
        logger.debug(f"Дублікат: {item.get('title', '')[:50]}")
        return False

    props_mapping = _get_properties_mapping(config)

    # Підготувати властивості
    props = {
        props_mapping["title"]: {"title": [{"text": {"content": item.get("title_uk", item.get("title", "Без назви"))[:2000]}}]},
        props_mapping["summary"]: {"rich_text": [{"text": {"content": item.get("summary_uk", "")[:2000]}}]},
        props_mapping["source"]: {"select": {"name": (item.get("source_name") or "Інше")[:100]}},
        props_mapping["type"]: {"select": {"name": item.get("type", "Невизначено")}},
        props_mapping["found_date"]: {"date": {"start": datetime.utcnow().strftime("%Y-%m-%d")}},
        props_mapping["url_hash"]: {"rich_text": [{"text": {"content": url_hash}}]},
    }

    # Посилання
    url = item.get("url")
    if url:
        props[props_mapping["url"]] = {"url": url}
    else:
        props[props_mapping["url"]] = {"url": None}

    # Тематика
    topics = item.get("topics_detected", [])
    if topics:
        props[props_mapping["topics"]] = {"multi_select": [{"name": t} for t in topics[:5]]}

    # Дедлайн
    deadline = item.get("deadline")
    if deadline:
        props[props_mapping["deadline"]] = {"date": {"start": deadline}}

    try:
        client.pages.create(parent={"database_id": db_id}, properties=props)
        _url_hash_cache.add(url_hash)
        logger.info(f"✅ Збережено: {item.get('title', '')[:60]}")
        return True
    except APIResponseError as e:
        logger.error(f"Notion API помилка при збереженні: {e}")
        return False


def get_upcoming_deadlines(days_ahead: int = 7, config: dict = None) -> list[dict]:
    """
    Отримати записи з дедлайном у найближчі N днів.
    Повертає список словників для Telegram-нагадувань.
    """
    if not os.environ.get("NOTION_API_KEY", ""):
        return []

    client = _get_client()
    config = config or {}
    db_id = _get_or_create_database(client, config)
    props_mapping = _get_properties_mapping(config)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    future = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    try:
        response = _query_database(
            client,
            db_id,
            filter={
                "and": [
                    {"property": props_mapping["deadline"], "date": {"on_or_after": today}},
                    {"property": props_mapping["deadline"], "date": {"on_or_before": future}},
                ]
            },
            sorts=[{"property": props_mapping["deadline"], "direction": "ascending"}],
        )
    except APIResponseError as e:
        logger.error(f"Помилка запиту дедлайнів: {e}")
        return []

    results = []
    for page in response.get("results", []):
        props = page.get("properties", {})
        title_prop = props.get(props_mapping["title"], {}).get("title", [])
        deadline_prop = props.get(props_mapping["deadline"], {}).get("date", {})
        url_prop = props.get(props_mapping["url"], {}).get("url", "")
        type_prop = props.get(props_mapping["type"], {}).get("select", {})

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


def get_weekly_stats(config: dict = None) -> dict:
    """Статистика за останній тиждень для дайджесту."""
    if not os.environ.get("NOTION_API_KEY", ""):
        return {}

    client = _get_client()
    config = config or {}
    try:
        db_id = _get_or_create_database(client, config)
    except Exception:
        return {}

    props_mapping = _get_properties_mapping(config)
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        response = _query_database(
            client,
            db_id,
            filter={"property": props_mapping["found_date"], "date": {"on_or_after": week_ago}},
        )
    except APIResponseError:
        return {}

    stats = {"total": 0, "by_type": {}, "by_topic": {}, "items": []}
    for page in response.get("results", []):
        props = page.get("properties", {})
        stats["total"] += 1

        type_name = (props.get(props_mapping["type"], {}).get("select") or {}).get("name", "Невизначено")
        stats["by_type"][type_name] = stats["by_type"].get(type_name, 0) + 1

        for t in (props.get(props_mapping["topics"], {}).get("multi_select") or []):
            tname = t.get("name", "")
            stats["by_topic"][tname] = stats["by_topic"].get(tname, 0) + 1

        title_prop = props.get(props_mapping["title"], {}).get("title", [])
        deadline_prop = (props.get(props_mapping["deadline"], {}).get("date") or {})
        stats["items"].append({
            "title": title_prop[0]["text"]["content"] if title_prop else "",
            "type": type_name,
            "deadline": deadline_prop.get("start"),
            "url": props.get(props_mapping["url"], {}).get("url", ""),
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
