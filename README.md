# Hermes Grant Scout 🎓

Автоматизований пошук наукових грантів, конференцій, стипендій та програм обміну в українському сегменті інтернету. Система використовує [Hermes Agent](https://github.com/NousResearch/hermes-agent) для щоденного пошуку, аналізує знахідки за допомогою LLM та систематизує результати в Notion з повідомленнями в Telegram.

## ✨ Можливості

- 🔍 **Пошук двічі на день** — о 09:00 та 18:00 (київський час)
- 🧠 **LLM-аналіз** — класифікація, витягування дедлайнів, генерація опису
- 📋 **Notion база даних** — автоматично створюється при першому запуску
- 📬 **Telegram-сповіщення** — миттєво про нові знахідки
- 🔔 **Нагадування** — за 7, 3 і 1 день до дедлайну
- 📊 **Тижневий дайджест** — щопонеділка о 10:00
- 🚫 **Дедуплікація** — повторний пошук не дублює записи
- ⚙️ **Управління темами** — через YAML-конфіг або Telegram-команди

## 🗂️ Структура проєкту

```
AI_Hermes_search/
├── config.yaml                    # Теми, джерела, розклад
├── skills/
│   └── grant-scout/
│       ├── SKILL.md               # Hermes skill definition
│       ├── requirements.txt
│       └── scripts/
│           ├── runner.py          # Головний оркестратор
│           ├── scraper.py         # Веб-скрапінг укр. сайтів
│           ├── google_search.py   # Google Custom Search
│           ├── notion_client.py   # Notion API
│           ├── analyzer.py        # LLM аналіз
│           ├── config_manager.py  # Управління темами
│           └── telegram_formatter.py
└── deploy/
    ├── deploy-gcp.sh              # Розгортання "одним кліком"
    ├── setup-vm.sh                # Налаштування VM
    ├── hermes-config.yaml         # Шаблон конфігу Hermes
    └── .env.template              # Шаблон змінних середовища
```

---

## 🚀 Швидкий старт

### 1. Підготовка API ключів

Детальна інструкція → [docs/SETUP_APIS.md](docs/SETUP_APIS.md)

Вам потрібно:
- [ ] OpenRouter API key
- [ ] Telegram Bot Token
- [ ] Notion Integration Token
- [ ] Google Custom Search API key + Engine ID

### 2. Налаштування .env

```bash
cd deploy/
cp .env.template .env
# Відредагуйте .env та заповніть всі значення
nano .env
```

### 3. Розгортання на GCP

Відкрийте **Google Cloud Shell** у вашому проєкті та виконайте:

```bash
# Завантажте проєкт (або клонуйте git-репозиторій)
# Потім:
cd AI_Hermes_search/deploy/
chmod +x deploy-gcp.sh setup-vm.sh
./deploy-gcp.sh
```

**Режими:**
```bash
./deploy-gcp.sh              # Повне розгортання або оновлення
./deploy-gcp.sh --update     # Тільки оновлення файлів скілу
./deploy-gcp.sh --status     # Статус VM
./deploy-gcp.sh --dry-run    # Перегляд команд без виконання
./deploy-gcp.sh --destroy    # Видалення VM
```

---

## 🧪 Тестування

Після розгортання підключіться до VM та виконайте тестовий запуск:

```bash
# Підключення до VM
gcloud compute ssh grant-scout-vm --zone=europe-west1-b --project=YOUR_PROJECT_ID

# Тестовий запуск (1 сайт, 5 результатів, без запису в Notion)
python3 ~/grant-scout/scripts/runner.py test

# Повний пошук
python3 ~/grant-scout/scripts/runner.py search

# Тижневий дайджест
python3 ~/grant-scout/scripts/runner.py digest

# Перевірка дедлайнів
python3 ~/grant-scout/scripts/runner.py deadlines
```

### 📱 Активація Telegram-бота (Home Channel)

При першому зверненні до вашого бота в Telegram (надіславши команду `/start` або будь-яке повідомлення), ви отримаєте сервісне повідомлення від Hermes:
> `📬 No home channel is set for Telegram. ... Type /sethome to make this chat your home channel, or ignore to skip.`

1. Надішліть боту команду:
   ```text
   /sethome
   ```
2. Бот зафіксує ваш чат як головний ("Home Channel") для відправки щоденних результатів пошуку, сповіщень та дайджестів.

---

## ⚙️ Управління темами

### Через конфіг (SSH)

Відредагуйте `~/grant-scout/config.yaml` на VM:

```bash
nano ~/grant-scout/config.yaml
# Змініть enabled: false щоб вимкнути тему
# Або додайте нову тему в список topics:
```

### Через Python CLI

```bash
# Список тем
python3 ~/grant-scout/scripts/runner.py topics

# Додати тему (LLM генерує ключові слова автоматично)
python3 ~/grant-scout/scripts/runner.py add-topic "Медицина"

# Додати тему з підказками для LLM (кращі ключові слова)
python3 ~/grant-scout/scripts/runner.py add-topic "Медицина: гранти, дослідження, лікування"

# Видалити тему
python3 ~/grant-scout/scripts/runner.py remove-topic "Медицина"
```

### Через Telegram

Напишіть вашому боту:
```
список тем
додай тему Медицина
видали тему Медицина
```

---

## 📊 Notion — структура бази даних

База даних створюється автоматично при першому запуску з полями:

| Поле | Тип | Опис |
|------|-----|------|
| Назва | Title | Назва гранту/конференції |
| Тип | Select | Грант / Конференція / Стипендія / Програма обміну |
| Тематика | Multi-select | Освіта / Мистецтво / Музика / EdTech |
| Джерело | Select | НФДУ / МОН / ЄвроОсвіта / Google |
| Дедлайн | Date | Кінцевий термін |
| Посилання | URL | Пряме посилання |
| Опис | Rich text | Короткий опис (LLM) |
| Фінансування | Rich text | Умови фінансування |
| Дата знахідки | Date | Коли знайдено |
| Статус | Select | 🆕 Нове / 👀 Переглянуто / 📝 Подано / 📦 Архів |
| Релевантність | Number | 0–100% |

---

## 📱 Telegram-команди

| Команда | Дія |
|---------|-----|
| `запусти пошук` | Негайний пошук |
| `дайджест` | Тижневий звіт |
| `дедлайни` | Перевірка дедлайнів |
| `список тем` | Активні теми |
| `додай тему X` | Додати тему — LLM автоматично генерує ключові слова |
| `додай тему X: підказка1, підказка2` | Додати тему з підказками для LLM |
| `видали тему X` | Видалити тему |
| `статус` | Статус системи |
| `допомога` | Список команд |

**Приклади з підказками:**
```
додай тему Медицина: гранти, клінічні дослідження, фармація
додай тему Архітектура: проєкти, реставрація, урбаністика
додай тему Екологія: клімат, відновлювана енергія, природа
```
Підказки допомагають LLM точніше підібрати ключові слова для пошуку.

---

## 🔧 Управління сервісом (на VM)

```bash
# Статус
systemctl status hermes-agent

# Перезапуск
sudo systemctl restart hermes-agent

# Логи
journalctl -u hermes-agent -f

# Логи Grant Scout
tail -f ~/.grant-scout/logs/search.log

# Cron-задачі
crontab -l

# Hermes статус
hermes cron list
```

---

## 💰 Орієнтовна вартість

| Сервіс | Вартість |
|--------|----------|
| GCP VM e2-medium | ~$25/міс |
| OpenRouter (безкоштовна модель) | $0 |
| Google Custom Search (≤100 запитів/день) | $0 |
| Notion API | $0 |
| Telegram Bot | $0 |
| **Разом** | **~$25/міс** |

---

## 🆘 Вирішення проблем

**Notion не записує:**
```bash
python3 ~/grant-scout/scripts/notion_client.py --test-connection
# Перевірте NOTION_API_KEY та NOTION_PAGE_ID в .env
# Переконайтесь що інтеграція має доступ до сторінки
```

**Google пошук не працює:**
```bash
# Перевірте ліміт запитів
cat ~/.grant-scout/google_search_state.json
# Перевірте ключі
echo $GOOGLE_CSE_API_KEY
```

**Hermes не запускається:**
```bash
journalctl -u hermes-agent --no-pager -n 50
# Перевірте ~/.hermes/.env
```

**Повторне розгортання після змін:**
```bash
# З локальної машини/Cloud Shell:
./deploy-gcp.sh --update
```
