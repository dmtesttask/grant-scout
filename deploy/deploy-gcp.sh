#!/usr/bin/env bash
# =============================================================================
# deploy-gcp.sh — Головний скрипт розгортання Hermes Grant Scout на GCP
# =============================================================================
# Запускається з Google Cloud Shell всередині вашого GCP проєкту.
# ІДЕМПОТЕНТНИЙ: безпечний для повторного запуску.
#
# Використання:
#   ./deploy-gcp.sh              — повне розгортання або оновлення
#   ./deploy-gcp.sh --update     — тільки оновлення файлів
#   ./deploy-gcp.sh --status     — статус VM та Hermes
#   ./deploy-gcp.sh --destroy    — видалення VM (з підтвердженням)
#   ./deploy-gcp.sh --dry-run    — показати команди без виконання
# =============================================================================
set -euo pipefail

# ── Кольоровий вивід ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step() { echo -e "\n${CYAN}▶ $*${NC}"; }

# ── Режим dry-run ─────────────────────────────────────────────────────────────
DRY_RUN=false
UPDATE_ONLY=false
STATUS_ONLY=false
DESTROY=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN=true ;;
        --update)     UPDATE_ONLY=true ;;
        --status)     STATUS_ONLY=true ;;
        --destroy)    DESTROY=true ;;
    esac
done

run() {
    if $DRY_RUN; then
        echo -e "  ${YELLOW}[DRY-RUN]${NC} $*"
    else
        "$@"
    fi
}

# ── Конфігурація (беремо з .env або запитуємо) ───────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    err "Файл .env не знайдено в $SCRIPT_DIR\nСтворіть його з шаблону: cp $SCRIPT_DIR/.env.template $SCRIPT_DIR/.env"
fi

# shellcheck source=/dev/null
source "$ENV_FILE"

# Перевірка обов'язкових змінних
check_required() {
    local var="$1"
    local desc="$2"
    if [ -z "${!var:-}" ]; then
        err "Змінна $var не встановлена в .env\nОпис: $desc\nДив. docs/SETUP_APIS.md"
    fi
}

check_required "GCP_PROJECT_ID"   "ID вашого GCP проєкту"
check_required "TELEGRAM_BOT_TOKEN"  "Токен Telegram бота"
check_required "TELEGRAM_CHAT_ID"    "Ваш Telegram user ID"
check_required "NOTION_API_KEY"      "Notion Integration Token"
check_required "NOTION_PAGE_ID"      "ID батьківської сторінки Notion"
check_required "OPENROUTER_API_KEY"  "OpenRouter API Key"

VM_NAME="${GCP_VM_NAME:-grant-scout-vm}"
ZONE="${GCP_ZONE:-europe-west1-b}"
REGION="${ZONE%-*}"    # europe-west1
MACHINE_TYPE="e2-medium"
IMAGE_FAMILY="debian-12"
IMAGE_PROJECT="debian-cloud"
DISK_SIZE="20GB"

# ─────────────────────────────────────────────────────────────────────────────
# Перевірка gcloud
# ─────────────────────────────────────────────────────────────────────────────
if ! command -v gcloud &>/dev/null; then
    err "gcloud CLI не знайдено.\nВстановіть: https://cloud.google.com/sdk/docs/install\nАбо запустіть скрипт з Google Cloud Shell."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Режим: --status
# ─────────────────────────────────────────────────────────────────────────────
if $STATUS_ONLY; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Grant Scout — Статус"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Проєкт: $GCP_PROJECT_ID"
    echo "  VM:     $VM_NAME ($ZONE)"
    echo ""

    if gcloud compute instances describe "$VM_NAME" \
        --zone="$ZONE" --project="$GCP_PROJECT_ID" \
        --format="table(name,status,networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null; then
        IP=$(gcloud compute instances describe "$VM_NAME" \
            --zone="$ZONE" --project="$GCP_PROJECT_ID" \
            --format="get(networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null)
        echo ""
        echo "  SSH: gcloud compute ssh $VM_NAME --zone=$ZONE --project=$GCP_PROJECT_ID"
        echo "  IP:  $IP"
    else
        warn "VM '$VM_NAME' не знайдено або не запущено"
    fi
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Режим: --destroy
# ─────────────────────────────────────────────────────────────────────────────
if $DESTROY; then
    warn "⚠️  Ця дія видалить VM '$VM_NAME' та всі її дані!"
    read -r -p "Підтвердіть видалення (введіть 'delete'): " confirm
    if [ "$confirm" != "delete" ]; then
        log "Скасовано"
        exit 0
    fi
    run gcloud compute instances delete "$VM_NAME" \
        --zone="$ZONE" --project="$GCP_PROJECT_ID" --quiet
    ok "VM видалено"
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Заголовок
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   Hermes Grant Scout — Розгортання на GCP"
if $DRY_RUN; then echo "   [DRY-RUN MODE — команди не виконуються]"; fi
if $UPDATE_ONLY; then echo "   [РЕЖИМ ОНОВЛЕННЯ]"; fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Проєкт:      $GCP_PROJECT_ID"
echo "  VM:          $VM_NAME"
echo "  Зона:        $ZONE"
echo "  Тип машини:  $MACHINE_TYPE"
echo "  ОС:          $IMAGE_FAMILY ($IMAGE_PROJECT)"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Крок 0: Активація GCP API
# ─────────────────────────────────────────────────────────────────────────────
if ! $UPDATE_ONLY; then
    step "Крок 0/5: Активація необхідних GCP API…"
    REQUIRED_APIS=(
        "compute.googleapis.com"          # Compute Engine (створення VM)
        "iam.googleapis.com"             # IAM (сервісні акаунти)
        "cloudresourcemanager.googleapis.com"  # Управління проєктом
    )

    for api in "${REQUIRED_APIS[@]}"; do
        # Перевірити чи вже увімкнено
        STATUS=$(gcloud services list \
            --enabled \
            --filter="name:$api" \
            --project="$GCP_PROJECT_ID" \
            --format="value(name)" 2>/dev/null || echo "")

        if [ -n "$STATUS" ]; then
            ok "API вже увімкнено: $api"
        else
            log "Вмикаємо API: $api…"
            run gcloud services enable "$api" --project="$GCP_PROJECT_ID"
            ok "API увімкнено: $api"
        fi
    done
fi

# ─────────────────────────────────────────────────────────────────────────────
# Крок 1: Створення VM (якщо не існує)
# ─────────────────────────────────────────────────────────────────────────────
if ! $UPDATE_ONLY; then
    step "Крок 1/5: VM instance…"

    VM_STATUS=$(gcloud compute instances describe "$VM_NAME" \
        --zone="$ZONE" --project="$GCP_PROJECT_ID" \
        --format="value(status)" 2>/dev/null || echo "NOT_FOUND")

    if [ "$VM_STATUS" = "NOT_FOUND" ]; then
        log "Створення VM '$VM_NAME'…"
        run gcloud compute instances create "$VM_NAME" \
            --project="$GCP_PROJECT_ID" \
            --zone="$ZONE" \
            --machine-type="$MACHINE_TYPE" \
            --image-family="$IMAGE_FAMILY" \
            --image-project="$IMAGE_PROJECT" \
            --boot-disk-size="$DISK_SIZE" \
            --boot-disk-type="pd-standard" \
            --tags="grant-scout" \
            --metadata="enable-oslogin=true" \
            --scopes="https://www.googleapis.com/auth/cloud-platform"

        log "Очікуємо запуску VM (30 сек)…"
        $DRY_RUN || sleep 30
        ok "VM '$VM_NAME' створено"
    elif [ "$VM_STATUS" = "RUNNING" ]; then
        ok "VM '$VM_NAME' вже запущена (статус: $VM_STATUS)"
    elif [ "$VM_STATUS" = "TERMINATED" ]; then
        log "Запуск зупиненої VM…"
        run gcloud compute instances start "$VM_NAME" \
            --zone="$ZONE" --project="$GCP_PROJECT_ID"
        $DRY_RUN || sleep 15
        ok "VM запущено"
    else
        warn "VM статус: $VM_STATUS"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Крок 2: Firewall правила
# ─────────────────────────────────────────────────────────────────────────────
if ! $UPDATE_ONLY; then
    step "Крок 2/5: Firewall правила…"

    if ! gcloud compute firewall-rules describe "allow-ssh-grant-scout" \
        --project="$GCP_PROJECT_ID" &>/dev/null; then
        log "Створення firewall правила для SSH…"
        run gcloud compute firewall-rules create "allow-ssh-grant-scout" \
            --project="$GCP_PROJECT_ID" \
            --direction=INGRESS \
            --action=ALLOW \
            --rules=tcp:22 \
            --target-tags="grant-scout" \
            --source-ranges="0.0.0.0/0"
        ok "Firewall правило створено"
    else
        ok "Firewall правило вже існує"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Крок 3: Копіювання файлів на VM
# ─────────────────────────────────────────────────────────────────────────────
step "Крок 3/5: Копіювання файлів на VM…"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Створити тимчасовий tar-архів
TMP_TAR="/tmp/grant-scout-deploy.tar.gz"
log "Пакування файлів…"
tar -czf "$TMP_TAR" \
    -C "$PROJECT_ROOT" \
    --exclude=".git" \
    --exclude="*.pyc" \
    --exclude="__pycache__" \
    --exclude=".env" \
    skills/ deploy/ config.yaml

# Скопіювати архів на VM
run gcloud compute scp "$TMP_TAR" \
    "$VM_NAME:/tmp/grant-scout-deploy.tar.gz" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" \
    --strict-host-key-checking=no

# Скопіювати .env окремо (з правами 600)
run gcloud compute scp "$ENV_FILE" \
    "$VM_NAME:/tmp/.env.deploy" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" \
    --strict-host-key-checking=no

ok "Файли скопійовано"

# ─────────────────────────────────────────────────────────────────────────────
# Крок 4: Розпакування та запуск setup-vm.sh
# ─────────────────────────────────────────────────────────────────────────────
step "Крок 4/5: Налаштування на VM…"
run gcloud compute ssh "$VM_NAME" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" \
    --strict-host-key-checking=no \
    --command="
        set -e
        echo '>> Розпакування файлів…'
        mkdir -p ~/grant-scout-src
        tar -xzf /tmp/grant-scout-deploy.tar.gz -C ~/grant-scout-src
        mv /tmp/.env.deploy ~/grant-scout-src/deploy/.env
        chmod 600 ~/grant-scout-src/deploy/.env

        echo '>> Запуск setup-vm.sh…'
        chmod +x ~/grant-scout-src/deploy/setup-vm.sh
        bash ~/grant-scout-src/deploy/setup-vm.sh
    "

# ─────────────────────────────────────────────────────────────────────────────
# Крок 5: Верифікація
# ─────────────────────────────────────────────────────────────────────────────
step "Крок 5/5: Верифікація…"
if ! $DRY_RUN; then
    VERIFY=$(gcloud compute ssh "$VM_NAME" \
        --zone="$ZONE" --project="$GCP_PROJECT_ID" \
        --strict-host-key-checking=no \
        --command="
            echo '=== Hermes ===' && ~/.local/bin/hermes --version 2>/dev/null || echo 'hermes: не знайдено'
            echo '=== Python ===' && python3 --version
            echo '=== Cron ===' && crontab -l 2>/dev/null | grep grant-scout | wc -l
            echo '=== Сервіс ===' && systemctl is-active hermes-agent 2>/dev/null || true
        " 2>/dev/null || echo "Не вдалося підключитися до VM")
    echo "$VERIFY"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Підсумок
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}✅ Розгортання завершено!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  🔗 Підключення до VM:"
echo "     gcloud compute ssh $VM_NAME --zone=$ZONE --project=$GCP_PROJECT_ID"
echo ""
echo "  🧪 Тестовий запуск (після підключення до VM):"
echo "     python3 ~/grant-scout/scripts/runner.py test"
echo ""
echo "  📊 Управління:"
echo "     ./deploy-gcp.sh --status    # статус VM"
echo "     ./deploy-gcp.sh --update    # оновити файли"
echo ""
