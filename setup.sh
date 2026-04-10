#!/usr/bin/env bash
# Kodi Telegram Bot — interactive installer and updater.
#
# Safe to run multiple times. Detects existing installations and offers
# merge / overwrite / skip for the .env file.
#
# Usage:
#   ./setup.sh              Full interactive install
#   ./setup.sh --update     Pull latest code, sync deps, restart service
#   ./setup.sh --help       Show this help
set -euo pipefail

# ── Constants ──

REPO_URL="https://github.com/shemekhe/kodi-telegram-bot.git"
SERVICE_NAME="kodi-telegram-bot"

# ── Colours (disabled when stdout is not a terminal) ──

if [ -t 1 ]; then
    GREEN='\033[0;32m' YELLOW='\033[1;33m' RED='\033[0;31m'
    BOLD='\033[1m' NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BOLD='' NC=''
fi

info()  { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
error() { printf "${RED}✗${NC} %s\n" "$1"; }
header(){ printf "\n${BOLD}── %s ──${NC}\n\n" "$1"; }

# ── Helpers ──

need_cmd() { command -v "$1" &>/dev/null; }

prompt() {
    local var="$1" text="$2" default="${3:-}"
    if [ -n "$default" ]; then
        printf "  %s [%s]: " "$text" "$default"
    else
        printf "  %s: " "$text"
    fi
    read -r value
    value="${value:-$default}"
    eval "$var=\"\$value\""
}

prompt_secret() {
    local var="$1" text="$2" default="${3:-}"
    if [ -n "$default" ]; then
        printf "  %s [%s]: " "$text" "$default"
    else
        printf "  %s: " "$text"
    fi
    read -rs value
    echo
    value="${value:-$default}"
    eval "$var=\"\$value\""
}

confirm() {
    local text="$1"
    printf "  %s [Y/n]: " "$text"
    read -r reply
    [[ "${reply:-y}" =~ ^[Yy]$ ]]
}

# ── Detect project directory ──

detect_project_dir() {
    if [ -f "main.py" ] && [ -f "config.py" ] && [ -f "pyproject.toml" ]; then
        PROJECT_DIR="$(pwd)"
    elif [ -f "$HOME/$SERVICE_NAME/main.py" ]; then
        PROJECT_DIR="$HOME/$SERVICE_NAME"
    else
        PROJECT_DIR="$HOME/$SERVICE_NAME"
    fi
}

# ── Install system dependencies ──

install_deps() {
    header "System dependencies"
    local missing=()
    need_cmd python3 || missing+=(python3)
    need_cmd git     || missing+=(git)
    if [ ${#missing[@]} -eq 0 ]; then
        info "python3 and git are installed"
        return
    fi
    warn "Installing: ${missing[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${missing[@]}"
    info "Installed ${missing[*]}"
}

# ── Install uv ──

install_uv() {
    header "Package manager (uv)"
    if need_cmd uv; then
        info "uv is installed ($(uv --version 2>/dev/null || echo 'unknown version'))"
        return
    fi
    warn "Installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    info "uv installed"
}

# ── Clone or verify repo ──

setup_repo() {
    header "Project repository"
    if [ -f "$PROJECT_DIR/main.py" ]; then
        info "Project found at $PROJECT_DIR"
    else
        warn "Cloning into $PROJECT_DIR…"
        git clone "$REPO_URL" "$PROJECT_DIR"
        info "Cloned"
    fi
    cd "$PROJECT_DIR"
}

# ── Sync dependencies ──

sync_deps() {
    header "Python dependencies"
    uv sync
    info "Dependencies synced"
}

# ── Interactive .env ──

write_env_file() {
    local api_id api_hash bot_token
    local kodi_url kodi_user kodi_pass kodi_start_cmd
    local download_dir organize allowed

    header "Configuration (.env)"
    echo "  Required Telegram credentials (from my.telegram.org and @BotFather):"
    echo

    prompt       api_id      "Telegram API ID"          "${EXISTING_TELEGRAM_API_ID:-}"
    prompt       api_hash    "Telegram API Hash"         "${EXISTING_TELEGRAM_API_HASH:-}"
    prompt_secret bot_token  "Telegram Bot Token"        "${EXISTING_TELEGRAM_BOT_TOKEN:-}"
    echo

    prompt kodi_url       "Kodi JSON-RPC URL"                       "${EXISTING_KODI_URL:-http://localhost:8080/jsonrpc}"
    prompt kodi_user      "Kodi HTTP username"                      "${EXISTING_KODI_USERNAME:-kodi}"
    prompt_secret kodi_pass "Kodi HTTP password"                    "${EXISTING_KODI_PASSWORD:-}"
    prompt kodi_start_cmd "Kodi start command (blank to skip)"      "${EXISTING_KODI_START_CMD:-sudo systemctl start kodi}"
    echo

    prompt download_dir "Download directory"                        "${EXISTING_DOWNLOAD_DIR:-~/Downloads}"
    prompt organize     "Organize media into folders? (1=yes, 0=no)" "${EXISTING_ORGANIZE_MEDIA:-1}"
    prompt allowed      "Allowed users (comma-separated IDs/usernames, blank=open)" "${EXISTING_ALLOWED_USERS:-}"
    echo

    local max_retry="$EXISTING_MAX_RETRY_ATTEMPTS"
    local max_concurrent="$EXISTING_MAX_CONCURRENT_DOWNLOADS"
    local min_disk="$EXISTING_MIN_FREE_DISK_MB"
    local disk_warn="$EXISTING_DISK_WARNING_MB"
    local mem_warn="$EXISTING_MEMORY_WARNING_PERCENT"
    local log_file="$EXISTING_LOG_FILE"
    local log_level="$EXISTING_LOG_LEVEL"
    local log_max="$EXISTING_LOG_MAX_MB"

    if confirm "Configure advanced settings (retries, thresholds, logging)?"; then
        prompt max_retry      "Max retry attempts"          "$max_retry"
        prompt max_concurrent "Max concurrent downloads"    "$max_concurrent"
        prompt min_disk       "Min free disk MB"            "$min_disk"
        prompt disk_warn      "Disk warning MB"             "$disk_warn"
        prompt mem_warn       "Memory warning percent (0=off)" "$mem_warn"
        prompt log_file       "Log file path"               "$log_file"
        prompt log_level      "Log level (DEBUG/INFO/WARNING/ERROR)" "$log_level"
        prompt log_max        "Max log file size MB"        "$log_max"
    fi

    cat > .env << EOF
# Telegram API / Bot details
TELEGRAM_API_ID=$api_id
TELEGRAM_API_HASH=$api_hash
TELEGRAM_BOT_TOKEN=$bot_token

# Kodi configuration
KODI_URL=$kodi_url
KODI_USERNAME=$kodi_user
KODI_PASSWORD=$kodi_pass

# File system
DOWNLOAD_DIR=$download_dir
ORGANIZE_MEDIA=$organize

# Kodi restart
KODI_START_CMD=$kodi_start_cmd

# Access control
ALLOWED_USERS=$allowed

# Limits
MAX_RETRY_ATTEMPTS=$max_retry
MAX_CONCURRENT_DOWNLOADS=$max_concurrent

# Resource thresholds
MIN_FREE_DISK_MB=$min_disk
DISK_WARNING_MB=$disk_warn
MEMORY_WARNING_PERCENT=$mem_warn

# Logging
LOG_FILE=$log_file
LOG_LEVEL=$log_level
LOG_MAX_MB=$log_max
EOF
    info ".env written"
}

load_existing_env() {
    EXISTING_TELEGRAM_API_ID=""
    EXISTING_TELEGRAM_API_HASH=""
    EXISTING_TELEGRAM_BOT_TOKEN=""
    EXISTING_KODI_URL="http://localhost:8080/jsonrpc"
    EXISTING_KODI_USERNAME="kodi"
    EXISTING_KODI_PASSWORD=""
    EXISTING_KODI_START_CMD=""
    EXISTING_DOWNLOAD_DIR="~/Downloads"
    EXISTING_ORGANIZE_MEDIA="1"
    EXISTING_ALLOWED_USERS=""
    EXISTING_MAX_RETRY_ATTEMPTS="3"
    EXISTING_MAX_CONCURRENT_DOWNLOADS="5"
    EXISTING_MIN_FREE_DISK_MB="200"
    EXISTING_DISK_WARNING_MB="500"
    EXISTING_MEMORY_WARNING_PERCENT="90"
    EXISTING_LOG_FILE="bot.log"
    EXISTING_LOG_LEVEL="INFO"
    EXISTING_LOG_MAX_MB="200"

    [ -f .env ] || return 0
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ ]] && continue
        [[ -z "$key" ]] && continue
        key="$(echo "$key" | xargs)"
        value="$(echo "$value" | sed 's/^["'\'']//' | sed 's/["'\'']*$//')"
        case "$key" in
            TELEGRAM_API_ID)           EXISTING_TELEGRAM_API_ID="$value" ;;
            TELEGRAM_API_HASH)         EXISTING_TELEGRAM_API_HASH="$value" ;;
            TELEGRAM_BOT_TOKEN)        EXISTING_TELEGRAM_BOT_TOKEN="$value" ;;
            KODI_URL)                  EXISTING_KODI_URL="$value" ;;
            KODI_USERNAME)             EXISTING_KODI_USERNAME="$value" ;;
            KODI_PASSWORD)             EXISTING_KODI_PASSWORD="$value" ;;
            KODI_START_CMD)            EXISTING_KODI_START_CMD="$value" ;;
            DOWNLOAD_DIR)              EXISTING_DOWNLOAD_DIR="$value" ;;
            ORGANIZE_MEDIA)            EXISTING_ORGANIZE_MEDIA="$value" ;;
            ALLOWED_USERS)             EXISTING_ALLOWED_USERS="$value" ;;
            MAX_RETRY_ATTEMPTS)        EXISTING_MAX_RETRY_ATTEMPTS="$value" ;;
            MAX_CONCURRENT_DOWNLOADS)  EXISTING_MAX_CONCURRENT_DOWNLOADS="$value" ;;
            MIN_FREE_DISK_MB)          EXISTING_MIN_FREE_DISK_MB="$value" ;;
            DISK_WARNING_MB)           EXISTING_DISK_WARNING_MB="$value" ;;
            MEMORY_WARNING_PERCENT)    EXISTING_MEMORY_WARNING_PERCENT="$value" ;;
            LOG_FILE)                  EXISTING_LOG_FILE="$value" ;;
            LOG_LEVEL)                 EXISTING_LOG_LEVEL="$value" ;;
            LOG_MAX_MB)                EXISTING_LOG_MAX_MB="$value" ;;
        esac
    done < .env
}

setup_env() {
    load_existing_env
    if [ -f .env ]; then
        echo
        echo "  .env already exists. Choose an action:"
        echo "    [m] Merge — keep existing values, prompt for missing ones"
        echo "    [o] Overwrite — re-enter all values from scratch"
        echo "    [s] Skip — leave .env as-is"
        printf "  Choice [m/o/s]: "
        read -r choice
        case "${choice:-m}" in
            m|M) write_env_file ;;
            o|O) load_existing_env </dev/null; write_env_file ;;
            s|S) info "Skipping .env"; return ;;
            *)   warn "Invalid choice, skipping .env"; return ;;
        esac
    else
        write_env_file
    fi
}

# ── systemd user service ──

setup_service() {
    header "Systemd user service"

    local svc_dir="$HOME/.config/systemd/user"
    local svc_file="$svc_dir/$SERVICE_NAME.service"
    mkdir -p "$svc_dir"

    cat > "$svc_file" << EOF
[Unit]
Description=Kodi Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    info "Service file written to $svc_file"

    systemctl --user daemon-reload

    if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "yes"; then
        warn "Enabling linger so the bot starts at boot (requires sudo)…"
        sudo loginctl enable-linger "$USER"
        info "Linger enabled"
    else
        info "Linger already enabled"
    fi

    systemctl --user enable "$SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    info "Service enabled and started"
    echo
    systemctl --user status "$SERVICE_NAME" --no-pager || true
}

# ── Sudoers rule for KODI_START_CMD ──

setup_sudoers() {
    header "Kodi restart permissions"

    local kodi_cmd
    kodi_cmd="$(grep -E '^KODI_START_CMD=' .env 2>/dev/null | head -1 | cut -d= -f2-)"

    if [ -z "$kodi_cmd" ] || ! echo "$kodi_cmd" | grep -q "^sudo "; then
        info "KODI_START_CMD does not use sudo — skipping sudoers setup"
        return
    fi

    local real_cmd="${kodi_cmd#sudo }"
    local bin_path
    bin_path="$(echo "$real_cmd" | awk '{print $1}')"
    local abs_bin
    abs_bin="$(command -v "$bin_path" 2>/dev/null || echo "$bin_path")"
    local sudoers_line="$USER ALL=(ALL) NOPASSWD: ${abs_bin} ${real_cmd#"$bin_path"}"
    sudoers_line="$(echo "$sudoers_line" | sed 's/  */ /g; s/ *$//')"

    local sudoers_file="/etc/sudoers.d/kodi-bot"
    if [ -f "$sudoers_file" ] && grep -qF "$real_cmd" "$sudoers_file" 2>/dev/null; then
        info "Sudoers rule already exists"
        return
    fi

    echo
    echo "  The /restart_kodi command needs passwordless sudo for:"
    echo "    $real_cmd"
    echo
    if confirm "Create sudoers rule? (requires sudo)"; then
        echo "$sudoers_line" | sudo tee "$sudoers_file" >/dev/null
        sudo chmod 0440 "$sudoers_file"
        if sudo visudo -cf "$sudoers_file" >/dev/null 2>&1; then
            info "Sudoers rule created at $sudoers_file"
        else
            error "Sudoers syntax check failed — removing file"
            sudo rm -f "$sudoers_file"
        fi
    else
        warn "Skipped. /restart_kodi may prompt for a password or fail."
    fi
}

# ── Update mode ──

do_update() {
    header "Updating $SERVICE_NAME"
    detect_project_dir
    cd "$PROJECT_DIR"
    git pull
    uv sync
    systemctl --user restart "$SERVICE_NAME"
    info "Updated and restarted"
    echo
    systemctl --user status "$SERVICE_NAME" --no-pager || true
}

# ── Usage ──

usage() {
    cat << 'EOF'
Kodi Telegram Bot — setup script

Usage:
  ./setup.sh              Interactive install (safe to re-run)
  ./setup.sh --update     Pull latest code, sync deps, restart service
  ./setup.sh --help       Show this help

The installer will:
  1. Install system dependencies (python3, git) if missing
  2. Install uv package manager if missing
  3. Clone the repo or use the current directory
  4. Sync Python dependencies
  5. Walk you through .env configuration
  6. Create and enable a systemd user service
EOF
}

# ── Main ──

case "${1:-}" in
    --update|-u) do_update ;;
    --help|-h)   usage ;;
    *)
        header "Kodi Telegram Bot — Setup"
        detect_project_dir
        install_deps
        install_uv
        setup_repo
        sync_deps
        setup_env
        setup_service
        setup_sudoers
        echo
        info "Setup complete! The bot is running."
        echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
        echo "  Stop:    systemctl --user stop $SERVICE_NAME"
        echo "  Restart: systemctl --user restart $SERVICE_NAME"
        echo "  Update:  ./setup.sh --update"
        ;;
esac
