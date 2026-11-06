#!/usr/bin/env bash
# TGD Bot interactive installer, migration runner and automatic updater.
#
# Main menu:
#   1) Install the bot files and start/restart its service
#   2) Run the idempotent database migration
#   3) Manage the fast-forward-only automatic updater
#
# The script never sources .env. Blank answers during installation leave the
# corresponding setting untouched, which is safe for both upgrades and fresh
# installations. No Docker cleanup, volume removal or forced Git reset is done.
set -Eeuo pipefail

# Color is useful on a terminal but noisy in pipes, CI and systemd logs.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  BLUE='\033[0;36m'
  BOLD='\033[1m'
  NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; NC=''
fi

info() { printf '%b[OK]%b %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%b[WARN]%b %s\n' "$YELLOW" "$NC" "$*"; }
die()  { printf '%b[ERROR]%b %s\n' "$RED" "$NC" "$*" >&2; exit 1; }
step() { printf '\n%b==> %s%b\n' "$BLUE" "$*" "$NC"; }

show_banner() {
  printf '%b' "$BLUE"
  cat <<'EOF'
  _____   ____   ____    ____    ___   _____
 |_   _| / ___| |  _ \  | __ )  / _ \ |_   _|
   | |  | |  _  | | | | |  _ \ | | | |  | |
   | |  | |_| | | |_| | | |_) | | |_| |  | |
   |_|   \____| |____/  |____/  \___/   |_|
EOF
  printf '%b' "$NC"
  printf '  %bTGD BOT%b | Docker VPS Hosting\n' "$BOLD" "$NC"
  printf '  Installer / Migration / Auto Updater\n'
  printf '  %s\n' "$APP_DIR"
}

trap 'printf "\n"; warn "cancelled by user"; exit 130' INT

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-tgdbot}"
SERVICE_USER="${SERVICE_USER:-tgdbot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
DB_PATH="${DATABASE_PATH:-$APP_DIR/vps.db}"
UPDATE_UNIT_NAME="${UPDATE_UNIT_NAME:-tgdbot-auto-update}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
GIT_BIN="${GIT_BIN:-git}"
AUTO_UPDATE_INTERVAL="${AUTO_UPDATE_INTERVAL:-1h}"

cd "$APP_DIR"
APP_DIR="$(pwd)"

usage() {
  cat <<EOF
TGD Bot installer

Usage:
  bash installer.sh                 Open the three-option interactive menu
  bash installer.sh --install       Prompt for settings, install and run
  bash installer.sh --migrate       Run the database migration
  bash installer.sh --auto-update   Check for and apply an update now
  bash installer.sh --enable-auto-update
                                    Enable the systemd update timer
  bash installer.sh --disable-auto-update
                                    Disable the timer and remove its units

During --install, press Enter at any prompt to keep that .env value unchanged.
The automatic updater uses git pull --ff-only and refuses a dirty worktree.
EOF
}

pause() {
  # Do not consume scripted input or hang a non-interactive invocation.
  if [[ -t 0 ]]; then
    printf '\nPress Enter to return to the menu...'
    IFS= read -r _ || true
  fi
}

python_for_bot() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    printf '%s\n' "$VENV_DIR/bin/python"
  elif command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    command -v "$PYTHON_BIN"
  else
    die "Python was not found. Run option 1 first or set PYTHON_BIN."
  fi
}

ensure_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    info "keeping existing $ENV_FILE"
    return 0
  fi
  [[ -f "$APP_DIR/.env.example" ]] || die ".env.example is missing from $APP_DIR"
  mkdir -p "$(dirname "$ENV_FILE")"
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  info "created $ENV_FILE from .env.example"
}

# Update exactly one key without executing .env or changing unrelated settings.
# Values are passed through the environment rather than interpolated into shell
# code. Newlines are rejected so a prompt can never add another setting.
set_env_value() {
  local key="$1"
  local value="$2"
  [[ -n "$value" ]] || return 0
  ENV_FILE="$ENV_FILE" ENV_KEY="$key" ENV_VALUE="$value" "$PYTHON_BIN" - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
key = os.environ["ENV_KEY"]
value = os.environ["ENV_VALUE"]
if "\n" in value or "\r" in value:
    raise SystemExit(f"{key} cannot contain a newline")

text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
for index, line in enumerate(lines):
    if line.lstrip().startswith("#") or not pattern.match(line):
        continue
    ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
    lines[index] = f"{key}={value}{ending}"
    break
else:
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.append(f"{key}={value}\n")

path.write_text("".join(lines), encoding="utf-8")
try:
    path.chmod(0o600)
except OSError:
    pass
PY
  info "updated $key"
}

prompt_install_values() {
  local token_input admin_input owner_input

  step "Bot configuration"
  cat <<'EOF'
  Enter a new value or press Enter to keep the current .env setting.
  The token is hidden while you type. Owner IDs may be comma separated.
EOF
  printf '  [1/3] Bot token: '
  IFS= read -r -s token_input || token_input=""
  printf '\n'
  printf '  [2/3] Admin ID / MAIN_ADMIN_ID: '
  IFS= read -r admin_input || admin_input=""
  printf '  [3/3] Owner ID(s) / OWNER_IDS: '
  IFS= read -r owner_input || owner_input=""

  if [[ -n "$token_input" ]]; then
    set_env_value DISCORD_TOKEN "$token_input"
  else
    info "DISCORD_TOKEN unchanged"
  fi
  if [[ -n "$admin_input" ]]; then
    set_env_value MAIN_ADMIN_ID "$admin_input"
  else
    info "MAIN_ADMIN_ID unchanged"
  fi
  if [[ -n "$owner_input" ]]; then
    set_env_value OWNER_IDS "$owner_input"
  else
    info "OWNER_IDS unchanged"
  fi
}

install_and_run() {
  step "Install bot files and run"
  ensure_env_file
  prompt_install_values
  [[ -f "$APP_DIR/tgdbot/scripts/install.sh" ]] || die "tgdbot/scripts/install.sh is missing"
  info "running the existing bot installer"
  APP_DIR="$APP_DIR" SERVICE_NAME="$SERVICE_NAME" SERVICE_USER="$SERVICE_USER" \
    PYTHON_BIN="$PYTHON_BIN" NO_START="${NO_START:-0}" \
    bash "$APP_DIR/tgdbot/scripts/install.sh"
  info "installation finished; the bot service was started unless NO_START=1 was set"
}

run_migration() {
  step "Database migration"
  local python
  python="$(python_for_bot)"
  [[ -f "$APP_DIR/tgdbot/scripts/migrate_database.py" ]] || die "migration runner is missing"
  mkdir -p "$APP_DIR/migration_reports"
  info "database: $DB_PATH"
  "$python" "$APP_DIR/tgdbot/scripts/migrate_database.py" \
    --database "$DB_PATH" --report-dir "$APP_DIR/migration_reports"
  info "migration completed (the runner creates a backup before changes)"
}

run_auto_update() {
  step "Auto Updater — check now"
  command -v "$GIT_BIN" >/dev/null 2>&1 || die "git is not installed"
  [[ -d "$APP_DIR/.git" ]] || die "$APP_DIR is not a Git checkout; automatic updates are unavailable"

  local status before after python
  # The systemd timer runs as root while install.sh normally makes the app
  # owned by its service user. Trust only this exact checkout for these Git
  # commands; do not change root's global Git configuration.
  status="$("$GIT_BIN" -c safe.directory="$APP_DIR" -C "$APP_DIR" status --porcelain)"
  if [[ -n "$status" ]]; then
    die "the worktree has local or untracked changes; commit/stash them before updating"
  fi

  before="$("$GIT_BIN" -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse HEAD)"
  info "checking the configured Git upstream (fast-forward only)"
  "$GIT_BIN" -c safe.directory="$APP_DIR" -C "$APP_DIR" pull --ff-only
  after="$("$GIT_BIN" -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse HEAD)"
  if [[ "$before" == "$after" ]]; then
    info "already up to date; no restart is needed"
    return 0
  fi

  python="$(python_for_bot)"
  info "update $before → $after; installing Python dependencies"
  "$python" -m pip install --quiet -r "$APP_DIR/requirements.txt"
  run_migration

  command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || die "update installed, but systemctl is unavailable; restart $SERVICE_NAME manually"
  info "restarting $SERVICE_NAME"
  "$SYSTEMCTL_BIN" restart "$SERVICE_NAME"
  info "automatic update completed"
}

write_update_units() {
  local bash_bin
  bash_bin="$(command -v bash)"
  cat > "$SYSTEMD_DIR/$UPDATE_UNIT_NAME.service" <<EOF
[Unit]
Description=TGD Bot automatic updater
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
ExecStart=$bash_bin $APP_DIR/installer.sh --auto-update
TimeoutStartSec=30min

[Install]
WantedBy=multi-user.target
EOF
  cat > "$SYSTEMD_DIR/$UPDATE_UNIT_NAME.timer" <<EOF
[Unit]
Description=Check for TGD Bot updates automatically

[Timer]
OnBootSec=10min
OnUnitActiveSec=$AUTO_UPDATE_INTERVAL
RandomizedDelaySec=60
Unit=$UPDATE_UNIT_NAME.service

[Install]
WantedBy=timers.target
EOF
  chmod 644 "$SYSTEMD_DIR/$UPDATE_UNIT_NAME.service" "$SYSTEMD_DIR/$UPDATE_UNIT_NAME.timer"
}

check_systemd_access() {
  [[ $EUID -eq 0 ]] && return 0
  if [[ -e "$SYSTEMD_DIR" ]]; then
    [[ -w "$SYSTEMD_DIR" ]] || die "run with sudo to manage the systemd updater timer"
  elif [[ ! -w "$(dirname "$SYSTEMD_DIR")" ]]; then
    die "run with sudo to manage the systemd updater timer"
  fi
}

enable_auto_update() {
  step "Enable automatic updates"
  [[ -d "$APP_DIR/.git" ]] || die "$APP_DIR is not a Git checkout; automatic updates are unavailable"
  command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || die "systemctl is not available"
  check_systemd_access
  mkdir -p "$SYSTEMD_DIR"
  write_update_units
  "$SYSTEMCTL_BIN" daemon-reload
  "$SYSTEMCTL_BIN" enable --now "$UPDATE_UNIT_NAME.timer"
  info "automatic updates enabled: $UPDATE_UNIT_NAME.timer ($AUTO_UPDATE_INTERVAL)"
  info "the updater will skip a dirty worktree and will never force-reset local changes"
}

disable_auto_update() {
  step "Disable automatic updates"
  command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || die "systemctl is not available"
  check_systemd_access
  "$SYSTEMCTL_BIN" disable --now "$UPDATE_UNIT_NAME.timer" 2>/dev/null || \
    warn "the updater timer was not active"
  rm -f "$SYSTEMD_DIR/$UPDATE_UNIT_NAME.service" "$SYSTEMD_DIR/$UPDATE_UNIT_NAME.timer"
  "$SYSTEMCTL_BIN" daemon-reload
  info "automatic updates disabled"
}

auto_update_menu() {
  local choice
  while true; do
    step "Auto Updater"
    printf '  Schedule: %s\n' "$AUTO_UPDATE_INTERVAL"
    cat <<'EOF'
  +--------------------------------------------------+
  |  AUTO UPDATER                                    |
  +--------------------------------------------------+
  |  1) Check for updates and apply now              |
  |     Fast-forward Git, migrate and restart        |
  |  2) Enable automatic updates                     |
  |     Use the systemd timer for scheduled checks   |
  |  3) Disable automatic updates                    |
  |     Stop the timer and remove its units          |
  |  0) Back                                         |
  +--------------------------------------------------+
EOF
    printf '  Select an option [0-3]: '
    if ! IFS= read -r choice; then
      printf '\n'
      return 0
    fi
    case "$choice" in
      1) run_auto_update; pause ;;
      2) enable_auto_update; pause ;;
      3) disable_auto_update; pause ;;
      0|b|B|back) return 0 ;;
      *) warn "choose 1, 2, 3 or 0" ;;
    esac
  done
}

main_menu() {
  local choice
  while true; do
    step "TGD Bot Installer"
    cat <<'EOF'
  +--------------------------------------------------+
  |  MAIN MENU                                       |
  +--------------------------------------------------+
  |  1) Install Files Of Bot And Run                 |
  |     Configure .env, install and start the bot    |
  |  2) Migration                                    |
  |     Back up and upgrade the database schema      |
  |  3) Auto Updater                                 |
  |     Check updates or manage the update timer     |
  |  0) Exit                                         |
  +--------------------------------------------------+
EOF
    printf '  Select an option [0-3]: '
    if ! IFS= read -r choice; then
      printf '\n'
      return 0
    fi
    case "$choice" in
      1) install_and_run; pause ;;
      2) run_migration; pause ;;
      3) auto_update_menu ;;
      0|q|Q|exit) info "bye"; return 0 ;;
      *) warn "choose 1, 2, 3 or 0" ;;
    esac
  done
}

case "${1:-}" in
  ""|--menu) show_banner; main_menu ;;
  --install) install_and_run ;;
  --migrate|--migration) run_migration ;;
  --auto-update|--run-auto-update) run_auto_update ;;
  --enable-auto-update) enable_auto_update ;;
  --disable-auto-update) disable_auto_update ;;
  --help|-h) usage ;;
  *) die "unknown option '$1' (use --help for usage)" ;;
esac
