"""
telegram_formatter.py — Форматування повідомлень для Telegram

Telegram підтримує Markdown V2 через parse_mode='MarkdownV2'.
Тут використовуємо простий Markdown (parse_mode='Markdown') для надійності.
"""

from datetime import datetime, date


TYPE_ICONS = {
    "Грант": "💰",
    "Конференція": "🎓",
    "Стипендія": "🏆",
    "Програма обміну": "🌍",
    "Невизначено": "📌",
}

DEADLINE_ICONS = {
    1: "🚨",  # завтра
    3: "⚠️",  # через 3 дні
    7: "📅",  # через тиждень
}


def escape_md(text: str) -> str:
    """Екранування символів для Telegram Markdown."""
    # Екрануємо лише критичні символи
    for ch in ["_", "*", "[", "]", "`"]:
        text = text.replace(ch, f"\\{ch}")
    return text


def format_new_finding(item: dict) -> str:
    """Форматування одного нового запису для Telegram."""
    icon = TYPE_ICONS.get(item.get("type", ""), "📌")
    title = item.get("title_uk", item.get("title", "Без назви"))[:120]
    summary = item.get("summary_uk", "")[:300]
    url = item.get("url", "")
    source = item.get("source_name", "")
    deadline = item.get("deadline")
    item_type = item.get("type", "Невизначено")
    relevance = item.get("relevance", 0)
    topics = ", ".join(item.get("topics_detected", []))
    funding = item.get("funding")

    lines = [f"{icon} *{escape_md(title)}*"]
    lines.append(f"🏷️ {item_type}" + (f" | {topics}" if topics else ""))

    if deadline:
        try:
            dl_date = datetime.strptime(deadline, "%Y-%m-%d").date()
            days_left = (dl_date - datetime.utcnow().date()).days
            if days_left >= 0:
                lines.append(f"⏰ Дедлайн: {deadline} (залишилось {days_left} дн.)")
            else:
                lines.append(f"⏰ Дедлайн: {deadline} *(минув)*")
        except ValueError:
            lines.append(f"⏰ Дедлайн: {deadline}")

    if funding:
        lines.append(f"💵 {escape_md(str(funding)[:100])}")

    if summary and summary != title:
        lines.append(f"\n_{escape_md(summary)}_")

    lines.append(f"\n🔗 [Докладніше]({url})")
    lines.append(f"📡 Джерело: {source} | Релевантність: {relevance}%")

    return "\n".join(lines)


def format_batch_findings(items: list[dict]) -> list[str]:
    """
    Форматування пачки нових знахідок.
    Повертає список повідомлень (Telegram має обмеження 4096 символів).
    """
    if not items:
        return []

    messages = []
    header = f"🔍 *Нові знахідки* — {len(items)} позицій\n{'─' * 30}"
    messages.append(header)

    for item in items:
        messages.append(format_new_finding(item))

    return messages


def format_deadline_reminder(deadlines: list[dict], days_ahead: int) -> str:
    """Форматування нагадування про дедлайни."""
    if not deadlines:
        return ""

    icon = DEADLINE_ICONS.get(days_ahead, "📅")
    lines = [f"{icon} *Нагадування про дедлайни* (через {days_ahead} дн.)\n"]

    for d in deadlines:
        item_icon = TYPE_ICONS.get(d.get("type", ""), "📌")
        title = escape_md(d.get("title", "Без назви")[:80])
        deadline = d.get("deadline", "")
        url = d.get("url", "")
        notion_url = d.get("notion_url", "")

        line = f"{item_icon} [{title}]({url})"
        line += f"\n   ⏰ {deadline}"
        if notion_url:
            line += f" | [Notion]({notion_url})"
        lines.append(line)

    return "\n\n".join(lines)


def format_weekly_digest(stats: dict) -> str:
    """Форматування щотижневого дайджесту."""
    if not stats or stats.get("total", 0) == 0:
        return "📊 *Тижневий дайджест*\n\nЗа тиждень нових знахідок не виявлено."

    total = stats.get("total", 0)
    by_type = stats.get("by_type", {})
    by_topic = stats.get("by_topic", {})
    items = stats.get("items", [])

    lines = [f"📊 *Тижневий дайджест Grant Scout*"]
    lines.append(f"🗓️ {datetime.utcnow().strftime('%d.%m.%Y')}\n")
    lines.append(f"✅ Знайдено за тиждень: *{total}* позицій\n")

    if by_type:
        lines.append("*За типом:*")
        for t, count in sorted(by_type.items(), key=lambda x: -x[1]):
            icon = TYPE_ICONS.get(t, "📌")
            lines.append(f"  {icon} {t}: {count}")

    if by_topic:
        lines.append("\n*За тематикою:*")
        for topic, count in sorted(by_topic.items(), key=lambda x: -x[1]):
            lines.append(f"  • {escape_md(topic)}: {count}")

    # Топ-5 за релевантністю (якщо є дедлайн — в першу чергу)
    with_deadlines = [i for i in items if i.get("deadline")]
    without_deadlines = [i for i in items if not i.get("deadline")]
    top_items = (with_deadlines + without_deadlines)[:5]

    if top_items:
        lines.append("\n*Найближчі дедлайни:*" if with_deadlines else "\n*Топ знахідок:*")
        for item in top_items:
            icon = TYPE_ICONS.get(item.get("type", ""), "📌")
            title = escape_md(item.get("title", "")[:60])
            url = item.get("url", "")
            deadline = item.get("deadline", "")
            deadline_str = f" ⏰ {deadline}" if deadline else ""
            lines.append(f"{icon} [{title}]({url}){deadline_str}")

    return "\n".join(lines)


def format_status_message(cron_info: dict = None) -> str:
    """Форматування статусу системи."""
    now = datetime.utcnow().strftime("%d.%m.%Y %H:%M") + " UTC"
    lines = [
        "⚙️ *Grant Scout — Статус*",
        f"🕐 Поточний час: {now}",
        "✅ Сервіс активний",
    ]
    if cron_info:
        lines.append(f"🔄 Наступний пошук: {cron_info.get('next_run', 'N/A')}")
        lines.append(f"📊 Всього знайдено: {cron_info.get('total_found', 0)}")
        lines.append(f"📅 Останній запуск: {cron_info.get('last_run', 'N/A')}")
    lines.append("\n_Команди: «пошук зараз», «дайджест», «список тем»_")
    return "\n".join(lines)


def format_help_message() -> str:
    """Список доступних команд."""
    return (
        "🤖 *Grant Scout — Команди*\n\n"
        "🔍 *Пошук:*\n"
        "  `запусти пошук` — негайний пошук\n"
        "  `тестовий пошук` — обмежений пошук (макс 10) з записом в Notion\n"
        "  `дайджест` — тижневий звіт\n"
        "  `дедлайни` — нагадування про дедлайни\n\n"
        "⚙️ *Управління темами:*\n"
        "  `список тем` — активні теми пошуку\n"
        "  `додай тему [назва]` — додати тему\n"
        "  `видали тему [назва]` — видалити тему\n\n"
        "📊 *Інше:*\n"
        "  `статус` — стан системи\n"
        "  `допомога` — ця інструкція"
    )


if __name__ == "__main__":
    # Тест форматування
    test_item = {
        "title": "Конкурс грантів НФДУ 2025 для підтримки досліджень у галузі освіти",
        "type": "Грант",
        "topics_detected": ["Освіта", "EdTech"],
        "deadline": "2025-08-15",
        "funding": "до 500 000 грн",
        "summary_uk": "НФДУ оголошує конкурс на отримання грантів для підтримки наукових досліджень у галузі освіти та педагогіки.",
        "url": "https://nfdu.gov.ua/news/2025-konkurs",
        "source_name": "НФДУ",
        "relevance": 95,
    }
    print(format_new_finding(test_item))
    print("\n" + "─" * 40 + "\n")
    print(format_help_message())
