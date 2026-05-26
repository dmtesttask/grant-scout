#!/usr/bin/env bash
# =============================================================================
# setup-vm.sh — Налаштування Hermes Grant Scout на GCP VM (Debian 12)
# =============================================================================
# Запускається автоматично з deploy-gcp.sh через SSH.
# ІДЕМПОТЕНТНИЙ: безпечний для повторного запуску — перевіряє стан
# перед кожним кроком і пропускає вже виконану роботу.
# =============================================================================
set -euo pipefail

# ── Кольоровий вивід ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Допоміжна функція: встановити якщо відсутнє ──────────────────────────────
install_if_missing() {
    local cmd="$1"
    local install_cmd="$2"
    local label="${3:-$cmd}"
    if command -v "$cmd" &>/dev/null; then
        ok "$label вже встановлено — пропускаємо"
    else
        log "Встановлення $label…"
        eval "$install_cmd"
        ok "$label встановлено"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRANT_SCOUT_DIR="$HOME/grant-scout"
HERMES_ENV="$HOME/.hermes/.env"
STATE_DIR="$HOME/.grant-scout"

export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   Grant Scout VM Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Крок 1: Оновлення системи
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 1/8: Оновлення пакетів системи…"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" -qq
ok "Система оновлена"

# ─────────────────────────────────────────────────────────────────────────────
# Крок 2: Системні залежності
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 2/8: Системні залежності…"
PACKAGES_TO_INSTALL=()
for pkg in python3 python3-pip python3-venv git curl wget jq; do
    if ! dpkg -l "$pkg" &>/dev/null; then
        PACKAGES_TO_INSTALL+=("$pkg")
    fi
done

if [ ${#PACKAGES_TO_INSTALL[@]} -gt 0 ]; then
    sudo apt-get install -y -qq "${PACKAGES_TO_INSTALL[@]}"
    ok "Встановлено: ${PACKAGES_TO_INSTALL[*]}"
else
    ok "Всі системні залежності вже є"
fi

# Python 3.11+ перевірка
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
log "Python версія: $PYTHON_VERSION"

# ─────────────────────────────────────────────────────────────────────────────
# Крок 3: Hermes Agent
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 3/8: Hermes Agent…"
if [ -x "$HOME/.local/bin/hermes" ]; then
    ok "Hermes Agent вже встановлено ($("$HOME/.local/bin/hermes" --version 2>/dev/null || echo 'версія невідома'))"
    log "Перевіряємо оновлення Hermes…"
    "$HOME/.local/bin/hermes" update --yes 2>/dev/null || warn "Не вдалося оновити Hermes"
else
    log "Встановлення Hermes Agent…"
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
    ok "Hermes Agent встановлено"
fi

# Встановлення Telegram залежностей для Hermes Agent
log "Встановлення додаткових залежностей для Telegram Gateway…"
if [ -x "$HOME/.hermes/hermes-agent/venv/bin/pip" ]; then
    "$HOME/.hermes/hermes-agent/venv/bin/pip" install --quiet python-telegram-bot
    ok "Telegram залежності для Hermes встановлено"
else
    warn "Віртуальне оточення Hermes не знайдено для встановлення Telegram залежностей"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Крок 4: .env файл (мерж нових змінних зі старими)
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 4/8: Налаштування змінних середовища…"
mkdir -p "$HOME/.hermes"

ENV_SOURCE="$SCRIPT_DIR/.env"
if [ ! -f "$ENV_SOURCE" ]; then
    warn ".env файл не знайдено в $SCRIPT_DIR — пропускаємо"
else
    if [ ! -f "$HERMES_ENV" ]; then
        cp "$ENV_SOURCE" "$HERMES_ENV"
        chmod 600 "$HERMES_ENV"
        ok ".env створено"
    else
        # Мерж: додаємо тільки відсутні змінні (не перезаписуємо існуючі)
        ADDED=0
        while IFS= read -r line; do
            [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
            KEY=$(echo "$line" | cut -d= -f1)
            if ! grep -q "^${KEY}=" "$HERMES_ENV" 2>/dev/null; then
                echo "$line" >> "$HERMES_ENV"
                ADDED=$((ADDED + 1))
            fi
        done < "$ENV_SOURCE"
        chmod 600 "$HERMES_ENV"
        if [ $ADDED -gt 0 ]; then
            ok ".env оновлено: додано $ADDED нових змінних"
        else
            ok ".env актуальний — змін не потрібно"
        fi
    fi
fi

# Додаємо TELEGRAM_ALLOWED_USERS для Hermes, якщо є TELEGRAM_CHAT_ID і ще не задано
if [ -f "$HERMES_ENV" ]; then
    if grep -q "^TELEGRAM_CHAT_ID=" "$HERMES_ENV" && ! grep -q "^TELEGRAM_ALLOWED_USERS=" "$HERMES_ENV"; then
        TG_CHAT_ID=$(grep "^TELEGRAM_CHAT_ID=" "$HERMES_ENV" | cut -d= -f2- | tr -d '"' | tr -d "'")
        echo "TELEGRAM_ALLOWED_USERS=$TG_CHAT_ID" >> "$HERMES_ENV"
        ok "Додано TELEGRAM_ALLOWED_USERS до .env"
    fi
fi

# Завантажити змінні в поточну сесію
if [ -f "$HERMES_ENV" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$HERMES_ENV"
    set +o allexport
fi

# ─────────────────────────────────────────────────────────────────────────────
# Крок 5: Конфігурація Hermes
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 5/8: Конфігурація Hermes Agent…"
HERMES_CFG="$HOME/.hermes/config.yaml"
cp "$SCRIPT_DIR/hermes-config.yaml" "$HERMES_CFG"
ok "Конфіг Hermes оновлено та застосовано"

# Налаштування Telegram Gateway (якщо токен є)
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    log "Конфігурація Telegram Gateway…"
    ok "Telegram Gateway налаштовано через змінні середовища"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Крок 6: Grant Scout скіл та Python залежності
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 6/8: Grant Scout скіл…"
mkdir -p "$GRANT_SCOUT_DIR/scripts" "$STATE_DIR"

# Копіюємо файли скілу (завжди — для оновлення)
cp -r "$SCRIPT_DIR/../skills/grant-scout/scripts/"* "$GRANT_SCOUT_DIR/scripts/"
cp "$SCRIPT_DIR/../skills/grant-scout/SKILL.md" "$GRANT_SCOUT_DIR/"
cp "$SCRIPT_DIR/../skills/grant-scout/requirements.txt" "$GRANT_SCOUT_DIR/"

# config.yaml — тільки якщо ще не існує
if [ ! -f "$GRANT_SCOUT_DIR/config.yaml" ]; then
    cp "$SCRIPT_DIR/../config.yaml" "$GRANT_SCOUT_DIR/"
    ok "config.yaml скопійовано"
else
    warn "config.yaml вже є — не перезаписуємо (ваші налаштування збережено)"
fi

# Встановлення Python залежностей
log "Встановлення Python залежностей…"
pip3 install --break-system-packages --quiet --upgrade -r "$GRANT_SCOUT_DIR/requirements.txt"
ok "Python залежності встановлено"

# Реєстрація grant-scout скілу
log "Реєстрація grant-scout скілу в Hermes…"
mkdir -p "$HOME/.hermes/skills/grant-scout"
cp "$GRANT_SCOUT_DIR/SKILL.md" "$HOME/.hermes/skills/grant-scout/SKILL.md"
if [ -d "$GRANT_SCOUT_DIR/scripts" ]; then
    cp -r "$GRANT_SCOUT_DIR/scripts" "$HOME/.hermes/skills/grant-scout/"
fi
ok "Grant Scout скіл встановлено"

# Очищаємо всі старі cron-задачі, які містять "grant-scout"
crontab -l 2>/dev/null | grep -v "grant-scout" > /tmp/current_cron || true

PYTHON_RUN="python3 $GRANT_SCOUT_DIR/scripts/runner.py"
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$LOG_DIR"

# Допоміжна функція для читання розкладу з config.yaml
get_schedule() {
    local key="$1"
    local default_val="$2"
    python3 -c "
import sys
try:
    import yaml
    cfg = yaml.safe_load(open('$GRANT_SCOUT_DIR/config.yaml'))
    v = cfg['search'].get('$key')
    if v is None:
        sys.exit(0)
    print(v)
except Exception:
    print('$default_val')
" 2>/dev/null || echo ""
}

MORNING_SCHED=$(get_schedule "schedule_morning" "0 6 * * *")
EVENING_SCHED=$(get_schedule "schedule_evening" "0 15 * * *")
DEADLINE_SCHED=$(get_schedule "deadline_check" "0 5 * * *")
DIGEST_SCHED=$(get_schedule "weekly_digest" "0 7 * * 1")

# Додаємо ранковий пошук
if [ -n "$MORNING_SCHED" ]; then
    log "Додаємо ранковий пошук на розклад: $MORNING_SCHED"
    echo "# grant-scout-morning" >> /tmp/current_cron
    echo "$MORNING_SCHED $PYTHON_RUN search >> $LOG_DIR/search.log 2>&1" >> /tmp/current_cron
fi

# Додаємо вечірній пошук
if [ -n "$EVENING_SCHED" ]; then
    log "Додаємо вечірній пошук на розклад: $EVENING_SCHED"
    echo "# grant-scout-evening" >> /tmp/current_cron
    echo "$EVENING_SCHED $PYTHON_RUN search >> $LOG_DIR/search.log 2>&1" >> /tmp/current_cron
fi

# Додаємо перевірку дедлайнів
if [ -n "$DEADLINE_SCHED" ]; then
    log "Додаємо перевірку дедлайнів на розклад: $DEADLINE_SCHED"
    echo "# grant-scout-deadlines" >> /tmp/current_cron
    echo "$DEADLINE_SCHED $PYTHON_RUN deadlines >> $LOG_DIR/deadlines.log 2>&1" >> /tmp/current_cron
fi

# Додаємо тижневий дайджест
if [ -n "$DIGEST_SCHED" ]; then
    log "Додаємо тижневий дайджест на розклад: $DIGEST_SCHED"
    echo "# grant-scout-digest" >> /tmp/current_cron
    echo "$DIGEST_SCHED $PYTHON_RUN digest >> $LOG_DIR/digest.log 2>&1" >> /tmp/current_cron
fi

# Додаємо ротацію логів (щонеділі о 03:00)
echo "# grant-scout-log-rotation" >> /tmp/current_cron
echo "0 3 * * 0 find $LOG_DIR -name '*.log' -mtime +30 -delete" >> /tmp/current_cron

# Застосовуємо оновлений crontab
crontab /tmp/current_cron
rm -f /tmp/current_cron

ok "Cron-задачі оновлено та налаштовано"

# ─────────────────────────────────────────────────────────────────────────────
# Крок 8: Systemd сервіс для Hermes Agent
# ─────────────────────────────────────────────────────────────────────────────
log "Крок 8/8: Systemd сервіс Hermes Agent…"
SERVICE_FILE="/etc/systemd/system/hermes-agent.service"

sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Hermes Agent — Grant Scout
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME
Environment="PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=$HERMES_ENV
ExecStart=$HOME/.local/bin/hermes gateway
TimeoutStopSec=240
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermes-agent

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable hermes-agent
sudo systemctl restart hermes-agent
ok "Hermes Agent сервіс створено та запущено"

# ─────────────────────────────────────────────────────────────────────────────
# Підсумок
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}✅ Grant Scout успішно встановлено!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📁 Директорія скілу:  $GRANT_SCOUT_DIR"
echo "📊 Стан та логи:     $STATE_DIR"
echo ""
echo "🚀 Команди для тестування:"
echo "   python3 $GRANT_SCOUT_DIR/scripts/runner.py test"
echo "   python3 $GRANT_SCOUT_DIR/scripts/runner.py search"
echo "   python3 $GRANT_SCOUT_DIR/scripts/runner.py topics"
echo ""
echo "📋 Статус сервісу:"
echo "   systemctl status hermes-agent"
echo "   hermes cron list"
echo ""
