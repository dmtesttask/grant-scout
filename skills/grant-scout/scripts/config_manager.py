"""
config_manager.py — Управління конфігурацією Grant Scout

Зчитує/записує config.yaml, надає API для додавання/видалення тем.
Атомарний запис: тимчасовий файл → rename.

Синтаксис Telegram-команди для додавання теми:
  Базовий:    «додай тему Медицина»
              → ключові слова генеруються LLM автоматично

  Розширений: «додай тему Медицина: гранти, дослідження, лікування»
              → підказки передаються LLM для кращої генерації
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)


CONFIG_PATH = Path(os.environ.get("GRANT_SCOUT_CONFIG", Path.home() / "grant-scout" / "config.yaml"))


def load_config() -> dict:
    """Завантажити конфіг з YAML-файлу."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Конфіг не знайдено: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(config: dict) -> None:
    """Атомарний запис конфігу (тимчасовий файл → rename)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        shutil.move(tmp_path, CONFIG_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def list_topics(enabled_only: bool = False) -> list[dict]:
    """Повернути список тем."""
    config = load_config()
    topics = config.get("topics", [])
    if enabled_only:
        topics = [t for t in topics if t.get("enabled", True)]
    return topics


# ── LLM-генерація ключових слів ──────────────────────────────────────────────

KEYWORD_PROMPT = """\
You are a keyword generation assistant for finding scientific grants and conferences targeting Ukrainian researchers.
Generate search keywords for the topic "{name}"{hint_part}.

Requirements:
- keywords_uk: 5-7 Ukrainian-language phrases (2-4 words each) that users actually search for on grant websites.
  Use Ukrainian words: грант, конкурс, стипендія, конференція, програма, фінансування.
- keywords_en: 4-6 English-language phrases suitable for international databases and Google Scholar.
- Cover variants: grant, competition, scholarship, conference, program, funding, fellowship.
- Take into account the Ukrainian academic and scientific context.

IMPORTANT: Return ONLY valid JSON with no explanations or extra text:
{{"keywords_uk": [...], "keywords_en": [...]}}"""


def _generate_keywords_llm(name: str, hints: list[str] = None) -> tuple[list[str], list[str]]:
    """
    Генерує ключові слова через OpenRouter LLM.
    Повертає (keywords_uk, keywords_en).
    Якщо LLM недоступний — повертає ([], []) і викликається fallback.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.info("OPENROUTER_API_KEY не встановлено — використовуємо шаблонні ключові слова")
        return [], []

    hint_part = ""
    if hints:
        hint_part = f" (підказки: {', '.join(hints)})"

    # Єдине джерело правди — config.yaml (llm.preset або llm.model)
    try:
        config = load_config()
    except Exception as e:
        logger.warning(f"Не вдалося завантажити конфіг — пропускаємо LLM генерацію ключових слів: {e}")
        return [], []

    llm_cfg = config.get("llm", {})
    preset = llm_cfg.get("preset", "").strip()
    if preset:
        model = f"@preset/{preset}"
        logger.info(f"Використовуємо OpenRouter пресет: {model}")
    else:
        model = llm_cfg.get("model")
        if not model:
            logger.warning("llm.model не задано в config.yaml — пропускаємо LLM генерацію")
            return [], []

    prompt = KEYWORD_PROMPT.format(name=name, hint_part=hint_part)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 400,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/grant-scout",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload, headers=headers, timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        kw_uk = [str(k).strip() for k in data.get("keywords_uk", []) if k][:8]
        kw_en = [str(k).strip() for k in data.get("keywords_en", []) if k][:7]
        if kw_uk and kw_en:
            logger.info(f"LLM згенерував {len(kw_uk)} укр. + {len(kw_en)} англ. ключових слів")
            return kw_uk, kw_en
    except Exception as e:
        logger.warning(f"LLM генерація ключових слів не вдалася: {e}")

    return [], []


def _fallback_keywords(name: str, hints: list[str] = None) -> tuple[list[str], list[str]]:
    """Шаблонна генерація ключових слів без LLM."""
    n = name.lower()
    hint_str = " ".join(hints).lower() if hints else ""

    keywords_uk = [
        f"грант {n}",
        f"конкурс {n} Україна",
        f"стипендія {n}",
        f"конференція {n} Україна",
        f"фінансування {n} проєктів",
    ]
    keywords_en = [
        f"{name} grant Ukraine",
        f"{name} scholarship Ukraine",
        f"{name} conference Ukraine",
        f"{name} funding program",
    ]

    # Додати підказки як додаткові ключові слова
    if hints:
        for hint in hints[:3]:
            keywords_uk.append(f"{hint.lower()} {n} грант")
            keywords_en.append(f"{hint.lower()} {name} grant")

    return keywords_uk, keywords_en


# ── Публічний API ─────────────────────────────────────────────────────────────

def parse_topic_command(raw: str) -> tuple[str, list[str]]:
    """
    Розбирає рядок команди від Telegram.

    Підтримує два формати:
      «Медицина»                          → name="Медицина", hints=[]
      «Медицина: гранти, дослідження»     → name="Медицина", hints=["гранти", "дослідження"]
    """
    if ":" in raw:
        name_part, hints_part = raw.split(":", 1)
        hints = [h.strip() for h in hints_part.split(",") if h.strip()]
        return name_part.strip(), hints
    return raw.strip(), []


def add_topic(
    name: str,
    keywords_uk: list[str] = None,
    keywords_en: list[str] = None,
    hints: list[str] = None,
) -> str:
    """
    Додати нову тему пошуку.

    Пріоритет ключових слів:
      1. Явно передані keywords_uk / keywords_en
      2. LLM-генерація на основі назви + підказок (hints)
      3. Шаблонна генерація (якщо LLM недоступний)

    Повертає рядок-результат для Telegram.
    """
    config = load_config()
    topics = config.setdefault("topics", [])

    # Перевірка дублікату (без урахування регістру)
    name_lower = name.strip().lower()
    for t in topics:
        if t["name"].lower() == name_lower:
            return f"⚠️ Тема «{t['name']}» вже існує."

    # Визначити ключові слова
    generated_by = ""
    if keywords_uk and keywords_en:
        # Явно передані — використовуємо як є
        generated_by = "вручну"
    else:
        # Спробувати LLM
        logger.info(f"Генерація ключових слів для теми «{name}»…")
        kw_uk, kw_en = _generate_keywords_llm(name, hints)
        if kw_uk and kw_en:
            keywords_uk = kw_uk
            keywords_en = kw_en
            generated_by = "LLM"
        else:
            # Fallback
            keywords_uk, keywords_en = _fallback_keywords(name, hints)
            generated_by = "шаблон"

    new_topic = {
        "name": name.strip(),
        "enabled": True,
        "keywords_uk": keywords_uk,
        "keywords_en": keywords_en,
    }
    topics.append(new_topic)
    config["topics"] = topics
    save_config(config)

    kw_preview = ", ".join(keywords_uk[:3])
    return (
        f"✅ Тему «{name}» додано ({generated_by})\n"
        f"🔤 {len(keywords_uk)} укр. + {len(keywords_en)} англ. ключових слів\n"
        f"_Приклади: {kw_preview}…_"
    )


def remove_topic(name: str) -> str:
    """
    Видалити тему за назвою (або вимкнути якщо точна відповідність не знайдена).
    Повертає рядок-результат для Telegram.
    """
    config = load_config()
    topics = config.get("topics", [])
    name_lower = name.strip().lower()

    original_count = len(topics)
    config["topics"] = [t for t in topics if t["name"].lower() != name_lower]

    if len(config["topics"]) == original_count:
        # Спробувати вимкнути за частковим збігом
        matched = False
        for t in config["topics"] if config["topics"] else topics:
            if name_lower in t["name"].lower():
                t["enabled"] = False
                matched = True
        if not matched:
            return f"❌ Тему «{name}» не знайдено."
        save_config(config)
        return f"⏸️ Тему «{name}» вимкнено (але не видалено)."

    save_config(config)
    return f"🗑️ Тему «{name}» видалено."


def toggle_topic(name: str, enabled: bool) -> str:
    """Увімкнути або вимкнути тему."""
    config = load_config()
    name_lower = name.strip().lower()
    for t in config.get("topics", []):
        if t["name"].lower() == name_lower:
            t["enabled"] = enabled
            save_config(config)
            state = "увімкнено ✅" if enabled else "вимкнено ⏸️"
            return f"Тему «{t['name']}» {state}."
    return f"❌ Тему «{name}» не знайдено."


def format_topics_list() -> str:
    """Форматований список тем для Telegram."""
    topics = list_topics()
    if not topics:
        return "📋 Теми пошуку не налаштовано."

    lines = ["📋 *Теми пошуку:*\n"]
    for i, t in enumerate(topics, 1):
        icon = "✅" if t.get("enabled", True) else "⏸️"
        kw_count = len(t.get("keywords_uk", [])) + len(t.get("keywords_en", []))
        lines.append(f"{i}. {icon} *{t['name']}* ({kw_count} ключових слів)")
    lines.append("\n_Команди: «додай тему X», «видали тему X»_")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print(format_topics_list())
    elif sys.argv[1] == "add" and len(sys.argv) >= 3:
        # Підтримка: python config_manager.py add "Медицина: гранти, лікування"
        raw = " ".join(sys.argv[2:])
        topic_name, topic_hints = parse_topic_command(raw)
        print(add_topic(topic_name, hints=topic_hints))
    elif sys.argv[1] == "remove" and len(sys.argv) >= 3:
        print(remove_topic(sys.argv[2]))
    elif sys.argv[1] == "list":
        print(format_topics_list())
