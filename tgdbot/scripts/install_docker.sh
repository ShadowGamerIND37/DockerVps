#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  TGD Bot v11 — Docker Engine installer for Ubuntu 22.04 / 24.04
#
#  Installs Docker Engine from Docker's official apt repository, configures the
#  daemon (log rotation, live-restore, optional XFS project quotas) and adds
#  the service user to the docker group.
#
#  It never runs destructive cleanup commands: no `docker system prune`, no
#  volume deletion, no container removal.
# ─────────────────────────────────────────────────────────────────────────────
set -Eeuo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { printf "${GREEN}[docker]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[docker]${NC} %s\n" "$*"; }
fail()  { printf "${RED}[docker]${NC} %s\n" "$*" >&2; exit 1; }

ENABLE_PQUOTA="${ENABLE_PQUOTA:-0}"     # 1 = ask for XFS project quota guidance
DOCKER_GROUP="${DOCKER_GROUP:-docker}"
SERVICE_USER="${SERVICE_USER:-}"

# ── 1. operating system ──────────────────────────────────────────────────────
. /etc/os-release 2>/dev/null || fail "cannot read /etc/os-release"
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
  *) warn "Untested distribution ${PRETTY_NAME:-unknown}; continuing anyway" ;;
esac
info "detected ${PRETTY_NAME:-unknown} (${ID:-?} ${VERSION_ID:-?})"

if [[ $EUID -ne 0 ]]; then
  fail "run this script as root (sudo bash install_docker.sh)"
fi

export DEBIAN_FRONTEND=noninteractive

# ── 2. install Docker Engine ─────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  info "Docker Engine already installed: $(docker --version)"
else
  info "installing prerequisites"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg lsb-release

  info "adding the Docker apt repository"
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
      -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
fi

systemctl enable docker >/dev/null 2>&1 || true
systemctl start docker

# ── 3. daemon configuration ──────────────────────────────────────────────────
DAEMON_JSON=/etc/docker/daemon.json
if [[ ! -f "$DAEMON_JSON" ]]; then
  info "writing a baseline $DAEMON_JSON"
  mkdir -p /etc/docker
  cat > "$DAEMON_JSON" <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true,
  "default-address-pools": [
    { "base": "172.20.0.0/16", "size": 24 }
  ]
}
JSON
  systemctl restart docker
else
  info "$DAEMON_JSON already exists — leaving it untouched"
fi

# ── 4. verify daemon access ──────────────────────────────────────────────────
docker info >/dev/null 2>&1 || fail "the Docker daemon is not answering on /var/run/docker.sock"
info "daemon OK: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"

# ── 5. optional service user ────────────────────────────────────────────────
if [[ -n "$SERVICE_USER" ]] && id "$SERVICE_USER" >/dev/null 2>&1; then
  getent group "$DOCKER_GROUP" >/dev/null || groupadd "$DOCKER_GROUP"
  if id -nG "$SERVICE_USER" | grep -qw "$DOCKER_GROUP"; then
    info "$SERVICE_USER is already in the $DOCKER_GROUP group"
  else
    usermod -aG "$DOCKER_GROUP" "$SERVICE_USER"
    warn "$SERVICE_USER added to $DOCKER_GROUP — note that Docker API access is
       equivalent to root on this host. Use a dedicated service account."
  fi
fi

# ── 6. storage driver / quota capability report ──────────────────────────────
DRIVER="$(docker info --format '{{.Driver}}' 2>/dev/null || echo unknown)"
BACKING="$(docker info --format '{{index .DriverStatus 0 1}}' 2>/dev/null || echo unknown)"
ROOT_DIR="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
info "storage driver: $DRIVER (backing filesystem: $BACKING), data dir: $ROOT_DIR"

FS_TYPE="$(stat -f -c '%T' "$ROOT_DIR" 2>/dev/null || echo unknown)"
info "docker data filesystem: $FS_TYPE"

case "$FS_TYPE" in
  xfs)
    if grep -qE 'pquota|prjquota' /proc/mounts; then
      info "XFS project quotas are ENABLED — hard per-VPS disk limits will work"
    else
      warn "XFS detected but not mounted with 'pquota'."
      warn "Per-VPS disk limits will be MONITORED ONLY unless you remount, e.g.:"
      warn "  1. add 'pquota' to the mount options in /etc/fstab"
      warn "  2. reboot (XFS quota options cannot be changed with mount -o remount)"
      warn "  3. apt-get install -y xfsprogs"
    fi
    ;;
  btrfs)
    if btrfs qgroup show "$ROOT_DIR" >/dev/null 2>&1; then
      info "Btrfs quota groups are ENABLED"
    else
      warn "Btrfs detected but quota groups are off. Run:"
      warn "  btrfs quota enable $ROOT_DIR && btrfs quota rescan -w $ROOT_DIR"
    fi
    ;;
  *)
    warn "Filesystem '$FS_TYPE' cannot enforce hard disk quotas."
    warn "TGD Bot will report disk usage but must NOT advertise an enforced limit."
    warn "For real quotas put $ROOT_DIR on XFS (pquota) or Btrfs (qgroups)."
    ;;
esac

if [[ "${ENABLE_PQUOTA}" == "1" ]]; then
  apt-get install -y -qq xfsprogs || warn "could not install xfsprogs"
fi

# ── 7. summary ───────────────────────────────────────────────────────────────
echo
info "Docker Engine installation complete."
docker info --format '  server version : {{.ServerVersion}}
  api version    : {{.APIVersion}}
  storage driver : {{.Driver}}
  cgroup driver  : {{.CgroupDriver}} (v{{.CgroupVersion}})
  cpus / memory  : {{.NCPU}} / {{.MemTotal}}
  root dir       : {{.DockerRootDir}}' 2>/dev/null || true
echo
info "Next: bash install.sh   (installs the bot, migrates the database, sets up systemd)"
