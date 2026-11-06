#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  TGD Bot v11 installer — Ubuntu 22.04 / 24.04
#
#  1. detect the OS          7. configure permissions
#  2. check Python           8. back up the database
#  3. install dependencies   9. run the schema migration
#  4. verify Docker Engine  10. verify the application
#  5. verify daemon access  11. install the systemd unit
#  6. create directories    12. start the bot / print status
#
#  Nothing destructive is ever executed: no `docker system prune`, no volume
#  removal, and existing .env / database files are never overwritten.
# ─────────────────────────────────────────────────────────────────────────────
set -Eeuo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;36m'; NC='\033[0m'
info()  { printf "${GREEN}[install]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[install]${NC} %s\n" "$*"; }
fail()  { printf "${RED}[install]${NC} %s\n" "$*" >&2; exit 1; }
step()  { printf "\n${BLUE}── %s${NC}\n" "$*"; }

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-tgdbot}"
SERVICE_USER="${SERVICE_USER:-tgdbot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$APP_DIR/.venv"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
NO_START="${NO_START:-0}"

cd "$APP_DIR"
info "application directory: $APP_DIR"

# ── 1. operating system ──────────────────────────────────────────────────────
step "1/12  Operating system"
. /etc/os-release 2>/dev/null || true
info "${PRETTY_NAME:-unknown}"
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04|ubuntu:24.04) info "supported release" ;;
  *) warn "this installer is tested on Ubuntu 22.04 / 24.04" ;;
esac

# ── 2. Python ────────────────────────────────────────────────────────────────
step "2/12  Python"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python3 not found"
PY_VERSION="$($PYTHON_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "found Python $PY_VERSION"
$PYTHON_BIN - <<'PY' || fail "Python 3.10 or newer is required"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# ── 3. dependencies ──────────────────────────────────────────────────────────
step "3/12  Python dependencies"
if [[ ! -d "$VENV_DIR" ]]; then
  apt-get update -qq || true
  apt-get install -y -qq python3-venv python3-dev build-essential libffi-dev libssl-dev || true
  info "creating virtualenv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
info "installed: $("$VENV_DIR/bin/pip" freeze | wc -l) packages"

# ── 4/5. Docker Engine ───────────────────────────────────────────────────────
step "4/12  Docker Engine"
if [[ "$SKIP_DOCKER" == "1" ]]; then
  warn "SKIP_DOCKER=1 — not touching Docker"
elif ! command -v docker >/dev/null 2>&1; then
  info "Docker Engine is missing — running install_docker.sh"
  bash "$APP_DIR/tgdbot/scripts/install_docker.sh"
else
  info "Docker Engine present: $(docker --version)"
fi

step "5/12  Docker daemon access"
if docker info >/dev/null 2>&1; then
  info "daemon reachable: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
else
  warn "the Docker daemon is not reachable. Start it with: systemctl start docker"
fi

# ── 6. directories ───────────────────────────────────────────────────────────
step "6/12  Directories"
# Read only the four path keys from .env. Never source it: values such as
# HOST_MOTD are shell-looking data, not installer code. Explicit shell values
# retain precedence, and legacy /var/lib/unixnodes paths are preserved.
configured_path() {
  local key="$1" fallback="$2" current="${!1:-}"
  if [[ -n "$current" ]]; then
    printf '%s\n' "$current"
  else
    "$VENV_DIR/bin/python" "$APP_DIR/tgdbot/scripts/env_value.py" \
      --env-file "$APP_DIR/.env" --key "$key" --default "$fallback"
  fi
}
VPS_DATA_DIR="$(configured_path DOCKER_VPS_DATA_DIR /var/lib/tgdbot/vps)"
BACKUP_DIR="$(configured_path DOCKER_BACKUP_DIR /var/backups/tgdbot)"
RUNTIME_DIR="$(configured_path DOCKER_RUNTIME_DIR /var/lib/tgdbot/runtime)"
SECRET_DIR="$(configured_path DOCKER_SECRET_DIR /var/lib/tgdbot/secrets)"
for directory in "$VPS_DATA_DIR" "$BACKUP_DIR" "$RUNTIME_DIR" "$SECRET_DIR" \
                 "$APP_DIR/logs" "$APP_DIR/backups" "$APP_DIR/migration_reports"; do
  mkdir -p "$directory"
  chmod 700 "$directory" 2>/dev/null || true
  info "  $directory"
done

# ── 7. permissions ───────────────────────────────────────────────────────────
step "7/12  Permissions"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  info "service user '$SERVICE_USER' exists"
else
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
    && info "created system user '$SERVICE_USER'" \
    || warn "could not create '$SERVICE_USER' — adjust SERVICE_USER and re-run"
fi
if id "$SERVICE_USER" >/dev/null 2>&1; then
  getent group docker >/dev/null && usermod -aG docker "$SERVICE_USER" \
    && warn "'$SERVICE_USER' added to the docker group (Docker API == host root)"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR" "$VPS_DATA_DIR" "$BACKUP_DIR" \
    "$RUNTIME_DIR" "$SECRET_DIR" 2>/dev/null || true
  [[ -f "$APP_DIR/.env" ]] && chmod 600 "$APP_DIR/.env"
  [[ -f "$APP_DIR/.secret_key" ]] && chmod 600 "$APP_DIR/.secret_key"
fi

# ── 7b. environment file ─────────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  warn "created .env from .env.example — edit it and add your DISCORD_TOKEN"
else
  info ".env already exists — leaving it untouched"
fi

# ── 8. database backup ───────────────────────────────────────────────────────
step "8/12  Database backup"
DB_PATH="${DATABASE_PATH:-$APP_DIR/vps.db}"
if [[ -f "$DB_PATH" ]]; then
  mkdir -p "$APP_DIR/backups"
  STAMP="$(date +%Y%m%d_%H%M%S)"
  cp -a "$DB_PATH" "$APP_DIR/backups/pre_install_${STAMP}_$(basename "$DB_PATH")"
  info "backed up $DB_PATH → backups/pre_install_${STAMP}_$(basename "$DB_PATH")"
else
  info "no existing database — a fresh one will be created"
fi

# ── 9. migration ─────────────────────────────────────────────────────────────
step "9/12  Database migration"
"$VENV_DIR/bin/python" "$APP_DIR/tgdbot/scripts/migrate_database.py" \
  --database "$DB_PATH" --report-dir "$APP_DIR/migration_reports" || fail "migration failed"

# ── 10. verification ─────────────────────────────────────────────────────────
step "10/12  Application verification"
"$VENV_DIR/bin/python" -c "import bot; print('  bot.py imports cleanly')" \
  || fail "bot.py failed to import"
"$VENV_DIR/bin/python" "$APP_DIR/tgdbot/scripts/healthcheck.py" \
  --database "$DB_PATH" || warn "health check reported problems (see above)"

# ── 11. systemd ──────────────────────────────────────────────────────────────
step "11/12  systemd"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__PYTHON__|$VENV_DIR/bin/python|g" \
    "$APP_DIR/tgdbot/scripts/tgdbot.service" > "$UNIT"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
info "installed $UNIT"

# ── 12. start ────────────────────────────────────────────────────────────────
step "12/12  Start"
if [[ "$NO_START" == "1" ]]; then
  warn "NO_START=1 — not starting the service"
else
  systemctl restart "$SERVICE_NAME" && sleep 2
  systemctl --no-pager --lines=10 status "$SERVICE_NAME" || true
fi

echo
info "Installation finished."
cat <<EOF

  start / stop      systemctl start|stop|restart $SERVICE_NAME
  logs              journalctl -u $SERVICE_NAME -f
                    tail -f $APP_DIR/logs/bot.log
  health check      $VENV_DIR/bin/python $APP_DIR/tgdbot/scripts/healthcheck.py
  acceptance tests  $VENV_DIR/bin/python $APP_DIR/tgdbot/scripts/acceptance_test.py
  migration report  ls $APP_DIR/migration_reports

  Edit $APP_DIR/.env, then restart the service.
EOF
