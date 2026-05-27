"""
runner.py — Головний оркестратор Grant Scout

Режими запуску:
  search          — Повний цикл пошуку (cron 2x/день)
  digest          — Тижневий дайджест (cron пн 10:00)
  deadlines       — Перевірка дедлайнів (cron щодня)
  test            — Тестовий запуск (1 тема, без запису)
  test-save       — Тестовий запуск з обмеженням до 10 ресурсів та записом в Notion
  add-topic NAME  — Додати тему
  remove-topic N  — Видалити тему
  topics          — Список тем
  status          — Status системи
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Завантаження .env ДО імпорту інших модулів ───────────────────────────────
# analyzer.py та інші модулі читають os.environ на рівні модуля (при імпорті),
# тому .env потрібно завантажити першим.
_ENV_FILE = Path.home() / ".hermes" / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip().strip("'\""))

# Додаємо поточну директорію до шляху
sys.path.insert(0, str(Path(__file__).parent))

import config_manager
import scraper
import web_search
import analyzer
import notion_db
import telegram_formatter

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("GRANT_SCOUT_STATE", Path.home() / ".grant-scout"))
STATE_FILE = STATE_DIR / "runner_state.json"

# Telegram налаштування (для відправки без Hermes)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ─────────────────────────────────────────────
# Стан виконання
# ─────────────────────────────────────────────

def load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_run": None, "total_found": 0, "total_saved": 0}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────
# Telegram відправка (пряма, без Hermes)
# ─────────────────────────────────────────────

def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """Відправити повідомлення в Telegram напряму через Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[Telegram не налаштовано] {text[:100]}…")
        return False

    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram має ліміт 4096 символів
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    success = True
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            if not resp.ok:
                logger.error(f"Telegram помилка: {resp.text}")
                success = False
        except Exception as e:
            logger.error(f"Telegram відправка: {e}")
            success = False
        time.sleep(0.3)  # щоб не перевищити rate limit
    return success


# ─────────────────────────────────────────────
# Режим: search
# ─────────────────────────────────────────────

def run_search(config: dict, test_mode: bool = False, limit: int = None) -> dict:
    """
    Повний цикл пошуку:
    1. Скрапінг сайтів
    2. Google Search
    3. LLM аналіз
    4. Дедуплікація + збереження в Notion
    5. Telegram-сповіщення
    """
    logger.info("=" * 50)
    logger.info(f"{'ТЕСТОВИЙ ' if test_mode else ''}{'ОБМЕЖЕНИЙ ' if limit else ''}ПОШУК — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    logger.info("=" * 50)

    # 1. Скрапінг
    if test_mode:
        # В тестовому режимі — тільки перший сайт, без запису в Notion
        test_config = dict(config)
        test_config["sources"] = {
            "websites": config.get("sources", {}).get("websites", [])[:1],
        }
        raw_items = scraper.scrape_all_sites(test_config)
        raw_items = raw_items[:5]  # Обмежити 5 позиціями
    elif limit is not None:
        # Для обмеженого тестового запуску з записом (test-save)
        test_config = dict(config)
        test_config["sources"] = {
            "websites": config.get("sources", {}).get("websites", [])[:1],
            "web_search": dict(config.get("sources", {}).get("web_search", {}))
        }
        test_config["sources"]["web_search"]["max_results_per_query"] = 5
        
        # Обмежуємо скрапінг до першого сайту (до 5 посилань)
        raw_items = scraper.scrape_all_sites(test_config)
        raw_items = raw_items[:5]
        
        # Обмежуємо веб-пошук: тільки перша тема і тільки 1 ключове слово
        topics = config.get("topics", [])
        if topics:
            first_topic = dict(topics[0])
            keywords = first_topic.get("keywords_uk", []) + first_topic.get("keywords_en", [])
            first_topic["keywords_uk"] = keywords[:1]
            first_topic["keywords_en"] = []
            test_config["topics"] = [first_topic]
            
            web_items = web_search.search_all_topics(test_config)
            raw_items.extend(web_items[:5])
            
        raw_items = raw_items[:limit]
    else:
        raw_items = scraper.scrape_all_sites(config)
        web_items = web_search.search_all_topics(config)
        raw_items.extend(web_items)

    logger.info(f"Зібрано {len(raw_items)} сирих результатів")

    if not raw_items:
        msg = "📭 Нових знахідок не виявлено за поточний цикл."
        logger.info(msg)
        if not test_mode:
            send_telegram(msg)
        return {"found": 0, "saved": 0}

    # 2. LLM аналіз
    analyzed = analyzer.analyze_batch(raw_items, config)
    logger.info(f"Після аналізу: {len(analyzed)} релевантних позицій")

    if test_mode:
        # В тестовому режимі — вивести результати без збереження
        print("\n🧪 ТЕСТОВІ РЕЗУЛЬТАТИ (без збереження в Notion):\n")
        for i, item in enumerate(analyzed[:3], 1):
            msg = telegram_formatter.format_new_finding(item)
            print(f"--- {i} ---\n{msg}\n")
        return {"found": len(raw_items), "saved": 0, "analyzed": len(analyzed)}

    # 3. Збереження в Notion (з дедуплікацією)
    saved_items = []
    for item in analyzed:
        if notion_db.save_item(item, config):
            saved_items.append(item)

    logger.info(f"Збережено {len(saved_items)} нових позицій в Notion")

    # 4. Telegram-сповіщення
    if saved_items:
        batch_mode = config.get("telegram", {}).get("notifications", {}).get("batch_mode", True)
        if batch_mode:
            header = f"🔍 *Нові знахідки* — {len(saved_items)} позицій\n{'─' * 30}"
            send_telegram(header)
            for item in saved_items:
                msg = telegram_formatter.format_new_finding(item)
                send_telegram(msg)
                time.sleep(0.5)
        else:
            for item in saved_items:
                msg = telegram_formatter.format_new_finding(item)
                send_telegram(msg)
                time.sleep(0.5)
    else:
        send_telegram("📭 Нових знахідок не виявлено — всі результати вже є в базі.")

    return {"found": len(raw_items), "saved": len(saved_items)}


# ─────────────────────────────────────────────
# Режим: deadlines
# ─────────────────────────────────────────────

def run_deadlines(config: dict) -> None:
    """Перевірка та відправка нагадувань про дедлайни."""
    logger.info("Перевірка дедлайнів…")
    reminder_days = config.get("telegram", {}).get("notifications", {}).get(
        "deadline_reminder_days", [7, 3, 1]
    )

    sent_any = False
    for days in reminder_days:
        deadlines = notion_db.get_upcoming_deadlines(days, config)
        # Фільтруємо точно ті, що через N днів (не більше)
        exact = [
            d for d in deadlines
            if d.get("deadline")
            and (datetime.strptime(d["deadline"], "%Y-%m-%d").date() - datetime.utcnow().date()).days == days
        ]
        if exact:
            msg = telegram_formatter.format_deadline_reminder(exact, days)
            send_telegram(msg)
            sent_any = True

    if not sent_any:
        logger.info("Нагадувань про дедлайни немає")


# ─────────────────────────────────────────────
# Режим: digest
# ─────────────────────────────────────────────

def run_digest(config: dict) -> None:
    """Щотижневий дайджест."""
    logger.info("Формування тижневого дайджесту…")
    stats = notion_db.get_weekly_stats(config)
    msg = telegram_formatter.format_weekly_digest(stats)
    send_telegram(msg)
    logger.info("Дайджест надіслано")


# ─────────────────────────────────────────────
# Головна точка входу
# ─────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(STATE_DIR / "grant-scout.log", encoding="utf-8"),
        ],
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1].lower()

    # Команди без конфігу
    if mode == "add-topic" and len(sys.argv) >= 3:
        raw = " ".join(sys.argv[2:])
        topic_name, topic_hints = config_manager.parse_topic_command(raw)
        result = config_manager.add_topic(topic_name, hints=topic_hints)
        print(result)
        return

    if mode == "remove-topic" and len(sys.argv) >= 3:
        topic_name = " ".join(sys.argv[2:])
        result = config_manager.remove_topic(topic_name)
        print(result)
        return

    if mode == "topics":
        print(config_manager.format_topics_list())
        return

    # Команди з конфігом
    config = config_manager.load_config()
    state = load_state()

    if mode == "search":
        result = run_search(config)
        state["last_run"] = datetime.now().isoformat()
        state["total_found"] = state.get("total_found", 0) + result.get("found", 0)
        state["total_saved"] = state.get("total_saved", 0) + result.get("saved", 0)
        save_state(state)

    elif mode == "test":
        run_search(config, test_mode=True)

    elif mode == "test-save":
        result = run_search(config, test_mode=False, limit=10)
        state["last_run"] = datetime.now().isoformat()
        state["total_found"] = state.get("total_found", 0) + result.get("found", 0)
        state["total_saved"] = state.get("total_saved", 0) + result.get("saved", 0)
        save_state(state)

    elif mode == "deadlines":
        run_deadlines(config)

    elif mode == "digest":
        run_digest(config)

    elif mode == "status":
        cron_info = {
            "last_run": state.get("last_run") or "Ніколи",
            "total_found": state.get("total_found", 0),
            "next_run": "09:00 або 18:00",
        }
        msg = telegram_formatter.format_status_message(cron_info)
        print(msg)
        send_telegram(msg)

    else:
        print(f"Невідомий режим: {mode}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
