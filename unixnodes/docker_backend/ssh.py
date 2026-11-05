"""
Guest initialization: OpenSSH installation, host keys, the root credential and
the container supervisor script.

Design notes
------------
Docker containers share the host kernel and have **no systemd**.  Pretending
otherwise produces VPS that cannot reboot their services.  Instead every
container runs a tiny POSIX supervisor as PID 1 (backed by ``docker init`` for
zombie reaping) which:

1. installs OpenSSH *once* — the result lands on the persistent volume, so a
   restart never re-downloads packages;
2. generates and keeps host keys (they live in ``/etc/ssh`` on the volume);
3. writes a deterministic ``sshd`` configuration;
4. applies the root credential from a read-only-until-used file and then
   truncates it, so no plaintext password ever survives on disk;
5. starts cron where the distribution ships it;
6. ``exec``s ``sshd -D`` so signals reach sshd directly and shutdown is clean.

The root password is **never** passed through an environment variable, a
command argument or the Docker inspect metadata — it travels as a file on a
bind mount and is destroyed after use.
"""

from __future__ import annotations

import logging
import os
import socket
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import config
from ..errors import SSHSetupError
from . import containers

logger = logging.getLogger("unixnodes.docker.ssh")

INIT_FILENAME = "unixnodes-init"

INIT_SCRIPT = r"""#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
#  UnixNodes container supervisor (PID 1)
#  Installed read-only by the platform. Do not edit.
# ─────────────────────────────────────────────────────────────────────────────
set -u

LOG=/var/log/unixnodes-init.log
CRED=/run/unixnodes/root.credential
STATE_DIR=/etc/unixnodes
MARKER="$STATE_DIR/.ssh-initialized"
SSHD_CONFIG=/etc/ssh/sshd_config.unixnodes

mkdir -p "$STATE_DIR" 2>/dev/null || true

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo -)" "$*" \
        >> "$LOG" 2>/dev/null || true
}

log "supervisor start (uid=$(id -u 2>/dev/null || echo ?))"

# ── 1. install OpenSSH (only ever needed on the very first boot) ────────────
install_openssh() {
    command -v sshd >/dev/null 2>&1 && return 0
    export DEBIAN_FRONTEND=noninteractive
    if command -v apt-get >/dev/null 2>&1; then
        log "apt-get: installing openssh-server"
        apt-get update -qq >> "$LOG" 2>&1 || true
        apt-get install -y --no-install-recommends \
            openssh-server ca-certificates cron procps iproute2 net-tools sudo \
            >> "$LOG" 2>&1 || true
        mkdir -p /run/sshd
    elif command -v apk >/dev/null 2>&1; then
        log "apk: installing openssh"
        apk add --no-cache openssh openssh-server-pam cronie procps iproute2 \
            bash sudo shadow >> "$LOG" 2>&1 || true
        mkdir -p /run/sshd /var/empty
    elif command -v dnf >/dev/null 2>&1; then
        log "dnf: installing openssh-server"
        dnf install -y openssh-server openssh-clients cronie procps-ng \
            iproute sudo >> "$LOG" 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        log "yum: installing openssh-server"
        yum install -y openssh-server openssh-clients cronie procps-ng \
            iproute sudo >> "$LOG" 2>&1 || true
    else
        log "no supported package manager found"
        return 1
    fi
    command -v sshd >/dev/null 2>&1
}

# ── 2. host keys ────────────────────────────────────────────────────────────
ensure_host_keys() {
    mkdir -p /etc/ssh
    if [ ! -s /etc/ssh/ssh_host_ed25519_key ] && [ ! -s /etc/ssh/ssh_host_rsa_key ]; then
        log "generating host keys"
        ssh-keygen -A >> "$LOG" 2>&1 || true
    fi
    if [ ! -s /etc/ssh/ssh_host_ed25519_key ] && [ ! -s /etc/ssh/ssh_host_rsa_key ]; then
        ssh-keygen -t rsa -b 3072 -N '' -f /etc/ssh/ssh_host_rsa_key >> "$LOG" 2>&1 || true
        ssh-keygen -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key >> "$LOG" 2>&1 || true
    fi
    chmod 600 /etc/ssh/ssh_host_*key 2>/dev/null || true
}

# ── 3. deterministic sshd configuration ─────────────────────────────────────
find_sftp_server() {
    for candidate in /usr/lib/openssh/sftp-server /usr/lib/ssh/sftp-server \
                     /usr/libexec/openssh/sftp-server /usr/libexec/sftp-server; do
        [ -x "$candidate" ] && { echo "$candidate"; return 0; }
    done
    command -v sftp-server 2>/dev/null || true
}

write_sshd_config() {
    SFTP=$(find_sftp_server)
    USE_PAM=no
    [ -f /etc/pam.d/sshd ] && USE_PAM=yes
    cat > "$SSHD_CONFIG" <<EOF
# Written by the UnixNodes supervisor. Regenerated on every boot.
Port __SSH_PORT__
AddressFamily any
ListenAddress 0.0.0.0

PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
PermitEmptyPasswords no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM $USE_PAM

X11Forwarding no
PrintMotd no
PrintLastLog yes
TCPKeepAlive yes
ClientAliveInterval 60
ClientAliveCountMax 3
MaxAuthTries 6
MaxSessions 10
MaxStartups 20:30:100
LoginGraceTime 60

LogLevel INFO
SyslogFacility AUTH
UseDNS no

Subsystem sftp $SFTP
EOF
    chmod 600 "$SSHD_CONFIG"
}

# ── 4. root credential (applied once, then destroyed) ───────────────────────
apply_credential() {
    [ -s "$CRED" ] || return 0
    if chpasswd < "$CRED" >> "$LOG" 2>&1; then
        log "root credential applied"
        : > "$CRED" 2>/dev/null || true
        chmod 600 "$CRED" 2>/dev/null || true
    else
        log "chpasswd failed — leaving credential in place for a retry"
    fi
}

# ── 5. optional platform startup hook (branding MOTD etc.) ─────────────────
run_platform_hook() {
    HOOK=/run/unixnodes/hook.sh
    [ -s "$HOOK" ] || return 0
    log "running platform hook"
    sh "$HOOK" >> "$LOG" 2>&1 || log "platform hook failed"
}

# ── 6. cron (where the distribution ships it) ───────────────────────────────
start_cron() {
    if command -v cron >/dev/null 2>&1; then
        mkdir -p /var/spool/cron/crontabs 2>/dev/null || true
        cron >> "$LOG" 2>&1 || log "cron failed to start"
    elif command -v crond >/dev/null 2>&1; then
        mkdir -p /var/spool/cron/crontabs 2>/dev/null || true
        crond -b >> "$LOG" 2>&1 || log "crond failed to start"
    fi
}

# ── 7. first-boot pass ──────────────────────────────────────────────────────
first_boot() {
    [ -f "$MARKER" ] && return 0
    if install_openssh; then
        echo "installed=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$MARKER" 2>/dev/null || true
    else
        log "OpenSSH is unavailable in this image"
    fi
}

first_boot
if ! command -v sshd >/dev/null 2>&1; then
    install_openssh || true
fi

ensure_host_keys
write_sshd_config
apply_credential
run_platform_hook
start_cron

mkdir -p /run/sshd /var/run/sshd 2>/dev/null || true
chmod 755 /run/sshd 2>/dev/null || true

SSHD_BIN=$(command -v sshd)
if [ -z "$SSHD_BIN" ]; then
    log "FATAL: sshd is not installed; sleeping so the container stays inspectable"
    exec sleep infinity
fi

if ! "$SSHD_BIN" -t -f "$SSHD_CONFIG" >> "$LOG" 2>&1; then
    log "FATAL: sshd configuration test failed"
    "$SSHD_BIN" -t -f "$SSHD_CONFIG" >> "$LOG" 2>&1 || true
fi

log "starting sshd in the foreground"
# `exec` makes sshd PID 1 (or the direct child of `docker init`) so SIGTERM
# from `docker stop` reaches it and the guest shuts down cleanly.
exec "$SSHD_BIN" -D -e -f "$SSHD_CONFIG"
"""

SSHD_CONFIG_TEMPLATE_OK = "SSHD_CONFIG written"


# ── host side asset management ──────────────────────────────────────────────

def ensure_runtime_assets() -> Dict[str, str]:
    """
    Create the host directories and write the supervisor script.

    Returns ``{"init_script": <host path>, "secret_dir": …, "data_dir": …}``.
    """
    for directory in (config.DOCKER_RUNTIME_DIR, config.DOCKER_SECRET_DIR,
                      config.DOCKER_VPS_DATA_DIR, config.DOCKER_BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    init_path = config.DOCKER_RUNTIME_DIR / INIT_FILENAME
    rendered = INIT_SCRIPT.replace("__SSH_PORT__", str(int(config.SSH_CONTAINER_PORT)))
    current = init_path.read_text() if init_path.exists() else ""
    if current != rendered:
        init_path.write_text(rendered, encoding="utf-8")
        logger.info("wrote container supervisor script to %s", init_path)
    os.chmod(init_path, 0o755)
    return {
        "init_script": str(init_path),
        "secret_dir": str(config.DOCKER_SECRET_DIR),
        "data_dir": str(config.DOCKER_VPS_DATA_DIR),
        "backup_dir": str(config.DOCKER_BACKUP_DIR),
    }


def secret_dir_for(container_name: str) -> Path:
    path = config.DOCKER_SECRET_DIR / container_name
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_credential(container_name: str, password: str) -> Path:
    """
    Stage ``root:<password>`` for the supervisor to pick up on next boot.

    The file is world-invisible (0600) and lives outside the container
    filesystem, so ``docker inspect`` never shows it.
    """
    directory = secret_dir_for(container_name)
    path = directory / "root.credential"
    path.write_text(f"root:{password}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def write_hook(container_name: str, script: str) -> Optional[Path]:
    """Optional startup hook (used for the platform MOTD/branding script)."""
    if not script or not script.strip():
        return None
    directory = secret_dir_for(container_name)
    path = directory / "hook.sh"
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o700)
    return path


def clear_credential(container_name: str) -> None:
    path = config.DOCKER_SECRET_DIR / container_name / "root.credential"
    try:
        if path.exists():
            path.write_text("", encoding="utf-8")
    except OSError:
        pass


def bind_mounts(container_name: str) -> Dict[str, Dict[str, str]]:
    """The two platform-owned, non-volume bind mounts for a VPS."""
    ensure_runtime_assets()
    secret = secret_dir_for(container_name)
    return {
        str(config.DOCKER_RUNTIME_DIR / INIT_FILENAME): {
            "bind": config.INIT_SCRIPT_TARGET, "mode": "ro"},
        str(secret): {"bind": "/run/unixnodes", "mode": "rw"},
    }


def container_command() -> list:
    """The container entrypoint (the supervisor script)."""
    return ["/bin/sh", config.INIT_SCRIPT_TARGET]


# ── in-guest operations ─────────────────────────────────────────────────────

def is_listening(client: Any, container_name: str, port: int = 0) -> bool:
    """Check whether sshd is accepting connections inside the container."""
    port = int(port or config.SSH_CONTAINER_PORT)
    probe = (
        f"if command -v ss >/dev/null 2>&1; then ss -lnt 2>/dev/null | grep -q ':{port} ' && echo LISTENING;"
        f" elif command -v netstat >/dev/null 2>&1; then netstat -lnt 2>/dev/null | grep -q ':{port} ' && echo LISTENING;"
        f" elif pgrep -x sshd >/dev/null 2>&1; then echo LISTENING; fi; true"
    )
    try:
        output = containers.exec_script(client, container_name, probe, timeout=30)
    except Exception as exc:
        logger.debug("ssh listen probe failed on %s: %s", container_name, exc)
        return False
    return "LISTENING" in output


def wait_ready(client: Any, container_name: str, *, attempts: Optional[int] = None,
               interval: Optional[float] = None) -> bool:
    """Poll until sshd is listening (or give up)."""
    tries = int(attempts or config.SSH_READY_ATTEMPTS)
    pause = float(interval or config.SSH_READY_INTERVAL)
    for _ in range(max(1, tries)):
        if is_listening(client, container_name):
            return True
        time.sleep(pause)
    return False


def set_root_password(client: Any, container_name: str, password: str) -> Tuple[bool, str]:
    """
    Rotate the root password on a live container.

    The password is streamed in as a file and consumed by ``chpasswd`` — it
    never appears in an argument list, an environment variable or a log line.
    """
    try:
        if not containers.put_file(client, container_name, "/run/unixnodes",
                                   f"root:{password}\n".encode("utf-8"),
                                   filename="root.credential", mode=0o600):
            return False, "Could not stage the credential file"
        code, out, err = containers.exec_argv(
            client, container_name,
            ["sh", "-c", "chpasswd < /run/unixnodes/root.credential && "
                         ": > /run/unixnodes/root.credential && echo PASSWORD_OK"],
            timeout=60,
        )
    except Exception as exc:
        return False, f"Password change failed: {exc}"
    if code != 0 or "PASSWORD_OK" not in out:
        return False, (err or out or "chpasswd failed").strip()[:400]
    return True, "Root password updated"


def ensure_ssh_running(client: Any, container_name: str) -> Tuple[bool, str]:
    """Restart the supervisor's sshd if it is not running (e.g. after a crash)."""
    if is_listening(client, container_name):
        return True, "sshd is listening"
    try:
        containers.exec_script(
            client, container_name,
            "mkdir -p /run/sshd; "
            "SSHD=$(command -v sshd); "
            '[ -n "$SSHD" ] && "$SSHD" -f /etc/ssh/sshd_config.unixnodes >/dev/null 2>&1 || true; '
            "sleep 2; true",
            timeout=60,
        )
    except Exception as exc:
        return False, f"Could not start sshd: {exc}"
    if is_listening(client, container_name):
        return True, "sshd restarted"
    return False, "sshd did not start — check `docker logs` for this container"


def guest_report(client: Any, container_name: str) -> Dict[str, Any]:
    """Small diagnostic snapshot used by ``!vps-info``."""
    script = r"""
echo "OS=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "KERNEL=$(uname -r)"
echo "SSHD=$(command -v sshd >/dev/null 2>&1 && echo yes || echo no)"
echo "CRON=$(command -v cron >/dev/null 2>&1 || command -v crond >/dev/null 2>&1 || echo none)"
echo "UPTIME=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
df -Pk / 2>/dev/null | awk 'NR==2{print "DISK_USED="$3"\nDISK_TOTAL="$2"\nDISK_PCT="$5}'
"""
    values: Dict[str, str] = {}
    try:
        output = containers.exec_script(client, container_name, script, timeout=45)
    except Exception as exc:
        return {"error": str(exc)}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# ── host side connectivity check ────────────────────────────────────────────

def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_banner(host: str, port: int, timeout: float = 8.0) -> str:
    """Read the SSH identification string (proves sshd answers, no auth yet)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(256)
        return data.decode("utf-8", "replace").strip()
    except OSError:
        return ""


def verify_ssh_login(host: str, port: int, username: str, password: str,
                     timeout: int = 20) -> Tuple[bool, str]:
    """
    Perform a real SSH login against the published port.

    Requires paramiko; when it is missing the check is skipped (the TCP/banner
    probe still runs) so provisioning does not fail on an optional dependency.
    """
    try:
        import paramiko
    except Exception:
        return False, "paramiko is not installed — login verification skipped"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, port=int(port), username=username, password=password,
                       timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                       allow_agent=False, look_for_keys=False)
        _stdin, stdout, _stderr = client.exec_command("echo UNIXNODES_SSH_OK; id -u", timeout=timeout)
        output = stdout.read().decode("utf-8", "replace")
        return ("UNIXNODES_SSH_OK" in output, output.strip()[:200])
    except paramiko.AuthenticationException:
        return False, "Authentication failed with the generated password"
    except paramiko.SSHException as exc:
        return False, f"SSH negotiation failed: {exc}"
    except Exception as exc:
        return False, f"Could not connect: {exc}"
    finally:
        try:
            client.close()
        except Exception:
            pass


def verify_access(host: str, port: int, password: str, *, username: str = "root") -> Tuple[bool, str]:
    """
    Layered connectivity check used at the end of provisioning.

    1. TCP connect  →  2. SSH banner  →  3. real login (when paramiko exists).
    """
    if not tcp_reachable(host, port):
        return False, f"Nothing is listening on {host}:{port}"
    banner = ssh_banner(host, port)
    if not banner.startswith("SSH-"):
        return False, f"{host}:{port} accepted the connection but did not speak SSH"
    if not config.SSH_VERIFY_LOGIN:
        return True, f"SSH banner received ({banner[:40]})"
    ok, detail = verify_ssh_login(host, port, username, password)
    if ok:
        return True, "Root login verified"
    if "paramiko is not installed" in detail:
        return True, f"SSH banner received ({banner[:40]}); login check skipped"
    return False, detail


def raise_for_failure(ok: bool, message: str) -> None:
    if not ok:
        raise SSHSetupError(message, user_message="SSH access could not be verified on the new VPS.")


def stat_mode(path: str) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return 0
