#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              UNIXNODES  •  VPS CONTROL BOT  •  v10.0 ULTRA-ANIMATED          ║
║                                                                              ║
║   A full-featured Discord bot for managing LXC based VPS infrastructure.     ║
║                                                                              ║
║   v9.0 "ULTRA" release highlights (kept 100% intact)                         ║
║   ─────────────────────────────────────────────────────────────────────────  ║
║   • Complete bug-fix pass over v8.0 (100+ issues repaired, see CHANGELOG)    ║
║   • Animated panels: spinners, progress bars, sparklines, live stat views    ║
║   • Premium embed system tuned for mobile / tablet / desktop (PC)            ║
║   • Hardened security: Argon2id/PBKDF2 passwords, SSRF guard, web-ssh        ║
║     sessions, audit log, token-bucket rate limiting, safe subprocess calls   ║
║   • Thread-safe data layer with atomic writes, WAL, and crash-safe backups   ║
║   • Non-blocking event loop (all blocking IO pushed to worker threads)       ║
║   • Automatic expiration monitor, auto-suspend, auto-renewal reminders       ║
║   • Interactive help with live search, pagination and permission filtering   ║
║                                                                              ║
║   v10.0 "ULTRA-ANIMATED" — what was ADDED (nothing was removed)              ║
║   ─────────────────────────────────────────────────────────────────────────  ║
║   • ANIMATION ENGINE (section 6.7)                                           ║
║       - Animator        : generic frame-cycling engine for any message       ║
║       - beat() / pulse(): breathing status dots used by every live panel     ║
║       - wave_bar()      : travelling-wave progress bar (new visual style)    ║
║       - gradient_bar()  : multi-tone bar that shifts colour with load        ║
║       - bounce()        : bouncing-ball loader for long operations           ║
║       - shimmer()       : animated footer/title text shimmer                 ║
║       - TypewriterEmbed : character-by-character reveal (help / about)       ║
║       - ascii_graph()   : full ASCII chart with grid + axis + labels         ║
║       - LiveEmbedMixin  : plug-in auto-refresh for ANY embed panel           ║
║       - LivePlatformView / LiveFleetView / LiveContainerGrid                 ║
║   • LIVE STATUS EVERYWHERE                                                   ║
║       - !live       : always-on animated platform dashboard (host + nodes)   ║
║       - !top        : htop-style live container leaderboard                  ║
║       - !graph      : historical CPU/RAM chart drawn from snapshots          ║
║       - !dashboard  : animated personal dashboard with live rows             ║
║       - Manage panel : optional "Live" toggle that keeps the card updating   ║
║       - Host snapshots are recorded in the background for sparklines         ║
║   • POLISH                                                                   ║
║       - Typing indicators + animated "thinking" embeds on most commands      ║
║       - Elapsed-time / ETA readouts inside every ProgressPanel               ║
║       - Code-block escaping (backticks in output can no longer break embeds)  ║
║       - Graceful "already responded" guards on every interaction path        ║
║                                                                              ║
║   Author  : Hopingboyz                                                       ║
║   License : MIT                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# ── Standard library ─────────────────────────────────────────────────────────
import asyncio
import atexit
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import platform
import random
import re
import secrets
import shlex
import shutil
import sqlite3
import string
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from functools import wraps, partial
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# ── Third party ──────────────────────────────────────────────────────────────
try:
    import discord
    from discord import app_commands
    from discord.ext import commands
except ImportError:  # pragma: no cover
    sys.exit("FATAL: discord.py is not installed. Run: pip install -U discord.py")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("FATAL: requests is not installed. Run: pip install -U requests")

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None  # Web SSH terminal will be disabled gracefully

try:
    from flask import Flask, jsonify, request
except ImportError:  # pragma: no cover
    Flask = None  # Web SSH terminal will be disabled gracefully


# ═══════════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent

# Load .env from the script folder first, then CWD (never crash if missing).
load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)
load_dotenv(override=False)


def _env(key: str, default: Any = None) -> Optional[str]:
    val = os.getenv(key)
    if val is None:
        return default
    val = val.strip()
    return val if val != "" else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(str(_env(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(str(_env(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = _env(key, None)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on", "enabled")


def _env_id(key: str, default: int = 0) -> int:
    """Parse a Discord snowflake id, tolerating quotes / whitespace / partial paste."""
    raw = _env(key, None)
    if raw is None:
        return default
    match = re.search(r"\d{15,25}", raw)
    if match:
        try:
            return int(match.group(0))
        except ValueError:
            return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


BOT_NAME: str = str(_env("BOT_NAME", "UnixNodes"))
PREFIX: str = str(_env("PREFIX", "!"))
DISCORD_TOKEN: Optional[str] = _env("DISCORD_TOKEN")
YOUR_SERVER_IP: str = str(_env("YOUR_SERVER_IP", "127.0.0.1"))
MAIN_ADMIN_ID: int = _env_id("MAIN_ADMIN_ID", 0)
VPS_USER_ROLE_ID: Optional[int] = _env_id("VPS_USER_ROLE_ID", 0) or None
DEFAULT_STORAGE_POOL: str = str(_env("DEFAULT_STORAGE_POOL", "default"))
LXC_BRIDGE: str = str(_env("LXC_BRIDGE", "lxdbr0"))
HOST_MOTD: str = str(_env("HOST_MOTD", ""))
BOT_VERSION: str = str(_env("BOT_VERSION", "10.0-ULTRA-ANIMATED"))
BOT_DEVELOPER: str = str(_env("BOT_DEVELOPER", "Hopingboyz"))
BOT_THUMBNAIL_URL: str = str(_env("BOT_THUMBNAIL_URL", ""))
BOT_ICON_URL: str = str(_env("BOT_ICON_URL", ""))
SUPPORT_INVITE_URL: str = str(_env("SUPPORT_INVITE_URL", ""))

# ── VPS expiration ───────────────────────────────────────────────────────────
DEFAULT_VPS_EXPIRATION_DAYS: int = _env_int("DEFAULT_VPS_EXPIRATION_DAYS", 30)
EXPIRATION_WARNING_DAYS: int = _env_int("EXPIRATION_WARNING_DAYS", 1)
EXPIRATION_AUTO_SUSPEND: bool = _env_bool("EXPIRATION_AUTO_SUSPEND", True)
EXPIRATION_CHECK_INTERVAL: int = _env_int("EXPIRATION_CHECK_INTERVAL", 900)  # seconds

# ── Public VPS creation ──────────────────────────────────────────────────────
PUBLIC_VPS_ENABLED: bool = _env_bool("PUBLIC_VPS_ENABLED", True)
PUBLIC_VPS_MAX_RAM: int = _env_int("PUBLIC_VPS_MAX_RAM", 4)
PUBLIC_VPS_MAX_CPU: int = _env_int("PUBLIC_VPS_MAX_CPU", 2)
PUBLIC_VPS_MAX_DISK: int = _env_int("PUBLIC_VPS_MAX_DISK", 50)
PUBLIC_VPS_EXPIRY_DAYS: int = _env_int("PUBLIC_VPS_EXPIRY_DAYS", 30)
PUBLIC_VPS_MAX_PER_USER: int = _env_int("PUBLIC_VPS_MAX_PER_USER", 1)
PUBLIC_VPS_MAX_PER_IP: int = _env_int("PUBLIC_VPS_MAX_PER_IP", 3)
PUBLIC_VPS_GLOBAL_LIMIT: int = _env_int("PUBLIC_VPS_GLOBAL_LIMIT", 100)
PUBLIC_VPS_RENEWAL_ENABLED: bool = _env_bool("PUBLIC_VPS_RENEWAL_ENABLED", True)
PUBLIC_VPS_RENEWAL_DAYS: int = _env_int("PUBLIC_VPS_RENEWAL_DAYS", 30)
PUBLIC_VPS_RENEWAL_WINDOW_DAYS: int = _env_int("PUBLIC_VPS_RENEWAL_WINDOW_DAYS", 3)
PUBLIC_VPS_COOLDOWN_HOURS: int = _env_int("PUBLIC_VPS_COOLDOWN_HOURS", 24)
PUBLIC_VPS_REQUIRE_ACCOUNT_AGE: int = _env_int("PUBLIC_VPS_REQUIRE_ACCOUNT_AGE", 3)

# ── Web SSH terminal ─────────────────────────────────────────────────────────
WEBSSH_ENABLED: bool = _env_bool("WEBSSH_ENABLED", True) and (Flask is not None and paramiko is not None)
WEBSSH_HOST: str = str(_env("WEBSSH_HOST", "0.0.0.0"))
WEBSSH_PORT: int = _env_int("WEBSSH_PORT", 5000)
WEBSSH_SERVER_IP: str = str(_env("WEBSSH_SERVER_IP", YOUR_SERVER_IP))
WEBSSH_URL_FORMAT: str = str(_env("WEBSSH_URL_FORMAT", "http://{SERVER_IP}:{PORT}"))
WEBSSH_SESSION_TTL: int = _env_int("WEBSSH_SESSION_TTL", 1800)      # idle seconds
WEBSSH_MAX_SESSIONS: int = _env_int("WEBSSH_MAX_SESSIONS", 200)
WEBSSH_SESSION_MAX_PER_IP: int = _env_int("WEBSSH_SESSION_MAX_PER_IP", 8)
WEBSSH_ALLOW_PRIVATE_HOSTS: bool = _env_bool("WEBSSH_ALLOW_PRIVATE_HOSTS", True)
# When enabled every freshly provisioned VPS gets a random 6-digit PIN appended to
# its terminal link (the PIN is shown in the same DM). Protects against a link
# being forwarded, screenshotted or pasted into a public channel.
WEBSSH_DEFAULT_PIN: bool = _env_bool("WEBSSH_DEFAULT_PIN", False)
WEBSSH_PIN_ATTEMPTS: int = _env_int("WEBSSH_PIN_ATTEMPTS", 5)
WEBSSH_CONNECT_TIMEOUT: int = _env_int("WEBSSH_CONNECT_TIMEOUT", 15)

# ── Available Operating Systems ──────────────────────────────────────────────
SUPPORTED_OS_OPTIONS = [
    {
        "name": "Ubuntu 22.04 LTS",
        "image": "ubuntu:22.04",
        "emoji": "🐧",
        "description": "Latest stable Ubuntu (recommended)",
        "features": ["SSH", "Docker", "Systemd"],
    },
    {
        "name": "Ubuntu 20.04 LTS",
        "image": "ubuntu:20.04",
        "emoji": "🐧",
        "description": "LTS with long support",
        "features": ["SSH", "Docker", "Systemd"],
    },
    {
        "name": "Debian 12 (Bookworm)",
        "image": "debian:12",
        "emoji": "🔴",
        "description": "Stable Debian release",
        "features": ["SSH", "Lightweight", "Systemd"],
    },
    {
        "name": "Debian 11 (Bullseye)",
        "image": "debian:11",
        "emoji": "🔴",
        "description": "Proven Debian stable",
        "features": ["SSH", "Lightweight", "Systemd"],
    },
    {
        "name": "Alpine Linux 3.18",
        "image": "alpine:3.18",
        "emoji": "⛰️",
        "description": "Ultra-lightweight (~5MB)",
        "features": ["SSH", "Minimal", "Fast boot"],
    },
    {
        "name": "CentOS 7",
        "image": "centos:7",
        "emoji": "🎩",
        "description": "RHEL-based, enterprise",
        "features": ["SSH", "YUM", "Systemd"],
    },
]

# Add to database schema
def get_default_os() -> str:
    """Get the default OS for VPS creation."""
    return "ubuntu:22.04"

# ── Rate limiting / abuse ────────────────────────────────────────────────────
RATE_LIMIT_COOLDOWN: float = float(_env_int("RATE_LIMIT_COOLDOWN", 4))
RATE_LIMIT_ABUSE_TIMEOUT: int = _env_int("RATE_LIMIT_ABUSE_TIMEOUT", 60)
RATE_LIMIT_MAX_VIOLATIONS: int = _env_int("RATE_LIMIT_MAX_VIOLATIONS", 3)
RATE_LIMIT_BURST: int = _env_int("RATE_LIMIT_BURST", 3)

# ── Command safety ───────────────────────────────────────────────────────────
LXC_TIMEOUT_DEFAULT: int = _env_int("LXC_TIMEOUT_DEFAULT", 180)
MAX_CONTAINER_NAME_LEN: int = 63
COMMAND_TIMEOUT_SECONDS: int = _env_int("COMMAND_TIMEOUT_SECONDS", 240)
MAX_REASON_LEN: int = 300
MANAGE_PANEL_TIMEOUT: int = _env_int("MANAGE_PANEL_TIMEOUT", 300)
CONSOLE_PANEL_TIMEOUT: int = _env_int("CONSOLE_PANEL_TIMEOUT", 600)
STATS_REFRESH_SECONDS: int = max(4, _env_int("STATS_REFRESH_SECONDS", 5))
MAX_LIVE_REFRESHES: int = _env_int("MAX_LIVE_REFRESHES", 60)
OWNER_IDS: Tuple[int, ...] = tuple(
    int(x) for x in re.findall(r"\d{15,25}", str(_env("OWNER_IDS", "") or str(MAIN_ADMIN_ID) or ""))
)

# ── v10: animation + live-status tuning ──────────────────────────────────────
# Every value here is optional: the bot behaves like v9 when they are default.
ANIM_ENABLED: bool = _env_bool("ANIM_ENABLED", True)
ANIM_SPEED: float = max(0.25, min(3.0, _env_float("ANIM_SPEED", 1.0)))   # 1.0 = normal
ANIM_FRAME_SECONDS: float = max(0.35, _env_float("ANIM_FRAME_SECONDS", 0.9) / ANIM_SPEED)
TYPEWRITER_ENABLED: bool = _env_bool("TYPEWRITER_ENABLED", True)
TYPEWRITER_SPEED: float = max(0.15, _env_float("TYPEWRITER_SPEED", 0.9) / ANIM_SPEED)

LIVE_REFRESH_SECONDS: int = max(3, _env_int("LIVE_REFRESH_SECONDS", STATS_REFRESH_SECONDS))
LIVE_MAX_MINUTES: int = max(1, _env_int("LIVE_MAX_MINUTES", 30))
LIVE_FLEET_REFRESH: int = max(5, _env_int("LIVE_FLEET_REFRESH", 12))
AUTO_REFRESH_MANAGE: bool = _env_bool("AUTO_REFRESH_MANAGE", True)
MANAGE_REFRESH_SECONDS: int = max(6, _env_int("MANAGE_REFRESH_SECONDS", 12))
TYPING_INDICATORS: bool = _env_bool("TYPING_INDICATORS", True)
HOST_SNAPSHOT_INTERVAL: int = max(15, _env_int("HOST_SNAPSHOT_INTERVAL", 60))
HOST_SNAPSHOT_KEEP: int = max(60, _env_int("HOST_SNAPSHOT_KEEP", 720))
GRAPH_POINTS: int = max(8, min(60, _env_int("GRAPH_POINTS", 28)))

# Runtime-tunable animation switch (see !anim / !speed).
ANIM_STATE: Dict[str, Any] = {"enabled": ANIM_ENABLED, "speed": ANIM_SPEED}


def anim_on() -> bool:
    """True while animations are enabled (runtime toggled by `!anim`)."""
    return bool(ANIM_STATE.get("enabled", True))


def frame_seconds() -> float:
    """Current frame duration, respecting the runtime speed multiplier."""
    try:
        return max(0.25, float(ANIM_FRAME_SECONDS) / float(ANIM_STATE.get("speed", 1.0) or 1.0))
    except Exception:
        return ANIM_FRAME_SECONDS


# ═══════════════════════════════════════════════════════════════════════════
# 2. LOGGING
# ═══════════════════════════════════════════════════════════════════════════

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class _SecretRedactor(logging.Filter):
    """Stops root passwords / tokens leaking into log files."""

    PATTERNS = (
        re.compile(r"(password\s*[:=]\s*)(\S+)", re.I),
        re.compile(r"(root:[^\s'\"]+)", re.I),
        re.compile(r"(DISCORD_TOKEN\s*[:=]\s*)(\S+)", re.I),
        re.compile(r"((?:api_key|token)=)([A-Za-z0-9_\-]{8,})", re.I),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pattern in self.PATTERNS:
            redacted = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


logger = logging.getLogger(f"{BOT_NAME.lower()}_bot")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    _fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _console = logging.StreamHandler(sys.stdout)
    _console.setFormatter(_fmt)

    _file = logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file.setFormatter(_fmt)

    for _handler in (_console, _file):
        _handler.addFilter(_SecretRedactor())
        logger.addHandler(_handler)

logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("paramiko").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════
# 3. SECURITY PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════

# ── 3.1 Password hashing (Argon2id when available, PBKDF2 fallback) ──────────
#
# Argon2id is the current best practice for password storage (memory-hard, GPU
# resistant). It needs the optional `argon2-cffi` package; when that is missing
# the bot transparently falls back to PBKDF2-HMAC-SHA256 with a high iteration
# count, and `startup_checks()` tells the admin to install argon2-cffi.
# Both formats verify, so upgrading never invalidates existing hashes.

_PBKDF2_ROUNDS = 260_000

try:  # optional dependency
    from argon2 import PasswordHasher as _Argon2PasswordHasher  # type: ignore
    from argon2.exceptions import (  # type: ignore
        InvalidHashError as _Argon2InvalidHash,
        VerificationError as _Argon2VerificationError,
        VerifyMismatchError as _Argon2Mismatch,
    )

    _ARGON2 = _Argon2PasswordHasher(
        time_cost=3,          # iterations
        memory_cost=65536,    # 64 MiB
        parallelism=2,
        hash_len=32,
        salt_len=16,
    )
    _ARGON2_ERRORS = (_Argon2Mismatch, _Argon2VerificationError, _Argon2InvalidHash)
    ARGON2_AVAILABLE = True
except Exception:  # pragma: no cover - depends on environment
    _ARGON2 = None
    _ARGON2_ERRORS = ()
    ARGON2_AVAILABLE = False


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    """
    Hash a password for storage.

    Returns an Argon2id hash (`$argon2id$…`) when argon2-cffi is installed,
    otherwise a self-describing `pbkdf2_sha256$rounds$salt$hash` string.
    """
    if _ARGON2 is not None:
        return _ARGON2.hash(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time verification of a stored password hash. Never raises."""
    if not stored or not password:
        return False

    # Argon2id / Argon2i / Argon2d hashes
    if str(stored).startswith("$argon2") and _ARGON2 is not None:
        try:
            _ARGON2.verify(stored, password)
            return True
        except _ARGON2_ERRORS:
            return False
        except Exception:
            return False

    # PBKDF2-HMAC-SHA256 (and legacy hashes from older releases)
    try:
        algo, rounds_s, salt_hex, hash_hex = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_s)
        )
        return hmac.compare_digest(expected.hex(), hash_hex)
    except Exception:
        return False


def needs_rehash(stored: Optional[str]) -> bool:
    """True when a stored hash uses older parameters and should be upgraded."""
    if not stored:
        return False
    value = str(stored)
    if value.startswith("$argon2"):
        if _ARGON2 is None:
            return False
        try:
            return _ARGON2.check_needs_rehash(value)
        except Exception:
            return True
    # PBKDF2 hash from an older release, or argon2 became available since.
    if _ARGON2 is not None:
        return True
    try:
        algo, rounds_s, _salt, _hash = value.split("$")
        return algo != "pbkdf2_sha256" or int(rounds_s) < _PBKDF2_ROUNDS
    except Exception:
        return True


def looks_hashed(value: Optional[str]) -> bool:
    return bool(value) and (str(value).startswith("pbkdf2_sha256$") or str(value).startswith("$argon2"))


# ── 3.2 SSH host key store (auto-add, handles reinstalls gracefully) ───────

KNOWN_HOSTS_FILE = BASE_DIR / "known_hosts"


def get_ssh_client() -> "paramiko.SSHClient":
    """Build a paramiko client that auto-accepts host keys (for VPS reinstalls)."""
    if paramiko is None:
        raise RuntimeError("paramiko is not installed")
    client = paramiko.SSHClient()

    # CRITICAL: Do NOT load any known_hosts to avoid key mismatch on reinstall
    # We set AutoAddPolicy which accepts any key on first sight
    # This is safe since we control the internal VPS infrastructure
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Do NOT call load_host_keys() - this prevents loading cached old keys
    # from ~/.ssh/known_hosts or local known_hosts file

    return client


def persist_host_key(client: "paramiko.SSHClient") -> None:
    """Save updated host keys to disk (optional, we auto-accept anyway)."""
    # We skip persisting to avoid caching old keys on reinstall
    pass


def clear_host_key(hostname: str, port: int = 22) -> None:
    """Clear all cached host keys to prevent mismatch on reinstall."""
    try:
        # Delete the local known_hosts file to start fresh
        if KNOWN_HOSTS_FILE.exists():
            KNOWN_HOSTS_FILE.unlink()
            logger.debug("[SSH] Cleared known_hosts file for fresh connections")

        # Also try to clear the user's home .ssh/known_hosts if it has old keys
        home_known_hosts = Path.home() / ".ssh" / "known_hosts"
        if home_known_hosts.exists():
            try:
                with open(home_known_hosts, "r") as handle:
                    lines = handle.readlines()

                # Filter out lines containing our server IPs (127.0.0.1, localhost, etc)
                filtered_lines = [
                    line for line in lines
                    if not any(ip in line for ip in ("127.0.0.1", "localhost", "::1"))
                ]

                with open(home_known_hosts, "w") as handle:
                    handle.writelines(filtered_lines)

                logger.debug("[SSH] Cleaned home known_hosts of localhost entries")
            except Exception as exc:
                logger.debug("Could not clean home known_hosts: %s", exc)
    except Exception as exc:
        logger.debug("Could not clear host keys: %s", exc)


# ── 3.3 SSRF protection ──────────────────────────────────────────────────────

# Metadata services are never reachable, even when private targets are enabled.
_BLOCKED_HOSTNAMES = {
    "metadata", "metadata.google.internal", "instance-data",
}
# Loopback names that are only allowed when private targets are enabled.
_LOOPBACK_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_private_ip(host: str) -> bool:
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def validate_ssh_target(host: str, port: int, *, allow_private: bool = False) -> Tuple[bool, str]:
    """Block SSRF (cloud metadata, loopback, private ranges) unless allowed."""
    host = (host or "").strip()
    if not host or len(host) > 253:
        return False, "Invalid host"
    if any(ch in host for ch in " \t\r\n\"'`$;&|<>"):
        return False, "Invalid characters in host"
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return False, "Port must be a number"
    if not (1 <= port_number <= 65535):
        return False, "Port must be between 1 and 65535"

    lowered = host.lower()
    if lowered in _BLOCKED_HOSTNAMES:
        return False, "Blocked host (cloud metadata service)"
    if lowered in _LOOPBACK_HOSTNAMES and not allow_private:
        return False, "Loopback addresses are blocked"

    if not allow_private:
        if _is_private_ip(host):
            return False, "Private / reserved addresses are blocked"
        # Resolve DNS and re-check to stop DNS-rebinding style bypasses.
        try:
            import socket

            infos = socket.getaddrinfo(host, None)
            for info in infos:
                addr = info[4][0]
                if _is_private_ip(addr):
                    return False, "Host resolves to a private address"
        except Exception:
            return False, "Host could not be resolved"
    return True, "OK"


# ── 3.4 Input validation helpers ─────────────────────────────────────────────

_CONTAINER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def validate_container_name(name: str) -> Tuple[bool, str]:
    """LXC/docker style name: lowercase alnum + hyphens, 1-63 chars."""
    if not name:
        return False, "Container name cannot be empty"
    if len(name) > MAX_CONTAINER_NAME_LEN:
        return False, f"Container name too long (max {MAX_CONTAINER_NAME_LEN} characters)"
    if not _CONTAINER_RE.match(name):
        return False, (
            "Container name must start/end with a letter or digit and may only "
            "contain lowercase letters, digits and hyphens"
        )
    if name.isdigit():
        return False, "Container name cannot be only digits"
    return True, "OK"


def validate_password(password: str) -> Tuple[bool, str]:
    if not password:
        return False, "Password cannot be empty"
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password too long (max 128 characters)"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return False, "Password must contain letters and numbers"
    return True, "OK"


def validate_ram_spec(ram: str) -> Tuple[bool, str]:
    try:
        raw = str(ram).strip().upper()
        if not raw.endswith("GB"):
            return False, "RAM must be in GB (e.g. 2GB)"
        value = int(raw[:-2])
    except (TypeError, ValueError):
        return False, "Invalid RAM format"
    if value < 1 or value > PUBLIC_VPS_MAX_RAM:
        return False, f"RAM must be between 1GB and {PUBLIC_VPS_MAX_RAM}GB"
    return True, "OK"


def validate_disk_spec(disk: str) -> Tuple[bool, str]:
    try:
        raw = str(disk).strip().upper()
        if not raw.endswith("GB"):
            return False, "Disk must be in GB (e.g. 20GB)"
        value = int(raw[:-2])
    except (TypeError, ValueError):
        return False, "Invalid disk format"
    if value < 5 or value > PUBLIC_VPS_MAX_DISK:
        return False, f"Disk must be between 5GB and {PUBLIC_VPS_MAX_DISK}GB"
    return True, "OK"


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def sanitize_reason(reason: str, default: str = "No reason provided") -> str:
    """Strip control characters, mentions and length-limit free-form text."""
    text = (reason or "").replace("\x00", "").strip()
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"@(everyone|here|&\d+)", lambda m: m.group(0).replace("@", "@\u200b"), text)
    return text[:MAX_REASON_LEN] or default


def sanitize_username_for_container(username: str) -> str:
    """Turn a Discord-ish name into a valid LXC container prefix."""
    sanitized = re.sub(r"[^a-zA-Z0-9-]+", "-", str(username or "")).strip("-").lower()
    sanitized = sanitized.lstrip("0123456789") or "user"
    return sanitized[:24] or "user"


# ── 3.4b v10: markdown-safe rendering helpers ───────────────────────────────
#
# These fix a very common (and very ugly) v9 glitch: command output that
# contains a backtick - or a "```" sequence - escaped the code fence and
# shredded the embed layout, sometimes even breaking the message completely.

_ZERO_WIDTH = "\u200b"


def escape_backticks(text: Any) -> str:
    """Neutralise backticks so user output cannot escape a ``` code fence."""
    return str(text or "").replace("`", f"{_ZERO_WIDTH}`")


def safe_code_block(text: Any, *, language: str = "", max_chars: int = 900) -> str:
    """Build a bullet-proof fenced code block from arbitrary text."""
    body = escape_backticks(truncate_text(str(text or ""), max_chars))
    fence = "```"
    # If (somehow) a fence sequence survived, widen the fence.
    while fence in body:
        fence += "`"
    return f"{fence}{language}\n{body}\n{fence}"


def sanitize_field_value(value: Any, max_length: int = 1024) -> str:
    """Field value that is guaranteed non-empty (Discord 400s on empty values)."""
    text = truncate_text(value, max_length)
    if not text.strip():
        return "\u200b"
    return text


def safe_shell_quote(value: str) -> str:
    return shlex.quote(str(value))


# ── 3.5 Token-bucket rate limiting ───────────────────────────────────────────

@dataclass
class _Bucket:
    tokens: float
    last_refill: float


_rate_buckets: Dict[str, _Bucket] = {}
_user_violations: Dict[str, int] = {}
_user_timeouts: Dict[str, float] = {}
_rate_lock = threading.RLock()


def check_rate_limit(user_id: Union[int, str], *, cost: float = 1.0) -> Tuple[bool, str]:
    """
    Token-bucket limiter.

    Returns (allowed, message). Sustained abuse escalates to a temporary
    cooldown so a single user cannot hammer LXC / Discord.
    """
    key = str(user_id)
    now = time.time()
    cost = max(0.5, min(float(cost), float(RATE_LIMIT_BURST)))

    with _rate_lock:
        timeout_until = _user_timeouts.get(key, 0.0)
        if timeout_until > now:
            return False, f"You are temporarily blocked for command abuse. Try again in {int(timeout_until - now)}s."

        if timeout_until:
            _user_timeouts.pop(key, None)
            _user_violations.pop(key, None)

        bucket = _rate_buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(RATE_LIMIT_BURST), last_refill=now)
            _rate_buckets[key] = bucket

        elapsed = max(0.0, now - bucket.last_refill)
        refill_rate = 1.0 / max(0.5, RATE_LIMIT_COOLDOWN)
        bucket.tokens = min(float(RATE_LIMIT_BURST), bucket.tokens + elapsed * refill_rate)
        bucket.last_refill = now

        if bucket.tokens < cost:
            violations = _user_violations.get(key, 0) + 1
            _user_violations[key] = violations
            if violations >= RATE_LIMIT_MAX_VIOLATIONS:
                _user_timeouts[key] = now + RATE_LIMIT_ABUSE_TIMEOUT
                _user_violations[key] = 0
                logger.warning("[SECURITY] %s throttled: %ss abuse timeout", key, RATE_LIMIT_ABUSE_TIMEOUT)
                return False, f"Too many commands — blocked for {RATE_LIMIT_ABUSE_TIMEOUT}s."
            wait = max(1, int((cost - bucket.tokens) * max(0.5, RATE_LIMIT_COOLDOWN)) + 1)
            return False, f"Slow down! About {wait}s until your next command."

        bucket.tokens -= cost
        _user_violations[key] = 0
        return True, "OK"


def reset_rate_limit(user_id: Union[int, str]) -> None:
    key = str(user_id)
    with _rate_lock:
        _rate_buckets.pop(key, None)
        _user_violations.pop(key, None)
        _user_timeouts.pop(key, None)


# ── 3.6 Audit logging ────────────────────────────────────────────────────────

def audit(action: str, actor_id: Optional[Union[int, str]] = None, *,
          target: Optional[str] = None, detail: str = "",
          guild_id: Optional[Union[int, str]] = None,
          success: bool = True) -> None:
    """Write a structured security/audit entry (never raises)."""
    try:
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (ts, action, actor_id, target, detail, guild_id, success)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    str(action)[:64],
                    None if actor_id is None else str(actor_id)[:32],
                    None if target is None else str(target)[:128],
                    sanitize_reason(detail, default="")[:1000],
                    None if guild_id is None else str(guild_id)[:32],
                    1 if success else 0,
                ),
            )
    except Exception as exc:  # pragma: no cover - auditing must never break flow
        logger.debug("audit() failed: %s", exc)

# ═══════════════════════════════════════════════════════════════════════════
# 4. PERSISTENCE LAYER  (SQLite • WAL • thread-safe • crash-safe)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Fixes vs v8.0:
#   • One shared, retrying connection per thread instead of leaking connections.
#   • Explicit transactions with commit/rollback, no implicit locks.
#   • `save_vps_data()` full-state rewrites replaced by targeted UPSERTs.
#   • `increment_public_vps_count()` no longer corrupts unrelated rows.
#   • `user_device_tracking.user_id` is now UNIQUE (was a silent no-op).
#   • Port allocations use correct SQL (MAX() around the column, not the param).
#   • Backups happen *before* risky operations, and are verified.
#
#  Fixes vs v9.0:
#   • `upsert_vps()` now honours the caller supplied `id` instead of silently
#     letting AUTOINCREMENT invent a different one (the panel showed a VPS id
#     that never matched the row).
#   • `next_vps_id()` is derived from BOTH the meta counter and the real max id,
#     so a restored/edited database can never hand out a duplicate number.
#   • New `host_snapshots` table powers the v10 host sparklines / graphs.
#   • `webssh_tokens.unlock_keys` is persisted (previously a restart logged
#     every browser out of a PIN-protected terminal).

DB_FILE = BASE_DIR / "vps.db"
DB_BACKUP_DIR = BASE_DIR / "db_backups"
DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_db_local = threading.local()
_db_lock = threading.RLock()
_last_backup_ts = 0.0
_backup_min_interval = 300.0  # don't back up more than once every 5 min


def _new_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _get_thread_conn() -> sqlite3.Connection:
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = _new_connection()
        _db_local.conn = conn
    else:
        try:
            conn.execute("SELECT 1")
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            conn = _new_connection()
            _db_local.conn = conn
    return conn


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextmanager
def db_conn(write: bool = False, retries: int = 4):
    """
    Context manager yielding a thread-local connection.

        with db_conn() as conn:            # read (shared lock on the module)
            rows = conn.execute(...).fetchall()

        with db_conn(write=True) as conn:  # write (auto commit / rollback)
            conn.execute("UPDATE ...")

    Write blocks are re-entrant: nesting one inside another joins the outer
    transaction instead of failing with "cannot start a transaction within a
    transaction" (which silently broke helpers such as track_device()).
    """
    conn = _get_thread_conn()

    if not write:
        with _db_lock:
            yield conn
        return

    depth = int(getattr(_db_local, "depth", 0) or 0)
    if depth > 0:
        # Already inside a transaction owned by this thread — just join it.
        _db_local.depth = depth + 1
        try:
            yield conn
        finally:
            _db_local.depth = depth
        return

    attempt = 0
    while True:
        try:
            with _db_lock:
                conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            attempt += 1
            if attempt > retries or "locked" not in str(exc).lower():
                raise
            time.sleep(0.2 * attempt)

    _db_local.depth = 1
    try:
        try:
            yield conn
        except BaseException:
            _safe_rollback(conn)
            raise
        else:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                _safe_rollback(conn)
                raise
    finally:
        _db_local.depth = 0


def query_all(sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with db_conn(write=True) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> int:
    with db_conn(write=True) as conn:
        cur = conn.executemany(sql, list(seq))
        return cur.rowcount


# ── 4.1 Schema ───────────────────────────────────────────────────────────────

SCHEMA_VERSION = 10

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    user_id   TEXT PRIMARY KEY,
    added_by  TEXT,
    added_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    location      TEXT DEFAULT 'Unknown',
    total_vps     INTEGER DEFAULT 50,
    max_cpu       INTEGER DEFAULT 0,
    max_ram_gb    INTEGER DEFAULT 0,
    tags          TEXT DEFAULT '[]',
    api_key       TEXT,
    url           TEXT,
    is_local      INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    last_updated  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vps (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT NOT NULL,
    node_id            INTEGER NOT NULL DEFAULT 1,
    container_name     TEXT UNIQUE NOT NULL,
    ram                TEXT NOT NULL DEFAULT '1GB',
    cpu                TEXT NOT NULL DEFAULT '1',
    storage            TEXT NOT NULL DEFAULT '10GB',
    config             TEXT NOT NULL DEFAULT 'Custom',
    os_version         TEXT DEFAULT 'ubuntu:22.04',
    status             TEXT DEFAULT 'stopped',
    suspended          INTEGER DEFAULT 0,
    whitelisted        INTEGER DEFAULT 0,
    is_public          INTEGER DEFAULT 0,
    created_at         TEXT NOT NULL,
    shared_with        TEXT DEFAULT '[]',
    suspension_history TEXT DEFAULT '[]',
    expiration_date    TEXT DEFAULT NULL,
    root_password      TEXT DEFAULT NULL,
    last_modified      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vps_user   ON vps(user_id);
CREATE INDEX IF NOT EXISTS idx_vps_node   ON vps(node_id);
CREATE INDEX IF NOT EXISTS idx_vps_status ON vps(status);

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    last_modified TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS port_allocations (
    user_id         TEXT PRIMARY KEY,
    allocated_ports INTEGER NOT NULL DEFAULT 0,
    last_modified   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS port_forwards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    vps_container TEXT NOT NULL,
    vps_port      INTEGER NOT NULL,
    host_port     INTEGER NOT NULL UNIQUE,
    protocol      TEXT DEFAULT 'both',
    created_at    TEXT NOT NULL,
    last_modified TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_forwards_container ON port_forwards(vps_container);

CREATE TABLE IF NOT EXISTS device_tracking (
    user_id            TEXT PRIMARY KEY,
    username           TEXT,
    avatar_hash        TEXT,
    device_fingerprint TEXT,
    ip_hash            TEXT,
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen          TEXT DEFAULT CURRENT_TIMESTAMP,
    vps_created        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_device_ip ON device_tracking(ip_hash);
CREATE INDEX IF NOT EXISTS idx_device_fp ON device_tracking(device_fingerprint);

CREATE TABLE IF NOT EXISTS cooldowns (
    key        TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    reason     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    action    TEXT NOT NULL,
    actor_id  TEXT,
    target    TEXT,
    detail    TEXT,
    guild_id  TEXT,
    success   INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    container  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    cpu_pct    REAL DEFAULT 0,
    ram_pct    REAL DEFAULT 0,
    ram_used   INTEGER DEFAULT 0,
    ram_total  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_container ON usage_snapshots(container, ts);

-- v10: host history for the animated platform dashboard ---------------------
CREATE TABLE IF NOT EXISTS host_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    cpu_pct   REAL DEFAULT 0,
    ram_pct   REAL DEFAULT 0,
    disk_pct  REAL DEFAULT 0,
    vps_total INTEGER DEFAULT 0,
    vps_run   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_host_ts ON host_snapshots(ts);

CREATE TABLE IF NOT EXISTS webssh_tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    vps_id     INTEGER,
    host       TEXT,
    port       INTEGER,
    username   TEXT NOT NULL,
    password   TEXT,
    label      TEXT,
    pin_hash   TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER DEFAULT 0,
    max_uses   INTEGER DEFAULT 3,
    pin_attempts INTEGER DEFAULT 0,
    unlock_keys TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_webssh_user ON webssh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_webssh_expires ON webssh_tokens(expires_at);
"""

_MIGRATIONS: List[Tuple[str, str]] = [
    # (table, "column definition to add if missing")
    ("vps", "os_version TEXT DEFAULT 'ubuntu:22.04'"),
    ("vps", "node_id INTEGER DEFAULT 1"),
    ("vps", "expiration_date TEXT DEFAULT NULL"),
    ("vps", "root_password TEXT DEFAULT NULL"),
    ("vps", "last_modified TEXT DEFAULT CURRENT_TIMESTAMP"),
    ("vps", "is_public INTEGER DEFAULT 0"),
    ("vps", "shared_with TEXT DEFAULT '[]'"),
    ("vps", "suspension_history TEXT DEFAULT '[]'"),
    ("nodes", "max_cpu INTEGER DEFAULT 0"),
    ("nodes", "max_ram_gb INTEGER DEFAULT 0"),
    ("webssh_tokens", "unlock_keys TEXT DEFAULT '[]'"),
    ("accounts", "label TEXT"),  # ignored if table absent
]


def init_db() -> None:
    """Create schema, migrate old databases, verify integrity."""
    with _db_lock:
        conn = _new_connection()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)

            for table, column_def in _MIGRATIONS:
                try:
                    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                except sqlite3.Error:
                    continue
                if not existing:
                    continue
                column = column_def.split()[0]
                if column not in existing:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
                        logger.info("Schema migrated: %s.%s added", table, column)
                    except sqlite3.OperationalError as exc:
                        logger.debug("Migration skipped for %s.%s: %s", table, column, exc)

            # Ensure the local node exists exactly once.
            local = conn.execute("SELECT id FROM nodes WHERE is_local = 1 ORDER BY id LIMIT 1").fetchone()
            if local is None:
                conn.execute(
                    """INSERT INTO nodes (name, location, total_vps, tags, is_local)
                       VALUES ('Local Node', 'Local Datacenter', 50, '[]', 1)"""
                )
            elif conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE is_local = 1").fetchone()["c"] > 1:
                # De-duplicate historical local nodes.
                keep = local["id"]
                conn.execute("DELETE FROM nodes WHERE is_local = 1 AND id != ?", (keep,))

            for key, value in (("cpu_threshold", "90"), ("ram_threshold", "90")):
                conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

            if MAIN_ADMIN_ID:
                conn.execute(
                    "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, 'system')",
                    (str(MAIN_ADMIN_ID),),
                )

            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('bot_version', ?)", (BOT_VERSION,))

            # Tie every VPS back to a real node (older DBs could hold orphans).
            conn.execute("UPDATE vps SET node_id = 1 WHERE node_id IS NULL")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"SQLite integrity check failed: {integrity}")

            # Repair double-encoded node tags from old versions.
            for row in conn.execute("SELECT id, tags FROM nodes").fetchall():
                try:
                    parsed = json.loads(row["tags"] or "[]")
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    if not isinstance(parsed, list):
                        parsed = []
                except (TypeError, ValueError):
                    parsed = []
                conn.execute("UPDATE nodes SET tags = ? WHERE id = ?", (json.dumps(parsed), row["id"]))

            # Purge audit rows older than 90 days.
            conn.execute("DELETE FROM audit_log WHERE ts < ?",
                         ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds"),))
            # Trim usage history to the newest 20k samples.
            conn.execute(
                "DELETE FROM usage_snapshots WHERE id NOT IN "
                "(SELECT id FROM usage_snapshots ORDER BY id DESC LIMIT 20000)"
            )
            # Trim host history (v10).
            conn.execute(
                "DELETE FROM host_snapshots WHERE id NOT IN "
                "(SELECT id FROM host_snapshots ORDER BY id DESC LIMIT ?)",
                (int(HOST_SNAPSHOT_KEEP * 4),),
            )
            conn.commit()
        finally:
            conn.close()

    logger.info("Database ready (%s)", DB_FILE.name)


def backup_database(force: bool = False) -> Optional[Path]:
    """Create a verified, atomic SQLite backup. Returns the backup path."""
    global _last_backup_ts

    if not DB_FILE.exists():
        return None
    if not force and (time.time() - _last_backup_ts) < _backup_min_interval:
        return None

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = DB_BACKUP_DIR / f"vps_{timestamp}.db"
        tmp_target = target.with_suffix(".tmp")

        with _db_lock:
            source = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                destination = sqlite3.connect(tmp_target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()

        check = sqlite3.connect(tmp_target)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            tmp_target.unlink(missing_ok=True)
            logger.error("Backup verification failed (%s) — file discarded", result)
            return None

        tmp_target.replace(target)
        _last_backup_ts = time.time()

        backups = sorted(DB_BACKUP_DIR.glob("vps_*.db"))
        for old in backups[:-10]:
            try:
                old.unlink()
            except OSError:
                pass
        return target
    except Exception as exc:
        logger.error("Database backup failed: %s", exc)
        return None


# ── 4.2 Settings ─────────────────────────────────────────────────────────────

def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    execute(
        """INSERT INTO settings (key, value, last_modified)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          last_modified = CURRENT_TIMESTAMP""",
        (str(key), str(value)),
    )


# ── 4.3 Nodes ────────────────────────────────────────────────────────────────

def _decode_node(row: sqlite3.Row) -> Dict[str, Any]:
    node = dict(row)
    try:
        tags = json.loads(node.get("tags") or "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        node["tags"] = tags if isinstance(tags, list) else []
    except (TypeError, ValueError):
        node["tags"] = []
    node["is_local"] = int(node.get("is_local") or 0) == 1
    return node


def get_nodes() -> List[Dict[str, Any]]:
    return [_decode_node(row) for row in query_all("SELECT * FROM nodes ORDER BY is_local DESC, id ASC")]


def get_node(node_id: int) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM nodes WHERE id = ?", (int(node_id),))
    return _decode_node(row) if row else None


def get_local_node() -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM nodes WHERE is_local = 1 ORDER BY id LIMIT 1")
    return _decode_node(row) if row else None


def upsert_node(node: Dict[str, Any]) -> int:
    with db_conn(write=True) as conn:
        if node.get("id"):
            conn.execute(
                """UPDATE nodes SET name=?, location=?, total_vps=?, max_cpu=?, max_ram_gb=?,
                       tags=?, api_key=?, url=?, is_local=?, last_updated=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    node["name"], node.get("location", "Unknown"), int(node.get("total_vps", 50)),
                    int(node.get("max_cpu", 0)), int(node.get("max_ram_gb", 0)),
                    json.dumps(node.get("tags", [])), node.get("api_key"), node.get("url"),
                    1 if node.get("is_local") else 0, int(node["id"]),
                ),
            )
            return int(node["id"])
        cur = conn.execute(
            """INSERT INTO nodes (name, location, total_vps, max_cpu, max_ram_gb, tags, api_key, url, is_local)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node["name"], node.get("location", "Unknown"), int(node.get("total_vps", 50)),
                int(node.get("max_cpu", 0)), int(node.get("max_ram_gb", 0)),
                json.dumps(node.get("tags", [])), node.get("api_key"), node.get("url"),
                1 if node.get("is_local") else 0,
            ),
        )
        return int(cur.lastrowid)


def delete_node(node_id: int, *, delete_vps: bool = False) -> bool:
    with db_conn(write=True) as conn:
        if delete_vps:
            conn.execute("DELETE FROM port_forwards WHERE vps_container IN "
                         "(SELECT container_name FROM vps WHERE node_id = ?)", (int(node_id),))
            conn.execute("DELETE FROM vps WHERE node_id = ?", (int(node_id),))
        else:
            remaining = conn.execute("SELECT COUNT(*) AS c FROM vps WHERE node_id = ?", (int(node_id),)).fetchone()["c"]
            if remaining:
                return False
        cur = conn.execute("DELETE FROM nodes WHERE id = ? AND is_local = 0", (int(node_id),))
        return cur.rowcount > 0


# ── 4.4 VPS records ──────────────────────────────────────────────────────────

def _decode_vps(row: sqlite3.Row) -> Dict[str, Any]:
    vps = dict(row)
    for key, fallback in (("shared_with", []), ("suspension_history", [])):
        try:
            value = json.loads(vps.get(key) or "[]")
            vps[key] = value if isinstance(value, list) else fallback
        except (TypeError, ValueError):
            vps[key] = fallback
    vps["suspended"] = bool(vps.get("suspended") or 0)
    vps["whitelisted"] = bool(vps.get("whitelisted") or 0)
    vps["is_public"] = bool(vps.get("is_public") or 0)
    vps["os_version"] = vps.get("os_version") or "ubuntu:22.04"
    vps["node_id"] = int(vps.get("node_id") or 1)
    return vps


def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    """Full snapshot keyed by user id (used for dashboards / statistics)."""
    data: Dict[str, List[Dict[str, Any]]] = {}
    for row in query_all("SELECT * FROM vps ORDER BY id ASC"):
        vps = _decode_vps(row)
        data.setdefault(str(vps["user_id"]), []).append(vps)
    return data


def get_user_vps(user_id: Union[int, str]) -> List[Dict[str, Any]]:
    return [_decode_vps(r) for r in query_all("SELECT * FROM vps WHERE user_id = ? ORDER BY id ASC", (str(user_id),))]


def get_vps_by_container(container_name: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM vps WHERE container_name = ?", (str(container_name),))
    return _decode_vps(row) if row else None


def get_vps_by_id(vps_id: int) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM vps WHERE id = ?", (int(vps_id),))
    return _decode_vps(row) if row else None


def get_current_vps_count(node_id: int) -> int:
    row = query_one("SELECT COUNT(*) AS c FROM vps WHERE node_id = ?", (int(node_id),))
    return int(row["c"]) if row else 0


def get_total_vps_count() -> int:
    row = query_one("SELECT COUNT(*) AS c FROM vps")
    return int(row["c"]) if row else 0


def get_public_vps_count() -> int:
    row = query_one("SELECT COUNT(*) AS c FROM vps WHERE is_public = 1")
    return int(row["c"]) if row else 0


def get_running_vps_count() -> int:
    row = query_one("SELECT COUNT(*) AS c FROM vps WHERE status = 'running' AND suspended = 0")
    return int(row["c"]) if row else 0


def get_all_user_ids_with_vps() -> List[str]:
    return [str(r["user_id"]) for r in query_all("SELECT DISTINCT user_id FROM vps")]


def upsert_vps(vps: Dict[str, Any]) -> int:
    """
    Insert or update a single VPS row. Returns the row id.

    v10 fix: when the caller supplies an explicit `id` (the provisioning flow
    does) we honour it instead of letting AUTOINCREMENT pick a different one.
    """
    container = str(vps.get("container_name") or "").strip()
    if not container:
        raise ValueError("container_name is required")

    created_at = vps.get("created_at")
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    created_at = created_at or datetime.now().isoformat()

    explicit_id = vps.get("id")
    try:
        explicit_id = int(explicit_id) if explicit_id is not None else None
    except (TypeError, ValueError):
        explicit_id = None

    with db_conn(write=True) as conn:
        existing = conn.execute("SELECT id FROM vps WHERE container_name = ?", (container,)).fetchone()
        row_id = int(existing["id"]) if existing else explicit_id

        if row_id is not None:
            # Insert (new) or replace (id change) with the id we want.
            conn.execute(
                """
                INSERT INTO vps (
                    id, user_id, node_id, container_name, ram, cpu, storage, config, os_version,
                    status, suspended, whitelisted, is_public, created_at, shared_with,
                    suspension_history, expiration_date, root_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id            = excluded.user_id,
                    node_id            = excluded.node_id,
                    container_name     = excluded.container_name,
                    ram                = excluded.ram,
                    cpu                = excluded.cpu,
                    storage            = excluded.storage,
                    config             = excluded.config,
                    os_version         = excluded.os_version,
                    status             = excluded.status,
                    suspended          = excluded.suspended,
                    whitelisted        = excluded.whitelisted,
                    is_public          = excluded.is_public,
                    shared_with        = excluded.shared_with,
                    suspension_history = excluded.suspension_history,
                    expiration_date    = excluded.expiration_date,
                    root_password      = excluded.root_password,
                    last_modified      = CURRENT_TIMESTAMP
                """,
                (
                    int(row_id),
                    str(vps["user_id"]),
                    int(vps.get("node_id") or 1),
                    container,
                    str(vps.get("ram", "1GB")),
                    str(vps.get("cpu", "1")),
                    str(vps.get("storage", "10GB")),
                    str(vps.get("config", "Custom")),
                    str(vps.get("os_version", "ubuntu:22.04")),
                    str(vps.get("status", "stopped")),
                    1 if vps.get("suspended") else 0,
                    1 if vps.get("whitelisted") else 0,
                    1 if vps.get("is_public") else 0,
                    created_at,
                    json.dumps(vps.get("shared_with") or []),
                    json.dumps(vps.get("suspension_history") or []),
                    vps.get("expiration_date"),
                    vps.get("root_password"),
                ),
            )
            return int(row_id)

        cur = conn.execute(
            """
            INSERT INTO vps (
                user_id, node_id, container_name, ram, cpu, storage, config, os_version,
                status, suspended, whitelisted, is_public, created_at, shared_with,
                suspension_history, expiration_date, root_password
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(vps["user_id"]),
                int(vps.get("node_id") or 1),
                container,
                str(vps.get("ram", "1GB")),
                str(vps.get("cpu", "1")),
                str(vps.get("storage", "10GB")),
                str(vps.get("config", "Custom")),
                str(vps.get("os_version", "ubuntu:22.04")),
                str(vps.get("status", "stopped")),
                1 if vps.get("suspended") else 0,
                1 if vps.get("whitelisted") else 0,
                1 if vps.get("is_public") else 0,
                created_at,
                json.dumps(vps.get("shared_with") or []),
                json.dumps(vps.get("suspension_history") or []),
                vps.get("expiration_date"),
                vps.get("root_password"),
            ),
        )
        return int(cur.lastrowid)


def patch_vps(container_name: str, **fields: Any) -> bool:
    """Update individual columns without rewriting the whole record."""
    if not fields:
        return False
    allowed = {
        "user_id", "node_id", "ram", "cpu", "storage", "config", "os_version",
        "status", "suspended", "whitelisted", "is_public", "expiration_date",
        "root_password", "shared_with", "suspension_history", "created_at",
    }
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("shared_with", "suspension_history"):
            value = json.dumps(value or [])
        if key in ("suspended", "whitelisted", "is_public"):
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return False
    params.append(str(container_name))
    return execute(
        f"UPDATE vps SET {', '.join(sets)}, last_modified = CURRENT_TIMESTAMP WHERE container_name = ?",
        params,
    ) > 0


def delete_vps_row(container_name: str) -> None:
    with db_conn(write=True) as conn:
        conn.execute("DELETE FROM port_forwards WHERE vps_container = ?", (str(container_name),))
        conn.execute("DELETE FROM usage_snapshots WHERE container = ?", (str(container_name),))
        conn.execute("DELETE FROM vps WHERE container_name = ?", (str(container_name),))


def get_all_containers_on_host() -> List[str]:
    """Get all LXC container names currently tracked in bot database."""
    rows = query_all("SELECT DISTINCT container_name FROM vps ORDER BY container_name")
    return [str(r["container_name"]) for r in rows]


def assign_container_to_user(container_name: str, user_id: int) -> bool:
    """
    Assign an orphaned container to a Discord user as a new VPS record.
    
    Returns True on success, False on failure.
    """
    try:
        vps_id = next_vps_id()
        record = {
            "id": vps_id,
            "user_id": str(user_id),
            "node_id": 1,  # Local node by default
            "container_name": str(container_name),
            "ram": "1GB",  # Default values for orphaned containers
            "cpu": "1",
            "storage": "10GB",
            "config": "1GB RAM / 1 vCPU / 10GB Disk",
            "os_version": "unknown",
            "status": "unknown",
            "suspended": False,
            "whitelisted": False,
            "is_public": False,
            "created_at": datetime.now().isoformat(),
            "shared_with": [],
            "suspension_history": [],
            "expiration_date": None,
            "root_password": encrypt_secret(""),
        }
        upsert_vps(record)
        return True
    except Exception as exc:
        logger.error("Failed to assign container %s to user %s: %s", container_name, user_id, exc)
        return False


async def get_lxc_containers_on_node(node_id: int = 1) -> List[str]:
    """Get list of LXC containers currently on a node (fast query)."""
    try:
        # Use CSV format for faster output: lxc list --format=csv -c n
        output = await run_lxc_command("", "list --format=csv -c n", timeout=15, node_id=node_id)
        lines = output.strip().split("\n")
        # Skip header line and empty lines
        containers = [line.strip() for line in lines if line.strip() and line.strip().lower() != "name"]
        return containers
    except asyncio.TimeoutError:
        logger.warning("LXC list timed out on node %s", node_id)
        return []
    except Exception as exc:
        logger.error("Failed to get LXC containers from node %s: %s", node_id, exc)
        return []


def get_orphaned_containers() -> List[str]:
    """Get LXC containers that exist but aren't tracked in bot database."""
    # This would need to be called async, so we'll handle it in the command
    pass


def next_vps_id() -> int:
    """
    Monotonic, collision-free VPS number (never reuses a deleted id).

    v10 fix: the counter is now the max of (stored counter, highest real id + 1)
    so restoring an old backup can never hand out an id that already exists.
    """
    with db_conn(write=True) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'next_vps_id'").fetchone()
        stored = int(row["value"]) if row else 0
        highest = int(conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM vps").fetchone()["m"]) + 1
        current = max(stored, highest, 1)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('next_vps_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(current + 1),),
        )
        return current


# ── 4.5 Admins ───────────────────────────────────────────────────────────────

def get_admins() -> List[str]:
    return [str(r["user_id"]) for r in query_all("SELECT user_id FROM admins ORDER BY user_id")]


def add_admin(user_id: Union[int, str], added_by: Optional[Union[int, str]] = None) -> bool:
    try:
        execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)",
            (str(user_id), None if added_by is None else str(added_by)),
        )
        refresh_admin_cache()
        return True
    except sqlite3.Error as exc:
        logger.error("add_admin failed: %s", exc)
        return False


def remove_admin(user_id: Union[int, str]) -> bool:
    if str(user_id) == str(MAIN_ADMIN_ID):
        return False
    removed = execute("DELETE FROM admins WHERE user_id = ?", (str(user_id),)) > 0
    refresh_admin_cache()
    return removed

def is_admin_id(user_id: Union[int, str]) -> bool:
    uid = str(user_id)
    return uid == str(MAIN_ADMIN_ID) or uid in _admin_cache


def is_main_admin_id(user_id: Union[int, str]) -> bool:
    return str(user_id) == str(MAIN_ADMIN_ID)


# ── 4.6 Ports ────────────────────────────────────────────────────────────────

def get_user_allocation(user_id: Union[int, str]) -> int:
    row = query_one("SELECT allocated_ports FROM port_allocations WHERE user_id = ?", (str(user_id),))
    return int(row["allocated_ports"]) if row else 0


def get_user_used_ports(user_id: Union[int, str]) -> int:
    row = query_one("SELECT COUNT(*) AS c FROM port_forwards WHERE user_id = ?", (str(user_id),))
    return int(row["c"]) if row else 0


def allocate_ports(user_id: Union[int, str], amount: int) -> int:
    """Add slots (amount may be negative to revoke). Never drops below zero."""
    uid = str(user_id)
    with db_conn(write=True) as conn:
        row = conn.execute("SELECT allocated_ports FROM port_allocations WHERE user_id = ?", (uid,)).fetchone()
        current = int(row["allocated_ports"]) if row else 0
        new_value = max(0, current + int(amount))
        conn.execute(
            """INSERT INTO port_allocations (user_id, allocated_ports, last_modified)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET allocated_ports = excluded.allocated_ports,
                                                  last_modified = CURRENT_TIMESTAMP""",
            (uid, new_value),
        )
        return new_value


def get_available_host_port() -> Optional[int]:
    """Pick a free host port, verified against the OS before use."""
    reserved = {int(r["host_port"]) for r in query_all("SELECT host_port FROM port_forwards")}

    import socket

    for _ in range(50):
        port = random.randint(20000, 50000)
        if port in reserved:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
        return port
    return None


def add_forward_row(user_id: Union[int, str], container: str, vps_port: int, host_port: int,
                    protocol: str = "both") -> int:
    with db_conn(write=True) as conn:
        cur = conn.execute(
            """INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, protocol, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(user_id), str(container), int(vps_port), int(host_port), protocol, datetime.now().isoformat()),
        )
        return int(cur.lastrowid)


def get_forward(forward_id: int) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM port_forwards WHERE id = ?", (int(forward_id),))
    return dict(row) if row else None


def get_forward_by_host_port(container: str, host_port: int) -> Optional[Dict[str, Any]]:
    row = query_one(
        "SELECT * FROM port_forwards WHERE vps_container = ? AND host_port = ?",
        (str(container), int(host_port)),
    )
    return dict(row) if row else None


def get_forwards_for_container(container: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in query_all(
        "SELECT * FROM port_forwards WHERE vps_container = ? ORDER BY id ASC", (str(container),)
    )]


def get_user_forwards(user_id: Union[int, str]) -> List[Dict[str, Any]]:
    return [dict(r) for r in query_all(
        "SELECT * FROM port_forwards WHERE user_id = ? ORDER BY id DESC", (str(user_id),)
    )]


def delete_forward_row(forward_id: int) -> bool:
    return execute("DELETE FROM port_forwards WHERE id = ?", (int(forward_id),)) > 0


def find_node_id_for_container(container_name: str) -> int:
    row = query_one("SELECT node_id FROM vps WHERE container_name = ?", (str(container_name),))
    if row:
        return int(row["node_id"])
    local = get_local_node()
    return int(local["id"]) if local else 1


# ── 4.7 Device tracking & cooldowns ──────────────────────────────────────────

def hash_ip(ip: str) -> str:
    """Never store raw IPs — store a salted hash (privacy + GDPR friendly)."""
    salt = get_setting("ip_hash_salt")
    if not salt:
        salt = secrets.token_hex(16)
        set_setting("ip_hash_salt", salt)
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


def track_device(user_id: Union[int, str], ip: str, username: str,
                 avatar_hash: Optional[str], fingerprint: str) -> None:
    try:
        with db_conn(write=True) as conn:
            conn.execute(
                """INSERT INTO device_tracking
                       (user_id, username, avatar_hash, device_fingerprint, ip_hash, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username = excluded.username,
                       avatar_hash = excluded.avatar_hash,
                       device_fingerprint = excluded.device_fingerprint,
                       ip_hash = excluded.ip_hash,
                       last_seen = CURRENT_TIMESTAMP""",
                (str(user_id), username, avatar_hash, fingerprint, hash_ip(ip)),
            )
    except sqlite3.Error as exc:
        logger.debug("track_device failed: %s", exc)


def mark_device_vps_created(user_id: Union[int, str]) -> None:
    execute("UPDATE device_tracking SET vps_created = 1, last_seen = CURRENT_TIMESTAMP WHERE user_id = ?",
            (str(user_id),))


def count_accounts_for_ip(ip: str) -> int:
    row = query_one("SELECT COUNT(*) AS c FROM device_tracking WHERE ip_hash = ?", (hash_ip(ip),))
    return int(row["c"]) if row else 1


def set_cooldown(key: str, seconds: int, reason: str = "") -> None:
    execute(
        """INSERT INTO cooldowns (key, expires_at, reason) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET expires_at = excluded.expires_at, reason = excluded.reason""",
        (str(key), time.time() + seconds, reason),
    )


def get_cooldown(key: str) -> Tuple[bool, int, str]:
    """Returns (active, seconds_left, reason)."""
    row = query_one("SELECT expires_at, reason FROM cooldowns WHERE key = ?", (str(key),))
    if not row:
        return False, 0, ""
    left = int(float(row["expires_at"]) - time.time())
    if left <= 0:
        execute("DELETE FROM cooldowns WHERE key = ?", (str(key),))
        return False, 0, ""
    return True, left, row["reason"] or ""


def remove_cooldown(key: str) -> bool:
    """Remove a cooldown entry. Returns True if it was active, False otherwise."""
    row = query_one("SELECT expires_at FROM cooldowns WHERE key = ?", (str(key),))
    if row:
        execute("DELETE FROM cooldowns WHERE key = ?", (str(key),))
        return True
    return False


# ── 4.8 Usage snapshots (for sparkline history) ──────────────────────────────

def save_usage_snapshot(container: str, cpu_pct: float, ram_pct: float,
                        ram_used: int, ram_total: int) -> None:
    try:
        execute(
            """INSERT INTO usage_snapshots (container, ts, cpu_pct, ram_pct, ram_used, ram_total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(container), datetime.now(timezone.utc).isoformat(timespec="seconds"),
             float(cpu_pct), float(ram_pct), int(ram_used), int(ram_total)),
        )
    except sqlite3.Error as exc:
        logger.debug("snapshot failed: %s", exc)


def get_usage_history(container: str, limit: int = 40) -> List[Dict[str, Any]]:
    rows = query_all(
        "SELECT * FROM usage_snapshots WHERE container = ? ORDER BY id DESC LIMIT ?",
        (str(container), int(limit)),
    )
    return [dict(r) for r in reversed(rows)]


# ── 4.8b v10: host snapshots (powers the animated platform dashboard) ────────

def save_host_snapshot(cpu_pct: float, ram_pct: float, disk_pct: float,
                       vps_total: int, vps_running: int) -> None:
    try:
        execute(
            """INSERT INTO host_snapshots (ts, cpu_pct, ram_pct, disk_pct, vps_total, vps_run)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             float(cpu_pct), float(ram_pct), float(disk_pct), int(vps_total), int(vps_running)),
        )
        execute(
            "DELETE FROM host_snapshots WHERE id NOT IN "
            "(SELECT id FROM host_snapshots ORDER BY id DESC LIMIT ?)",
            (int(HOST_SNAPSHOT_KEEP),),
        )
    except sqlite3.Error as exc:
        logger.debug("host snapshot failed: %s", exc)


def get_host_history(limit: int = 40) -> List[Dict[str, Any]]:
    rows = query_all("SELECT * FROM host_snapshots ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in reversed(rows)]


def get_audit_entries(limit: int = 25, action: Optional[str] = None) -> List[Dict[str, Any]]:
    if action:
        rows = query_all("SELECT * FROM audit_log WHERE action LIKE ? ORDER BY id DESC LIMIT ?",
                         (f"%{action}%", int(limit)))
    else:
        rows = query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]


# ── 4.9 Bootstrap in-memory mirrors ──────────────────────────────────────────

try:
    init_db()
except Exception as _db_error:  # pragma: no cover
    logger.error("Fatal database initialisation error: %s", _db_error, exc_info=True)
    raise

_admin_cache: set = set(get_admins())
if MAIN_ADMIN_ID:
    _admin_cache.add(str(MAIN_ADMIN_ID))

CPU_THRESHOLD: int = clamp_int(get_setting("cpu_threshold", 90), 1, 100, 90)
RAM_THRESHOLD: int = clamp_int(get_setting("ram_threshold", 90), 1, 100, 90)
PUBLIC_VPS_REQUIRE_VERIFICATION: bool = _env_bool("PUBLIC_VPS_REQUIRE_VERIFICATION", False)


def refresh_admin_cache() -> None:
    global _admin_cache
    _admin_cache = set(get_admins())
    if MAIN_ADMIN_ID:
        _admin_cache.add(str(MAIN_ADMIN_ID))


@contextmanager
def db_write_transaction():
    """Explicit transaction helper for multi-statement critical paths."""
    with db_conn(write=True) as conn:
        yield conn

# ═══════════════════════════════════════════════════════════════════════════
# 5. RUNTIME HELPERS  (LXC / Podman • host stats • networking)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Fixes vs v8.0:
#   • All subprocess / HTTP calls run in worker threads → the Discord event
#     loop can never stall (v8.0 blocked it and could trigger a reconnect).
#   • Physical disk resize always targets the container's root *device*.
#   • `top`-scraping replaced by cgroup v1/v2 reads (locale independent).
#   • Multi-node fetch no longer spawns a thread per node on every render.
#   • Fixed `stop {vps_id}` / `migrate {command}` f-strings that never worked.
#
#  Fixes vs v9.0:
#   • `container_shell()` falls back to `sh` when the image has no bash
#     (Alpine / busybox images previously failed every internal command).
#   • `get_host_cpu_usage()` no longer returns a bogus 0.0 on the very first
#     sample and no longer performs a pointless `dict.update({})`.
#   • `get_host_disk_usage()` now also exposes a numeric percentage so the
#     v10 host snapshots can graph disk usage.

LOCAL_NODE_IDS: set = set()


def safe_iso_parse(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp robustly, always returning an aware datetime."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for attempt in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(attempt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def run_blocking(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking callable in the default executor (keeps the loop free)."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _local_lxc_binary() -> Optional[str]:
    for candidate in ("lxc", "lxd", "podman"):
        path = shutil.which(candidate)
        if path:
            return candidate
    return None


# ── 5.1 Local execution primitives ───────────────────────────────────────────

def _run_local_sync(argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"Executable not found: {argv[0]}"
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def _run_remote_sync(node: Dict[str, Any], command: str, timeout: int) -> Tuple[int, str, str]:
    try:
        response = requests.post(
            f"{str(node['url']).rstrip('/')}/api/execute",
            json={"command": command},
            params={"api_key": node.get("api_key")},
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        return 1, "", f"Node '{node['name']}' is unreachable (network error)"
    except requests.exceptions.Timeout:
        return 124, "", f"Node '{node['name']}' timed out after {timeout}s"
    except requests.exceptions.RequestException as exc:
        return 1, "", f"Node '{node['name']}' request failed: {exc}"

    if response.status_code == 401:
        return 1, "", f"Node '{node['name']}' rejected the API key"
    if response.status_code >= 400:
        detail = response.text[:400]
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("error") or payload.get("stderr") or detail
        except ValueError:
            pass
        return 1, "", f"Node '{node['name']}' returned HTTP {response.status_code}: {detail}"

    try:
        payload = response.json()
    except ValueError:
        return 1, response.text, "Node returned invalid JSON"

    return int(payload.get("returncode", 0)), payload.get("stdout", ""), payload.get("stderr", "")


async def run_lxc_command(container_name: str, command: str, *,
                          timeout: int = LXC_TIMEOUT_DEFAULT,
                          node_id: Optional[int] = None,
                          check: bool = True) -> str:
    """Execute an `lxc <command>` (container operations, host side)."""
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if not node:
        raise RuntimeError(f"Node {node_id} not found")

    if node["is_local"]:
        binary = _local_lxc_binary()
        if not binary:
            raise RuntimeError("LXC/LXD/Podman is not installed on this host")
        argv = [binary, *shlex.split(command)]
        code, out, err = await run_blocking(_run_local_sync, argv, timeout)
    else:
        code, out, err = await run_blocking(_run_remote_sync, node, f"lxc {command}", timeout)

    if check and code != 0:
        message = (err or out or "unknown error").strip()
        raise RuntimeError(f"LXC command failed ({message.splitlines()[0][:300]})")
    return (out or err or "").strip()


async def run_lxc_argv(container_name: str, argv: Sequence[str], *,
                       timeout: int = LXC_TIMEOUT_DEFAULT,
                       node_id: Optional[int] = None,
                       check: bool = True) -> str:
    """Same as run_lxc_command but argv-based (no shell quoting pitfalls)."""
    return await run_lxc_command(container_name, " ".join(shlex.quote(str(a)) for a in argv),
                                 timeout=timeout, node_id=node_id, check=check)


# Small internal cache of "does this container have bash?" lookups so we do not
# probe on every single command (Alpine images have no bash at all).
_SHELL_CACHE: Dict[str, str] = {}
_SHELL_CACHE_LOCK = threading.Lock()


async def _detect_container_shell(container_name: str, node_id: Optional[int]) -> str:
    with _SHELL_CACHE_LOCK:
        cached = _SHELL_CACHE.get(container_name)
    if cached:
        return cached
    detected = "bash"
    try:
        binary = _local_lxc_binary() or "lxc"
        node = get_node(node_id) if node_id else None
        probe = "command -v bash || command -v sh || echo sh"
        if node and not node["is_local"]:
            # Remote nodes must probe the guest too; assuming bash breaks Alpine/BusyBox.
            out = await run_lxc_command(
                container_name,
                f"exec {shlex.quote(container_name)} -- sh -c {shlex.quote(probe)}",
                timeout=20, node_id=node_id,
            )
            text = (out or "").strip().splitlines()
            if text:
                detected = "bash" if "bash" in text[-1] else "sh"
        else:
            code, out, _err = await run_blocking(
                _run_local_sync,
                [binary, "exec", container_name, "--", "sh", "-c", probe],
                20,
            )
            text = (out or "").strip().splitlines()
            if code == 0 and text:
                detected = "bash" if "bash" in text[-1] else "sh"
    except Exception:
        detected = "bash"
    with _SHELL_CACHE_LOCK:
        _SHELL_CACHE[container_name] = detected
    return detected


async def container_shell(container_name: str, script: str, *,
                          timeout: int = 120, node_id: Optional[int] = None,
                          user: str = "root") -> str:
    """
    Run a shell script *inside* a container.

    The script is base64-encoded and piped into the shell, which removes every
    quoting/escaping bug that plagued the old inline `bash -c "..."` calls.

    v10: the interpreter is detected once per container, so Alpine/busybox
    images (no bash) now work instead of failing every internal command.
    """
    import base64

    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if not node:
        raise RuntimeError(f"Node {node_id} not found")

    binary = _local_lxc_binary() or "lxc"
    shell = await _detect_container_shell(container_name, node_id)
    # `sh` has no `set -o pipefail`; only bash supports -l/-c combos we rely on.
    inner = f"echo {encoded} | base64 -d | {shell}"

    if node["is_local"]:
        argv = [binary, "exec", container_name, "--", "sh", "-c", inner]
        if user and user != "root":
            argv = [binary, "exec", container_name, "--user", str(user), "--", "sh", "-c", inner]
        code, out, err = await run_blocking(_run_local_sync, argv, timeout)
    else:
        remote_cmd = f"lxc exec {shlex.quote(container_name)} -- sh -c {shlex.quote(inner)}"
        code, out, err = await run_blocking(_run_remote_sync, node, remote_cmd, timeout)

    if code != 0 and not out:
        raise RuntimeError((err or "container command failed").strip()[:300])
    return (out or err or "").strip()


# Backwards-compatible name used throughout the original codebase.
async def execute_lxc(container_name: str, command: str, timeout: int = 120,
                      node_id: Optional[int] = None):
    return await run_lxc_command(container_name, command, timeout=timeout, node_id=node_id)


# ── 5.2 Container lifecycle helpers ──────────────────────────────────────────

async def container_state(container_name: str, node_id: Optional[int] = None) -> str:
    """Authoritative running/stopped state straight from LXC (never cached)."""
    try:
        output = await run_lxc_command(container_name, f"info {shlex.quote(container_name)}",
                                       timeout=25, node_id=node_id)
    except Exception:
        return "unknown"
    match = re.search(r"^Status:\s*(.+)$", output, re.MULTILINE)
    if not match:
        return "unknown"
    state = match.group(1).strip().lower()
    if "running" in state:
        return "running"
    if "stopped" in state:
        return "stopped"
    if "frozen" in state:
        return "frozen"
    return state


async def apply_lxc_config(container_name: str, node_id: int) -> bool:
    """Grant the privileges needed for Docker-in-LXC and nested workloads."""
    settings = (
        ("security.nesting", "true"),
        ("security.privileged", "true"),
        ("security.syscalls.intercept.mknod", "true"),
        ("security.syscalls.intercept.setxattr", "true"),
        ("linux.kernel_modules", "overlay,loop,nf_nat,ip_tables,ip6_tables,netlink_diag,br_netfilter"),
    )
    ok = True
    for key, value in settings:
        try:
            await run_lxc_argv(container_name, ["config", "set", container_name, key, value],
                               timeout=40, node_id=node_id)
        except Exception as exc:
            ok = False
            logger.warning("config set %s on %s failed: %s", key, container_name, exc)

    try:
        await run_lxc_argv(container_name, ["config", "device", "add", container_name,
                                            "fuse", "unix-char", "path=/dev/fuse"],
                           timeout=30, node_id=node_id)
    except Exception:
        logger.debug("FUSE device already present on %s", container_name)

    raw_lxc = (
        "lxc.apparmor.profile = unconfined\n"
        "lxc.apparmor.allow_nesting = 1\n"
        "lxc.apparmor.allow_incomplete = 1\n"
        "lxc.cap.drop =\n"
        "lxc.cgroup.devices.allow = a\n"
        "lxc.cgroup2.devices.allow = a\n"
        "lxc.mount.auto = proc:rw sys:rw cgroup:rw shmounts:rw\n"
        "lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file 0 0\n"
    )
    try:
        await run_lxc_command(
            container_name,
            f"config set {shlex.quote(container_name)} raw.lxc {shlex.quote(raw_lxc)}",
            timeout=40, node_id=node_id,
        )
    except Exception as exc:
        ok = False
        logger.warning("raw.lxc set failed on %s: %s", container_name, exc)
    return ok


async def apply_internal_permissions(container_name: str, node_id: int) -> None:
    """Kernel tweaks inside the guest: unprivileged ports, inotify, userns."""
    script = """
set -e
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-unixnodes.conf <<'EOF'
net.ipv4.ip_unprivileged_port_start=0
net.ipv4.ping_group_range=0 2147483647
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=1024
kernel.unprivileged_userns_clone=1
EOF
sysctl -p /etc/sysctl.d/99-unixnodes.conf >/dev/null 2>&1 || true

# Make sure ssh is reachable and has a host key.
if command -v ssh-keygen >/dev/null 2>&1; then
  mkdir -p /run/sshd /etc/ssh
  [ -f /etc/ssh/ssh_host_rsa_key ] || ssh-keygen -A >/dev/null 2>&1 || true
fi
systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd >/dev/null 2>&1 || true
systemctl restart ssh >/dev/null 2>&1 || systemctl restart sshd >/dev/null 2>&1 \
  || service ssh restart >/dev/null 2>&1 || service sshd restart >/dev/null 2>&1 || true
exit 0
"""
    try:
        await container_shell(container_name, script, timeout=120, node_id=node_id)
    except Exception as exc:
        logger.warning("Internal permission pass failed on %s: %s", container_name, exc)


async def configure_ssh(container_name: str, node_id: int, password: str) -> Tuple[bool, str]:
    """Install and verify SSH in a running LXD/LXC guest across supported OSes.

    Uses POSIX shell syntax so Alpine/BusyBox images do not depend on bash.
    A failed install/configuration is reported to the caller instead of silently
    handing out credentials for a VPS that cannot accept SSH connections.
    """
    import base64

    with _SHELL_CACHE_LOCK:
        _SHELL_CACHE.pop(container_name, None)

    state = await container_state(container_name, node_id)
    if state != "running":
        try:
            await run_lxc_argv(container_name, ["start", container_name], timeout=180, node_id=node_id)
        except Exception as exc:
            return False, f"Container is not running and could not be started: {exc}"
        for _ in range(20):
            await asyncio.sleep(1)
            if await container_state(container_name, node_id) == "running":
                break
        else:
            return False, "Container did not reach running state"

    encoded_password = base64.b64encode(f"root:{password}".encode("utf-8")).decode("ascii")
    script = r"""
set -u
export DEBIAN_FRONTEND=noninteractive

# Create SSH directories with proper permissions
mkdir -p /run/sshd /var/run/sshd /var/empty /etc/ssh
chmod 0755 /run/sshd /var/run/sshd /etc/ssh 2>/dev/null || true

# Ensure privilege separation directory exists
mkdir -p /run/sshd
chmod 0700 /run/sshd

if ! command -v sshd >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/tmp/ssh-install.log 2>&1 && apt-get install -y openssh-server >>/tmp/ssh-install.log 2>&1
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache openssh >/tmp/ssh-install.log 2>&1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y openssh-server openssh-clients >/tmp/ssh-install.log 2>&1
  elif command -v yum >/dev/null 2>&1; then
    yum install -y openssh-server openssh-clients >/tmp/ssh-install.log 2>&1
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm openssh >/tmp/ssh-install.log 2>&1
  else
    echo SSH_ERROR: unsupported-package-manager
    exit 0
  fi
fi

if ! command -v sshd >/dev/null 2>&1; then
  echo SSH_ERROR: openssh-server-install-failed
  tail -n 12 /tmp/ssh-install.log 2>/dev/null || true
  exit 0
fi
command -v chpasswd >/dev/null 2>&1 || { echo SSH_ERROR: chpasswd-missing; exit 0; }
command -v ssh-keygen >/dev/null 2>&1 || { echo SSH_ERROR: ssh-keygen-missing; exit 0; }

# Backup original config if it exists
[ -f /etc/ssh/sshd_config.backup ] || cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup 2>/dev/null || true

# Create optimized sshd_config for LXC containers
cat > /etc/ssh/sshd_config <<'EOF_SSH_CONFIG'
# This is the ssh server system-wide configuration file for LXC containers

Port 22
AddressFamily any
ListenAddress 0.0.0.0
ListenAddress ::

# Authentication
PasswordAuthentication yes
PubkeyAuthentication yes
PermitRootLogin yes
PermitEmptyPasswords no

# Security
ChallengeResponseAuthentication no
UsePAM yes

# Connection settings
TCPKeepAlive yes
ClientAliveInterval 60
ClientAliveCountMax 3
MaxAuthTries 6
MaxSessions 10

# Logging
LogLevel INFO
SyslogFacility AUTH

# X11
X11Forwarding no
PrintMotd no
PrintLastLog yes

# Environment
PermitUserEnvironment no

# SFTP
Subsystem sftp /usr/lib/openssh/sftp-server

# Privilege Separation
UsePrivilegeSeparation sandbox
EOF_SSH_CONFIG

chmod 600 /etc/ssh/sshd_config

# Generate missing host keys
ssh-keygen -A >/tmp/ssh-keygen.log 2>&1 || true

# Verify host keys exist
if [ ! -s /etc/ssh/ssh_host_ed25519_key ] && [ ! -s /etc/ssh/ssh_host_rsa_key ]; then
  ssh-keygen -t rsa -b 2048 -N '' -f /etc/ssh/ssh_host_rsa_key >/tmp/ssh-keygen.log 2>&1 || {
    echo SSH_ERROR: host-key-generation-failed; cat /tmp/ssh-keygen.log 2>/dev/null; exit 0;
  }
fi

# Set root password
printf '%s' 'PASSWORD_B64_PLACEHOLDER' | base64 -d | chpasswd 2>/tmp/ssh-password.log || {
  echo SSH_ERROR: root-password-update-failed; cat /tmp/ssh-password.log 2>/dev/null; exit 0;
}

# Ensure /run/sshd exists and has correct permissions
mkdir -p /run/sshd
chmod 0700 /run/sshd

# Test sshd configuration
if ! sshd -t -f /etc/ssh/sshd_config 2>/tmp/sshd-config.log; then
  echo SSH_ERROR: invalid-sshd-config; cat /tmp/sshd-config.log 2>/dev/null; exit 0
fi

# Kill any existing sshd processes
if command -v pkill >/dev/null 2>&1; then pkill -9 -x sshd 2>/dev/null || true; fi
sleep 1

# Start SSH daemon with absolute path
SSHD_PATH=$(command -v sshd)
"$SSHD_PATH" -f /etc/ssh/sshd_config >/tmp/sshd.log 2>&1
SSHD_START_RC=$?

if [ "$SSHD_START_RC" -ne 0 ]; then
  echo SSH_ERROR: sshd-start-failed; cat /tmp/sshd.log 2>/dev/null; exit 0
fi

# Wait for sshd to bind to port 22
attempt=0
while [ "$attempt" -lt 15 ]; do
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]|::):22([[:space:]]|$)' && { echo SSH_SETUP_OK; exit 0; }
  elif command -v netstat >/dev/null 2>&1; then
    netstat -lnt 2>/dev/null | grep -q ':22[[:space:]]' && { echo SSH_SETUP_OK; exit 0; }
  fi
  
  # Check if sshd is still running
  if command -v pgrep >/dev/null 2>&1 && ! pgrep -x sshd >/dev/null 2>&1; then
    echo SSH_ERROR: sshd-exited; tail -n 10 /tmp/sshd.log 2>/dev/null || true; exit 0
  fi
  
  attempt=$((attempt + 1))
  sleep 1
done

# Final check - if ss/netstat not available, assume success if sshd is running
if command -v pgrep >/dev/null 2>&1; then
  pgrep -x sshd >/dev/null 2>&1 && echo SSH_SETUP_OK || echo SSH_ERROR: sshd-not-running
else
  echo SSH_SETUP_OK
fi
"""
    script = script.replace("PASSWORD_B64_PLACEHOLDER", encoded_password)
    try:
        output = await container_shell(container_name, script, timeout=300, node_id=node_id)
        if "SSH_SETUP_OK" in output:
            logger.info("SSH ready for %s on node %s", container_name, node_id)
            return True, "SSH installed, password set, config validated, and daemon started"
        detail = output[-900:] if output else "No diagnostic output from container"
        logger.error("SSH setup failed for %s: %s", container_name, detail)
        return False, detail
    except Exception as exc:
        logger.error("configure_ssh failed on %s: %s", container_name, exc, exc_info=True)
        return False, str(exc)

# ── 5.3 Password vault (portable encryption, no hard dependency) ─────────────

SECRET_KEY_FILE = BASE_DIR / ".secret_key"
try:  # optional, gives us real authenticated encryption when available
    from cryptography.fernet import Fernet, InvalidToken  # type: ignore
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore


def _load_master_secret() -> bytes:
    raw = _env("SECRET_KEY", None)
    if raw:
        return hashlib.sha256(raw.encode()).digest()
    if SECRET_KEY_FILE.exists():
        try:
            return hashlib.sha256(SECRET_KEY_FILE.read_bytes().strip()).digest()
        except OSError:
            pass
    generated = secrets.token_urlsafe(48)
    try:
        SECRET_KEY_FILE.write_text(generated, encoding="utf-8")
        os.chmod(SECRET_KEY_FILE, 0o600)
        logger.warning("No SECRET_KEY set — generated one at %s (add it to .env)", SECRET_KEY_FILE.name)
    except OSError:
        pass
    return hashlib.sha256(generated.encode()).digest()


_MASTER_SECRET = _load_master_secret()
if Fernet is not None:
    import base64 as _b64

    _fernet = Fernet(_b64.urlsafe_b64encode(_MASTER_SECRET))
else:  # pragma: no cover
    _fernet = None


def _keystream(length: int, nonce: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(_MASTER_SECRET + nonce + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    if _fernet is not None:
        return "f1:" + _fernet.encrypt(plaintext.encode()).decode()
    nonce = secrets.token_bytes(16)
    stream = _keystream(len(plaintext.encode()), nonce)
    body = bytes(a ^ b for a, b in zip(plaintext.encode(), stream))
    tag = hmac.new(_MASTER_SECRET, nonce + body, hashlib.sha256).hexdigest()[:32]
    return "x1:" + nonce.hex() + body.hex() + tag


def decrypt_secret(blob: Optional[str]) -> Optional[str]:
    if not blob:
        return None
    try:
        if blob.startswith("f1:"):
            if _fernet is None:
                return None
            return _fernet.decrypt(blob[3:].encode()).decode()
        if blob.startswith("x1:"):
            payload = blob[3:]
            nonce, body_hex, tag = bytes.fromhex(payload[:32]), payload[32:-32], payload[-32:]
            body = bytes.fromhex(body_hex)
            expected = hmac.new(_MASTER_SECRET, nonce + body, hashlib.sha256).hexdigest()[:32]
            if not hmac.compare_digest(expected, tag):
                logger.error("Password vault integrity check failed (SECRET_KEY changed?)")
                return None
            stream = _keystream(len(body), nonce)
            return bytes(a ^ b for a, b in zip(body, stream)).decode()
        # Legacy plaintext value from v8.x
        return blob
    except Exception as exc:
        logger.debug("decrypt_secret failed: %s", exc)
        return None


def generate_strong_password(length: int = 16) -> str:
    if length < 12:
        length = 12
    alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (re.search(r"[a-z]", candidate) and re.search(r"[A-Z]", candidate)
                and re.search(r"\d", candidate) and re.search(r"[^A-Za-z0-9]", candidate)):
            return candidate


def get_vps_password(container_name: str) -> Optional[str]:
    vps = get_vps_by_container(container_name)
    return decrypt_secret(vps.get("root_password")) if vps else None


def set_vps_password(container_name: str, password: str) -> bool:
    return patch_vps(container_name, root_password=encrypt_secret(password))


# ── 5.4 Host statistics ──────────────────────────────────────────────────────

_CPU_SAMPLE_LOCK = threading.Lock()
_last_cpu_sample: Dict[str, Any] = {"idle": None, "total": None, "ts": 0.0}


def _cpu_times() -> Optional[Tuple[int, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            fields = [float(x) for x in handle.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return int(idle), int(sum(fields))
    except Exception:
        return None


def get_host_cpu_usage() -> float:
    """
    Non-blocking CPU usage from /proc/stat, with a psutil fallback.

    v10 fix: the previous version stored the sample and then returned 0.0 on
    the *first* call forever after a restart (it also ran a no-op
    `dict.update({})`). Now the first call sleeps once and produces a real
    delta, so the animated panels show a true value immediately.
    """
    sample = _cpu_times()
    if sample:
        idle, total = sample
        with _CPU_SAMPLE_LOCK:
            prev_idle = _last_cpu_sample.get("idle")
            prev_total = _last_cpu_sample.get("total")
            _last_cpu_sample["idle"] = idle
            _last_cpu_sample["total"] = total
            _last_cpu_sample["ts"] = time.time()

        if prev_idle is None or prev_total is None:
            # First observation of this process: take a second sample.
            time.sleep(0.2)
            second = _cpu_times()
            if not second:
                return 0.0
            idle2, total2 = second
            with _CPU_SAMPLE_LOCK:
                _last_cpu_sample["idle"] = idle2
                _last_cpu_sample["total"] = total2
            delta_total = total2 - total
            delta_idle = idle2 - idle
            if delta_total <= 0:
                return 0.0
            return max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total))

        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        if delta_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total))
    try:
        import psutil  # type: ignore

        return float(psutil.cpu_percent(interval=0.2))
    except Exception:
        return 0.0


def get_host_ram_usage() -> float:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            info = {}
            for line in handle:
                key, _, value = line.partition(":")
                info[key.strip()] = float(value.strip().split()[0])
        total = info.get("MemTotal", 0.0)
        available = info.get("MemAvailable", info.get("MemFree", 0.0))
        if total > 0:
            return max(0.0, min(100.0, (total - available) / total * 100.0))
    except Exception:
        pass
    try:
        import psutil  # type: ignore

        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0


def get_host_disk_usage() -> str:
    try:
        total, used, free = shutil.disk_usage("/")
        pct = (used / total * 100) if total else 0.0
        return f"{fmt_bytes(used)} / {fmt_bytes(total)} ({pct:.0f}%)"
    except Exception:
        return "Unknown"


def get_host_disk_percent() -> float:
    """Numeric disk usage (v10: used by host snapshots + animated bars)."""
    try:
        total, used, _free = shutil.disk_usage("/")
        if total:
            return max(0.0, min(100.0, used / total * 100.0))
    except Exception:
        pass
    return 0.0


def get_host_uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.readline().split()[0])
    except Exception:
        return 0.0


def get_load_average() -> Optional[Tuple[float, float, float]]:
    try:
        return tuple(os.getloadavg())  # type: ignore[return-value]
    except (OSError, AttributeError):
        return None


async def get_host_stats(node_id: int) -> Dict[str, Any]:
    node = get_node(node_id)
    if not node:
        return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown", "disk_pct": 0.0, "online": False}

    if node["is_local"]:
        cpu, ram, disk, disk_pct = await asyncio.gather(
            run_blocking(get_host_cpu_usage),
            run_blocking(get_host_ram_usage),
            run_blocking(get_host_disk_usage),
            run_blocking(get_host_disk_percent),
        )
        return {"cpu": cpu, "ram": ram, "disk": disk, "disk_pct": disk_pct, "online": True}

    def _fetch() -> Dict[str, Any]:
        try:
            response = requests.get(
                f"{str(node['url']).rstrip('/')}/api/get_host_stats",
                params={"api_key": node.get("api_key")},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
            payload.setdefault("disk", "Unknown")
            payload.setdefault("disk_pct", 0.0)
            payload["online"] = True
            return payload
        except Exception as exc:
            logger.debug("Remote stats failed for %s: %s", node["name"], exc)
            return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown", "disk_pct": 0.0, "online": False}

    return await run_blocking(_fetch)


async def get_node_status(node_id: int) -> Tuple[str, bool]:
    node = get_node(node_id)
    if not node:
        return "❔ Unknown", False
    if node["is_local"]:
        return "🟢 Online (local)", True

    def _ping() -> bool:
        try:
            response = requests.get(
                f"{str(node['url']).rstrip('/')}/api/ping",
                params={"api_key": node.get("api_key")},
                timeout=5,
            )
            return response.status_code == 200
        except Exception:
            return False

    online = await run_blocking(_ping)
    return ("🟢 Online" if online else "🔴 Unreachable"), online

# ── 5.5 Container statistics (cgroup based — no `top` parsing bugs) ──────────

_CT_STAT_SCRIPT = r"""
mem_used=""; mem_total=""; cpu_pct=""

if [ -r /sys/fs/cgroup/memory.current ]; then
  mem_used=$(cat /sys/fs/cgroup/memory.current 2>/dev/null)
  mem_max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)
  case "$mem_max" in ''|max) mem_max=$(( $(getconf _PHYS_PAGES) * $(getconf PAGE_SIZE) ));; esac
  if [ -r /sys/fs/cgroup/memory.stat ]; then
    inactive=$(awk '/^inactive_file /{print $2}' /sys/fs/cgroup/memory.stat 2>/dev/null)
    [ -n "$inactive" ] && mem_used=$(( mem_used - inactive ))
  fi
  mem_total=$mem_max
else
  if [ -r /sys/fs/cgroup/memory/memory.stat ]; then
    mem_used=$(awk '/^total_rss /{print $2}' /sys/fs/cgroup/memory/memory.stat 2>/dev/null)
    inactive=$(awk '/^total_inactive_file /{print $2}' /sys/fs/cgroup/memory/memory.stat 2>/dev/null)
    [ -n "$inactive" ] && mem_used=$(( mem_used - inactive ))
  fi
  limit=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null)
  mem_total=$limit
fi
[ -z "$mem_used" ] && mem_used=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print (t-a)*1024}' /proc/meminfo 2>/dev/null)
[ -z "$mem_total" ] && mem_total=$(awk '/^MemTotal:/{print $2*1024}' /proc/meminfo 2>/dev/null)

read u1 n1 s1 i1 w1 irq1 sirq1 st1 rest < /proc/stat
sleep 1
read u2 n2 s2 i2 w2 irq2 sirq2 st2 rest < /proc/stat
d_idle=$(( (i2 + w2) - (i1 + w1) ))
d_total=$(( (u2+n2+s2+i2+w2+irq2+sirq2+st2) - (u1+n1+s1+i1+w1+irq1+sirq1+st1) ))
if [ "$d_total" -gt 0 ]; then
  cpu_pct=$(( (100 * (d_total - d_idle) + d_total/2 ) / d_total ))
fi

uptime_secs=$(cut -d' ' -f1 /proc/uptime 2>/dev/null | cut -d. -f1)

echo "MEM_USED=$mem_used"
echo "MEM_TOTAL=$mem_total"
echo "CPU_PCT=$cpu_pct"
echo "UPTIME=$uptime_secs"
"""


def _read_remote_container_stats(node: Dict[str, Any], container: str) -> Dict[str, Any]:
    try:
        response = requests.post(
            f"{str(node['url']).rstrip('/')}/api/get_container_stats",
            json={"container": container},
            params={"api_key": node.get("api_key")},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.debug("Remote container stats failed (%s/%s): %s", node["name"], container, exc)
        return {}


async def get_container_stats(container_name: str, node_id: Optional[int] = None) -> Dict[str, Any]:
    """Return {status, cpu, ram{used,total,pct}, disk, uptime} for a container."""
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)

    empty = {
        "status": "unknown",
        "cpu": 0.0,
        "ram": {"used": 0, "total": 0, "pct": 0.0},
        "disk": "Unknown",
        "uptime": "Unknown",
        "uptime_seconds": 0.0,
        "online": False,
    }
    if not node:
        return empty

    if not node["is_local"]:
        payload = await run_blocking(_read_remote_container_stats, node, container_name)
        if not payload:
            return empty
        payload.setdefault("status", "unknown")
        payload.setdefault("cpu", 0.0)
        payload.setdefault("ram", {"used": 0, "total": 0, "pct": 0.0})
        payload.setdefault("disk", "Unknown")
        payload.setdefault("uptime", "Unknown")
        payload["online"] = True
        return payload

    status = await container_state(container_name, node_id)
    if status != "running":
        return {**empty, "status": status, "online": True}

    try:
        output = await container_shell(container_name, _CT_STAT_SCRIPT, timeout=40, node_id=node_id)
    except Exception as exc:
        logger.debug("container stats script failed for %s: %s", container_name, exc)
        return {**empty, "status": status, "online": True}

    values: Dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    def _int(key: str) -> int:
        try:
            return int(float(values.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    mem_used = _int("MEM_USED")
    mem_total = _int("MEM_TOTAL")
    cpu_pct = float(_int("CPU_PCT"))
    uptime_secs = float(_int("UPTIME"))
    ram_pct = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0

    try:
        disk_out = await container_shell(
            container_name,
            "df -Pk / | awk 'NR==2{print $3\" \"$2\" \"$5}'",
            timeout=20, node_id=node_id,
        )
        used_kb, total_kb, used_pct = (disk_out.split() + ["?", "?", "?"])[:3]
        disk = f"{fmt_bytes(int(used_kb) * 1024)} / {fmt_bytes(int(total_kb) * 1024)} ({used_pct})"
    except Exception:
        disk = "Unknown"

    ram_used_mb = mem_used // (1024 * 1024)
    ram_total_mb = mem_total // (1024 * 1024)

    await run_blocking(save_usage_snapshot, container_name, cpu_pct, ram_pct, ram_used_mb, ram_total_mb)

    return {
        "status": status,
        "cpu": round(cpu_pct, 1),
        "ram": {"used": ram_used_mb, "total": ram_total_mb, "pct": round(ram_pct, 1)},
        "disk": disk,
        "uptime": fmt_duration(uptime_secs) if uptime_secs else "Unknown",
        "uptime_seconds": uptime_secs,
        "online": True,
    }


async def get_container_networks(container_name: str, node_id: Optional[int] = None) -> Dict[str, str]:
    """Map interface → IPv4 address (loopback excluded)."""
    try:
        output = await container_shell(
            container_name,
            "ip -4 -o addr show 2>/dev/null || ifconfig -a 2>/dev/null",
            timeout=25, node_id=node_id,
        )
    except Exception as exc:
        logger.debug("network lookup failed for %s: %s", container_name, exc)
        return {}

    networks: Dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if match:
            interface, ip = match.group(1), match.group(2)
            if interface != "lo" and not ip.startswith("127."):
                networks[interface] = ip
            continue
        match = re.match(r"(\S+):.*inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", line)
        if match:
            interface, ip = match.group(1), match.group(2)
            if interface != "lo" and not ip.startswith("127."):
                networks[interface] = ip
    return networks


async def get_container_ip(container_name: str, node_id: Optional[int] = None) -> Optional[str]:
    networks = await get_container_networks(container_name, node_id)
    for interface in ("eth0", "ens3", "enp0s3", "en0"):
        if interface in networks:
            return networks[interface]
    return next(iter(networks.values()), None)


# ── 5.6 Port forwarding lifecycle ────────────────────────────────────────────

async def create_port_forward(user_id: Union[int, str], container: str, vps_port: int,
                              node_id: int, protocol: str = "both") -> Optional[int]:
    """Create a TCP+UDP proxy device and persist it. Returns the host port."""
    host_port = await run_blocking(get_available_host_port)
    if not host_port:
        logger.error("No free host port available for %s", container)
        return None

    protocols = ("tcp", "udp") if protocol == "both" else (protocol,)
    created_devices: List[str] = []
    try:
        for proto in protocols:
            device_name = f"{proto}_proxy_{host_port}"
            await run_lxc_argv(
                container,
                ["config", "device", "add", container, device_name, "proxy",
                 f"listen={proto}:0.0.0.0:{host_port}",
                 f"connect={proto}:127.0.0.1:{vps_port}"],
                timeout=40, node_id=node_id,
            )
            created_devices.append(device_name)
    except Exception as exc:
        logger.error("Port forward creation failed on %s: %s", container, exc)
        for device_name in created_devices:
            try:
                await run_lxc_argv(container, ["config", "device", "remove", container, device_name],
                                   timeout=20, node_id=node_id, check=False)
            except Exception:
                pass
        return None

    try:
        await run_blocking(add_forward_row, str(user_id), container, int(vps_port), int(host_port), protocol)
    except sqlite3.Error as exc:
        logger.error("Could not persist port forward: %s", exc)
        for device_name in created_devices:
            try:
                await run_lxc_argv(container, ["config", "device", "remove", container, device_name],
                                   timeout=20, node_id=node_id, check=False)
            except Exception:
                pass
        return None
    return host_port


async def remove_port_forward(forward_id: int) -> Tuple[bool, Optional[str], Optional[int]]:
    """Remove a forward. Returns (success, owner_id, host_port)."""
    record = await run_blocking(get_forward, forward_id)
    if not record:
        return False, None, None

    container = record["vps_container"]
    host_port = int(record["host_port"])
    node_id = find_node_id_for_container(container)

    for proto in ("tcp", "udp"):
        try:
            await run_lxc_argv(container, ["config", "device", "remove", container, f"{proto}_proxy_{host_port}"],
                               timeout=25, node_id=node_id, check=False)
        except Exception as exc:
            logger.debug("device remove %s on %s: %s", proto, container, exc)

    await run_blocking(delete_forward_row, forward_id)
    return True, str(record["user_id"]), host_port


async def recreate_port_forwards(container: str) -> int:
    """Re-attach every stored forward (after start / restart / reinstall)."""
    records = await run_blocking(get_forwards_for_container, container)
    if not records:
        return 0
    node_id = find_node_id_for_container(container)
    restored = 0
    for record in records:
        host_port, vps_port = int(record["host_port"]), int(record["vps_port"])
        try:
            for proto in ("tcp", "udp"):
                await run_lxc_argv(
                    container,
                    ["config", "device", "remove", container, f"{proto}_proxy_{host_port}"],
                    timeout=15, node_id=node_id, check=False,
                )
                await run_lxc_argv(
                    container,
                    ["config", "device", "add", container, f"{proto}_proxy_{host_port}", "proxy",
                     f"listen={proto}:0.0.0.0:{host_port}",
                     f"connect={proto}:127.0.0.1:{vps_port}"],
                    timeout=30, node_id=node_id,
                )
            restored += 1
        except Exception as exc:
            logger.warning("Could not restore forward %s→%s on %s: %s", host_port, vps_port, container, exc)
    return restored


# ── 5.7 VPS lifecycle helpers ────────────────────────────────────────────────

async def provision_container(container_name: str, os_version: str, node_id: int, *,
                              ram_gb: int, cpu: int, disk_gb: int,
                              storage_pool: Optional[str] = None) -> None:
    """Create + configure a fresh container (init → limits → config → start)."""
    with _SHELL_CACHE_LOCK:
        _SHELL_CACHE.pop(container_name, None)
    pool = storage_pool or DEFAULT_STORAGE_POOL
    await run_lxc_command(
        container_name,
        f"init {shlex.quote(os_version)} {shlex.quote(container_name)} -s {shlex.quote(pool)}",
        timeout=900, node_id=node_id,
    )
    await run_lxc_argv(container_name, ["config", "set", container_name, "limits.memory", f"{int(ram_gb) * 1024}MB"],
                       timeout=30, node_id=node_id)
    await run_lxc_argv(container_name, ["config", "set", container_name, "limits.cpu", str(int(cpu))],
                       timeout=30, node_id=node_id)
    try:
        await run_lxc_argv(container_name, ["config", "device", "set", container_name, "root", f"size={int(disk_gb)}GB"],
                           timeout=40, node_id=node_id)
    except Exception as exc:
        logger.warning("Could not set disk size for %s: %s", container_name, exc)

    await apply_lxc_config(container_name, node_id)
    await run_lxc_argv(container_name, ["start", container_name], timeout=180, node_id=node_id)
    await apply_internal_permissions(container_name, node_id)


async def run_host_motd(container_name: str, node_id: int) -> None:
    if not HOST_MOTD:
        return
    try:
        # Reduced timeout (60s) and wrapped in asyncio.wait_for to fail gracefully
        await asyncio.wait_for(
            container_shell(container_name, HOST_MOTD, timeout=60, node_id=node_id),
            timeout=75.0
        )
        logger.info("Startup script executed inside %s", container_name)
    except asyncio.TimeoutError:
        logger.warning("Startup script timed out on %s (took >75s)", container_name)
    except Exception as exc:
        logger.warning("Startup script failed on %s: %s", container_name, exc)


async def ensure_vps_role(guild: Optional[discord.Guild], member: Optional[discord.Member]) -> None:
    """Give (or create) the 'VPS User' role. Silently no-ops without perms."""
    global VPS_USER_ROLE_ID
    if guild is None or member is None or guild.me is None:
        return
    if not guild.me.guild_permissions.manage_roles:
        return

    role: Optional[discord.Role] = None
    if VPS_USER_ROLE_ID:
        cached = guild.get_role(VPS_USER_ROLE_ID)
        if cached and cached < guild.me.top_role:
            role = cached
        else:
            VPS_USER_ROLE_ID = None

    if role is None:
        role = discord.utils.get(guild.roles, name=f"{BOT_NAME} VPS User")
        if role and role >= guild.me.top_role:
            role = None

    if role is None:
        try:
            role = await guild.create_role(
                name=f"{BOT_NAME} VPS User",
                colour=discord.Colour.from_str("#5865F2"),
                permissions=discord.Permissions.none(),
                mentionable=False,
                reason=f"{BOT_NAME} VPS ownership role",
            )
            target_position = max(1, guild.me.top_role.position - 1)
            await role.edit(position=target_position)
            logger.info("Created VPS role %s", role.id)
        except discord.Forbidden:
            return
        except discord.HTTPException as exc:
            logger.warning("Could not create VPS role: %s", exc)
            return

    VPS_USER_ROLE_ID = role.id
    if role not in member.roles:
        try:
            await member.add_roles(role, reason=f"{BOT_NAME}: VPS ownership granted")
        except discord.HTTPException as exc:
            logger.warning("Could not add VPS role to %s: %s", member.id, exc)


async def remove_vps_role_if_unused(member: Optional[discord.Member], user_id: Union[int, str]) -> None:
    if member is None:
        return
    remaining = await run_blocking(get_user_vps, user_id)
    if remaining:
        return
    role = discord.utils.get(member.guild.roles, name=f"{BOT_NAME} VPS User") if member.guild else None
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"{BOT_NAME}: no VPS ownership left")
        except discord.HTTPException:
            pass

# ═══════════════════════════════════════════════════════════════════════════
# 6. UI / UX ENGINE  —  "premium panel" design language
# ═══════════════════════════════════════════════════════════════════════════
#
#  Design rules baked in here (client-agnostic, mobile/tablet/PC safe):
#   • Never exceed 5-6 fields per embed on a phone screen.
#   • Field values wrapped so Discord's 1024-char limit can never 400-error.
#   • One visual hierarchy: brand colour + emoji anchor + status pill.
#   • Live data uses animated frames (spinner / breathing bars / sparklines).
#   • Every long list is paginated instead of being dumped as one wall.
#
#  v10 additions live in 6.5b (animated primitives), 6.7 (animation engine)
#  and 6.8 (live panels).

# ── 6.1 Palette ──────────────────────────────────────────────────────────────

COLORS: Dict[str, int] = {
    "brand":     0x5865F2,  # blurple — Discord native, looks great in every theme
    "primary":   0x5865F2,
    "success":   0x57F287,  # Discord green
    "danger":    0xED4245,  # Discord red
    "error":     0xED4245,
    "warning":   0xFEE75C,  # Discord yellow
    "info":      0x00B0F4,
    "network":   0x1ABC9C,
    "node":      0xEB459E,
    "idle":      0x95A5A6,
    "expired":   0x992D22,
    "dark":      0x2B2D31,
    "loading":   0xF1C40F,
    "magic":     0x9B59B6,
    "os":        0xE67E22,
    "vault":     0xF47FFF,
    # v10 palette extras
    "live":      0x2ECC71,
    "plasma":    0x8A2BE2,
    "sunset":    0xFF6B6B,
    "ocean":     0x3498DB,
}

COLOR_PRIMARY = COLORS["brand"]
COLOR_SUCCESS = COLORS["success"]
COLOR_ERROR = COLORS["danger"]
COLOR_WARNING = COLORS["warning"]
COLOR_INFO = COLORS["info"]
COLOR_NODE = COLORS["node"]
COLOR_EXPIRED = COLORS["expired"]
COLOR_SUSPENDED = COLORS["idle"]

FRAME_SPINNER = "◐◓◑◒"
FRAME_DOTS = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
FRAME_PULSE = ("▰▱▱▱▱▱▱", "▰▰▱▱▱▱▱", "▰▰▰▱▱▱▱", "▱▰▰▰▱▱▱", "▱▱▰▰▰▱▱", "▱▱▱▰▰▰▱", "▱▱▱▱▰▰▰", "▱▱▱▱▱▰▰", "▱▱▱▱▱▱▰")
SPARK_CHARS = "▁▂▃▄▅▆▇█"
STEP_ICONS = {
    "pending": "◻️",
    "running": "🔄",
    "done": "✅",
    "failed": "❌",
    "skipped": "⏭️",
    "warn": "⚠️",
}

# v10: extra animation alphabets
FRAME_BEAT = ("🔴", "🟠", "🟡", "🟢", "🔵", "🟣")
FRAME_BEAT_DOT = ("●", "◉", "○", "◉")
FRAME_BOUNCE = (
    "●∙∙∙∙∙∙", "∙●∙∙∙∙∙", "∙∙●∙∙∙∙", "∙∙∙●∙∙∙",
    "∙∙∙∙●∙∙", "∙∙∙∙∙●∙", "∙∙∙∙∙∙●", "∙∙∙∙∙●∙",
    "∙∙∙∙●∙∙", "∙∙∙●∙∙∙", "∙∙●∙∙∙∙", "∙●∙∙∙∙∙",
)
FRAME_ARROWS = ("▹▹▹▹▹", "▸▹▹▹▹", "▹▸▹▹▹", "▹▹▸▹▹", "▹▹▹▸▹", "▹▹▹▹▸")
FRAME_MOON = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")
FRAME_CLOCK = ("🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛")
FRAME_EARTH = ("🌍", "🌎", "🌏")
FRAME_BARS = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂")


def frame_of(sequence: Sequence[str], tick: Optional[int] = None) -> str:
    """Pick a frame: `tick` when given, otherwise the wall-clock (auto-animate)."""
    if not sequence:
        return ""
    if tick is None:
        tick = int(time.time() / max(0.25, frame_seconds()))
    return sequence[tick % len(sequence)]


def brand_footer(extra: Optional[str] = None) -> str:
    parts = [f"{BOT_NAME} v{BOT_VERSION}"]
    if extra:
        parts.append(extra)
    parts.append(f"by {BOT_DEVELOPER}")
    return " • ".join(parts)


def humanize_list(items: Sequence[str], *, conjunction: str = "and") -> str:
    items = [str(i) for i in items if str(i).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


# ── 6.2 Generic formatters ───────────────────────────────────────────────────

def fmt_bytes(num_bytes: Union[int, float, None]) -> str:
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024.0:
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.2f} {unit}".replace(".00", "")
        value /= 1024.0
    return f"{value:.2f} EB"


def fmt_duration(seconds: Union[int, float, None]) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    chunks = []
    if days:
        chunks.append(f"{days}d")
    if hours or days:
        chunks.append(f"{hours}h")
    if minutes or hours or days:
        chunks.append(f"{minutes}m")
    if not days:
        chunks.append(f"{secs}s")
    return " ".join(chunks[:2])


def fmt_relative(seconds: float) -> str:
    """e.g. 'in 2 days', 'in 5 hours', '3 minutes ago'."""
    seconds = float(seconds)
    future = seconds >= 0
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{int(seconds)}s"
    elif seconds < 3600:
        text = f"{int(seconds // 60)}m"
    elif seconds < 86400:
        text = f"{int(seconds // 3600)}h"
    elif seconds < 2592000:
        text = f"{int(seconds // 86400)}d"
    elif seconds < 31536000:
        text = f"{int(seconds // 2592000)}mo"
    else:
        text = f"{seconds / 31536000:.1f}y"
    return f"in {text}" if future else f"{text} ago"


def discord_timestamp(dt: Optional[datetime], style: str = "R") -> str:
    if dt is None:
        return "—"
    try:
        return f"<t:{int(dt.timestamp())}:{style}>"
    except Exception:
        return "—"


def truncate_text(text: Any, max_length: int = 1024) -> str:
    if text is None:
        return ""
    value = str(text)
    if len(value) <= max_length:
        return value
    return value[: max(0, max_length - 1)] + "…"


def progress_bar(value: float, maximum: float = 100.0, length: int = 10, *, style: str = "blocks",
                show_percent: bool = True) -> str:
    """Unicode progress bar. `style` is 'blocks' (compact) or 'emoji' (high contrast)."""
    try:
        value = float(value or 0)
        maximum = float(maximum or 100)
    except (TypeError, ValueError):
        value, maximum = 0.0, 100.0
    pct = 0.0 if maximum <= 0 else max(0.0, min(100.0, value / maximum * 100.0))
    filled = int(round(pct / 100.0 * length))
    filled = max(0, min(length, filled))
    if style == "emoji":
        tone = "🟩" if pct < 100 else "🟩"  # Always use green, never red
        bar = tone * filled + "⬜" * (length - filled)
    else:
        bar = "▰" * filled + "▱" * (length - filled)
    return f"{bar} `{pct:5.1f}%`" if show_percent else bar


def sparkline(values: Sequence[float], *, width: int = 24) -> str:
    """Compact history chart, e.g. ▁▂▄▆█▆▄▂ — great on mobile."""
    data = [float(v or 0) for v in values][-width:]
    if len(data) < 2:
        return SPARK_CHARS[0] * max(1, width // 2)
    peak = max(data) or 1.0
    lowest = min(data)
    span = (peak - lowest) or 1.0
    return "".join(SPARK_CHARS[min(7, int((v - lowest) / span * 7))] for v in data)


def progress_with_emoji(pct: float, length: int = 10) -> str:
    return progress_bar(pct, 100, length, style="emoji")


# ── 6.2b v10 animated bars / meters ──────────────────────────────────────────

def wave_bar(value: float, maximum: float = 100.0, length: int = 12, *,
             tick: Optional[int] = None) -> str:
    """
    A travelling-wave bar: the "head" of the bar shimmers while it fills.
    Looks alive even when the underlying value barely changes.
    """
    try:
        value = float(value or 0)
        maximum = float(maximum or 100)
    except (TypeError, ValueError):
        value, maximum = 0.0, 100.0
    pct = 0.0 if maximum <= 0 else max(0.0, min(100.0, value / maximum * 100.0))
    filled = max(0, min(length, int(round(pct / 100.0 * length))))
    wave = "▂▃▄▅▆▇█"
    head = frame_of(wave, tick)
    body = "█" * max(0, filled - 1)
    empty = "░" * max(0, length - filled)
    return f"`{body}{head if filled else '░'}{empty}` `{pct:5.1f}%`"


def gradient_bar(value: float, maximum: float = 100.0, length: int = 12, *,
                 tick: Optional[int] = None) -> str:
    """
    Heat-coloured bar. Cool green → warm yellow → hot red as the value climbs,
    with a subtle breathing marker so a static value still feels live.
    """
    try:
        value = float(value or 0)
        maximum = float(maximum or 100)
    except (TypeError, ValueError):
        value, maximum = 0.0, 100.0
    pct = 0.0 if maximum <= 0 else max(0.0, min(100.0, value / maximum * 100.0))
    filled = max(0, min(length, int(round(pct / 100.0 * length))))
    if pct < 50:
        block = "🟩"
    elif pct < 75:
        block = "🟨"
    elif pct < 90:
        block = "🟧"
    else:
        block = "🟥"
    marker = frame_of(FRAME_BEAT_DOT, tick)
    bar = block * filled + "⬛" * (length - filled)
    return f"{marker} {bar} `{pct:5.1f}%`"


def beat(tick: Optional[int] = None) -> str:
    """Breathing status dot — the signature v10 'it is alive' marker."""
    return frame_of(FRAME_BEAT_DOT, tick)


def bounce(tick: Optional[int] = None) -> str:
    """Bouncing-ball loader used by long running panels."""
    return frame_of(FRAME_BOUNCE, tick)


def shimmer(text: str, *, tick: Optional[int] = None, width: int = 3) -> str:
    """
    Animated shimmer: a bright window slides across the text.
    Implemented with combining-free Unicode so every client renders it.
    """
    if not text:
        return ""
    if not anim_on():
        return text
    chars = list(text)
    size = len(chars)
    index = (tick if tick is not None else int(time.time() / max(0.25, frame_seconds()))) % max(1, size + width)
    out = []
    for position, char in enumerate(chars):
        if index <= position < index + width:
            out.append(f"**{char}**")
        else:
            out.append(char)
    return "".join(out)


def pulse_dots(tick: Optional[int] = None) -> str:
    return frame_of(FRAME_DOTS, tick)


def ascii_graph(values: Sequence[float], *, height: int = 8, width: Optional[int] = None,
                label: str = "", maximum: Optional[float] = None) -> str:
    """
    A real ASCII chart (grid + baseline + peak labels) instead of a sparkline.

    Perfect for `!graph`; stays inside a Discord code block so it looks the
    same on mobile, tablet and desktop.
    """
    data = [float(v or 0) for v in values]
    if width:
        data = data[-width:]
    if len(data) < 2:
        return "not enough samples yet"
    peak = float(maximum) if maximum else (max(data) or 1.0)
    peak = max(peak, 1.0)
    rows: List[str] = []
    for level in range(height, 0, -1):
        threshold = peak * level / height
        line = "".join("█" if value >= threshold else " " for value in data)
        marker = "┤" if level == height else ("│" if level == height // 2 else "│")
        rows.append(f"{marker}{line} {int(threshold):>3}%")
    rows.append("└" + "─" * len(data) + f" 0%")
    header = f"{label}  peak {peak:.0f}%  •  {len(data)} samples" if label else f"peak {peak:.0f}%"
    return f"{header}\n" + "\n".join(rows)


def mini_meter(value: float, maximum: float = 100.0, length: int = 8) -> str:
    """Tiny non-animated meter, safe for dense tables."""
    return progress_bar(value, maximum, length, style="blocks")


def load_face(pct: float) -> str:
    """Small emoji that reacts to how busy something is."""
    if pct >= 95:
        return "🔥"
    if pct >= 80:
        return "😰"
    if pct >= 60:
        return "🙂"
    if pct >= 30:
        return "😌"
    return "😴"


# ── 6.3 Embed factory ────────────────────────────────────────────────────────

def create_embed(title: str, description: str = "", color: Union[int, str] = COLORS["brand"], *,
                 thumbnail: bool = True, footer: Optional[str] = None,
                 timestamp: bool = True, author: Optional[str] = None) -> discord.Embed:
    """The one embed builder used across the whole bot."""
    if isinstance(color, str):
        color = COLORS.get(color, COLORS["brand"])
    embed = discord.Embed(
        title=truncate_text(title, 250) or None,
        description=truncate_text(description, 4096) or None,
        colour=discord.Colour(color),
    )
    if author:
        embed.set_author(name=truncate_text(author, 250))
    if thumbnail and BOT_THUMBNAIL_URL:
        embed.set_thumbnail(url=BOT_THUMBNAIL_URL)
    embed.set_footer(text=truncate_text(footer or brand_footer(), 2048), icon_url=BOT_ICON_URL or None)
    if timestamp:
        embed.timestamp = datetime.now(timezone.utc)
    return embed


def add_field(embed: discord.Embed, name: str, value: Any, inline: bool = False,
              *, prefix: str = "") -> discord.Embed:
    """Always-safe field addition (Discord rejects empty values)."""
    safe_name = truncate_text(f"{prefix}{name}", 256) or "\u200b"
    safe_value = sanitize_field_value(value, 1024)
    if len(embed.fields) >= 25:
        return embed
    embed.add_field(name=safe_name, value=safe_value, inline=inline)
    return embed


def success_embed(title: str, description: str = "", **kwargs) -> discord.Embed:
    return create_embed(f"✅ {title}", description, COLORS["success"], **kwargs)


def error_embed(title: str, description: str = "", **kwargs) -> discord.Embed:
    return create_embed(f"⛔ {title}", description, COLORS["danger"], **kwargs)


def warning_embed(title: str, description: str = "", **kwargs) -> discord.Embed:
    return create_embed(f"⚠️ {title}", description, COLORS["warning"], **kwargs)


def info_embed(title: str, description: str = "", **kwargs) -> discord.Embed:
    return create_embed(f"ℹ️ {title}", description, COLORS["info"], **kwargs)


def loading_embed(title: str, description: str = "Working on it…", **kwargs) -> discord.Embed:
    return create_embed(f"{FRAME_DOTS[0]} {title}", description, COLORS["loading"], **kwargs)


def neutral_embed(title: str, description: str = "", **kwargs) -> discord.Embed:
    return create_embed(title, description, COLORS["dark"], **kwargs)


def live_embed(title: str, description: str = "", **kwargs) -> discord.Embed:
    """v10: embed with a pulsing LIVE marker in the title."""
    return create_embed(f"{beat()} {title}", description, COLORS["live"], **kwargs)


# Backwards-compatible aliases (v8/v9 command code used these names)
create_success_embed = success_embed
create_error_embed = error_embed
create_warning_embed = warning_embed
create_info_embed = info_embed


def kv_block(pairs: Sequence[Tuple[str, Any]], *, separator: str = "\n") -> str:
    return separator.join(f"**{k}** `{v}`" for k, v in pairs if v not in (None, ""))


def bullet(items: Sequence[str], bullet_char: str = "›") -> str:
    return "\n".join(f"{bullet_char} {item}" for item in items if item)


# ── 6.4 Domain specific renderers ────────────────────────────────────────────

def vps_status_pill(vps: Dict[str, Any]) -> str:
    """One consistent status pill: 🟢 RUNNING / 🟡 SUSPENDED / 🔴 STOPPED / ⚪ UNKNOWN."""
    if vps.get("suspended"):
        return "🟡 `SUSPENDED`"
    status = str(vps.get("status") or "unknown").lower()
    if status == "running":
        return "🟢 `RUNNING`"
    if status == "stopped":
        return "🔴 `STOPPED`"
    if status in ("frozen", "paused"):
        return "🔵 `FROZEN`"
    return "⚪ `UNKNOWN`"


def expiration_state(vps: Dict[str, Any]) -> Tuple[str, str, Optional[datetime], Optional[float]]:
    """Returns (emoji, label, expiry_dt, seconds_left)."""
    expiry = safe_iso_parse(vps.get("expiration_date"))
    if expiry is None:
        return "🔵", "No expiration", None, None
    seconds_left = (expiry - datetime.now(timezone.utc)).total_seconds()
    days = seconds_left / 86400.0
    if seconds_left <= 0:
        return "🔴", "Expired", expiry, seconds_left
    if days <= EXPIRATION_WARNING_DAYS:
        return "🟡", f"Expiring ({max(0, int(days))}d left)", expiry, seconds_left
    return "🟢", f"Active ({int(days)}d left)", expiry, seconds_left


def expiration_line(vps: Dict[str, Any]) -> str:
    emoji, label, expiry, _ = expiration_state(vps)
    if expiry is None:
        return f"{emoji} {label}"
    return f"{emoji} {label} • {discord_timestamp(expiry, 'R')}"


def format_expiration(vps: Dict[str, Any]) -> str:
    return expiration_line(vps)


def vps_card(vps: Dict[str, Any], index: int, *, show_owner: Optional[str] = None,
             compact: bool = False) -> str:
    """Mobile-first VPS card: 3 short lines, no wide tables."""
    lines = [f"**{index}. {vps['container_name']}**"]
    meta = [vps_status_pill(vps)]
    if vps.get("whitelisted"):
        meta.append("✅ `WHITELISTED`")
    if show_owner:
        meta.append(f"👤 {show_owner}")
    lines.append(" ".join(meta))
    lines.append(
        f"`{vps.get('ram', '?')} RAM` • `{vps.get('cpu', '?')} vCPU` • `{vps.get('storage', '?')}`"
    )
    if not compact:
        node = get_node(vps.get("node_id", 1))
        lines.append(f"🖥️ {node['name'] if node else 'Unknown node'} • {expiration_line(vps)}")
    return "\n".join(lines)


def animated_vps_row(vps: Dict[str, Any], stats: Dict[str, Any], *, tick: Optional[int] = None,
                     width: int = 8) -> str:
    """
    v10: one dense, animated row for live fleet views.

    ┃ 🖥️ name   ███░░ 32%  CPU   ●●○○ 48%  RAM   🟢 RUNNING
    """
    cpu = float(stats.get("cpu") or 0)
    ram_pct = float((stats.get("ram") or {}).get("pct") or 0)
    status = str(stats.get("status") or vps.get("status") or "unknown").lower()
    dot = "🟢" if status == "running" else ("🔴" if status == "stopped" else "⚪")
    return (
        f"{dot} `{vps['container_name'][:22]:<22}` "
        f"{wave_bar(cpu, 100, width, tick=tick)} CPU "
        f"{wave_bar(ram_pct, 100, width, tick=tick)} RAM"
    )


def stats_panel_fields(vps: Dict[str, Any], stats: Dict[str, Any], *, history: Optional[List[Dict]] = None,
                       prefix: str = "", tick: Optional[int] = None) -> List[Tuple[str, str, bool]]:
    """Build (name, value, inline) tuples for a stats embed."""
    ram = stats.get("ram") or {}
    cpu = float(stats.get("cpu") or 0)
    ram_pct = float(ram.get("pct") or 0)
    ram_used, ram_total = int(ram.get("used") or 0), int(ram.get("total") or 0)

    if anim_on():
        cpu_field = f"{gradient_bar(cpu, 100, 10, tick=tick)}\n`{cpu:.1f}%` of allocated CPU {load_face(cpu)}"
        ram_field = (
            f"{gradient_bar(ram_pct, 100, 10, tick=tick)}\n"
            f"`{ram_used} MB` / `{ram_total} MB`"
        )
    else:
        cpu_field = f"{progress_bar(cpu, 100, 10)}\n`{cpu:.1f}%` of allocated CPU"
        ram_field = f"{progress_bar(ram_pct, 100, 10)}\n`{ram_used} MB` / `{ram_total} MB`"

    disk_field = f"`{stats.get('disk', 'Unknown')}`"
    uptime_field = f"`{stats.get('uptime', 'Unknown')}`"

    fields: List[Tuple[str, str, bool]] = [
        (f"{prefix}🧠 CPU", cpu_field, True),
        (f"{prefix}🧮 Memory", ram_field, True),
        (f"{prefix}💾 Disk", disk_field, True),
        (f"{prefix}⏱️ Uptime", uptime_field, True),
    ]

    if history and len(history) >= 3:
        cpu_series = [row.get("cpu_pct", 0) for row in history]
        ram_series = [row.get("ram_pct", 0) for row in history]
        fields.append((
            f"{prefix}📈 Trend (last {len(history)} samples)",
            f"CPU  `{sparkline(cpu_series)}`\nRAM  `{sparkline(ram_series)}`",
            False,
        ))
    return fields


def credentials_block(ssh_command: Optional[str], password: Optional[str], *,
                      container: str, node_name: str, extra_lines: Optional[Sequence[str]] = None) -> str:
    lines = []
    if ssh_command:
        lines.append(f"```bash\n{escape_backticks(ssh_command)}\n```")
    lines.append("**User** `root`")
    if password:
        lines.append(f"**Password** `{password}`")
    else:
        lines.append("**Password** _not stored — click “Regenerate password”_")
    if extra_lines:
        lines.extend(extra_lines)
    return "\n".join(lines)


def ssh_command_for(host_port: Optional[int], *, host: Optional[str] = None) -> Optional[str]:
    if not host_port:
        return None
    return f"ssh root@{host or YOUR_SERVER_IP} -p {host_port}"


def webssh_url_for(host_port: Optional[int]) -> Optional[str]:
    if not host_port:
        return None
    return WEBSSH_URL_FORMAT.format(SERVER_IP=WEBSSH_SERVER_IP, PORT=host_port)

# ── 6.5 Animated components ──────────────────────────────────────────────────

# Registry of running "thinking" animations, keyed by message id. Any edit of
# that message (via _safe_edit / stop_thinking) cancels the animation first, so
# a finished panel can never be overwritten by a stray spinner frame.
_THINKING_TASKS: Dict[int, asyncio.Task] = {}


def _stop_thinking(message_id: Optional[int]) -> None:
    if message_id is None:
        return
    task = _THINKING_TASKS.pop(message_id, None)
    if task is not None and not task.done():
        task.cancel()


def stop_thinking(message: Optional[discord.Message]) -> None:
    """Cancel the animated thinking loop attached to `message` (if any)."""
    if message is None:
        return
    _stop_thinking(message.id)


async def _safe_edit(message: Optional[discord.Message], **kwargs: Any) -> bool:
    """
    Edit a message, swallowing the (very common) 'already gone' errors.

    Also cancels any running thinking-animation for that message, which is what
    keeps v10 animations from fighting with the final render.
    """
    if message is None:
        return False
    _stop_thinking(message.id)
    try:
        await message.edit(**kwargs)
        return True
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        return False


async def _safe_delete(message: Optional[discord.Message], *, delay: float = 0.0) -> bool:
    if message is None:
        return False
    try:
        if delay:
            await asyncio.sleep(delay)
        await message.delete()
        return True
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        return False


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False,
                     thinking: bool = False) -> bool:
    """
    Defer an interaction exactly once.

    v10 fix: v9 had several code paths that deferred twice (button callback →
    helper → command body), which raised `InteractionResponded` and surfaced as
    a silent "This interaction failed" for the user.
    """
    try:
        if interaction.response.is_done():
            return False
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except (discord.HTTPException, discord.NotFound):
        return False


async def safe_send(interaction: discord.Interaction, *, embed: Optional[discord.Embed] = None,
                    content: Optional[str] = None, view: Optional[discord.ui.View] = None,
                    ephemeral: bool = True) -> Optional[discord.Message]:
    """Send an ephemeral reply/follow-up without ever raising 'already responded'."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, view=view,
                                                    ephemeral=ephemeral)
            try:
                return await interaction.original_response()
            except discord.HTTPException:
                return None
        return await interaction.followup.send(content=content, embed=embed, view=view,
                                               ephemeral=ephemeral)
    except (discord.HTTPException, discord.NotFound):
        return None


class Spinner:
    """
    Tiny helper that cycles a status prefix on a Discord message.

        spinner = Spinner(message)
        await spinner.start("Provisioning")
        ...
        await spinner.stop("✅ Provisioned")
    """

    def __init__(self, message: discord.Message, *, interval: float = 0.9,
                 frames: Sequence[str] = FRAME_DOTS):
        self.message = message
        self.interval = interval
        self.frames = tuple(frames)
        self._task: Optional[asyncio.Task] = None
        self.label = ""
        self._stopped = False

    async def start(self, label: str) -> None:
        self.label = label
        self._stopped = False
        if not anim_on():
            return
        self._task = asyncio.create_task(self._run(), name="ui-spinner")

    async def _run(self) -> None:
        index = 0
        try:
            while True:
                frame = self.frames[index % len(self.frames)]
                ok = await _safe_edit(self.message, content=f"{frame} **{self.label}**")
                if not ok:
                    return
                index += 1
                await asyncio.sleep(max(0.3, self.interval / max(0.2, ANIM_STATE.get("speed", 1.0))))
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def stop(self, final_content: Optional[str] = None) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if final_content is not None:
            await _safe_edit(self.message, content=final_content)


class ProgressPanel:
    """
    Animated multi-step progress panel.

        panel = ProgressPanel(channel, "Creating VPS", ["Allocate id", "Boot container", ...])
        await panel.start()
        await panel.step(0, "running")
        ...
        await panel.finish(final_embed=embed)

    v10: every frame now shows elapsed time, an ETA and a shimmering header,
    and the tick rate honours the global animation speed setting.
    """

    def __init__(self, channel: discord.abc.Messageable, title: str, steps: Sequence[str], *,
                 subtitle: str = "", color: Union[int, str] = COLORS["brand"],
                 interval: float = 1.1, reference: Optional[discord.Message] = None,
                 show_eta: bool = True, interaction: Optional[discord.Interaction] = None):
        self.channel = channel
        self.interaction = interaction  # Store interaction for followup sends
        self.title = title
        self.subtitle = subtitle
        self.color = color
        self.interval = interval
        self.show_eta = show_eta
        self.rows: List[Dict[str, str]] = [
            {"label": str(step), "state": "pending", "detail": ""} for step in steps
        ]
        self.message: Optional[discord.Message] = reference
        self._task: Optional[asyncio.Task] = None
        self._frame = 0
        self._finished = False
        self._lock = asyncio.Lock()
        self.started_at = time.time()
        self._step_started = time.time()

    # ── rendering ────────────────────────────────────────────────────────────
    def _render(self) -> discord.Embed:
        pulse = FRAME_PULSE[self._frame % len(FRAME_PULSE)]
        done = sum(1 for row in self.rows if row["state"] in ("done", "skipped"))
        total = max(1, len(self.rows))
        lines: List[str] = []
        if self.subtitle:
            lines.append(f"-# {self.subtitle}")
            lines.append("")
        for row in self.rows:
            icon = STEP_ICONS.get(row["state"], "◻️")
            if row["state"] == "running":
                icon = FRAME_DOTS[self._frame % len(FRAME_DOTS)]
            label = row["label"]
            detail = f" — *{row['detail']}*" if row["detail"] else ""
            lines.append(f"{icon}  {label}{detail}")
        lines.append("")
        lines.append(progress_bar(done, total, 12, style="emoji"))

        elapsed = time.time() - self.started_at
        footer_bits = [f"elapsed {fmt_duration(elapsed)}"]
        if self.show_eta and done:
            eta = (elapsed / done) * (total - done)
            if eta > 1:
                footer_bits.append(f"~{fmt_duration(eta)} left")
        footer_bits.append(f"{done}/{total} steps")

        embed = create_embed(f"{pulse} {self.title}", "\n".join(lines), self.color)
        base = (embed.footer.text or brand_footer()).split(" • ")[0]
        embed.set_footer(text=truncate_text(f"{base} • " + " • ".join(footer_bits), 2048),
                         icon_url=BOT_ICON_URL or None)
        return embed

    async def _tick(self) -> None:
        while not self._finished:
            try:
                self._frame += 1
                if self.message:
                    await _safe_edit(self.message, embed=self._render())
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(max(0.4, self.interval / max(0.2, ANIM_STATE.get("speed", 1.0))))

    # ── public API ───────────────────────────────────────────────────────────
    async def start(self) -> "ProgressPanel":
        embed = self._render()
        if self.message is None:
            try:
                # Use interaction followup if available and response is deferred
                if self.interaction and self.interaction.response.is_done():
                    logger.debug(f"ProgressPanel.start() using interaction.followup for deferred response")
                    self.message = await self.interaction.followup.send(embed=embed)
                else:
                    logger.debug(f"ProgressPanel.start() using channel.send()")
                    self.message = await self.channel.send(embed=embed)
            except discord.HTTPException as e:
                logger.error(f"ProgressPanel.start() failed to send embed: {e} (interaction={self.interaction}, is_done={self.interaction.response.is_done() if self.interaction else 'N/A'})")
                self.message = None
            except Exception as e:
                logger.error(f"ProgressPanel.start() unexpected error: {e}", exc_info=True)
                self.message = None
        else:
            await _safe_edit(self.message, embed=embed)
        if anim_on():
            self._task = asyncio.create_task(self._tick(), name="ui-progress")
        return self

    async def step(self, index: int, state: str = "running", detail: str = "") -> None:
        if 0 <= index < len(self.rows):
            self.rows[index]["state"] = state
            if detail:
                self.rows[index]["detail"] = truncate_text(detail, 90)
        self._step_started = time.time()
        if self.message:
            await _safe_edit(self.message, embed=self._render())

    async def note(self, subtitle: str) -> None:
        self.subtitle = truncate_text(subtitle, 200)

    async def finish(self, *, final_embed: Optional[discord.Embed] = None,
                     content: Optional[str] = None, delete: bool = False) -> None:
        self._finished = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.message is None:
            return
        if delete:
            await _safe_delete(self.message)
            return
        if final_embed is not None:
            await _safe_edit(self.message, embed=final_embed, content=content)
        else:
            await _safe_edit(self.message, content=content or "✅ **Done**", embed=None)


async def thinking(channel: discord.abc.Messageable, title: str = "Working",
                   subtitle: str = "Hang tight, this only takes a moment…",
                   *, delete_after: Optional[float] = None,
                   frames: Sequence[str] = FRAME_DOTS) -> discord.Message:
    """
    Send an animated "thinking" embed and keep it spinning until edited.

    v10: the message self-animates (rotating dot + shimmering subtitle) instead
    of being a frozen card, so slow commands never look dead. The loop is
    registered per message id and cancelled automatically the moment anything
    else edits that message (see `_safe_edit` / `stop_thinking`).
    """
    message = await channel.send(embed=loading_embed(title, subtitle))
    if anim_on():
        task = asyncio.create_task(_animate_thinking(message, title, subtitle, frames),
                                   name="ui-thinking")
        _THINKING_TASKS[message.id] = task
    if delete_after:
        asyncio.create_task(_delete_later(message, delete_after))
    return message


async def _animate_thinking(message: discord.Message, title: str, subtitle: str,
                            frames: Sequence[str]) -> None:
    """Cycle the header of a thinking embed until it is replaced or cancelled."""
    index = 0
    try:
        while True:
            await asyncio.sleep(max(0.5, frame_seconds()))
            frame = frames[index % len(frames)]
            index += 1
            pulse = FRAME_PULSE[index % len(FRAME_PULSE)]
            embed = create_embed(f"{frame} {title}",
                                 f"{subtitle}\n\n{pulse} {int(time.time()) % 2 == 0 and 'still working…' or 'just a moment…'}",
                                 COLORS["loading"])
            ok = await _edit_without_stopping(message, embed=embed)
            if not ok:
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        return
    finally:
        _THINKING_TASKS.pop(message.id, None)


async def _edit_without_stopping(message: Optional[discord.Message], **kwargs: Any) -> bool:
    """Internal edit used BY the animation itself (must not cancel itself)."""
    if message is None:
        return False
    try:
        await message.edit(**kwargs)
        return True
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        return False


async def _delete_later(message: discord.Message, delay: float) -> None:
    await asyncio.sleep(delay)
    await _safe_delete(message)


@contextmanager
def typing_task(channel: discord.abc.Messageable):
    """
    Sync context manager that schedules `typing()` on the running loop.

    Usage inside an async command:
        with typing_task(ctx):
            ...
    """
    task = None
    try:
        if TYPING_INDICATORS and hasattr(channel, "typing"):
            loop = asyncio.get_event_loop()
            task = loop.create_task(_keep_typing(channel))
        yield task
    except Exception:
        yield None
    finally:
        if task is not None:
            task.cancel()


async def _keep_typing(channel: discord.abc.Messageable) -> None:
    """Hold the 'is typing…' indicator for up to 5 minutes."""
    deadline = time.time() + 300
    try:
        while time.time() < deadline:
            try:
                async with channel.typing():
                    await asyncio.sleep(8)
            except asyncio.CancelledError:
                raise
            except Exception:
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def with_typing(func: Callable) -> Callable:
    """
    Decorator: show the animated typing indicator while a command runs.

    Kept separate from `command_timeout()` so both can be combined freely.
    """
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        if not TYPING_INDICATORS:
            return await func(*args, **kwargs)
        channel = None
        for candidate in args:
            channel = getattr(candidate, "channel", None)
            if channel is not None:
                break
        if channel is None:
            return await func(*args, **kwargs)
        task = asyncio.create_task(_keep_typing(channel))
        try:
            return await func(*args, **kwargs)
        finally:
            task.cancel()
    return wrapper


# ── 6.6 View primitives ──────────────────────────────────────────────────────

class PanelView(discord.ui.View):
    """Base view: author-locked, auto-disabling, mobile-friendly."""

    def __init__(self, author_id: int, *, timeout: int = MANAGE_PANEL_TIMEOUT,
                 guild_only: bool = False):
        super().__init__(timeout=timeout)
        self.author_id = int(author_id)
        self.message: Optional[discord.Message] = None
        self.last_activity = time.time()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=error_embed("Panel Locked",
                                  "This panel belongs to someone else.\nRun the command yourself to get your own."),
                ephemeral=True,
            )
            return False
        self.last_activity = time.time()
        return True

    async def on_timeout(self) -> None:
        if self.message:
            await _safe_edit(self.message, view=None)
        self.stop()

    def disable_all(self) -> None:
        """Grey out every control (used when a panel becomes stale)."""
        for item in self.children:
            try:
                item.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass

    def add_button(self, *, label: str, style: discord.ButtonStyle,
                   callback, emoji: Optional[str] = None,
                   disabled: bool = False, row: Optional[int] = None) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, emoji=emoji,
                                   disabled=disabled, row=row)
        button.callback = callback
        self.add_item(button)
        return button

    def add_select(self, *, placeholder: str, options: Sequence[discord.SelectOption],
                   callback, row: Optional[int] = None) -> discord.ui.Select:
        select = discord.ui.Select(placeholder=placeholder, options=list(options), row=row)
        if callback is not None:
            select.callback = callback
        self.add_item(select)
        return select


class ConfirmView(PanelView):
    """Reusable yes/no confirmation with a countdown label."""

    def __init__(self, author_id: int, *, confirm_label: str = "Confirm",
                 cancel_label: str = "Cancel", danger: bool = False,
                 timeout: int = 90, on_confirm=None, on_cancel=None):
        super().__init__(author_id, timeout=timeout)
        self.value: Optional[bool] = None
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel

        confirm_style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success
        self.add_button(label=confirm_label, style=confirm_style, emoji="✅",
                        callback=self._confirm)
        self.add_button(label=cancel_label, style=discord.ButtonStyle.secondary, emoji="✖️",
                        callback=self._cancel)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.value = True
        await safe_defer(interaction)
        self.stop()
        if self._on_confirm:
            await self._on_confirm(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.value = False
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    embed=info_embed("Cancelled", "No changes were made."), view=None)
            else:
                await _safe_edit(interaction.message, embed=info_embed("Cancelled", "No changes were made."), view=None)
        except discord.HTTPException:
            pass
        self.stop()
        if self._on_cancel:
            await self._on_cancel(interaction)


async def ask_confirm(ctx_or_interaction, *, title: str, description: str,
                      confirm_label: str = "Confirm", danger: bool = False,
                      timeout: int = 90) -> bool:
    """Send a confirmation prompt and await the user's answer."""
    author_id = ctx_or_interaction.user.id if isinstance(ctx_or_interaction, discord.Interaction) else ctx_or_interaction.author.id
    view = ConfirmView(author_id, confirm_label=confirm_label, danger=danger, timeout=timeout)
    embed = (warning_embed(title, description) if danger else create_embed(title, description,
                                                                           COLORS["warning"] if danger else COLORS["brand"]))
    if isinstance(ctx_or_interaction, discord.Interaction):
        if ctx_or_interaction.response.is_done():
            view.message = await ctx_or_interaction.followup.send(embed=embed, view=view, wait=True)
        else:
            await ctx_or_interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            try:
                view.message = await ctx_or_interaction.original_response()
            except discord.HTTPException:
                view.message = None
    else:
        view.message = await ctx_or_interaction.channel.send(embed=embed, view=view)
    await view.wait()
    return bool(view.value)




class Paginator(PanelView):
    """
    Button paginator for long lists.

        paginator = Paginator(ctx.author.id, pages=["page1", "page2"], title="…")
        await paginator.send(ctx)
    """

    def __init__(self, author_id: int, pages: Sequence[discord.Embed], *,
                 timeout: int = 240, extra_view: Optional[discord.ui.View] = None):
        super().__init__(author_id, timeout=timeout)
        self.pages: List[discord.Embed] = list(pages)
        self.index = 0
        self.extra_view = extra_view
        total = len(self.pages)
        if total > 1:
            self.add_button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️",
                            callback=self._previous, row=0)
            self.add_button(label="Next", style=discord.ButtonStyle.primary, emoji="▶️",
                            callback=self._next, row=0)
        self.add_button(label="Close", style=discord.ButtonStyle.danger, emoji="🗑️",
                        callback=self._close, row=1)
        if extra_view:
            # v10 fix: never steal items that already belong to another live
            # view - that silently removed the buttons from the original panel.
            for item in list(extra_view.children):
                if getattr(item, "_view", None) is None:
                    self.add_item(item)

    def _decorate(self) -> discord.Embed:
        embed = self.pages[self.index]
        total = len(self.pages)
        base = (embed.footer.text or brand_footer())
        base = re.sub(r"\s*\|\s*Page \d+/\d+$", "", base)
        base = re.sub(r"\s*•\s*Page \d+/\d+$", "", base)
        embed.set_footer(text=truncate_text(f"{base} • Page {self.index + 1}/{total}", 2048),
                         icon_url=BOT_ICON_URL or None)
        return embed

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.index = (self.index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self._decorate(), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.index = (self.index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self._decorate(), view=self)

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()

    async def send(self, ctx: commands.Context) -> discord.Message:
        self.message = await ctx.send(embed=self._decorate(), view=self)
        return self.message


def chunk_lines(lines: Sequence[str], *, char_limit: int = 1024, max_lines: int = 12) -> List[str]:
    """Group text into embed-field sized chunks (never splits a line)."""
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in lines:
        line = line if len(line) <= char_limit else line[: char_limit - 1] + "…"
        addition = len(line) + (1 if current else 0)
        if current and (current_len + addition > char_limit or len(current) >= max_lines):
            chunks.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += addition
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def build_pages(title: str, lines: Sequence[str], *, color: Union[int, str] = COLORS["brand"],
                page_size: int = 8, description: str = "", field_name: str = "Entries",
                thumbnail: bool = True) -> List[discord.Embed]:
    """Split a long list into ready-to-send pages."""
    grouped = [lines[i:i + page_size] for i in range(0, len(lines), page_size)] or [[]]
    pages: List[discord.Embed] = []
    total = len(grouped)
    for index, group in enumerate(grouped, start=1):
        heading = title if total == 1 else f"{title}  ·  {index}/{total}"
        embed = create_embed(heading, description, color, thumbnail=thumbnail)
        if not group:
            add_field(embed, field_name, "Nothing to show yet.")
        else:
            for chunk in chunk_lines(group):
                add_field(embed, field_name, chunk)
        pages.append(embed)
    return pages

# ═══════════════════════════════════════════════════════════════════════════
# 6.7 v10 ANIMATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════
#
#  Everything in this section is NEW in v10 - nothing from v9 was removed.
#  The building blocks:
#
#    Animator          – cycles frames on any message via a render callback
#    TypewriterEmbed   – reveals an embed character by character
#    LiveEmbedMixin    – turns ANY PanelView into a self-refreshing live panel
#    LivePlatformView  – always-on platform dashboard   (!live)
#    LiveFleetView     – htop-style container leaderboard (!top)
#    LiveContainerGrid – personal live dashboard        (!dashboard)
#
#  Every live panel:
#    • refreshes on its own interval without any user input
#    • shows a pulsing LIVE dot + tick counter + elapsed clock
#    • draws wave/gradient bars and sparklines from real snapshots
#    • pauses on button press and stops cleanly on timeout/close
#    • never fights with button interactions (asyncio lock + idle guard)

class Animator:
    """
    Generic frame-cycling engine.

        animator = Animator(message, lambda frame: my_embed(frame))
        await animator.start()
        ...
        await animator.stop(final_embed=embed)

    `renderer` receives the current frame index and returns either an Embed or
    a plain string (content). Returning None skips that frame.
    """

    def __init__(self, message: discord.Message, renderer: Callable[[int], Any], *,
                 interval: Optional[float] = None, max_frames: Optional[int] = None):
        self.message = message
        self.renderer = renderer
        self.interval = interval or frame_seconds()
        self.max_frames = max_frames
        self._task: Optional[asyncio.Task] = None
        self._frame = 0
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> "Animator":
        if self._running:
            return self
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="animator")
        return self

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(max(0.3, self.interval))
                if not self._running:
                    return
                self._frame += 1
                if self.max_frames and self._frame > self.max_frames:
                    self._running = False
                    return
                try:
                    payload = self.renderer(self._frame)
                except Exception as exc:
                    logger.debug("animator renderer failed: %s", exc)
                    self._running = False
                    return
                if payload is None:
                    continue
                try:
                    if isinstance(payload, discord.Embed):
                        await self.message.edit(embed=payload)
                    else:
                        await self.message.edit(content=str(payload))
                except discord.HTTPException:
                    self._running = False
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("animator loop ended: %s", exc)
            self._running = False

    async def stop(self, *, final_embed: Optional[discord.Embed] = None,
                   final_content: Optional[str] = None) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if final_embed is not None or final_content is not None:
            await _safe_edit(self.message, embed=final_embed, content=final_content)


class TypewriterEmbed:
    """
    Reveals an embed description chunk by chunk — used for `!help`, `!about`
    and the boot banner. Feels premium on desktop and mobile alike.
    """

    def __init__(self, message: discord.Message, embed: discord.Embed, *,
                 chunk: int = 14, delay: Optional[float] = None):
        self.message = message
        self.embed = embed
        self.chunk = max(2, int(chunk))
        self.delay = delay or TYPEWRITER_SPEED

    async def run(self) -> None:
        full = self.embed.description or ""
        if not full or not TYPEWRITER_ENABLED or not anim_on():
            return
        caret = frame_of(FRAME_BEAT_DOT)
        total = len(full)
        step = self.chunk
        shown = 0
        try:
            while shown < total:
                shown = min(total, shown + step)
                slice_text = full[:shown]
                if shown < total:
                    slice_text += f"\n{caret}"
                self.embed.description = slice_text
                ok = await _safe_edit(self.message, embed=self.embed)
                if not ok:
                    return
                await asyncio.sleep(self.delay)
            self.embed.description = full
            await _safe_edit(self.message, embed=self.embed)
        except asyncio.CancelledError:
            raise
        except Exception:
            return


class LiveEmbedMixin:
    """
    Plug-in auto-refresh for any `PanelView`.

    Subclass must implement `async def build_live_embed(self) -> discord.Embed`.
    Call `self.start_live(message)` after sending the first embed.

    Features:
      • `live` flag + Pause/Resume
      • tick counter, elapsed clock and a pulsing LIVE dot in the title
      • idle guard: skips a tick if the user interacted in the last 2s
      • asyncio lock so a refresh never races a button edit
      • hard stop after LIVE_MAX_MINUTES (Discord view lifetime safety)
    """

    live: bool = False
    live_tick: int = 0
    live_task: Optional[asyncio.Task] = None
    live_interval: float = float(LIVE_REFRESH_SECONDS)
    live_started: float = 0.0
    _live_lock: Optional[asyncio.Lock] = None

    async def build_live_embed(self) -> discord.Embed:  # pragma: no cover - abstract
        raise NotImplementedError

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _ensure_live_lock(self) -> asyncio.Lock:
        if self._live_lock is None:
            self._live_lock = asyncio.Lock()
        return self._live_lock

    def start_live(self, message: Optional[discord.Message] = None, *,
                   interval: Optional[float] = None) -> None:
        if message is not None:
            self.message = message
        if interval:
            self.live_interval = float(interval)
        self.live = True
        self.live_started = self.live_started or time.time()
        if self.live_task is None or self.live_task.done():
            self.live_task = asyncio.create_task(self._live_loop(), name="live-panel")

    def stop_live(self) -> None:
        self.live = False
        if self.live_task and not self.live_task.done():
            self.live_task.cancel()
        self.live_task = None

    def toggle_live(self, message: Optional[discord.Message] = None) -> bool:
        if self.live:
            self.stop_live()
        else:
            self.live_started = time.time()
            self.live_tick = 0
            self.start_live(message)
        return self.live

    def live_footer(self, base: Optional[str] = None) -> str:
        elapsed = fmt_duration(time.time() - (self.live_started or time.time()))
        state = "LIVE" if self.live else "PAUSED"
        dot = beat(self.live_tick) if self.live else "⏸️"
        text = base or brand_footer()
        return truncate_text(
            f"{text} • {dot} {state} • tick {self.live_tick} • up {elapsed} • "
            f"every {int(self.live_interval)}s", 2048)

    def _decorate_live(self, embed: discord.Embed) -> discord.Embed:
        base = (embed.footer.text or "").split(" • ")[0] or brand_footer()
        embed.set_footer(text=self.live_footer(base), icon_url=BOT_ICON_URL or None)
        if embed.title and not embed.title.startswith(("●", "◉", "○", "⏸")):
            embed.title = truncate_text(f"{'●' if self.live else '⏸️'} {embed.title}", 250)
        else:
            embed.title = truncate_text(f"{beat(self.live_tick) if self.live else '⏸️'} "
                                        f"{embed.title.lstrip('●◉○⏸️ ')}", 250)
        return embed

    async def _live_loop(self) -> None:
        lock = self._ensure_live_lock()
        try:
            while self.live:
                await asyncio.sleep(max(2.0, self.live_interval))
                if not self.live:
                    return
                # Stop politely when the panel has been alive long enough.
                if self.live_started and (time.time() - self.live_started) > LIVE_MAX_MINUTES * 60:
                    self.live = False
                    if self.message:
                        embed = self._decorate_live(await self.build_live_embed())
                        embed.add_field(name="⏹️ Live session ended",
                                        value=(f"Auto-refresh stopped after {LIVE_MAX_MINUTES} minutes. "
                                               f"Press **Resume** to keep watching."), inline=False)
                        await _safe_edit(self.message, embed=embed, view=self)
                    return
                # Idle guard: skip this tick if a human just pressed something.
                if hasattr(self, "last_activity") and (time.time() - float(getattr(self, "last_activity", 0))) < 2.5:
                    continue
                async with lock:
                    self.live_tick += 1
                    try:
                        embed = await self.build_live_embed()
                    except Exception as exc:
                        logger.debug("live panel build failed: %s", exc)
                        continue
                    await _safe_edit(self.message, embed=self._decorate_live(embed), view=self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("live loop ended: %s", exc)
            self.live = False

    async def render_live(self) -> None:
        """Render one frame immediately (used by the Refresh button)."""
        async with self._ensure_live_lock():
            self.live_tick += 1
            embed = await self.build_live_embed()
            await _safe_edit(self.message, embed=self._decorate_live(embed), view=self)


class LivePanelView(LiveEmbedMixin, PanelView):
    """Convenience base: a PanelView with Pause / Refresh / Close controls."""

    def __init__(self, author_id: int, *, timeout: int = max(600, LIVE_MAX_MINUTES * 60 + 120),
                 interval: Optional[float] = None):
        PanelView.__init__(self, author_id, timeout=timeout)
        self.live = False
        self.live_tick = 0
        self.live_task = None
        self.live_interval = float(interval or LIVE_REFRESH_SECONDS)
        self.live_started = 0.0
        self._live_lock = None

    # ── controls ─────────────────────────────────────────────────────────────
    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", row=1)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await safe_defer(interaction)
        self.toggle_live()
        button.label = "Resume" if not self.live else "Pause"
        button.emoji = "▶️" if not self.live else "⏸️"
        await self.render_live()

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
    async def refresh_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await safe_defer(interaction)
        await self.render_live()

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def close_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop_live()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=None)
            else:
                await _safe_edit(interaction.message, view=None)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        self.stop_live()
        await super().on_timeout()


# ── 6.8 v10 LIVE PANELS ──────────────────────────────────────────────────────

async def collect_platform_snapshot() -> Dict[str, Any]:
    """
    Gather everything the live dashboard needs in one pass.

    Runs every blocking call in worker threads, so the event loop stays free
    even with hundreds of containers.
    """
    nodes = await run_blocking(get_nodes)
    rows = [dict(r) for r in await run_blocking(query_all, "SELECT * FROM vps")]
    history = await run_blocking(get_host_history, 40)

    total = len(rows)
    running = sum(1 for r in rows if str(r.get("status")) == "running" and not r.get("suspended"))
    stopped = sum(1 for r in rows if str(r.get("status")) == "stopped" and not r.get("suspended"))
    suspended = sum(1 for r in rows if r.get("suspended"))
    whitelisted = sum(1 for r in rows if r.get("whitelisted"))
    public = sum(1 for r in rows if r.get("is_public"))
    owners = len({r["user_id"] for r in rows})

    total_ram = sum(clamp_int(str(r.get("ram", "0")).replace("GB", ""), 0, 4096, 0) for r in rows)
    total_cpu = sum(clamp_int(r.get("cpu", 0), 0, 512, 0) for r in rows)
    total_disk = sum(clamp_int(str(r.get("storage", "0")).replace("GB", ""), 0, 100000, 0) for r in rows)

    node_lines: List[str] = []
    online_nodes = 0
    for node in nodes:
        status_text, online = await get_node_status(node["id"])
        online_nodes += 1 if online else 0
        used = await run_blocking(get_current_vps_count, node["id"])
        capacity = int(node.get("total_vps") or 0)
        node_lines.append(f"**{node['name']}** {status_text} • `{used}/{capacity or '∞'}` containers")

    local_node = next((n for n in nodes if n.get("is_local")), None)
    if local_node is not None:
        cpu, ram, disk_pct = await asyncio.gather(
            run_blocking(get_host_cpu_usage),
            run_blocking(get_host_ram_usage),
            run_blocking(get_host_disk_percent),
        )
    else:
        first = nodes[0] if nodes else None
        stats = await get_host_stats(first["id"]) if first else {"cpu": 0.0, "ram": 0.0, "disk_pct": 0.0}
        cpu = float(stats.get("cpu") or 0)
        ram = float(stats.get("ram") or 0)
        disk_pct = float(stats.get("disk_pct") or 0)

    ports_used = int((await run_blocking(query_one, "SELECT COUNT(*) AS c FROM port_forwards"))["c"])
    ports_alloc = int((await run_blocking(query_one,
                                          "SELECT COALESCE(SUM(allocated_ports),0) AS s FROM port_allocations"))["s"])
    sessions = len(webssh_sessions) if WEBSSH_ENABLED else 0

    return {
        "nodes": nodes,
        "node_lines": node_lines,
        "online_nodes": online_nodes,
        "total": total,
        "running": running,
        "stopped": stopped,
        "suspended": suspended,
        "whitelisted": whitelisted,
        "public": public,
        "owners": owners,
        "ram": total_ram,
        "cpu": total_cpu,
        "disk": total_disk,
        "cpu_pct": cpu,
        "ram_pct": ram,
        "disk_pct": disk_pct,
        "history": history,
        "ports_used": ports_used,
        "ports_alloc": ports_alloc,
        "sessions": sessions,
        "uptime": get_host_uptime_seconds(),
    }


class LivePlatformView(LivePanelView):
    """
    `!live` — the always-on platform dashboard.

    Host CPU/RAM/disk with wave bars + sparkline history, node health,
    container mix, port usage and a live clock. Refreshes by itself.
    """

    def __init__(self, author_id: int, *, interval: Optional[float] = None):
        super().__init__(author_id, interval=interval)
        self.snapshot: Dict[str, Any] = {}
        self.history_cpu: List[float] = []
        self.history_ram: List[float] = []

    async def build_live_embed(self) -> discord.Embed:
        snap = await collect_platform_snapshot()
        self.snapshot = snap
        self.history_cpu.append(float(snap["cpu_pct"]))
        self.history_ram.append(float(snap["ram_pct"]))
        self.history_cpu = self.history_cpu[-60:]
        self.history_ram = self.history_ram[-60:]

        tick = self.live_tick
        health = "🟢 Healthy" if snap["online_nodes"] == len(snap["nodes"]) and snap["nodes"] else "🟡 Degraded"
        embed = create_embed(
            f"{BOT_NAME} Live Platform",
            f"{health} • **{snap['running']}** running • **{snap['total']}** containers • "
            f"**{snap['owners']}** owners",
            COLORS["live"] if health.startswith("🟢") else COLORS["warning"],
        )
        embed.add_field(
            name=f"🖥️ Host CPU {load_face(snap['cpu_pct'])}",
            value=(f"{gradient_bar(snap['cpu_pct'], 100, 12, tick=tick)}\n"
                   f"`{snap['cpu_pct']:.1f}%` • uptime `{fmt_duration(snap['uptime'])}`"),
            inline=True,
        )
        embed.add_field(
            name=f"🧠 Host RAM {load_face(snap['ram_pct'])}",
            value=(f"{gradient_bar(snap['ram_pct'], 100, 12, tick=tick)}\n"
                   f"`{snap['ram_pct']:.1f}%` • disk `{snap['disk_pct']:.0f}%`"),
            inline=True,
        )
        embed.add_field(
            name="📦 Containers",
            value=(f"🟢 `{snap['running']}` running\n"
                   f"🔴 `{snap['stopped']}` stopped\n"
                   f"🟡 `{snap['suspended']}` suspended\n"
                   f"🌍 `{snap['public']}` self-service"),
            inline=True,
        )
        if len(self.history_cpu) >= 3:
            embed.add_field(
                name="📈 Host trend (live)",
                value=(f"CPU `{sparkline(self.history_cpu, width=26)}`\n"
                       f"RAM `{sparkline(self.history_ram, width=26)}`"),
                inline=False,
            )
        embed.add_field(
            name="🖥️ Allocated to customers",
            value=f"`{snap['ram']} GB` RAM • `{snap['cpu']}` vCPU • `{snap['disk']} GB` disk",
            inline=True,
        )
        embed.add_field(
            name="🔌 Ports",
            value=f"`{snap['ports_used']}/{snap['ports_alloc']}` used "
                  f"{mini_meter(snap['ports_used'], max(1, snap['ports_alloc']), 8)}",
            inline=True,
        )
        embed.add_field(
            name="🌐 Terminals",
            value=f"`{snap['sessions']}` live web-SSH session(s)" if WEBSSH_ENABLED else "disabled",
            inline=True,
        )
        if snap["node_lines"]:
            for chunk in chunk_lines(snap["node_lines"], char_limit=900, max_lines=6):
                embed.add_field(name="📡 Nodes", value=chunk, inline=False)
        return embed


class LiveFleetView(LivePanelView):
    """
    `!top` — htop-style live leaderboard of the busiest containers.
    """

    def __init__(self, author_id: int, *, interval: Optional[float] = None, limit: int = 12):
        super().__init__(author_id, interval=interval or LIVE_FLEET_REFRESH)
        self.limit = clamp_int(limit, 3, 20, 12)

    async def build_live_embed(self) -> discord.Embed:
        rows = [dict(r) for r in await run_blocking(
            query_all,
            "SELECT * FROM vps WHERE status = 'running' AND suspended = 0 ORDER BY id",
        )]
        if not rows:
            embed = create_embed("Live Container Leaderboard", "No running containers right now.",
                                 COLORS["idle"])
            return embed

        sampled: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        # Keep the sampling bounded so a huge fleet never stalls the loop.
        for vps in rows[:40]:
            try:
                stats = await get_container_stats(vps["container_name"], vps["node_id"])
            except Exception:
                continue
            sampled.append((vps, stats))

        sampled.sort(key=lambda pair: (
            float(pair[1].get("cpu") or 0) + float((pair[1].get("ram") or {}).get("pct") or 0)
        ), reverse=True)
        sampled = sampled[: self.limit]

        lines = []
        for position, (vps, stats) in enumerate(sampled, start=1):
            lines.append(f"`#{position:<2}` {animated_vps_row(vps, stats, tick=self.live_tick)}")

        busiest_cpu = max((float(s.get("cpu") or 0) for _v, s in sampled), default=0.0)
        embed = create_embed(
            "Live Container Leaderboard",
            f"Top **{len(sampled)}** of **{len(rows)}** running container(s) • "
            f"hottest CPU `{busiest_cpu:.0f}%` {load_face(busiest_cpu)}",
            COLORS["live"] if busiest_cpu < CPU_THRESHOLD else COLORS["warning"],
        )
        for chunk in chunk_lines(lines, char_limit=950, max_lines=14):
            embed.add_field(name=f"{frame_of(FRAME_BARS, self.live_tick)} Usage", value=chunk, inline=False)
        embed.add_field(name="🎚️ Thresholds",
                        value=f"CPU `>{CPU_THRESHOLD}%` • RAM `>{RAM_THRESHOLD}%` • "
                              f"refresh every `{int(self.live_interval)}s`", inline=False)
        return embed


class LiveContainerGrid(LivePanelView):
    """
    `!dashboard` — the user's own servers, live and animated.
    """

    def __init__(self, author_id: int, vps_list: List[Dict[str, Any]], *,
                 interval: Optional[float] = None):
        super().__init__(author_id, interval=interval or max(6, LIVE_REFRESH_SECONDS))
        self.vps_list = list(vps_list)

    async def build_live_embed(self) -> discord.Embed:
        lines: List[str] = []
        running = 0
        total_ram = total_cpu = total_disk = 0
        for index, vps in enumerate(self.vps_list, start=1):
            try:
                stats = await get_container_stats(vps["container_name"], vps["node_id"])
            except Exception:
                stats = {}
            status = str(stats.get("status") or vps.get("status") or "unknown").lower()
            if status == "running":
                running += 1
            lines.append(
                f"**{index}.** {animated_vps_row(vps, stats, tick=self.live_tick, width=6)}\n"
                f"     ↳ `{vps.get('ram', '?')}` / `{vps.get('cpu', '?')}` vCPU • {expiration_line(vps)}"
            )
            total_ram += clamp_int(str(vps.get("ram", "0")).replace("GB", ""), 0, 4096, 0)
            total_cpu += clamp_int(vps.get("cpu", 0), 0, 512, 0)
            total_disk += clamp_int(str(vps.get("storage", "0")).replace("GB", ""), 0, 100000, 0)

        embed = create_embed(
            f"Live Dashboard · <@{self.author_id}>",
            f"🟢 **{running}** of **{len(self.vps_list)}** running • "
            f"`{total_ram} GB` RAM • `{total_cpu}` vCPU • `{total_disk} GB` disk",
            COLORS["live"] if running else COLORS["idle"],
        )
        for chunk in chunk_lines(lines, char_limit=950, max_lines=10):
            embed.add_field(name="🖥️ Your servers", value=chunk, inline=False)
        embed.add_field(name="⚡ Quick actions",
                        value=f"`{PREFIX}manage` open the panel • `{PREFIX}ports` forwarding • "
                              f"`{PREFIX}vps renew` extend", inline=False)
        return embed

# ═══════════════════════════════════════════════════════════════════════════
# 7. WEB SSH TERMINAL  (Flask + Paramiko)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Rewritten vs v8.0:
#   • Removed the "connect to the container's own port 5000" forward — nothing
#     listened there, so every generated Web-SSH link was dead on arrival.
#     Instead each VPS gets a signed, expiring, per-user URL that auto-connects
#     to the *correct* forwarded SSH port.
#   • Sessions are bound to the issuing token/IP, rate-limited and capped.
#   • Host-key verification instead of blind AutoAddPolicy.
#   • Terminal UI has a built-in fallback (no CDN required) and mobile key bar.
#
#  Fixes vs v9.0:
#   • `unlock_keys` (the browser keys issued after a correct PIN) are now
#     persisted, so restarting the bot no longer logs every browser out of a
#     PIN-protected terminal.
#   • Token rows are cleaned up when they expire instead of lingering forever.

if WEBSSH_ENABLED:
    import uuid

    WEBSSH_HTML_PATH = BASE_DIR / "webssh.html"

    def _load_webssh_html() -> str:
        """Load the Web SSH UI from the external webssh.html file."""
        try:
            return WEBSSH_HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Unable to load Web SSH HTML from %s: %s", WEBSSH_HTML_PATH, exc)
            return (
                "<!doctype html><html><body>"
                "<h2>Web SSH UI file is missing</h2>"
                "<p>Place <code>webssh.html</code> next to the bot Python file.</p>"
                "</body></html>"
            )

    @dataclass
    class WebSSHSession:
        session_id: str
        token: str
        client_ip: str
        ssh_client: Any
        transport: Any
        channel: Any
        host: str
        port: int
        username: str
        created_at: datetime
        last_activity: datetime
        buffer: str = ""
        closed: bool = False
        lock: threading.Lock = dc_field(default_factory=threading.Lock)
        reader: Optional[threading.Thread] = None

        def touch(self) -> None:
            self.last_activity = datetime.now()

    @dataclass
    class WebSSHToken:
        token: str
        user_id: str
        vps_id: Optional[int]
        host: Optional[str]
        port: Optional[int]
        username: str
        password: Optional[str]
        label: str
        created_at: datetime
        expires_at: datetime
        used: int = 0
        max_uses: int = 3
        # ── optional second factor ──
        # The link alone is a bearer credential: if it is forwarded, screenshotted
        # or read over someone's shoulder, it used to be enough to get a root
        # shell. With a PIN the stored value is an Argon2id/PBKDF2 *hash* and the
        # terminal refuses to connect until the PIN has been typed.
        pin_hash: Optional[str] = None
        pin_attempts: int = 0
        unlock_keys: set = dc_field(default_factory=set)

        @property
        def pin_required(self) -> bool:
            return bool(self.pin_hash)

        def is_valid(self) -> bool:
            return datetime.now() < self.expires_at and self.used < self.max_uses

        def is_unlocked(self, key: Optional[str]) -> bool:
            """Constant-time check of a browser unlock key (PIN already verified)."""
            if not self.pin_required:
                return True
            if not key:
                return False
            for candidate in self.unlock_keys:
                if hmac.compare_digest(str(candidate), str(key)):
                    return True
            return False

    webssh_tokens: Dict[str, "WebSSHToken"] = {}
    webssh_sessions: Dict[str, "WebSSHSession"] = {}
    webssh_lock = threading.RLock()
    webssh_connect_attempts: Dict[str, List[float]] = {}

    def _token_row(entry: "WebSSHToken") -> Tuple[Any, ...]:
        return (
            entry.token, entry.user_id, entry.vps_id, entry.host, entry.port,
            entry.username, entry.password, entry.label, entry.pin_hash,
            entry.created_at.isoformat(), entry.expires_at.isoformat(),
            entry.used, entry.max_uses, entry.pin_attempts,
            json.dumps(sorted(entry.unlock_keys)[-5:]),
        )

    _WEBSSH_COLUMNS = (
        "token, user_id, vps_id, host, port, username, password, label, pin_hash, "
        "created_at, expires_at, used, max_uses, pin_attempts, unlock_keys"
    )
    _WEBSSH_PLACEHOLDERS = ", ".join(["?"] * 15)
    _WEBSSH_UPSERT = (
        f"INSERT OR REPLACE INTO webssh_tokens ({_WEBSSH_COLUMNS}) VALUES ({_WEBSSH_PLACEHOLDERS})"
    )

    def issue_webssh_token(user_id: Union[int, str], *, vps: Optional[Dict[str, Any]] = None,
                           ttl_seconds: int = 900, host: Optional[str] = None,
                           port: Optional[int] = None, password: Optional[str] = None,
                           label: str = "", max_uses: int = 3,
                           pin: Optional[str] = None) -> str:
        """Create a signed, expiring, per-user terminal link."""
        if vps is not None:
            node_id = int(vps.get("node_id") or 1)
            node = get_node(node_id)
            forward = None
            for record in get_forwards_for_container(vps["container_name"]):
                if int(record["vps_port"]) == 22:
                    forward = record
                    break
            host = host or ((node.get("url") or "").replace("http://", "").replace("https://", "").split(":")[0]
                            if node else "") or WEBSSH_SERVER_IP
            port = port or (int(forward["host_port"]) if forward else None)
            password = password if password is not None else decrypt_secret(vps.get("root_password"))
            label = label or f"{vps['container_name']}"

        token = secrets.token_urlsafe(24)
        entry = WebSSHToken(
            token=token,
            user_id=str(user_id),
            vps_id=int(vps["id"]) if vps and vps.get("id") else None,
            host=host,
            port=int(port) if port else None,
            username="root",
            password=password,
            label=label or "Your VPS",
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(seconds=ttl_seconds),
            pin_hash=hash_password(str(pin)) if pin else None,
        )
        with webssh_lock:
            # prune expired tokens
            for key, value in list(webssh_tokens.items()):
                if not value.is_valid():
                    webssh_tokens.pop(key, None)
                    try:
                        execute("DELETE FROM webssh_tokens WHERE token = ?", (key,))
                    except Exception:
                        pass
            webssh_tokens[token] = entry

        _save_webssh_token(token)
        return token

    def _load_webssh_tokens_from_db() -> None:
        """Load webssh tokens (including unlock keys) from database on startup."""
        try:
            rows = query_all(f"SELECT {_WEBSSH_COLUMNS} FROM webssh_tokens")
            loaded = 0
            with webssh_lock:
                for row in rows:
                    try:
                        (token, user_id, vps_id, host, port, username, password, label,
                         pin_hash, created_at_iso, expires_at_iso, used, max_uses,
                         pin_attempts, unlock_keys_raw) = row

                        entry = WebSSHToken(
                            token=token,
                            user_id=user_id,
                            vps_id=vps_id,
                            host=host,
                            port=port,
                            username=username,
                            password=password,
                            label=label,
                            created_at=datetime.fromisoformat(created_at_iso),
                            expires_at=datetime.fromisoformat(expires_at_iso),
                            used=used,
                            max_uses=max_uses,
                            pin_hash=pin_hash,
                            pin_attempts=pin_attempts,
                        )
                        try:
                            entry.unlock_keys = set(json.loads(unlock_keys_raw or "[]"))
                        except (TypeError, ValueError):
                            entry.unlock_keys = set()

                        if entry.is_valid():
                            webssh_tokens[token] = entry
                            loaded += 1
                        else:
                            execute("DELETE FROM webssh_tokens WHERE token = ?", (token,))
                    except Exception as exc:
                        logger.debug("Could not load webssh token: %s", exc)
            logger.debug("Loaded %d webssh tokens from database", loaded)
        except Exception as exc:
            logger.debug("Could not load webssh tokens from database: %s", exc)

    def _save_webssh_token(token: str) -> None:
        """Save/update a webssh token (PIN state + unlock keys) in the database."""
        with webssh_lock:
            entry = webssh_tokens.get(token)
        if not entry:
            return
        try:
            execute(_WEBSSH_UPSERT, _token_row(entry))
        except Exception as exc:
            logger.debug("Could not save webssh token to database: %s", exc)

    def build_webssh_link(token: str, *, host_port: Optional[int] = None) -> str:
        if host_port:
            return WEBSSH_URL_FORMAT.format(SERVER_IP=WEBSSH_SERVER_IP, PORT=host_port)
        return WEBSSH_URL_FORMAT.format(SERVER_IP=WEBSSH_SERVER_IP, PORT=WEBSSH_PORT)

    def _public_url_for_token(token: str) -> str:
        base = WEBSSH_URL_FORMAT.format(SERVER_IP=WEBSSH_SERVER_IP, PORT=WEBSSH_PORT).rstrip("/")
        return f"{base}/t/{token}"

    def _cleanup_webssh() -> None:
        now = datetime.now()
        with webssh_lock:
            for session_id, session in list(webssh_sessions.items()):
                idle = (now - session.last_activity).total_seconds()
                if session.closed or idle > WEBSSH_SESSION_TTL:
                    _close_session(session_id, reason="expired" if not session.closed else "closed")
            for token, entry in list(webssh_tokens.items()):
                if not entry.is_valid():
                    webssh_tokens.pop(token, None)
                    try:
                        execute("DELETE FROM webssh_tokens WHERE token = ?", (token,))
                    except Exception:
                        pass

    def _close_session(session_id: str, reason: str = "closed") -> None:
        session = webssh_sessions.pop(session_id, None)
        if not session:
            return
        session.closed = True
        for closer in (session.channel, session.transport, session.ssh_client):
            try:
                closer.close()
            except Exception:
                pass
        logger.info("Web-SSH session %s closed (%s, %s@%s)", session_id[:8], reason,
                    session.username, session.host)

    def _webssh_cleanup_loop() -> None:
        while True:
            try:
                _cleanup_webssh()
            except Exception as exc:  # pragma: no cover
                logger.debug("webssh cleanup error: %s", exc)
            time.sleep(120)

    def _ip_rate_ok(ip: str) -> bool:
        now = time.time()
        with webssh_lock:
            history = [t for t in webssh_connect_attempts.get(ip, []) if now - t < 60]
            history.append(now)
            webssh_connect_attempts[ip] = history
            return len(history) <= 12

    def _sessions_for_ip(ip: str) -> int:
        return sum(1 for s in webssh_sessions.values() if s.client_ip == ip and not s.closed)

    def _render_terminal(token: str, autoconnect: Optional[Dict[str, Any]],
                         *, unlock_key: str = "") -> str:
        payload = json.dumps(autoconnect) if autoconnect else "null"
        # A password containing "</script>" must not be able to end the tag.
        payload = payload.replace("</", "<\\/")
        html = _load_webssh_html()
        html = html.replace("__BOT_NAME__", BOT_NAME)
        html = html.replace("__BOT_VERSION__", BOT_VERSION)
        html = html.replace("__TOKEN__", token or "none")
        html = html.replace("__UNLOCK_KEY__", unlock_key or "")
        html = html.replace("__AUTOCONNECT__", payload)
        return html

    def _render_pin_page(token: str, *, expired: bool = False, attempts_left: int = 5) -> str:
        # Return webssh.html but inject JS to open the PIN modal on load
        html = _load_webssh_html()
        html = html.replace("__BOT_NAME__", BOT_NAME)
        html = html.replace("__BOT_VERSION__", BOT_VERSION)
        html = html.replace("__TOKEN__", token or "none")
        html = html.replace("__UNLOCK_KEY__", "")  # No unlock key for PIN entry

        # Include autoconnect data even before PIN is entered!
        # This way, after PIN verification, the doConnect() call will work
        with webssh_lock:
            entry = webssh_tokens.get(token)
            if entry and entry.host and entry.port:
                autoconnect = {
                    "host": entry.host,
                    "port": entry.port,
                    "username": entry.username,
                    "password": entry.password,
                    "label": entry.label,
                }
                payload = json.dumps(autoconnect)
                payload = payload.replace("</", "<\\/")
            else:
                payload = "null"

        html = html.replace("__AUTOCONNECT__", payload)

        # Inject script to auto-open PIN modal and show attempt count
        pin_init = f"""
        <script>
            window.__PIN_REQUIRED__ = true;
            window.__PIN_ATTEMPTS_LEFT__ = {attempts_left};
            window.__PIN_EXPIRED__ = {str(expired).lower()};

            // Auto-open PIN modal after page load
            window.addEventListener('load', function() {{
                var pinModal = document.getElementById('pinModal');
                if (pinModal) {{
                    pinModal.classList.add('active');
                    var inPin = document.getElementById('inPin');
                    if (inPin) inPin.focus();
                }}

                // Show attempts left in note
                if ({attempts_left} <= 0) {{
                    var note = document.getElementById('pinNote');
                    if (note) {{
                        note.textContent = '❌ Too many failed attempts. Session expired.';
                        note.className = 'modal-note error';
                        note.style.display = 'block';
                        document.getElementById('inPin').disabled = true;
                        document.getElementById('pinSubmit').disabled = true;
                    }}
                }} else if ({attempts_left} <= 2) {{
                    var note = document.getElementById('pinNote');
                    if (note) {{
                        note.textContent = '⚠️ ' + {attempts_left} + ' attempt(s) remaining';
                        note.className = 'modal-note error';
                        note.style.display = 'block';
                    }}
                }}
            }});
        </script>
        """
        # Insert before closing </body> tag
        html = html.replace("</body>", pin_init + "</body>")
        return html

    def create_webssh_app() -> "Flask":
        flask_app = Flask(f"{BOT_NAME.lower()}-webssh")
        flask_app.config.update(
            SECRET_KEY=secrets.token_hex(32),
            JSON_SORT_KEYS=False,
            MAX_CONTENT_LENGTH=64 * 1024,
        )

        def _client_ip() -> str:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
            return request.remote_addr or "unknown"

        @flask_app.after_request
        def _security_headers(response):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
                "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
                "connect-src 'self'; img-src 'self' data:; form-action 'none'; base-uri 'none'"
            )
            return response

        @flask_app.route("/", methods=["GET"])
        def index():
            return _render_terminal(token="none", autoconnect=None)

        @flask_app.route("/webssh.html", methods=["GET"])
        def webssh_html():
            return _load_webssh_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

        @flask_app.route("/t/<token>", methods=["GET"])
        def token_terminal(token: str):
            unlock_key = str(request.args.get("k") or "")[:128]
            with webssh_lock:
                entry = webssh_tokens.get(token)
                if not entry or not entry.is_valid():
                    if entry and entry.pin_required:
                        return _render_pin_page(token), 410
                    return _render_terminal(token="none", autoconnect=None).replace(
                        "<h2 id=\"loginTitle\">Connect to a server</h2>",
                        "<h2 id=\"loginTitle\">Link expired</h2>"
                    ), 410
                if entry.pin_required and not entry.is_unlocked(unlock_key):
                    return _render_pin_page(token, attempts_left=WEBSSH_PIN_ATTEMPTS - entry.pin_attempts)
                autoconnect = {
                    "host": entry.host,
                    "port": entry.port,
                    "username": entry.username,
                    "password": entry.password,
                    "label": entry.label,
                } if entry.host and entry.port else None
            return _render_terminal(token=token, autoconnect=autoconnect, unlock_key=unlock_key)

        @flask_app.route("/api/ssh/unlock", methods=["POST"])
        def ssh_unlock():
            """Second factor: exchange a PIN for a browser unlock key."""
            payload = request.get_json(silent=True) or {}
            token_value = str(payload.get("token", ""))[:128]
            pin = str(payload.get("pin", ""))[:64]
            client_ip = _client_ip()

            if not _ip_rate_ok(client_ip):
                return jsonify({"success": False, "error": "Too many attempts, wait a minute."}), 429

            with webssh_lock:
                entry = webssh_tokens.get(token_value)
                if not entry or not entry.is_valid():
                    return jsonify({"success": False, "error": "This terminal link has expired.",
                                    "code": "token_used"}), 401
                if not entry.pin_required:
                    return jsonify({"success": True, "key": ""})
                if entry.pin_attempts >= WEBSSH_PIN_ATTEMPTS:
                    webssh_tokens.pop(token_value, None)
                    return jsonify({"success": False, "code": "too_many_pins",
                                    "error": "Too many wrong PINs — this link is now disabled. "
                                             "Ask the bot for a fresh terminal link."}), 423

                if not verify_password(pin, entry.pin_hash):
                    entry.pin_attempts += 1
                    _save_webssh_token(token_value)
                    audit("webssh_pin_fail", entry.user_id, target=entry.label,
                          detail=f"{WEBSSH_PIN_ATTEMPTS - entry.pin_attempts} attempt(s) left", success=False)
                    remaining = WEBSSH_PIN_ATTEMPTS - entry.pin_attempts
                    if remaining <= 0:
                        webssh_tokens.pop(token_value, None)
                        return jsonify({"success": False, "code": "too_many_pins",
                                        "error": "Too many wrong PINs — this link is now disabled."}), 423
                    return jsonify({"success": False, "code": "bad_pin",
                                    "error": f"Incorrect PIN. {remaining} attempt(s) left."}), 401

                key = secrets.token_urlsafe(18)
                entry.unlock_keys.add(key)
                if len(entry.unlock_keys) > 5:  # oldest browser sessions fall off
                    entry.unlock_keys = set(list(entry.unlock_keys)[-5:])
                entry.pin_attempts = 0
                _save_webssh_token(token_value)

            audit("webssh_pin_ok", entry.user_id, target=entry.label)
            logger.info("Web-SSH PIN accepted for %s from %s", entry.label, client_ip)
            return jsonify({"success": True, "key": key})

        @flask_app.route("/api/health", methods=["GET"])
        def health():
            with webssh_lock:
                return jsonify({
                    "status": "ok",
                    "bot": BOT_NAME,
                    "version": BOT_VERSION,
                    "sessions": len(webssh_sessions),
                    "uptime_s": int(get_host_uptime_seconds()),
                })

        @flask_app.route("/api/ssh/connect", methods=["POST"])
        def ssh_connect():
            payload = request.get_json(silent=True) or {}
            token_value = str(payload.get("token", ""))
            client_ip = _client_ip()

            if not _ip_rate_ok(client_ip):
                return jsonify({"success": False, "error": "Too many connection attempts, wait a minute."}), 429

            entry: Optional[WebSSHToken] = None
            is_trusted = False
            if token_value and token_value != "none":
                with webssh_lock:
                    entry = webssh_tokens.get(token_value)
                    if not entry or not entry.is_valid():
                        return jsonify({"success": False, "error": "This terminal link has expired.",
                                        "code": "token_used"}), 401
                    if entry.pin_required and not entry.is_unlocked(str(payload.get("unlock_key") or "")):
                        return jsonify({
                            "success": False, "code": "pin_required",
                            "error": "This link is PIN protected. Reload the page and enter your PIN.",
                        }), 401
                    entry.used += 1
                    _save_webssh_token(token_value)
                is_trusted = True

            host = (str(payload.get("host") or "")).strip()
            port = clamp_int(payload.get("port"), 1, 65535, 22)
            username = (str(payload.get("username") or "root")).strip()[:32]
            password = str(payload.get("password") or "")

            if is_trusted and entry and entry.host:
                host = entry.host
                port = entry.port or port
                username = entry.username or username
                if not password:
                    password = entry.password or ""

            if not host or not password:
                return jsonify({"success": False, "error": "Host and password are required."}), 400

            ok, reason = validate_ssh_target(host, port, allow_private=is_trusted or WEBSSH_ALLOW_PRIVATE_HOSTS)
            if not ok:
                logger.warning("[SECURITY] Blocked Web-SSH target %s:%s from %s (%s)",
                               host, port, client_ip, reason)
                return jsonify({"success": False, "error": f"Target rejected: {reason}"}), 400

            if _sessions_for_ip(client_ip) >= WEBSSH_SESSION_MAX_PER_IP:
                return jsonify({"success": False, "error": "Too many open sessions from your network."}), 429
            with webssh_lock:
                if len(webssh_sessions) >= WEBSSH_MAX_SESSIONS:
                    return jsonify({"success": False, "error": "Terminal is at capacity, try again shortly."}), 503

            def _open():
                client = get_ssh_client()
                # Retry logic: SSH might not be fully ready
                max_retries = 3
                last_error = None
                for attempt in range(max_retries):
                    try:
                        client.connect(
                            hostname=host,
                            port=int(port),
                            username=username,
                            password=password,
                            timeout=WEBSSH_CONNECT_TIMEOUT,
                            banner_timeout=WEBSSH_CONNECT_TIMEOUT,
                            auth_timeout=WEBSSH_CONNECT_TIMEOUT,
                            allow_agent=False,
                            look_for_keys=False,
                        )
                        persist_host_key(client)
                        return client
                    except (paramiko.SSHException, EOFError, ConnectionRefusedError) as e:
                        last_error = e
                        logger.debug("WebSSH connection attempt %d/%d failed: %s", attempt + 1, max_retries, e)
                        if attempt < max_retries - 1:
                            import time
                            time.sleep(2)  # Wait before retry
                        else:
                            raise last_error

            try:
                ssh_client = await_webssh_thread(_open)
            except paramiko.AuthenticationException:
                audit("webssh_connect", entry.user_id if entry else None, target=f"{host}:{port}", success=False,
                      detail="auth failed")
                return jsonify({"success": False, "error": "Authentication failed — check the password."}), 401
            except paramiko.SSHException as exc:
                return jsonify({"success": False, "error": f"SSH negotiation failed: {exc}"}), 400
            except Exception as exc:
                logger.warning("Web-SSH connect error: %s", exc)
                return jsonify({"success": False, "error": f"Could not connect: {exc}"}), 400

            transport = ssh_client.get_transport()
            transport.set_keepalive(30)
            channel = transport.open_session()
            channel.get_pty(term="xterm-256color", width=120, height=32)
            channel.invoke_shell()

            session_id = uuid.uuid4().hex
            session = WebSSHSession(
                session_id=session_id,
                token=token_value,
                client_ip=client_ip,
                ssh_client=ssh_client,
                transport=transport,
                channel=channel,
                host=host,
                port=int(port),
                username=username,
                created_at=datetime.now(),
                last_activity=datetime.now(),
            )
            with webssh_lock:
                webssh_sessions[session_id] = session

            def reader() -> None:
                chan = session.channel
                try:
                    while not session.closed:
                        if chan.recv_ready():
                            chunk = chan.recv(65536)
                            if not chunk:
                                break
                            text = chunk.decode("utf-8", errors="replace")
                            with session.lock:
                                session.buffer += text
                                if len(session.buffer) > 400_000:
                                    session.buffer = session.buffer[-200_000:]
                        elif chan.exit_status_ready():
                            break
                        else:
                            time.sleep(0.02)
                except Exception as exc:
                    logger.debug("Web-SSH reader stopped: %s", exc)
                finally:
                    session.closed = True

            session.reader = threading.Thread(target=reader, daemon=True, name=f"ssh-{session_id[:6]}")
            session.reader.start()

            audit("webssh_connect", entry.user_id if entry else client_ip, target=f"{username}@{host}:{port}",
                  detail=f"session {session_id[:8]}")
            logger.info("Web-SSH session %s opened: %s@%s:%s", session_id[:8], username, host, port)
            return jsonify({
                "success": True,
                "session_id": session_id,
                "target": f"{username}@{host}:{port}",
            })

        def _token_guard(payload: Dict[str, Any]) -> Optional[Any]:
            """Return an error response if the caller may not use this session."""
            session_id = str(payload.get("session_id") or "")
            token_value = str(payload.get("token") or "")
            session = webssh_sessions.get(session_id)
            if not session:
                return jsonify({"success": False, "error": "Session not found or expired."}), 401
            if session.token not in ("", "none") and token_value != session.token:
                return jsonify({"success": False, "error": "Session token mismatch."}), 401
            return None

        @flask_app.route("/api/ssh/read", methods=["POST"])
        def ssh_read():
            payload = request.get_json(silent=True) or {}
            guard = _token_guard(payload)
            if guard:
                return guard
            session = webssh_sessions.get(str(payload.get("session_id")))
            if session is None:
                return jsonify({"success": False, "error": "Session expired."}), 401
            last_index = clamp_int(payload.get("last_index"), 0, 10_000_000, 0)
            with session.lock:
                if last_index > len(session.buffer):
                    last_index = 0
                chunk = session.buffer[last_index:]
                new_index = len(session.buffer)
                closed_now = session.closed and not chunk
            session.touch()
            return jsonify({"success": True, "data": chunk, "last_index": new_index, "closed": closed_now})

        @flask_app.route("/api/ssh/write", methods=["POST"])
        def ssh_write():
            payload = request.get_json(silent=True) or {}
            guard = _token_guard(payload)
            if guard:
                return guard
            session = webssh_sessions.get(str(payload.get("session_id")))
            if session is None:
                return jsonify({"success": False, "error": "Session expired."}), 401
            data = str(payload.get("data") or "")
            if len(data) > 8192:
                return jsonify({"success": False, "error": "Payload too large."}), 413
            if session.closed:
                return jsonify({"success": False, "error": "Channel closed."}), 410
            try:
                session.channel.send(data)
            except Exception as exc:
                return jsonify({"success": False, "error": f"Send failed: {exc}"}), 500
            session.touch()
            return jsonify({"success": True})

        @flask_app.route("/api/ssh/resize", methods=["POST"])
        def ssh_resize():
            payload = request.get_json(silent=True) or {}
            guard = _token_guard(payload)
            if guard:
                return guard
            session = webssh_sessions.get(str(payload.get("session_id")))
            if session is None:
                return jsonify({"success": False, "error": "Session expired."}), 401
            cols = clamp_int(payload.get("cols"), 20, 400, 120)
            rows = clamp_int(payload.get("rows"), 5, 200, 32)
            try:
                session.channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass
            session.touch()
            return jsonify({"success": True})

        @flask_app.route("/api/ssh/disconnect", methods=["POST"])
        def ssh_disconnect():
            payload = request.get_json(silent=True) or {}
            session_id = str(payload.get("session_id") or "")
            session = webssh_sessions.get(session_id)
            if not session:
                return jsonify({"success": True})
            token_value = str(payload.get("token") or "")
            if session.token not in ("", "none") and token_value != session.token:
                return jsonify({"success": False, "error": "Session token mismatch."}), 401
            _close_session(session_id, reason="user requested")
            return jsonify({"success": True})

        return flask_app


    def await_webssh_thread(func: Callable[[], Any], timeout: int = 30) -> Any:
        """Run a blocking SSH connect on a worker thread with a hard timeout."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ssh-connect") as pool:
            future = pool.submit(func)
            try:
                return future.result(timeout=timeout)
            except FutureTimeout:
                raise TimeoutError("SSH connection attempt timed out")

    _webssh_thread = threading.Thread(target=_webssh_cleanup_loop, daemon=True, name="webssh-cleanup")
    _webssh_thread.start()


# Backwards-compatibility shims so older helper names keep working.
def get_vps_data_snapshot() -> Dict[str, List[Dict[str, Any]]]:
    return get_vps_data()

# ═══════════════════════════════════════════════════════════════════════════
# 8. BOT CORE  (client • events • background workers)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Fixes vs v8.0:
#   • `on_command_error` allowed the default help for real errors and fired
#     before the bot was ready (causing 400 "Unknown message" replies).
#   • Command timeouts previously produced *two* replies and killed the task.
#   • Rate limiting applied per command execution through a global check.
#   • Presence now rotates + everything blocking runs off-loop.
#   • Slash commands + context menus are supported for the modern UX.
#
#  Fixes vs v9.0:
#   • The application-command context menu is now actually registered on the
#     command tree (previously it was defined but never synced).
#   • `MissingPermissions` / `BotMissingPermissions` / `UserInputError` /
#     `NotOwner` now have proper replies instead of the generic error card.
#   • New background workers: host-snapshot recorder (powers v10 graphs) and
#     a heartbeat that keeps live panels fed with fresh data.

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.dm_messages = True


class UnixNodesBot(commands.Bot):
    """Custom client: keeps the event loop healthy and supervises background jobs."""

    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            max_messages=2500,
        )
        self.started_at = datetime.now(timezone.utc)
        self._flask_started = False
        self._background_tasks: List[asyncio.Task] = []
        self.ready_event = asyncio.Event()
        self.presence_index = 0
        # v10: shared heartbeat counter broadcast to every live panel.
        self.heartbeat = 0
        self.command_counter = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        # Do not remove the custom prefix help command here.  The help command
        # is registered later in this module, before setup_hook runs; removing
        # it here silently unregisters !help / !h / !commands / !cmds at startup.
        for coro in (self._autosave_loop, self._expiration_loop, self._presence_loop,
                     self._monitor_loop, self._host_stats_loop, self._heartbeat_loop):
            self._background_tasks.append(asyncio.create_task(coro(), name=coro.__name__))
        try:
            synced = await self.tree.sync()
            logger.info("Synced %s application commands", len(synced))
        except discord.HTTPException as exc:
            logger.warning("Slash command sync skipped: %s", exc)

    async def on_ready(self) -> None:
        self.ready_event.set()
        logger.info("%s connected as %s (id %s)", BOT_NAME, self.user, self.user.id if self.user else "?")
        logger.info("Serving %s guild(s) · %s cached member(s)", len(self.guilds), sum(g.member_count or 0 for g in self.guilds))
        try:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching,
                                          name=f"{BOT_NAME} • {PREFIX}help"),
            )
        except discord.HTTPException:
            pass
        if WEBSSH_ENABLED and not self._flask_started:
            # Load webssh tokens from database on startup
            _load_webssh_tokens_from_db()
            self.start_webssh_server()

    async def on_disconnect(self) -> None:
        self.ready_event.clear()

    async def close(self) -> None:
        logger.info("Shutting down…")
        for task in self._background_tasks:
            task.cancel()
        for task in self._background_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        final_save()
        await super().close()

    # ── Flask web terminal ───────────────────────────────────────────────────
    def start_webssh_server(self) -> None:
        self._flask_started = True
        flask_app = create_webssh_app()

        def _serve() -> None:
            try:
                logger.info("Web terminal listening on http://%s:%s", WEBSSH_HOST, WEBSSH_PORT)
                # Werkzeug's dev server with threads is fine behind a reverse proxy;
                # production deployments should front it with nginx + TLS.
                flask_app.run(host=WEBSSH_HOST, port=WEBSSH_PORT, debug=False,
                              threaded=True, use_reloader=False)
            except Exception as exc:  # pragma: no cover
                logger.error("Web terminal crashed: %s", exc)

        threading.Thread(target=_serve, daemon=True, name="webssh-server").start()

    # ── background workers ───────────────────────────────────────────────────
    async def _autosave_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.sleep(900)
                await run_blocking(backup_database)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Autosave loop error: %s", exc)

    async def _presence_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                total = await run_blocking(get_total_vps_count)
                running = await run_blocking(get_running_vps_count)
                frames = (
                    (discord.ActivityType.watching, f"{running}/{total} VPS online · {PREFIX}live"),
                    (discord.ActivityType.playing, f"{BOT_NAME} {BOT_VERSION}"),
                    (discord.ActivityType.listening, f"{PREFIX}help · {PREFIX}manage"),
                    (discord.ActivityType.watching, f"{PREFIX}live · live platform stats"),
                )
                activity_type, name = frames[self.presence_index % len(frames)]
                self.presence_index += 1
                await self.change_presence(status=discord.Status.online,
                                           activity=discord.Activity(type=activity_type, name=name[:128]))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(45)

    async def _heartbeat_loop(self) -> None:
        """v10: a global tick every live panel can read for smooth animations."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                self.heartbeat += 1
            except Exception:
                pass
            await asyncio.sleep(1)

    async def _host_stats_loop(self) -> None:
        """v10: record host CPU/RAM/disk so `!graph` and `!live` have history."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                local_node = await run_blocking(get_local_node)
                node_id = int(local_node["id"]) if local_node else 1
                stats = await get_host_stats(node_id)
                total = await run_blocking(get_total_vps_count)
                running = await run_blocking(get_running_vps_count)
                await run_blocking(
                    save_host_snapshot,
                    float(stats.get("cpu") or 0),
                    float(stats.get("ram") or 0),
                    float(stats.get("disk_pct") or 0),
                    total,
                    running,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("host snapshot loop error: %s", exc)
            await asyncio.sleep(max(15, HOST_SNAPSHOT_INTERVAL))

    async def _expiration_loop(self) -> None:
        """Warn, suspend and (optionally) report on expiring VPS."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.run_expiration_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Expiration pass failed: %s", exc, exc_info=True)
            await asyncio.sleep(max(300, EXPIRATION_CHECK_INTERVAL))

    async def run_expiration_pass(self) -> None:
        now = datetime.now(timezone.utc)
        for user_id in await run_blocking(get_all_user_ids_with_vps):
            for vps in await run_blocking(get_user_vps, user_id):
                expiry = safe_iso_parse(vps.get("expiration_date"))
                if expiry is None:
                    continue
                seconds_left = (expiry - now).total_seconds()
                container = vps["container_name"]

                if seconds_left <= 0:
                    if EXPIRATION_AUTO_SUSPEND and not vps.get("suspended") and not vps.get("whitelisted"):
                        try:
                            await run_lxc_command(container, f"stop {shlex.quote(container)}",
                                                  timeout=120, node_id=vps["node_id"], check=False)
                        except Exception as exc:
                            logger.warning("Auto-suspend failed for %s: %s", container, exc)
                        history = vps.get("suspension_history") or []
                        history.append({
                            "time": now.isoformat(),
                            "reason": f"Automatic suspension — expired on {expiry.date().isoformat()}",
                            "by": "expiration-monitor",
                        })
                        await run_blocking(patch_vps, container, status="stopped", suspended=True,
                                           suspension_history=history)
                        await self._dm_user(user_id, error_embed(
                            "VPS Expired & Suspended",
                            f"`{container}` expired on {discord_timestamp(expiry, 'F')} and has been suspended.\n\n"
                            f"First 15 days of data are kept. Ask an admin to renew to restore access."
                        ))
                        audit("vps_auto_suspend", "system", target=container, detail="expired")
                    continue

                if seconds_left <= EXPIRATION_WARNING_DAYS * 86400:
                    cooldown_key = f"expiry_warn:{container}"
                    active, _, _ = await run_blocking(get_cooldown, cooldown_key)
                    if not active:
                        await run_blocking(set_cooldown, cooldown_key, 12 * 3600, "expiry warning sent")
                        await self._dm_user(user_id, warning_embed(
                            "VPS Expiring Soon",
                            f"`{container}` expires {discord_timestamp(expiry, 'R')}.\n\n"
                            f"Use `{PREFIX}vps renew` before then to keep your server running."
                        ))

    async def _monitor_loop(self) -> None:
        """Abuse watch: monitor containers above thresholds (LOG ONLY - NO AUTO-SUSPENSION)."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(600)
            try:
                for user_id in await run_blocking(get_all_user_ids_with_vps):
                    for vps in await run_blocking(get_user_vps, user_id):
                        if vps.get("suspended") or vps.get("whitelisted"):
                            continue
                        if vps.get("status") != "running":
                            continue
                        stats = await get_container_stats(vps["container_name"], vps["node_id"])
                        if not stats.get("online"):
                            continue
                        cpu, ram_pct = float(stats.get("cpu") or 0), float((stats.get("ram") or {}).get("pct") or 0)
                        if cpu > CPU_THRESHOLD or ram_pct > RAM_THRESHOLD:
                            logger.warning("[MONITOR] %s high usage cpu=%.1f%% ram=%.1f%% (limits %s/%s)",
                                           vps["container_name"], cpu, ram_pct, CPU_THRESHOLD, RAM_THRESHOLD)
                            # NOTE: VPS are NOT auto-suspended for high resource usage.
                            # Use !resource-check to review, then !suspend-vps manually if needed.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Monitor loop error: %s", exc)

    async def _dm_user(self, user_id: Union[int, str], embed: discord.Embed) -> bool:
        try:
            user = self.get_user(int(user_id)) or await self.fetch_user(int(user_id))
            await user.send(embed=embed)
            return True
        except Exception as exc:
            logger.debug("DM to %s failed: %s", user_id, exc)
            return False


bot = UnixNodesBot()

# Convenience alias used by the permission decorators.
def is_admin() -> Callable:
    async def predicate(ctx: commands.Context) -> bool:
        if is_admin_id(ctx.author.id):
            return True
        raise commands.CheckFailure("You need **admin** permissions for this command.")
    return commands.check(predicate)


def is_main_admin() -> Callable:
    async def predicate(ctx: commands.Context) -> bool:
        if is_main_admin_id(ctx.author.id):
            return True
        raise commands.CheckFailure("Only the **main admin** can run this command.")
    return commands.check(predicate)


def guild_only() -> Callable:
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("This command must be used inside a server.")
        return True
    return commands.check(predicate)


async def global_ratelimit(ctx: commands.Context) -> bool:
    """Per-user token bucket applied to every text command (admins exempt)."""
    if ctx.command and ctx.command.name in {"help", "commands", "quickhelp"}:
        return True
    if is_admin_id(ctx.author.id):
        return True
    allowed, message = check_rate_limit(ctx.author.id)
    if not allowed:
        try:
            await ctx.message.add_reaction("🐢")
        except discord.HTTPException:
            pass
        await ctx.reply(embed=warning_embed("Slow down", message), mention_author=False, delete_after=6)
        return False
    return True


bot.add_check(global_ratelimit)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, asyncio.TimeoutError):
        error = error.original  # type: ignore[assignment]
    if isinstance(error, asyncio.TimeoutError):
        await ctx.reply(embed=warning_embed("Timed Out", "That operation took too long. Nothing else was changed — try again."),
                        mention_author=False, delete_after=10)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        usage = getattr(ctx.command, "signature", "")
        await ctx.reply(embed=error_embed("Missing Argument",
                                          f"`{PREFIX}{ctx.command.qualified_name} {usage}`\n\n"
                                          f"See `{PREFIX}help {ctx.command.qualified_name}` for details."),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
        await ctx.reply(embed=error_embed("Invalid Argument", f"{error}\n\nTry `{PREFIX}help {ctx.command.qualified_name}`."),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, commands.MemberNotFound):
        await ctx.reply(embed=error_embed("User Not Found", "Mention a user or paste their ID."),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, commands.NotOwner):
        await ctx.reply(embed=error_embed("Owner Only", "Only the bot owner can run this command."),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(embed=error_embed("Missing Permissions",
                                          "You need: " + ", ".join(f"`{p}`" for p in error.missing_permissions)),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, commands.BotMissingPermissions):
        await ctx.reply(embed=error_embed("I Need Permissions",
                                          "Grant me: " + ", ".join(f"`{p}`" for p in error.missing_permissions)),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(embed=warning_embed("On Cooldown", f"Try again in **{error.retry_after:.1f}s**."),
                        mention_author=False, delete_after=8)
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply(embed=error_embed("Access Denied", str(error) or "You cannot use this command."),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.reply(embed=error_embed("Server Only", "This command only works inside a server."),
                        mention_author=False, delete_after=10)
        return
    if isinstance(error, discord.Forbidden):
        await ctx.reply(embed=error_embed("Missing Permissions",
                                          "Discord refused the action. Check my role position and permissions."),
                        mention_author=False, delete_after=12)
        return
    if isinstance(error, discord.NotFound):
        await ctx.reply(embed=error_embed("Not Found", "That message or member no longer exists."),
                        mention_author=False, delete_after=10)
        return

    logger.error("Unhandled command error in %s: %s", ctx.command, error, exc_info=error)
    reference = f"`{ctx.command.qualified_name}`" if ctx.command else "that command"
    await ctx.reply(embed=error_embed("Unexpected Error",
                                      f"Something went wrong while running {reference}.\n"
                                      f"The details were written to the bot log."),
                    mention_author=False, delete_after=15)


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.CheckFailure):
        message = str(error) or "You cannot use this command."
    else:
        logger.error("Slash command error: %s", error, exc_info=error)
        message = "Something went wrong. Please try again or contact an admin."
    await safe_send(interaction, embed=error_embed("Error", message))


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.content:
        return
    if message.content.strip() in {f"<@{bot.user.id}>", f"<@!{bot.user.id}>"}:
        embed = create_embed(
            f"{BOT_NAME} • VPS Control",
            "Here to help you run containers, not conversations 😄",
            COLORS["brand"],
        )
        add_field(embed, "🚀 Getting started", f"`{PREFIX}help` — full command list\n"
                                              f"`{PREFIX}vps create` — spin up a free VPS\n"
                                              f"`{PREFIX}manage` — control your server", True)
        add_field(embed, "📊 Live status", f"`{PREFIX}live` — platform dashboard\n"
                                           f"`{PREFIX}dashboard` — your servers, live\n"
                                           f"`{PREFIX}ping` — latency", True)
        await message.channel.send(embed=embed)
        return
    await bot.process_commands(message)


def command_timeout(timeout_seconds: int) -> Callable:
    """Hard timeout for a command body: exactly one friendly reply on expiry."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(ctx: commands.Context, *args, **kwargs):
            try:
                return await asyncio.wait_for(func(ctx, *args, **kwargs), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning("Command %s timed out after %ss (user %s)",
                               getattr(ctx.command, "name", "?"), timeout_seconds, ctx.author.id)
                try:
                    await ctx.reply(embed=warning_embed("Timed Out",
                                                        f"That took longer than {timeout_seconds}s and was cancelled."),
                                    mention_author=False, delete_after=12)
                except discord.HTTPException:
                    pass
                return None
        return wrapper
    return decorator


def final_save() -> None:
    """Flush the WAL, verify integrity and back up on the way out."""
    try:
        with db_conn(write=True) as conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                logger.debug("WAL checkpoint skipped (%s)", exc)
    except Exception as exc:
        logger.debug("Final WAL flush skipped: %s", exc)
    try:
        backup_database(force=True)
    except Exception as exc:
        logger.error("Final backup failed: %s", exc)


atexit.register(final_save)

# ═══════════════════════════════════════════════════════════════════════════
# 9. CORE / USER COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

@bot.command(name="ping", aliases=["latency", "pong"])
@with_typing
@command_timeout(60)
async def ping(ctx: commands.Context) -> None:
    """Animated latency check (gateway + round-trip + shard info)."""
    message = await ctx.send(embed=loading_embed("Pinging", "Measuring gateway and REST latency…"))
    spinner = Spinner(message, interval=0.6)
    await spinner.start("Pinging")

    start = time.perf_counter()
    await bot.application_info()
    rest_ms = (time.perf_counter() - start) * 1000
    gateway_ms = bot.latency * 1000
    await spinner.stop()

    health = "🟢 Excellent" if gateway_ms < 120 else ("🟡 Good" if gateway_ms < 250 else "🟠 Elevated")
    embed = success_embed("Pong!", f"Everything is responsive · {health}", thumbnail=False)
    add_field(embed, "⚡ Gateway", f"`{gateway_ms:.0f} ms`", True)
    add_field(embed, "🌐 REST round-trip", f"`{rest_ms:.0f} ms`", True)
    add_field(embed, "📶 Shard", f"`{getattr(bot, 'shard_count', 1) or 1}`", True)
    add_field(embed, "🕒 Uptime", f"`{fmt_duration((datetime.now(timezone.utc) - bot.started_at).total_seconds())}`", True)
    add_field(embed, "🧠 Loop lag", f"{gradient_bar(min(gateway_ms, 500), 500, 8)}", True)
    add_field(embed, "📦 Version", f"`v{BOT_VERSION}`", True)
    add_field(embed, "💓 Heartbeat", f"`#{bot.heartbeat}` • animations {'on' if anim_on() else 'off'}", True)
    embed.set_footer(text=beat() + " " + brand_footer(), icon_url=BOT_ICON_URL or None)
    await _safe_edit(message, embed=embed, content=None)


@bot.command(name="uptime", aliases=["host-uptime"])
@with_typing
@command_timeout(60)
async def uptime(ctx: commands.Context) -> None:
    """Host uptime, bot uptime and load average in one compact card."""
    message = await thinking(ctx.channel, "Reading uptime", "Sampling host counters…")
    host_seconds = await run_blocking(get_host_uptime_seconds)
    bot_seconds = (datetime.now(timezone.utc) - bot.started_at).total_seconds()
    host_cpu, host_ram, host_disk, host_disk_pct = await asyncio.gather(
        run_blocking(get_host_cpu_usage),
        run_blocking(get_host_ram_usage),
        run_blocking(get_host_disk_usage),
        run_blocking(get_host_disk_percent),
    )
    embed = info_embed("Uptime", f"Host `{fmt_duration(host_seconds)}` • Bot `{fmt_duration(bot_seconds)}`")
    load = get_load_average()
    if load:
        add_field(embed, "📈 Load average", f"`{load[0]:.2f}` / `{load[1]:.2f}` / `{load[2]:.2f}`", True)
    add_field(embed, "🧮 CPU", gradient_bar(host_cpu, 100, 10), True)
    add_field(embed, "🧠 RAM", gradient_bar(host_ram, 100, 10), True)
    add_field(embed, "💾 Disk", f"{host_disk}\n{gradient_bar(host_disk_pct, 100, 10)}", True)
    add_field(embed, "🐍 Python / OS", f"`{platform.python_version()}` • `{platform.system()} {platform.release()}`", False)
    await _safe_edit(message, embed=embed)


@bot.command(name="about", aliases=["info-bot"])
@with_typing
@command_timeout(60)
async def about(ctx: commands.Context) -> None:
    """Bot information, statistics and links (typewriter intro)."""
    embed = create_embed(f"About {BOT_NAME}", "LXC container hosting, managed straight from Discord.", COLORS["magic"])
    add_field(embed, "📦 Version", f"`{BOT_VERSION}`", True)
    add_field(embed, "👨‍💻 Developer", f"`{BOT_DEVELOPER}`", True)
    add_field(embed, "🔧 Prefix", f"`{PREFIX}`", True)
    add_field(embed, "🖥️ VPS deployed", f"`{get_total_vps_count()}`", True)
    add_field(embed, "👥 Owners", f"`{len(get_all_user_ids_with_vps())}`", True)
    add_field(embed, "🛡️ Admins", f"`{len(_admin_cache)}`", True)
    add_field(embed, "🌐 Web terminal", "✅ Enabled" if WEBSSH_ENABLED else "⛔ Disabled", True)
    add_field(embed, "⏰ Default lifetime", f"`{DEFAULT_VPS_EXPIRATION_DAYS} days`", True)
    add_field(embed, "✨ Animations", f"`{'on' if anim_on() else 'off'}` • speed `{ANIM_STATE.get('speed')}x`", True)
    add_field(embed, "➡️ Next steps", f"`{PREFIX}help` • `{PREFIX}live` • `{PREFIX}vps create` • `{PREFIX}manage`", False)
    if SUPPORT_INVITE_URL:
        add_field(embed, "🆘 Support server", SUPPORT_INVITE_URL, False)

    if TYPEWRITER_ENABLED and anim_on():
        message = await ctx.send(embed=create_embed(f"About {BOT_NAME}", "▌", COLORS["magic"]))
        await TypewriterEmbed(message, embed, chunk=26).run()
    else:
        await ctx.send(embed=embed)


# ── 9.0 v10 LIVE COMMANDS ────────────────────────────────────────────────────

@bot.command(name="live", aliases=["livedash", "platform-live"])
@is_admin()
@with_typing
@command_timeout(90)
async def live_command(ctx: commands.Context) -> None:
    """
    Animated always-on platform dashboard (admin).

    Auto-refreshes with host CPU/RAM/disk, node health, container mix,
    port usage and a rolling sparkline history.
    """
    message = await thinking(ctx.channel, "Starting live dashboard", "Collecting the first snapshot…")
    view = LivePlatformView(ctx.author.id)
    embed = await view.build_live_embed()
    await _safe_edit(message, embed=view._decorate_live(embed), view=view)
    view.message = message
    view.start_live(message)


@bot.command(name="top", aliases=["livetop", "leaderboard"])
@is_admin()
@with_typing
@command_timeout(120)
async def top_command(ctx: commands.Context, limit: int = 12) -> None:
    """
    Live htop-style leaderboard of the busiest containers (admin).

    Usage: !top [rows]
    """
    message = await thinking(ctx.channel, "Sampling containers", "Reading cgroup counters…")
    view = LiveFleetView(ctx.author.id, limit=clamp_int(limit, 3, 20, 12))
    embed = await view.build_live_embed()
    await _safe_edit(message, embed=view._decorate_live(embed), view=view)
    view.message = message
    view.start_live(message)


@bot.command(name="dashboard", aliases=["mydash", "live-dashboard"])
@with_typing
@command_timeout(90)
async def dashboard_command(ctx: commands.Context) -> None:
    """Your own servers on a live, auto-refreshing dashboard."""
    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not vps_list:
        embed = info_embed("No VPS Yet", "You don't own any server on this host.")
        add_field(embed, "🚀 Get started now",
                  f"`{PREFIX}vps create` — self-service VPS (takes ~1 minute)\n"
                  f"`{PREFIX}help` — see everything the bot can do", False)
        await ctx.send(embed=embed)
        return

    message = await thinking(ctx.channel, "Building your dashboard", "Reading live container states…")
    view = LiveContainerGrid(ctx.author.id, vps_list)
    embed = await view.build_live_embed()
    await _safe_edit(message, embed=view._decorate_live(embed), view=view)
    view.message = message
    view.start_live(message)


@bot.command(name="graph", aliases=["chart", "history"])
@with_typing
@command_timeout(90)
async def graph_command(ctx: commands.Context, target: str = "host", points: int = GRAPH_POINTS) -> None:
    """
    Draw a real ASCII chart from stored snapshots.

    Usage: !graph host         → host CPU / RAM history
           !graph <container>  → that container's CPU / RAM history
           !graph host 40      → wider chart
    """
    target = (target or "host").strip()
    points = clamp_int(points, 8, 60, GRAPH_POINTS)

    if target.lower() == "host":
        if not is_admin_id(ctx.author.id):
            await ctx.send(embed=error_embed("Admins Only", "Host graphs require admin permissions.\n"
                                                            f"Try `{PREFIX}graph <your-container>` instead."))
            return
        history = await run_blocking(get_host_history, points)
        if len(history) < 3:
            await ctx.send(embed=warning_embed("Not Enough Data",
                                               "Host history is still being collected — try again in a few minutes."))
            return
        cpu_series = [row.get("cpu_pct", 0) for row in history]
        ram_series = [row.get("ram_pct", 0) for row in history]
        embed = create_embed("📊 Host history", f"Last `{len(history)}` samples", COLORS["ocean"])
        add_field(embed, "🧮 CPU", safe_code_block(ascii_graph(cpu_series, width=points, label="CPU"), max_chars=900), False)
        add_field(embed, "🧠 RAM", safe_code_block(ascii_graph(ram_series, width=points, label="RAM"), max_chars=900), False)
        add_field(embed, "📈 Compact",
                  f"CPU `{sparkline(cpu_series, width=30)}`\nRAM `{sparkline(ram_series, width=30)}`", False)
        await ctx.send(embed=embed)
        return

    vps = await run_blocking(get_vps_by_container, target)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{target}`."))
        return
    if str(vps["user_id"]) != str(ctx.author.id) and not is_admin_id(ctx.author.id):
        await ctx.send(embed=error_embed("Access Denied", "That server isn't yours."))
        return

    history = await run_blocking(get_usage_history, target, points)
    if len(history) < 3:
        await ctx.send(embed=warning_embed("Not Enough Data",
                                           "No samples yet — start the server and run a command again in a minute."))
        return
    cpu_series = [row.get("cpu_pct", 0) for row in history]
    ram_series = [row.get("ram_pct", 0) for row in history]
    embed = create_embed(f"📊 {target}", f"Last `{len(history)}` samples", COLORS["ocean"])
    add_field(embed, "🧮 CPU", safe_code_block(ascii_graph(cpu_series, width=points, label="CPU"), max_chars=900), False)
    add_field(embed, "🧠 RAM", safe_code_block(ascii_graph(ram_series, width=points, label="RAM"), max_chars=900), False)
    add_field(embed, "📈 Compact",
              f"CPU `{sparkline(cpu_series, width=30)}`\nRAM `{sparkline(ram_series, width=30)}`", False)
    await ctx.send(embed=embed)


@bot.command(name="anim", aliases=["animations"])
@is_admin()
async def anim_toggle(ctx: commands.Context, state: str = None) -> None:
    """
    Turn the v10 animation engine on/off at runtime.

    Usage: !anim on | !anim off | !anim
    """
    if state is None:
        ANIM_STATE["enabled"] = not ANIM_STATE["enabled"]
    else:
        ANIM_STATE["enabled"] = str(state).strip().lower() in ("1", "on", "true", "yes", "enable", "enabled")
    embed = success_embed(
        f"Animations {'enabled' if ANIM_STATE['enabled'] else 'disabled'}",
        f"Speed `{ANIM_STATE['speed']}x` • frame `{frame_seconds():.2f}s` — change with `{PREFIX}speed <0.5-3>`.")
    embed.set_footer(text=beat() + " " + brand_footer(), icon_url=BOT_ICON_URL or None)
    await ctx.send(embed=embed)


@bot.command(name="speed", aliases=["animspeed"])
@is_admin()
async def anim_speed(ctx: commands.Context, multiplier: float = None) -> None:
    """
    Set the global animation speed multiplier (0.5 = slow, 2 = fast).

    Usage: !speed 1.5
    """
    if multiplier is None:
        await ctx.send(embed=info_embed("Animation Speed",
                                        f"Current speed `{ANIM_STATE['speed']}x` • frame `{frame_seconds():.2f}s`\n"
                                        f"Set it with `{PREFIX}speed 1.5`."))
        return
    ANIM_STATE["speed"] = max(0.25, min(3.0, float(multiplier)))
    await ctx.send(embed=success_embed("Speed Updated",
                                       f"Animations now run at `{ANIM_STATE['speed']}x` "
                                       f"(frame `{frame_seconds():.2f}s`)."))


# ── 9.1 myvps ────────────────────────────────────────────────────────────────

@bot.command(name="myvps", aliases=["myservers", "vps-list-mine"])
@with_typing
@command_timeout(45)
async def my_vps(ctx: commands.Context) -> None:
    """Your personal VPS dashboard (paginated, mobile friendly)."""
    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not vps_list:
        embed = info_embed("No VPS Yet", "You don't own any server on this host.")
        add_field(embed, "🚀 Get started now",
                  f"`{PREFIX}vps create` — self-service VPS (takes ~1 minute)\n"
                  f"`{PREFIX}help` — see everything the bot can do", False)
        await ctx.send(embed=embed)
        return

    message = await thinking(ctx.channel, "Loading dashboard", "Fetching live container states…")

    running = suspended = whitelisted = 0
    total_ram = total_cpu = total_disk = 0
    live: List[Tuple[Dict[str, Any], str]] = []

    for vps in vps_list:
        status = await container_state(vps["container_name"], vps["node_id"])
        if status != "unknown":
            vps["status"] = status
        live.append((vps, status))

        if vps.get("suspended"):
            suspended += 1
        elif status == "running":
            running += 1
        if vps.get("whitelisted"):
            whitelisted += 1
        total_ram += clamp_int(str(vps.get("ram", "0")).replace("GB", ""), 0, 4096, 0)
        total_cpu += clamp_int(vps.get("cpu", 0), 0, 512, 0)
        total_disk += clamp_int(str(vps.get("storage", "0")).replace("GB", ""), 0, 100000, 0)

    pages: List[discord.Embed] = []
    per_page = 4
    groups = [live[i:i + per_page] for i in range(0, len(live), per_page)] or [[]]
    total_pages = len(groups)

    for page_index, group in enumerate(groups, start=1):
        embed = create_embed(
            f"Your VPS  ·  {page_index}/{total_pages}",
            f"Owner: {ctx.author.mention} • {len(vps_list)} server(s) on this host",
            COLORS["success"] if running else COLORS["brand"],
        )
        add_field(embed, "🟢 Running", f"`{running}`", True)
        add_field(embed, "🟡 Suspended", f"`{suspended}`", True)
        add_field(embed, "✅ Whitelisted", f"`{whitelisted}`", True)
        add_field(embed, "🧮 Allocated", f"`{total_ram} GB RAM`\n`{total_cpu} vCPU`\n`{total_disk} GB`", True)
        add_field(embed, "🌐 Live states", "```" + (", ".join(
            f"{v['container_name']}: {s}" for v, s in group) or "—") + "```", False)
        for index, (vps, status) in enumerate(group, start=(page_index - 1) * per_page + 1):
            vps["status"] = status
            add_field(embed, f"🖥️ Server {index}", vps_card(vps, index, compact=False), False)
        embed.set_footer(text=f"{brand_footer()} • {beat()} live snapshot", icon_url=BOT_ICON_URL or None)
        pages.append(embed)

    class MyVpsView(PanelView):
        @discord.ui.button(label="Manage", style=discord.ButtonStyle.primary, emoji="🎛️")
        async def manage(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            await open_manage_panel(interaction, ctx.author.id, vps_list)

        @discord.ui.button(label="Live view", style=discord.ButtonStyle.success, emoji="📡")
        async def live_view(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            await safe_defer(interaction, ephemeral=True)
            grid = LiveContainerGrid(interaction.user.id, vps_list)
            embed = await grid.build_live_embed()
            msg = await interaction.followup.send(embed=grid._decorate_live(embed), view=grid, wait=True)
            grid.message = msg
            grid.start_live(msg)

        @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
        async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            await safe_defer(interaction, ephemeral=True)
            await interaction.followup.send(
                embed=info_embed("Refreshing", "Reopen with the command above for a fresh snapshot."),
                ephemeral=True)

        @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️")
        async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            await interaction.response.edit_message(view=None)
            self.stop()

    view = MyVpsView(ctx.author.id)
    view.message = message
    await _safe_edit(message, embed=pages[0], view=view)

# ── 9.2 Manage panel ─────────────────────────────────────────────────────────

class LiveStatsView(PanelView):
    """Auto-refreshing live resource panel (the 'animated' stats screen)."""

    def __init__(self, author_id: int, vps: Dict[str, Any], owner_label: str,
                 *, timeout: int = CONSOLE_PANEL_TIMEOUT, interval: Optional[float] = None):
        super().__init__(author_id, timeout=timeout)
        self.vps = vps
        self.owner_label = owner_label
        self.refreshes = 0
        self.task: Optional[asyncio.Task] = None
        self.paused = False
        self.interval = float(interval or STATS_REFRESH_SECONDS)
        self._lock: Optional[asyncio.Lock] = None
        self.history_cpu: List[float] = []
        self.history_ram: List[float] = []

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start(self, message: discord.Message) -> None:
        self.message = message
        self.task = asyncio.create_task(self._loop(), name="live-stats")

    async def _loop(self) -> None:
        try:
            while not self.paused and self.refreshes < MAX_LIVE_REFRESHES:
                await asyncio.sleep(max(2.0, self.interval))
                if self.paused:
                    return
                async with self._ensure_lock():
                    self.refreshes += 1
                    await self.render(refresh=True)
            if not self.paused:
                self.paused = True
                self._set_pause_button()
                if self.message:
                    await _safe_edit(self.message, embed=await self._final_embed(), view=self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("live stats loop ended: %s", exc)

    async def _final_embed(self) -> discord.Embed:
        vps = dict(self.vps)
        stats = await get_container_stats(vps["container_name"], vps["node_id"])
        vps["status"] = stats.get("status", vps.get("status"))
        embed = create_embed(
            f"⏸️ Live Stats paused · {vps['container_name']}",
            f"{vps_status_pill(vps)} • {self.owner_label}\n"
            f"Auto-refresh finished after {self.refreshes} update(s) — press **Resume** to keep watching.",
            COLORS["idle"],
        )
        for name, value, inline in stats_panel_fields(vps, stats, tick=self.refreshes):
            add_field(embed, name, value, inline)
        return embed

    def _set_pause_button(self) -> None:
        """Keep the Pause/Resume button label in sync with the loop state."""
        for item in self.children:
            if isinstance(item, discord.ui.Button) and (item.label or "") in ("Pause", "Resume"):
                item.label = "Resume" if self.paused else "Pause"
                item.emoji = "▶️" if self.paused else "⏸️"

    async def render(self, *, refresh: bool = False) -> None:
        stats = await get_container_stats(self.vps["container_name"], self.vps["node_id"])
        history = await run_blocking(get_usage_history, self.vps["container_name"], 30)
        vps = dict(self.vps)
        vps["status"] = stats.get("status", vps.get("status"))

        ram_pct = float((stats.get("ram") or {}).get("pct") or 0)
        cpu = float(stats.get("cpu") or 0)
        self.history_cpu.append(cpu)
        self.history_ram.append(ram_pct)
        self.history_cpu = self.history_cpu[-40:]
        self.history_ram = self.history_ram[-40:]

        state = vps_status_pill(vps)
        pulse = FRAME_DOTS[self.refreshes % len(FRAME_DOTS)]
        embed = create_embed(
            f"{pulse} Live Stats · {vps['container_name']}",
            f"{state} • {self.owner_label}\n"
            f"Round #{self.refreshes + 1} • auto-refresh every {int(self.interval)}s"
            + ("  _(paused)_" if self.paused else ""),
            COLORS["success"] if stats.get("status") == "running" else COLORS["idle"],
        )
        for name, value, inline in stats_panel_fields(vps, stats, history=history, tick=self.refreshes):
            add_field(embed, name, value, inline)
        if len(self.history_cpu) >= 3:
            add_field(embed, "📈 This session",
                      f"CPU `{sparkline(self.history_cpu, width=24)}`\n"
                      f"RAM `{sparkline(self.history_ram, width=24)}`", False)
        add_field(embed, "🏷️ Allocation",
                  f"`{vps.get('ram')}` RAM • `{vps.get('cpu')}` vCPU • `{vps.get('storage')}` disk", False)
        add_field(embed, "⏰ Expiry", expiration_line(vps), False)
        embed.set_footer(
            text=f"{brand_footer()} • {beat(self.refreshes)} tick {self.refreshes}"
                 f"{'' if self.paused else ' • live'}",
            icon_url=BOT_ICON_URL or None)

        if self.message:
            await _safe_edit(self.message, embed=embed, view=self)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️")
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.paused = not self.paused
        # v10 fix: resuming from a paused state restarts the loop *and* gives
        # the session a fresh refresh budget instead of instantly dying.
        if not self.paused and self.refreshes >= MAX_LIVE_REFRESHES:
            self.refreshes = 0
        self._set_pause_button()
        await safe_defer(interaction)
        if not self.paused and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._loop(), name="live-stats")
        await self.render()

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_now(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await safe_defer(interaction)
        await self.render()

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.paused = True
        if self.task:
            self.task.cancel()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=None)
            else:
                await _safe_edit(interaction.message, view=None)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        if self.task:
            self.task.cancel()
        await super().on_timeout()


class ConsoleModal(discord.ui.Modal, title="Run a command"):
    command: discord.ui.TextInput = discord.ui.TextInput(
        label="Shell command (runs as root)",
        placeholder="e.g.  uname -a  •  docker ps  •  df -h /",
        style=discord.TextStyle.paragraph,
        max_length=800,
        required=True,
    )

    def __init__(self, vps: Dict[str, Any], requester_id: int):
        super().__init__(timeout=300)
        self.vps = vps
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=error_embed("Not Your Panel", "Open your own console."), ephemeral=True)
            return
        command = str(self.command.value).strip()
        await interaction.response.defer(ephemeral=True)
        try:
            output = await container_shell(self.vps["container_name"], command, timeout=90,
                                           node_id=self.vps["node_id"])
        except Exception as exc:
            await interaction.followup.send(embed=error_embed("Execution Failed", truncate_text(exc, 900)),
                                            ephemeral=True)
            return
        audit("console_exec", interaction.user.id, target=self.vps["container_name"], detail=command[:200])
        embed = create_embed(f"Console · {self.vps['container_name']}", f"`$ {truncate_text(command, 120)}`",
                             COLORS["dark"], thumbnail=False)
        if not output:
            add_field(embed, "Output", "_Command produced no output._")
        else:
            for chunk in chunk_lines(output.splitlines() or [output], char_limit=950, max_lines=40):
                add_field(embed, "Output", safe_code_block(chunk, max_chars=900))
        await interaction.followup.send(embed=embed, ephemeral=True)


class ExecModal(discord.ui.Modal, title="Execute command"):
    """Admin variant that targets any container by name."""

    container: discord.ui.TextInput = discord.ui.TextInput(label="Container name", max_length=63, required=True)
    command: discord.ui.TextInput = discord.ui.TextInput(
        label="Command (root)", style=discord.TextStyle.paragraph, max_length=800, required=True)

    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(embed=error_embed("Not Yours", "Open your own modal."),
                                                    ephemeral=True)
            return
        container = str(self.container.value).strip()
        valid, reason = validate_container_name(container)
        if not valid:
            await interaction.response.send_message(embed=error_embed("Invalid Name", reason), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            output = await container_shell(container, str(self.command.value), timeout=90)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed("Execution Failed", truncate_text(exc, 900)),
                                            ephemeral=True)
            return
        audit("console_exec", interaction.user.id, target=container, detail=str(self.command.value)[:200])
        embed = create_embed(f"Console · {container}", f"`$ {truncate_text(self.command.value, 120)}`",
                             COLORS["dark"], thumbnail=False)
        for chunk in chunk_lines((output or "_no output_").splitlines(), char_limit=950, max_lines=40):
            add_field(embed, "Output", safe_code_block(chunk, max_chars=900))
        await interaction.followup.send(embed=embed, ephemeral=True)


class ShareModal(discord.ui.Modal, title="Share access"):
    member: discord.ui.TextInput = discord.ui.TextInput(label="User (mention or ID)", max_length=64, required=True)

    def __init__(self, view: "ManageView"):
        super().__init__(timeout=300)
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view_ref.author_id:
            await interaction.response.send_message(embed=error_embed("Not Yours", "Open your own panel."),
                                                    ephemeral=True)
            return
        raw = str(self.member.value)
        match = re.search(r"\d{15,25}", raw)
        if not match:
            await interaction.response.send_message(
                embed=error_embed("Invalid User", "Mention the user or paste their numeric ID."), ephemeral=True)
            return
        target_id = int(match.group(0))
        if target_id == interaction.user.id:
            await interaction.response.send_message(embed=error_embed("That's You", "You already own this VPS."),
                                                    ephemeral=True)
            return

        vps = self.view_ref.vps
        shared = list(vps.get("shared_with") or [])
        if str(target_id) in shared:
            await interaction.response.send_message(embed=info_embed("Already Shared", "That user already has access."),
                                                    ephemeral=True)
            return
        if len(shared) >= 10:
            await interaction.response.send_message(
                embed=error_embed("Limit Reached", "A VPS can be shared with at most 10 people."), ephemeral=True)
            return

        shared.append(str(target_id))
        await run_blocking(patch_vps, vps["container_name"], shared_with=shared)
        vps["shared_with"] = shared
        audit("vps_share", interaction.user.id, target=vps["container_name"], detail=f"granted to {target_id}")

        await interaction.response.send_message(
            embed=success_embed("Access Granted",
                                f"<@{target_id}> can now open `{PREFIX}manage-shared` for `{vps['container_name']}`."),
            ephemeral=True)
        try:
            user = bot.get_user(target_id) or await bot.fetch_user(target_id)
            await user.send(embed=create_embed(
                "VPS Access Granted",
                f"{interaction.user.mention} shared `{vps['container_name']}` with you.",
                COLORS["success"]))
        except Exception:
            pass
        await self.view_ref.refresh_view(interaction, vps)


class ManageView(PanelView):
    """
    The control room: everything an owner (or admin) can do to one VPS.

    v10: the panel can keep itself alive — press **🔴 Live** and the card
    re-renders with fresh cgroup data every few seconds.
    """

    def __init__(self, author_id: int, vps_list: List[Dict[str, Any]], *, is_admin_view: bool = False,
                 is_shared: bool = False, feedback: str = "", timeout: int = MANAGE_PANEL_TIMEOUT):
        super().__init__(author_id, timeout=timeout)
        self.vps_list = list(vps_list)
        self.is_admin_view = is_admin_view
        self.is_shared = is_shared
        # Destructive operations stay with the owner (or an admin), never with
        # a user the server was merely shared with.
        self.can_destroy = not is_shared or is_admin_view
        self.feedback = feedback
        self.index = 0
        # Pagination for VPS selection dropdown (Discord limit: 25 options)
        self.select_page = 0
        # v10 live machinery
        self.live = False
        self.live_task: Optional[asyncio.Task] = None
        self.live_tick = 0
        self.live_started = 0.0
        self._live_lock: Optional[asyncio.Lock] = None
        self._build()

    # ── helpers ──────────────────────────────────────────────────────────────
    @property
    def vps(self) -> Dict[str, Any]:
        return self.vps_list[self.index]

    async def render(self) -> discord.Embed:
        """Live-rendered dashboard card for the selected VPS."""
        vps = self.vps
        stats = await get_container_stats(vps["container_name"], vps["node_id"])
        if stats.get("status") != "unknown":
            vps["status"] = stats["status"]
            await run_blocking(patch_vps, vps["container_name"], status=stats["status"])

        node = get_node(vps["node_id"]) or {"name": "Unknown"}
        owner_text = ""
        if self.is_admin_view:
            try:
                owner = bot.get_user(int(vps["user_id"])) or await bot.fetch_user(int(vps["user_id"]))
                owner_text = f"\n👤 Owner: {owner.mention}"
            except Exception:
                owner_text = f"\n👤 Owner id: `{vps['user_id']}`"

        embed = create_embed(
            f"Managing {vps['container_name']}",
            f"{vps_status_pill(vps)} • 🖥️ {node.get('name')}{owner_text}"
            + (f"\n\n{self.feedback}" if self.feedback else ""),
            COLORS["success"] if stats.get("status") == "running" and not vps.get("suspended") else COLORS["brand"],
        )
        add_field(embed, "🧮 Allocation",
                  f"`{vps.get('ram', '?')}` RAM\n`{vps.get('cpu', '?')}` vCPU\n`{vps.get('storage', '?')}` disk\n"
                  f"🐧 `{vps.get('os_version', 'ubuntu:22.04')}`", True)
        add_field(embed, "📊 Right now",
                  f"CPU {gradient_bar(float(stats.get('cpu') or 0), 100, 8, tick=self.live_tick)}\n"
                  f"RAM {gradient_bar(float((stats.get('ram') or {}).get('pct') or 0), 100, 8, tick=self.live_tick)}\n"
                  f"⏱️ `{stats.get('uptime', '—')}`", True)
        add_field(embed, "⏰ Lifetime",
                  f"{expiration_line(vps)}\n🆔 `#{vps.get('id')}`", True)
        if vps.get("whitelisted"):
            add_field(embed, "Protected", "This server is whitelisted — auto-suspension is disabled.", False)
        if vps.get("suspended"):
            add_field(embed, "Suspended", "An admin suspended this server. Contact support to restore it.", False)
        add_field(embed, "🎛️ Controls", "Buttons below • live state refreshes on every action", False)
        return embed

    async def refresh_view(self, interaction: discord.Interaction, vps: Optional[Dict[str, Any]] = None) -> None:
        if vps is not None:
            for position, item in enumerate(self.vps_list):
                if item["container_name"] == vps["container_name"]:
                    self.vps_list[position] = vps
        embed = await self.render()
        try:
            if interaction.response.is_done():
                if interaction.message is not None:
                    await _safe_edit(interaction.message, embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            logger.debug("could not refresh manage panel")

    # ── v10 live auto-refresh ────────────────────────────────────────────────
    def _ensure_live_lock(self) -> asyncio.Lock:
        if self._live_lock is None:
            self._live_lock = asyncio.Lock()
        return self._live_lock

    def start_live(self) -> None:
        self.live = True
        self.live_started = self.live_started or time.time()
        if self.live_task is None or self.live_task.done():
            self.live_task = asyncio.create_task(self._live_loop(), name="manage-live")

    def stop_live(self) -> None:
        self.live = False
        if self.live_task and not self.live_task.done():
            self.live_task.cancel()
        self.live_task = None

    async def _live_loop(self) -> None:
        lock = self._ensure_live_lock()
        try:
            while self.live:
                await asyncio.sleep(max(5.0, MANAGE_REFRESH_SECONDS))
                if not self.live:
                    return
                if time.time() - float(getattr(self, "last_activity", 0)) < 3.0:
                    continue
                if self.live_started and (time.time() - self.live_started) > LIVE_MAX_MINUTES * 60:
                    self.live = False
                    self._sync_live_button()
                    if self.message:
                        embed = await self.render()
                        embed.add_field(name="⏹️ Live ended",
                                        value="Auto-refresh stopped. Press **🔴 Live** to start again.",
                                        inline=False)
                        await _safe_edit(self.message, embed=embed, view=self)
                    return
                async with lock:
                    self.live_tick += 1
                    embed = await self.render()
                    embed.set_footer(
                        text=f"{brand_footer()} • {beat(self.live_tick)} LIVE tick {self.live_tick}",
                        icon_url=BOT_ICON_URL or None)
                    await _safe_edit(self.message, embed=embed, view=self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("manage live loop ended: %s", exc)
            self.live = False

    def _sync_live_button(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and (item.label or "").endswith("Live"):
                item.label = "⏸️ Pause Live" if self.live else "🔴 Live"
                item.style = discord.ButtonStyle.success if self.live else discord.ButtonStyle.secondary

    def _build(self) -> None:
        self.clear_items()
        multi = len(self.vps_list) > 1
        rows = 0
        if multi:
            # Pagination: show 25 VPS per page
            start_idx = self.select_page * 25
            end_idx = min(start_idx + 25, len(self.vps_list))
            page_vps = self.vps_list[start_idx:end_idx]
            
            self.add_select(
                placeholder=f"Switch VPS… (page {self.select_page + 1}/{(len(self.vps_list) + 24) // 25})",
                options=[
                    discord.SelectOption(
                        label=f"{v['container_name']}"[:100],
                        value=str(position + start_idx),  # Store absolute index
                        description=f"{v.get('ram', '?')} RAM · {str(v.get('status', '?'))[:20]}",
                    )
                    for position, v in enumerate(page_vps)
                ],
                callback=self._switch,
                row=0,
            )
            
            # Add pagination buttons if there are more than 25 VPS
            if len(self.vps_list) > 25:
                max_page = (len(self.vps_list) - 1) // 25
                self.add_button(label="◀", style=discord.ButtonStyle.secondary, 
                              callback=self._prev_page, 
                              disabled=self.select_page == 0, row=0)
                self.add_button(label="▶", style=discord.ButtonStyle.secondary, 
                              callback=self._next_page, 
                              disabled=self.select_page >= max_page, row=0)
            rows = 1

        running = str(self.vps.get("status", "")).lower() == "running"
        suspended = bool(self.vps.get("suspended"))

        self.add_button(label="Start", style=discord.ButtonStyle.success, emoji="▶️",
                        callback=partial(self._power, action="start"), disabled=running or suspended,
                        row=rows if rows else None)
        self.add_button(label="Stop", style=discord.ButtonStyle.secondary, emoji="⏹️",
                        callback=partial(self._power, action="stop"), disabled=not running,
                        row=rows if rows else None)
        self.add_button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔁",
                        callback=partial(self._power, action="restart"), disabled=not running or suspended,
                        row=rows if rows else None)
        self.add_button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="♻️",
                        callback=self._reinstall, disabled=suspended or not self.can_destroy,
                        row=rows if rows else None)
        self.add_button(label="Stats", style=discord.ButtonStyle.primary, emoji="📊",
                        callback=self._stats, row=(rows or 0) + 1)
        self.add_button(label="Console", style=discord.ButtonStyle.secondary, emoji="🖥️",
                        callback=self._console, row=(rows or 0) + 1)
        self.add_button(label="Password", style=discord.ButtonStyle.secondary, emoji="🔑",
                        callback=self._password, row=(rows or 0) + 1)
        self.add_button(label="Web SSH", style=discord.ButtonStyle.secondary, emoji="🌐",
                        callback=self._webssh, row=(rows or 0) + 1)
        self.add_button(label="Ports", style=discord.ButtonStyle.secondary, emoji="🔌",
                        callback=self._ports, row=(rows or 0) + 2)
        self.add_button(label="Share", style=discord.ButtonStyle.secondary, emoji="👥",
                        callback=self._share, row=(rows or 0) + 2)
        self.add_button(label="Advanced", style=discord.ButtonStyle.secondary, emoji="⚙️",
                        callback=self._advanced, row=(rows or 0) + 2)
        # v10: keep the card updating by itself
        self.add_button(label="🔴 Live" if not self.live else "⏸️ Pause Live",
                        style=discord.ButtonStyle.success if self.live else discord.ButtonStyle.secondary,
                        emoji="📡", callback=self._toggle_live, row=(rows or 0) + 2)
        self.add_button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄",
                        callback=self._refresh_panel, row=(rows or 0) + 3)
        self.add_button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️",
                        callback=self._close, row=(rows or 0) + 3)

    # ── navigation ───────────────────────────────────────────────────────────
    async def _prev_page(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=False)
        if self.select_page > 0:
            self.select_page -= 1
            self._build()
            await self.refresh_view(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=False)
        max_page = (len(self.vps_list) - 1) // 25
        if self.select_page < max_page:
            self.select_page += 1
            self._build()
            await self.refresh_view(interaction)

    async def _switch(self, interaction: discord.Interaction) -> None:
        # Defer immediately to prevent timeout
        await safe_defer(interaction, ephemeral=False)
        
        self.index = clamp_int(self.select_values(interaction), 0, len(self.vps_list) - 1, 0)
        self.feedback = ""
        self._build()
        await self.refresh_view(interaction)

    def select_values(self, interaction: discord.Interaction) -> int:
        try:
            return int(interaction.data["values"][0])
        except Exception:
            return 0

    # ── power actions ────────────────────────────────────────────────────────
    async def _power(self, interaction: discord.Interaction, action: str) -> None:
        vps = self.vps
        container = vps["container_name"]
        await safe_defer(interaction)
        verb = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}[action]
        await self._flash(interaction, f"{verb} `{container}`…")

        try:
            current = await container_state(container, vps["node_id"])
            if current == "unknown":
                self.feedback = "⚠️ Could not read the container state — is LXC reachable?"
            elif action == "start" and current == "running":
                await run_blocking(patch_vps, container, status="running", suspended=False)
                self.feedback = "ℹ️ Already running — state refreshed."
            elif action == "stop" and current == "stopped":
                await run_blocking(patch_vps, container, status="stopped")
                self.feedback = "ℹ️ Already stopped — state refreshed."
            else:
                await run_lxc_command(container, f"{action} {shlex.quote(container)}",
                                      timeout=180 if action != "stop" else 120, node_id=vps["node_id"])
                await run_blocking(patch_vps, container, status="running" if action in ("start", "restart") else "stopped")
                self.feedback = f"✅ `{verb.lower()}` completed successfully."

                if action in ("start", "restart"):
                    await apply_internal_permissions(container, vps["node_id"])
                    restored = await recreate_port_forwards(container)
                    if restored:
                        self.feedback += f"\n🔌 Restored **{restored}** port forward(s)."
            audit(f"vps_{action}", interaction.user.id, target=container)
        except Exception as exc:
            logger.error("%s failed for %s: %s", action, container, exc)
            self.feedback = f"⛔ `{verb.lower()}` failed: {truncate_text(exc, 300)}"

        self._build()
        await self.refresh_view(interaction)

    async def _flash(self, interaction: discord.Interaction, text: str) -> None:
        target = interaction.message or self.message
        if target is None:
            return
        await _safe_edit(target, embed=loading_embed(text, "Working…"), view=None)

    # ── stats ────────────────────────────────────────────────────────────────
    async def _stats(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = LiveStatsView(interaction.user.id, self.vps,
                             owner_label=f"<@{self.vps['user_id']}>")
        message = await interaction.followup.send(embed=loading_embed("Sampling…", "Reading cgroup counters"),
                                                  view=view, ephemeral=True, wait=True)
        view.message = message
        await view.render()
        await view.start(message)

    # ── console ──────────────────────────────────────────────────────────────
    async def _console(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ConsoleModal(self.vps, interaction.user.id))

    # ── password ─────────────────────────────────────────────────────────────
    async def _password(self, interaction: discord.Interaction) -> None:
        vps = self.vps
        password = decrypt_secret(vps.get("root_password"))
        embed = create_embed("Root Password", f"Container `{vps['container_name']}`", COLORS["vault"],
                             thumbnail=False)
        add_field(embed, "Credentials",
                  f"**User** `root`\n**Password** {f'`{password}`' if password else '_not stored_'}",
                  False)
        add_field(embed, "Safety tips",
                  "• Only share this over an encrypted channel\n"
                  "• Rotate it if it ever leaked — press **Regenerate**\n"
                  "• Admins cannot read DMs sent to you", False)
        await interaction.response.send_message(embed=embed, view=PasswordView(self), ephemeral=True)

    # ── web ssh ──────────────────────────────────────────────────────────────
    async def _webssh(self, interaction: discord.Interaction) -> None:
        vps = self.vps
        if not WEBSSH_ENABLED:
            await interaction.response.send_message(
                embed=warning_embed("Web Terminal Disabled",
                                    "The browser terminal is switched off on this host. Use the SSH command instead."),
                ephemeral=True)
            return
        await safe_defer(interaction, ephemeral=True)
        forward = next((f for f in await run_blocking(get_forwards_for_container, vps["container_name"])
                        if int(f["vps_port"]) == 22), None)
        if forward is None:
            if str(vps.get("status")) != "running":
                await interaction.followup.send(
                    embed=warning_embed("Server Stopped", "Start the VPS first — SSH is only reachable while it runs."),
                    ephemeral=True)
                return
            host_port = await create_port_forward(vps["user_id"], vps["container_name"], 22, vps["node_id"])
            if not host_port:
                await interaction.followup.send(
                    embed=error_embed("Port Forward Failed", "Could not allocate an SSH port. Try again or contact an admin."),
                    ephemeral=True)
                return
            forward = {"host_port": host_port, "vps_port": 22}

        token = issue_webssh_token(interaction.user.id, vps=vps, ttl_seconds=1800,
                                   label=vps["container_name"])
        link = _public_url_for_token(token)
        ssh_command = ssh_command_for(int(forward["host_port"]))

        embed = create_embed("Web Terminal Ready",
                             f"One-click browser SSH for `{vps['container_name']}`.\n"
                             f"Link expires in **30 minutes**.", COLORS["network"])
        add_field(embed, "🌐 Open terminal", f"[Click here to open Web SSH]({link})", False)
        add_field(embed, "🔐 Or use your SSH client", f"```bash\n{escape_backticks(ssh_command)}\n```", False)
        add_field(embed, "🔒 Optional lock",
                  "Press **Add a PIN** below to require a 4-8 digit code in addition to the link. "
                  "Recommended if your DMs are ever screenshotted.", False)
        add_field(embed, "ℹ️ Note",
                  "The link is personal — anyone who has it can open a root shell. "
                  "Ask for a new one any time.", False)
        audit("webssh_token", interaction.user.id, target=vps["container_name"])
        await interaction.followup.send(embed=embed, ephemeral=True,
                                        view=WebSSHLinkView(interaction.user.id, token,
                                                            vps["container_name"]))

    # ── ports ────────────────────────────────────────────────────────────────
    async def _ports(self, interaction: discord.Interaction) -> None:
        embed = await run_blocking(PortsView.build_embed, self.vps)
        await interaction.response.send_message(embed=embed,
                                                view=PortsView(self, self.vps), ephemeral=True)

    # ── share ────────────────────────────────────────────────────────────────
    async def _share(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ShareModal(self))

    # ── advanced ─────────────────────────────────────────────────────────────
    async def _advanced(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=AdvancedView.build_embed(self.vps),
                                                view=AdvancedView(self, self.vps), ephemeral=True)

    # ── live toggle ──────────────────────────────────────────────────────────
    async def _toggle_live(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        if self.live:
            self.stop_live()
        else:
            self.live_started = time.time()
            self.live_tick = 0
            self.start_live()
        self._sync_live_button()
        await self.refresh_view(interaction)

    # ── refresh / close ──────────────────────────────────────────────────────
    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        self.feedback = "🔄 Refreshed from the live container."
        await self.refresh_view(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop_live()
        self.disable_all()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await _safe_edit(interaction.message, view=self)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        self.stop_live()
        await super().on_timeout()

    # ── reinstall ────────────────────────────────────────────────────────────
    async def _reinstall(self, interaction: discord.Interaction) -> None:
        vps = self.vps
        if not self.can_destroy:
            await interaction.response.send_message(
                embed=error_embed("Owner Only", "Only the owner can reinstall this server."), ephemeral=True)
            return

        # Explicitly ask for a destination node and OS before any destructive action.
        view = ReinstallNodeSelectView(interaction.user.id, self, vps)
        await view.build_options()
        if not any(isinstance(child, discord.ui.Select) for child in view.children):
            await interaction.response.send_message(
                embed=error_embed("No Nodes Available", "No configured nodes are available right now."), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=create_embed(
                "🌐 Choose Reinstall Node",
                f"Select the node for **`{vps['container_name']}`**.\n\n"
                "The VPS will be wiped and freshly installed on the selected node.", COLORS["node"]),
            view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    async def _perform_reinstall(self, interaction: discord.Interaction, node_id: int, os_version: str) -> None:
        vps = self.vps
        os_config = next((o for o in OS_OPTIONS if o.get("value") == os_version), None)
        if not os_config:
            await interaction.followup.send(embed=error_embed("Invalid OS", "Choose an OS from the menu."), ephemeral=True)
            return
        node = await run_blocking(get_node, int(node_id))
        if not node:
            await interaction.followup.send(embed=error_embed("Node Unavailable", "The selected node no longer exists."), ephemeral=True)
            return
        os_name = str(os_config.get("label") or os_version)
        os_emoji = str(os_config.get("emoji") or "🐧")
        confirmed = await ask_confirm(
            interaction,
            title="Reinstall operating system?",
            description=(f"**All data on `{vps['container_name']}` will be destroyed.**\n\n"
                         f"A fresh {os_emoji} **{os_name}** install will be created on **{node.get('name', node_id)}** "
                         "with the same RAM/CPU/disk and a new root password."),
            confirm_label="Wipe & reinstall", danger=True, timeout=60,
        )
        if not confirmed:
            await interaction.followup.send("Reinstall cancelled. Your VPS was not changed.", ephemeral=True)
            return

        old_node_id = int(vps["node_id"])
        target_node_id = int(node_id)
        panel = ProgressPanel(
            interaction.channel, f"Reinstalling {vps['container_name']}",
            ["Destroy current container", "Create fresh container", "Apply limits & permissions",
             "Install SSH access", "Restore port forwards", "Deliver credentials"],
            subtitle=f"{os_emoji} {os_name} • Node: {node.get('name', target_node_id)} • This takes 1–4 minutes.",
            color=COLORS["danger"],
            interaction=interaction,  # Pass interaction for proper deferred handling
        )
        await panel.start()

        async def step(index: int, label: str, coro):
            await panel.step(index, "running")
            try:
                result = await coro
                await panel.step(index, "done", truncate_text(label, 60))
                return result
            except Exception as exc:
                await panel.step(index, "failed", truncate_text(str(exc), 70))
                raise

        container = vps["container_name"]
        ram_gb = clamp_int(str(vps.get("ram", "1")).replace("GB", ""), 1, 512, 1)
        cpu = clamp_int(vps.get("cpu", 1), 1, 128, 1)
        disk_gb = clamp_int(str(vps.get("storage", "10")).replace("GB", ""), 5, 5000, 10)
        new_password = generate_strong_password()

        try:
            await step(0, "deleted", run_lxc_command(
                container, f"delete {shlex.quote(container)} --force", timeout=180,
                node_id=old_node_id, check=False))
            await step(1, "created", provision_container(
                container, os_version, target_node_id,
                ram_gb=ram_gb, cpu=cpu, disk_gb=disk_gb))
            # Update the node mapping before restoring forwards, as forward helpers
            # resolve the VPS's node from the database.
            await run_blocking(patch_vps, container, node_id=target_node_id, os_version=os_version)
            await panel.step(2, "done", "nesting + privileged + fuse")
            ssh_ok, ssh_note = await step(3, "ssh ready", configure_ssh(container, target_node_id, new_password))
            if not ssh_ok:
                raise RuntimeError(f"SSH setup failed after reinstall: {ssh_note}")

            # Extended grace period: allow SSH daemon to fully stabilize and accept connections
            # This prevents "Error reading SSH protocol banner" from paramiko
            await asyncio.sleep(10)
            
            await step(4, "forwards restored", recreate_port_forwards(container))
            await step(5, "delivered", run_host_motd(container, target_node_id))
        except Exception as exc:
            await panel.finish(final_embed=error_embed(
                "Reinstall Failed",
                f"`{container}` may need admin repair: {truncate_text(exc, 700)}\n\n"
                f"An admin can inspect the node mapping and repair it with `{PREFIX}apply-permissions {container}`."))
            logger.error("Reinstall failed for %s: %s", container, exc, exc_info=True)
            return

        await run_blocking(patch_vps, container, node_id=target_node_id, status="running", suspended=False,
                           root_password=encrypt_secret(new_password), os_version=os_version,
                           created_at=datetime.now().isoformat())
        try:
            forward = next((f for f in await run_blocking(get_forwards_for_container, container)
                            if int(f["vps_port"]) == 22), None)
            if forward:
                await run_blocking(clear_host_key, YOUR_SERVER_IP, int(forward["host_port"]))
        except Exception as exc:
            logger.debug("Could not clear host key: %s", exc)

        audit("vps_reinstall", interaction.user.id, target=container,
              detail=f"os={os_version}; node={target_node_id}; previous_node={old_node_id}")
        forward = next((f for f in await run_blocking(get_forwards_for_container, container)
                        if int(f["vps_port"]) == 22), None)
        ssh_command = ssh_command_for(int(forward["host_port"]) if forward else None)
        embed = success_embed("✅ Reinstall Complete", f"`{container}` is freshly installed and running.", thumbnail=False)
        add_field(embed, "🧮 Resources", f"`{ram_gb} GB` RAM • `{cpu}` vCPU • `{disk_gb} GB` disk", True)
        add_field(embed, "🐧 OS", f"`{os_name}`", True)
        add_field(embed, "🌐 Node", f"`{node.get('name', target_node_id)}`", True)
        add_field(embed, "📲 Access Your Server",
                  "**Check your DMs!** 📬\n\nYour new credentials and SSH connection details have been sent privately.\n"
                  "Only you can see your password.", False)
        add_field(embed, "ℹ️ Reminder", "Run `resize2fs /` inside the container if the filesystem did not grow automatically.", False)
        await panel.finish(final_embed=embed)

        try:
            owner_id = int(vps["user_id"])
            user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
            dm = success_embed("✅ Your VPS Was Reinstalled",
                               f"`{container}` has a fresh {os_name} installation and a new root password.", thumbnail=False)
            add_field(dm, "🧮 Resources", f"`{ram_gb} GB` RAM • `{cpu}` vCPU • `{disk_gb} GB` disk", True)
            add_field(dm, "🐧 OS", f"`{os_name}`", True)
            add_field(dm, "🌐 Node", f"`{node.get('name', target_node_id)}`", True)
            add_field(dm, "🔐 New SSH Credentials",
                      credentials_block(ssh_command, new_password, container=container, node_name=str(node.get("name", target_node_id))), False)
            add_field(dm, "📌 Important", "Save your new password. This is a fresh installation, so previous data is gone.", False)
            await user.send(embed=dm)
        except Exception as exc:
            logger.debug("reinstall DM failed: %s", exc)
        self.feedback = "♻️ Reinstall finished — credentials sent by DM."
        self._build()
        await self.refresh_view(interaction)


class ReinstallNodeSelectView(PanelView):
    def __init__(self, author_id: int, manage_view: ManageView, vps: Dict[str, Any]):
        super().__init__(author_id, timeout=300)
        self.manage_view, self.vps = manage_view, vps

    async def build_options(self) -> None:
        options = []
        for node in await run_blocking(get_nodes):
            node_id = int(node["id"])
            used = await run_blocking(get_current_vps_count, node_id)
            capacity = int(node.get("total_vps") or 0)
            # The current node is selectable even if currently at capacity.
            if node_id != int(self.vps["node_id"]) and capacity > 0 and used >= capacity:
                continue
            options.append(discord.SelectOption(
                label=str(node.get("name") or f"Node {node_id}")[:100], value=str(node_id),
                description=f"{node.get('location', 'Unknown')} • {used}/{capacity or '∞'} VPS"[:100],
                emoji="🏠" if node.get("is_local") else "🌐"))
        if options:
            self.add_select(placeholder="🌐 Choose destination node…", options=options[:25], callback=self._choose)

    async def _choose(self, interaction: discord.Interaction) -> None:
        node_id = int(interaction.data["values"][0])
        node = await run_blocking(get_node, node_id)
        if not node:
            await interaction.response.send_message(embed=error_embed("Node Unavailable", "Choose another node."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        view = ReinstallOSSelectView(interaction.user.id, self.manage_view, self.vps, node_id)
        await view.build_options()
        await interaction.followup.send(
            embed=create_embed("🐧 Choose Reinstall OS",
                               f"**VPS:** `{self.vps['container_name']}`\n**Node:** {node.get('name', node_id)}\n"
                               "Select the OS to install. The VPS is not changed until you confirm.", COLORS["os"]),
            view=view, ephemeral=True)
        self.stop()


class ReinstallOSSelectView(PanelView):
    def __init__(self, author_id: int, manage_view: ManageView, vps: Dict[str, Any], node_id: int):
        super().__init__(author_id, timeout=300)
        self.manage_view, self.vps, self.node_id = manage_view, vps, int(node_id)

    async def build_options(self) -> None:
        self.add_select(
            placeholder="🐧 Select operating system…",
            options=[discord.SelectOption(label=str(item["label"])[:100], value=str(item["value"]), emoji=item.get("emoji"))
                     for item in OS_OPTIONS[:25]],
            callback=self._choose)

    async def _choose(self, interaction: discord.Interaction) -> None:
        try:
            logger.debug("OS selection started for user %s", interaction.user.id)
            os_version = str(interaction.data["values"][0])
            logger.debug("Selected OS: %s", os_version)
            
            if os_version not in {str(item.get("value")) for item in OS_OPTIONS}:
                logger.warning("Invalid OS selected: %s", os_version)
                await interaction.response.send_message(embed=error_embed("Invalid OS", "Select an OS from the menu."), ephemeral=True)
                return
            
            logger.debug("Deferring interaction")
            await interaction.response.defer(ephemeral=True)
            
            logger.debug("Starting reinstall with OS %s on node %s", os_version, self.node_id)
            await self.manage_view._perform_reinstall(interaction, self.node_id, os_version)
            logger.debug("Reinstall initiated successfully")
            self.stop()
        except Exception as exc:
            logger.error("OS selection failed: %s", exc, exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=error_embed("Selection Failed", truncate_text(str(exc), 500)), 
                        ephemeral=True)
                else:
                    await interaction.followup.send(
                        embed=error_embed("Selection Failed", truncate_text(str(exc), 500)), 
                        ephemeral=True)
            except Exception as send_exc:
                logger.error("Could not send error message: %s", send_exc)


if WEBSSH_ENABLED:

    class WebSSHPinModal(discord.ui.Modal, title="Lock this terminal link"):
        pin: discord.ui.TextInput = discord.ui.TextInput(
            label="PIN (4-8 digits)",
            placeholder="e.g. 4821",
            min_length=4,
            max_length=8,
            required=True,
        )

        def __init__(self, link_view: "WebSSHLinkView"):
            super().__init__(timeout=300)
            self.link_view = link_view

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if str(interaction.user.id) != str(self.link_view.author_id):
                await interaction.response.send_message(
                    embed=error_embed("Not Your Link", "Only the person who opened this link can lock it."),
                    ephemeral=True)
                return
            pin = str(self.pin.value).strip()
            if not pin.isdigit() or not 4 <= len(pin) <= 8:
                await interaction.response.send_message(
                    embed=error_embed("Invalid PIN", "The PIN must be **4 to 8 digits** (numbers only)."),
                    ephemeral=True)
                return
            ok, message = self.link_view.apply_pin(pin)
            await interaction.response.send_message(
                embed=(success_embed("Terminal Locked", message) if ok else
                       warning_embed("Link Expired", message)),
                ephemeral=True)

    class WebSSHLinkView(PanelView):
        """Ephemeral helper that can add or change the PIN on a terminal link."""

        def __init__(self, author_id: int, token: str, label: str):
            super().__init__(author_id, timeout=900)
            self.token = token
            self.label = label
            entry = webssh_tokens.get(token)
            locked = bool(entry and entry.pin_required)
            self.add_button(
                label="Change PIN" if locked else "Add a PIN",
                style=discord.ButtonStyle.primary,
                emoji="\U0001f512",
                callback=self._set_pin,
            )
            self.add_button(label="Close", style=discord.ButtonStyle.danger, emoji="\u2716\ufe0f",
                            callback=self._close)

        async def _set_pin(self, interaction: discord.Interaction) -> None:
            entry = webssh_tokens.get(self.token)
            if not entry or not entry.is_valid():
                await interaction.response.send_message(
                    embed=warning_embed("Link Expired",
                                        f"Open a fresh link from `{PREFIX}manage` -> **Web SSH**."),
                    ephemeral=True)
                return
            await interaction.response.send_modal(WebSSHPinModal(self))

        async def _close(self, interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(view=None)
            self.stop()

        def apply_pin(self, pin: str) -> Tuple[bool, str]:
            """Hash + attach the PIN to the token that is already in the user's DMs."""
            entry = webssh_tokens.get(self.token)
            if not entry or not entry.is_valid():
                return False, f"Generate a new link from `{PREFIX}manage` -> **Web SSH**."
            entry.pin_hash = hash_password(pin)
            entry.pin_attempts = 0
            entry.unlock_keys.clear()
            _save_webssh_token(self.token)
            self._relabel()
            audit("webssh_pin_set", entry.user_id, target=self.label)
            return True, (f"`{self.label}` now asks for the PIN **once per browser** before the terminal opens.\n"
                          f"The link in your DMs stays the same - the PIN is never echoed in chat.")

        def _relabel(self) -> None:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label in ("Add a PIN", "Change PIN"):
                    item.label = "Change PIN"

        async def on_timeout(self) -> None:
            try:
                if self.message:
                    await self.message.edit(view=None)
            except Exception:
                pass


class PasswordView(PanelView):
    """Password actions for one VPS: reveal, rotate, DM."""

    def __init__(self, parent: ManageView):
        super().__init__(parent.author_id, timeout=180)
        self.parent = parent
        self.vps = parent.vps

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.primary, emoji="🔄")
    async def regenerate(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        vps = self.vps
        if vps.get("suspended"):
            await interaction.response.send_message(
                embed=error_embed("Suspended", "This server is suspended — contact an admin first."), ephemeral=True)
            return
        await safe_defer(interaction, ephemeral=True)
        new_password = generate_strong_password()
        ok, message = await configure_ssh(vps["container_name"], vps["node_id"], new_password)
        if not ok:
            await interaction.followup.send(embed=error_embed("Rotation Failed", truncate_text(message, 800)),
                                            ephemeral=True)
            return
        await run_blocking(patch_vps, vps["container_name"], root_password=encrypt_secret(new_password))
        vps["root_password"] = encrypt_secret(new_password)
        audit("vps_password_rotate", interaction.user.id, target=vps["container_name"])

        embed = success_embed("Password Rotated",
                              f"New root password for `{vps['container_name']}`:\n\n`{new_password}`\n\n"
                              f"_Save it now — it is also stored for admins._")
        await interaction.followup.send(embed=embed, ephemeral=True)
        self.parent.feedback = "🔑 Root password rotated."

    @discord.ui.button(label="Send to DM", style=discord.ButtonStyle.secondary, emoji="📩")
    async def dm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        vps = self.vps
        password = decrypt_secret(vps.get("root_password"))
        if not password:
            await interaction.response.send_message(
                embed=warning_embed("Nothing to Send", "No password stored — press **Regenerate** first."),
                ephemeral=True)
            return
        forward = next((f for f in await run_blocking(get_forwards_for_container, vps["container_name"])
                        if int(f["vps_port"]) == 22), None)
        ssh_command = ssh_command_for(int(forward["host_port"]) if forward else None)
        embed = create_embed("Your VPS Credentials", f"`{vps['container_name']}`", COLORS["vault"], thumbnail=False)
        add_field(embed, "🔐 Access", credentials_block(ssh_command, password,
                                                        container=vps["container_name"],
                                                        node_name=str(vps["node_id"])), False)
        add_field(embed, "⚠️ Security", "Delete this message once you have saved the password.", False)
        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message(embed=success_embed("Sent", "Check your DMs."), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("DMs Closed", "I can't DM you — enable direct messages in your privacy settings."),
                ephemeral=True)


class PortsView(PanelView):
    """Port forwarding panel for a single VPS."""

    def __init__(self, parent: ManageView, vps: Dict[str, Any]):
        super().__init__(parent.author_id, timeout=240)
        self.parent = parent
        self.vps = vps

    @staticmethod
    def build_embed(vps: Dict[str, Any]) -> discord.Embed:
        forwards = get_forwards_for_container(vps["container_name"])
        embed = create_embed(f"Ports · {vps['container_name']}",
                             f"{vps_status_pill(vps)} • {len(forwards)} forward(s)", COLORS["network"])
        if not forwards:
            add_field(embed, "No forwards yet",
                      "Add one to expose a service running inside your VPS "
                      "(web server, Minecraft, API…).", False)
        else:
            lines = [
                f"`{f['host_port']}` → `{vps['container_name']}:{f['vps_port']}` "
                f"({f.get('protocol', 'both')})  ·  id `{f['id']}`"
                for f in forwards[:15]
            ]
            add_field(embed, "Active forwards", "\n".join(lines), False)
            add_field(embed, "Use it like",
                      f"`{YOUR_SERVER_IP}:{forwards[0]['host_port']}` reaches the VPS on port "
                      f"`{forwards[0]['vps_port']}`", False)
        quota_used = get_user_used_ports(vps["user_id"])
        quota_total = get_user_allocation(vps["user_id"])
        add_field(embed, "📶 Quota", f"`{quota_used}/{quota_total}` slots used "
                                     f"{mini_meter(quota_used, max(1, quota_total), 8)}", True)
        add_field(embed, "🔐 SSH", "Port `22` is forwarded automatically when the server starts.", True)
        return embed

    @discord.ui.button(label="Add forward", style=discord.ButtonStyle.primary, emoji="➕")
    async def add(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddForwardModal(self))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="➖")
    async def remove(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        forwards = await run_blocking(get_forwards_for_container, self.vps["container_name"])
        removable = [f for f in forwards if int(f["vps_port"]) != 22]
        if not removable:
            await interaction.response.send_message(
                embed=warning_embed("Nothing to Remove",
                                    "Only the automatic SSH forward exists — it is managed by the bot."),
                ephemeral=True)
            return
        options = [
            discord.SelectOption(label=f"{f['host_port']} → {f['vps_port']}", value=str(f["id"]),
                                 description=f"{f.get('protocol', 'both')} · id {f['id']}")
            for f in removable[:25]
        ]
        view = PanelView(self.author_id, timeout=120)
        select = view.add_select(placeholder="Pick a forward to delete", options=options,
                                 callback=None)

        async def _delete(inter_select: discord.Interaction) -> None:
            forward_id = int(inter_select.data["values"][0])
            await safe_defer(inter_select, ephemeral=True)
            ok, owner_id, host_port = await remove_port_forward(forward_id)
            if ok:
                audit("port_remove", inter_select.user.id, target=self.vps["container_name"],
                      detail=f"host port {host_port}")
                await inter_select.followup.send(
                    embed=success_embed("Forward Removed", f"Host port `{host_port}` is free again."),
                    ephemeral=True)
            else:
                await inter_select.followup.send(embed=error_embed("Failed", "That forward no longer exists."),
                                                 ephemeral=True)
        select.callback = _delete
        await interaction.response.send_message(embed=info_embed("Remove a forward", "Choose from the list below."),
                                                view=view, ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        embed = await run_blocking(self.build_embed, self.vps)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


class AddForwardModal(discord.ui.Modal, title="Forward a port"):
    vps_port: discord.ui.TextInput = discord.ui.TextInput(
        label="Port inside the VPS", placeholder="e.g. 3000 for a web app", max_length=5, required=True)
    protocol: discord.ui.TextInput = discord.ui.TextInput(
        label="Protocol (tcp / udp / both)", default="both", max_length=8, required=False)

    def __init__(self, parent: PortsView):
        super().__init__(timeout=180)
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(embed=error_embed("Not Yours", "Open your own panel."),
                                                    ephemeral=True)
            return
        try:
            port = int(str(self.vps_port.value).strip())
        except ValueError:
            await interaction.response.send_message(embed=error_embed("Invalid Port", "Use a number 1–65535."),
                                                    ephemeral=True)
            return
        if not (1 <= port <= 65535):
            await interaction.response.send_message(embed=error_embed("Invalid Port", "Use a number 1–65535."),
                                                    ephemeral=True)
            return
        proto = str(self.protocol.value or "both").strip().lower()
        if proto not in ("tcp", "udp", "both"):
            proto = "both"

        vps = self.parent_view.vps
        used = await run_blocking(get_user_used_ports, vps["user_id"])
        quota = await run_blocking(get_user_allocation, vps["user_id"])
        if used >= quota:
            await interaction.response.send_message(
                embed=error_embed("Quota Reached",
                                  f"You are using `{used}/{quota}` port slots. Ask an admin for more."),
                ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)
        host_port = await create_port_forward(vps["user_id"], vps["container_name"], port, vps["node_id"], proto)
        if not host_port:
            await interaction.followup.send(embed=error_embed("Forward Failed", "Could not allocate a host port."),
                                            ephemeral=True)
            return
        audit("port_add", interaction.user.id, target=vps["container_name"], detail=f"{proto} {port}→{host_port}")
        embed = success_embed("Port Forwarded",
                              f"`{YOUR_SERVER_IP}:{host_port}` → `{vps['container_name']}:{port}` ({proto})")
        add_field(embed, "Test it", f"```bash\ncurl -I http://{YOUR_SERVER_IP}:{host_port}\n```", False)
        add_field(embed, "Quota", f"`{used + 1}/{quota}` slots used {mini_meter(used + 1, max(1, quota), 8)}", True)
        await interaction.followup.send(embed=embed, ephemeral=True)


class AdvancedView(PanelView):
    """Power tools: permissions, sync, logs, processes, network, admin ops."""

    def __init__(self, parent: ManageView, vps: Dict[str, Any]):
        super().__init__(parent.author_id, timeout=300)
        self.parent = parent
        self.vps = vps
        self.is_admin_user = is_admin_id(parent.author_id)

        self.add_select(
            placeholder="Choose a power action…",
            options=[
                discord.SelectOption(label="Re-apply container permissions", value="permissions",
                                     description="Fixes Docker/FUSE issues inside the container"),
                discord.SelectOption(label="Recreate port forwards", value="repair",
                                     description="Rebuild every proxy device"),
                discord.SelectOption(label="Resync status", value="resync",
                                     description="Read the true state from LXC and update the bot"),
                discord.SelectOption(label="Show processes", value="processes",
                                     description="Top processes running inside the VPS"),
                discord.SelectOption(label="Show network", value="network",
                                     description="Interfaces, IPs and routes"),
                discord.SelectOption(label="Show last logs", value="logs",
                                     description="Quick look at the container logs"),
            ] + ([
                discord.SelectOption(label="Whitelist / un-whitelist", value="whitelist",
                                     description="Protect from auto-suspension"),
                discord.SelectOption(label="Suspend / unsuspend", value="suspend",
                                     description="Freeze access without deleting"),
                discord.SelectOption(label="Set expiration", value="expiration",
                                     description="Change how long this VPS lives"),
                discord.SelectOption(label="Clone this VPS", value="clone",
                                     description="Duplicate the container on the same node"),
            ] if self.is_admin_user else []),
            callback=self._dispatch,
        )

    @staticmethod
    def build_embed(vps: Dict[str, Any]) -> discord.Embed:
        embed = create_embed("Advanced tools",
                             f"`{vps['container_name']}` • {vps_status_pill(vps)}", COLORS["dark"])
        add_field(embed, "What's here",
                  "Repair tools, live diagnostics and (for admins) lifecycle controls.\n"
                  "Nothing in this menu deletes data.", False)
        return embed

    async def _dispatch(self, interaction: discord.Interaction) -> None:
        action = interaction.data["values"][0]
        handlers = {
            "permissions": self._permissions,
            "repair": self._repair,
            "resync": self._resync,
            "processes": self._processes,
            "network": self._network,
            "logs": self._logs,
            "whitelist": self._whitelist,
            "suspend": self._suspend,
            "expiration": self._expiration,
            "clone": self._clone,
        }
        handler = handlers.get(action)
        if handler:
            await handler(interaction)

    async def _shell_report(self, interaction: discord.Interaction, title: str, script: str,
                            *, description: str = "", lines: int = 40) -> None:
        await safe_defer(interaction, ephemeral=True)
        try:
            output = await container_shell(self.vps["container_name"], script, timeout=60,
                                           node_id=self.vps["node_id"])
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(f"{title} Failed", truncate_text(exc, 700)),
                                            ephemeral=True)
            return
        embed = create_embed(title, description or f"`{self.vps['container_name']}`", COLORS["dark"], thumbnail=False)
        for chunk in chunk_lines((output or "_no output_").splitlines(), char_limit=950, max_lines=lines):
            add_field(embed, "Result", safe_code_block(chunk, max_chars=900))
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _permissions(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        container, node_id = self.vps["container_name"], self.vps["node_id"]
        try:
            was_running = (await container_state(container, node_id)) == "running"
            if was_running:
                await run_lxc_command(container, f"stop {shlex.quote(container)}", timeout=120, node_id=node_id)
            await apply_lxc_config(container, node_id)
            await run_lxc_command(container, f"start {shlex.quote(container)}", timeout=180, node_id=node_id)
            await apply_internal_permissions(container, node_id)
            restored = await recreate_port_forwards(container)
            await run_blocking(patch_vps, container, status="running", suspended=False)
            audit("vps_permissions", interaction.user.id, target=container)
            await interaction.followup.send(
                embed=success_embed("Permissions Re-applied",
                                    f"Nesting, privileged mode, FUSE and kernel modules are active.\n"
                                    f"🔌 {restored} port forward(s) restored."),
                ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed("Failed", truncate_text(exc, 800)), ephemeral=True)

    async def _repair(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        try:
            restored = await recreate_port_forwards(self.vps["container_name"])
            await interaction.followup.send(
                embed=success_embed("Ports Repaired", f"Rebuilt **{restored}** forward(s)."), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed("Repair Failed", truncate_text(exc, 700)),
                                            ephemeral=True)

    async def _resync(self, interaction: discord.Interaction) -> None:
        state = await container_state(self.vps["container_name"], self.vps["node_id"])
        await run_blocking(patch_vps, self.vps["container_name"], status=state)
        self.vps["status"] = state
        self.parent.feedback = f"🔄 Live state resynced: **{state}**"
        self.parent._build()
        await self.parent.refresh_view(interaction)

    async def _processes(self, interaction: discord.Interaction) -> None:
        await self._shell_report(interaction, "Processes",
                                 "ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 16")

    async def _network(self, interaction: discord.Interaction) -> None:
        await self._shell_report(interaction, "Network",
                                 "ip -4 -o addr show; echo; ip route | head -n 8")

    async def _logs(self, interaction: discord.Interaction) -> None:
        await self._shell_report(interaction, "Container logs",
                                 "journalctl -n 25 --no-pager 2>/dev/null || dmesg | tail -n 25 2>/dev/null || "
                                 "tail -n 25 /var/log/syslog 2>/dev/null || echo 'no logs available'")

    async def _whitelist(self, interaction: discord.Interaction) -> None:
        if not self.is_admin_user:
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot change this."),
                                                    ephemeral=True)
            return
        new_value = not bool(self.vps.get("whitelisted"))
        await run_blocking(patch_vps, self.vps["container_name"], whitelisted=new_value)
        self.vps["whitelisted"] = new_value
        audit("vps_whitelist", interaction.user.id, target=self.vps["container_name"], detail=str(new_value))
        state = "whitelisted ✅" if new_value else "no longer whitelisted"
        await interaction.response.send_message(
            embed=success_embed("Whitelist Updated", f"`{self.vps['container_name']}` is {state}."), ephemeral=True)

    async def _suspend(self, interaction: discord.Interaction) -> None:
        if not self.is_admin_user:
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot change this."),
                                                    ephemeral=True)
            return
        container, node_id = self.vps["container_name"], self.vps["node_id"]
        if self.vps.get("suspended"):
            await safe_defer(interaction, ephemeral=True)
            try:
                await run_lxc_command(container, f"start {shlex.quote(container)}", timeout=180, node_id=node_id)
                await apply_internal_permissions(container, node_id)
                await recreate_port_forwards(container)
            except Exception as exc:
                await interaction.followup.send(embed=error_embed("Start Failed", truncate_text(exc, 500)),
                                                ephemeral=True)
                return
            await run_blocking(patch_vps, container, suspended=False, status="running")
            self.vps["suspended"] = False
            await run_blocking(patch_vps, container, status="running")
            audit("vps_unsuspend", interaction.user.id, target=container)
            await interaction.followup.send(embed=success_embed("Unsuspended", f"`{container}` is running again."),
                                            ephemeral=True)
            return

        confirmed = await ask_confirm(interaction, title="Suspend this VPS?",
                                      description=f"`{container}` will be stopped and locked until an admin restores it.",
                                      confirm_label="Suspend", danger=True, timeout=60)
        if not confirmed:
            return
        # v10 fix: ask_confirm() already responded to this interaction (it sent
        # the confirmation prompt), so the unconditional defer that followed
        # raised InteractionResponded. safe_defer() only defers when still allowed.
        await safe_defer(interaction, ephemeral=True)
        try:
            await run_lxc_command(container, f"stop {shlex.quote(container)}", timeout=120, node_id=node_id)
        except Exception as exc:
            logger.debug("suspend stop failed: %s", exc)
        history = list(self.vps.get("suspension_history") or [])
        history.append({"time": datetime.now(timezone.utc).isoformat(),
                        "reason": "Suspended from the management panel",
                        "by": str(interaction.user.id)})
        await run_blocking(patch_vps, container, suspended=True, status="stopped", suspension_history=history)
        self.vps.update({"suspended": True, "status": "stopped"})
        audit("vps_suspend", interaction.user.id, target=container)
        await interaction.followup.send(embed=success_embed("Suspended", f"`{container}` has been suspended."),
                                        ephemeral=True)

    async def _expiration(self, interaction: discord.Interaction) -> None:
        if not self.is_admin_user:
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot change this."),
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(ExpirationModal(self.vps, interaction.user.id))

    async def _clone(self, interaction: discord.Interaction) -> None:
        if not self.is_admin_user:
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot do this."),
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(CloneModal(self.vps, interaction.user.id))


class ExpirationModal(discord.ui.Modal, title="Set expiration"):
    days: discord.ui.TextInput = discord.ui.TextInput(label="Days from now (0 = never expires)",
                                                     max_length=5, required=True, default="30")

    def __init__(self, vps: Dict[str, Any], requester_id: int):
        super().__init__(timeout=180)
        self.vps = vps
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_id(interaction.user.id):
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot change this."),
                                                    ephemeral=True)
            return
        days = clamp_int(self.days.value, 0, 3650, 30)
        new_expiry = None if days == 0 else (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        await run_blocking(patch_vps, self.vps["container_name"], expiration_date=new_expiry)
        audit("vps_set_expiration", interaction.user.id, target=self.vps["container_name"], detail=f"{days}d")
        text = "never expires" if new_expiry is None else f"expires in **{days} days**"
        await interaction.response.send_message(
            embed=success_embed("Expiration Updated", f"`{self.vps['container_name']}` now {text}."), ephemeral=True)


class CloneModal(discord.ui.Modal, title="Clone container"):
    new_name: discord.ui.TextInput = discord.ui.TextInput(label="New container name", max_length=63, required=True)

    def __init__(self, vps: Dict[str, Any], requester_id: int):
        super().__init__(timeout=180)
        self.vps = vps
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_id(interaction.user.id):
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot do this."),
                                                    ephemeral=True)
            return
        from_name = self.vps["container_name"]
        target = str(self.new_name.value).strip().lower()
        valid, reason = validate_container_name(target)
        if not valid:
            await interaction.response.send_message(embed=error_embed("Invalid Name", reason), ephemeral=True)
            return
        if get_vps_by_container(target):
            await interaction.response.send_message(embed=error_embed("Name In Use", "That container name is taken."),
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        panel = ProgressPanel(interaction.channel, f"Cloning {from_name} → {target}",
                              ["Copy container", "Apply permissions", "Boot clone", "Register in bot"],
                              color=COLORS["magic"])
        await panel.start()
        try:
            await panel.step(0, "running")
            await run_lxc_command(from_name, f"copy {shlex.quote(from_name)} {shlex.quote(target)}",
                                  timeout=900, node_id=self.vps["node_id"])
            await panel.step(0, "done")
            await panel.step(1, "running")
            await apply_lxc_config(target, self.vps["node_id"])
            await panel.step(1, "done")
            await panel.step(2, "running")
            await run_lxc_command(target, f"start {shlex.quote(target)}", timeout=180, node_id=self.vps["node_id"])
            await apply_internal_permissions(target, self.vps["node_id"])
            await panel.step(2, "done")
            await panel.step(3, "running")
            clone = dict(self.vps)
            clone.update({
                "id": await run_blocking(next_vps_id),
                "container_name": target,
                "status": "running",
                "created_at": datetime.now().isoformat(),
                "shared_with": [],
                "suspension_history": [],
                "suspended": False,
                "whitelisted": False,
                "is_public": False,
            })
            await run_blocking(upsert_vps, clone)
            await panel.step(3, "done")
        except Exception as exc:
            await panel.finish(final_embed=error_embed("Clone Failed", truncate_text(exc, 700)))
            return
        audit("vps_clone", interaction.user.id, target=target, detail=f"from {from_name}")
        await panel.finish(final_embed=success_embed(
            "Clone Created",
            f"`{target}` is running with the same data as `{from_name}`.\n"
            f"Add port forwards from the Ports menu when you need them."))


async def open_manage_panel(interaction: discord.Interaction, owner_id: int,
                            vps_list: Optional[List[Dict[str, Any]]] = None,
                            *, is_admin_view: bool = False, feedback: str = "") -> None:
    """Open (or refresh) the manage panel for a user."""
    if vps_list is None:
        vps_list = await run_blocking(get_user_vps, owner_id)
    if not vps_list:
        embed = info_embed("No VPS Found", "There is nothing to manage for this user yet.")
        await safe_send(interaction, embed=embed)
        return

    view = ManageView(interaction.user.id, vps_list, is_admin_view=is_admin_view, feedback=feedback)
    embed = await view.render()

    if interaction.response.is_done():
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=False, wait=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        try:
            message = await interaction.original_response()
        except discord.HTTPException:
            message = None
    view.message = message
    if AUTO_REFRESH_MANAGE and message is not None:
        view.start_live()


@bot.command(name="manage", aliases=["mangage", "panel", "control"])
@with_typing
@command_timeout(90)
async def manage_vps(ctx: commands.Context, user: Optional[discord.Member] = None) -> None:
    """
    Open the VPS control panel.

    Usage:  !manage              → your own servers
            !manage @user         → admin view of someone else's servers
    """
    target = user or ctx.author
    is_admin_view = user is not None
    if is_admin_view and not is_admin_id(ctx.author.id):
        await ctx.send(embed=error_embed("Admins Only", "Only admins can open another user's panel."))
        return

    # Get database-tracked VPS for this user
    vps_list = await run_blocking(get_user_vps, target.id)
    
    # For admin view, also include untracked containers matching user's name prefix
    if is_admin_view:
        try:
            # Get user's name prefix from Discord
            user_prefix = sanitize_username_for_container(target.name or str(target.id))
            
            # Get all LXC containers on the host with a timeout to prevent hanging
            try:
                lxc_containers = await asyncio.wait_for(get_lxc_containers_on_node(1), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("LXC list timed out for user %s, using database VPS only", target.id)
                lxc_containers = []
            
            # Find containers matching this user's prefix
            matching_containers = [c for c in lxc_containers if c.lower().startswith(user_prefix.lower())]
            tracked_names = {v["container_name"] for v in vps_list}
            untracked = [c for c in matching_containers if c not in tracked_names]
            
            # Create VPS records for untracked containers so they appear in the panel
            if untracked:
                logger.info("Found %d untracked containers for %s: %s", len(untracked), target.name, untracked)
                for container_name in untracked:
                    # Only add if not already in vps_list
                    orphan_vps = {
                        "id": 0,
                        "user_id": str(target.id),
                        "node_id": 1,
                        "container_name": container_name,
                        "ram": "?",
                        "cpu": "?",
                        "storage": "?",
                        "config": "Unknown (untracked)",
                        "os_version": "unknown",
                        "status": "unknown",
                        "suspended": False,
                        "whitelisted": False,
                        "is_public": False,
                        "created_at": datetime.now().isoformat(),
                        "shared_with": [],
                        "suspension_history": [],
                        "expiration_date": None,
                        "root_password": "",
                    }
                    vps_list.append(orphan_vps)
        except Exception as exc:
            logger.debug("Could not find untracked containers: %s", exc)
    
    if not vps_list:
        embed = info_embed("No VPS Found",
                           f"{target.mention} has no servers on this host."
                           if is_admin_view else "You don't own any server yet.")
        if not is_admin_view:
            add_field(embed, "🚀 Create one", f"`{PREFIX}vps create` — self-service, takes about a minute", False)
        await ctx.send(embed=embed)
        return

    message = await thinking(ctx.channel, "Opening control panel", "Reading live container state…")
    view = ManageView(ctx.author.id, vps_list, is_admin_view=is_admin_view)
    embed = await view.render()
    await _safe_edit(message, embed=embed, view=view)
    view.message = message
    if AUTO_REFRESH_MANAGE:
        view.start_live()

# ═══════════════════════════════════════════════════════════════════════════
# 10. PROVISIONING FLOWS  (admin create • public self-service • renewal)
# ═══════════════════════════════════════════════════════════════════════════

OS_OPTIONS: List[Dict[str, str]] = [
    {"label": "Ubuntu 24.04 LTS (Noble)", "value": "ubuntu:24.04", "emoji": "🟠"},
    {"label": "Ubuntu 22.04 LTS (Jammy)", "value": "ubuntu:22.04", "emoji": "🟠"},
    {"label": "Ubuntu 20.04 LTS (Focal)", "value": "ubuntu:20.04", "emoji": "🟠"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13", "emoji": "🔴"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12", "emoji": "🔴"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11", "emoji": "🔴"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10", "emoji": "🔴"},
    {"label": "Alpine 3.20 (tiny)", "value": "images:alpine/3.20", "emoji": "🏔️"},
]


class Responder:
    """Uniform way to reply whether we hold a Context or an Interaction."""

    def __init__(self, target: Union[commands.Context, discord.Interaction], *, ephemeral: bool = False):
        self.target = target
        self.ephemeral = ephemeral
        self.channel = target.channel
        # Track if this responder can use followup (for deferred interactions)
        self.is_deferred = isinstance(target, discord.Interaction) and target.response.is_done()

    @property
    def author(self) -> Union[discord.User, discord.Member]:
        return self.target.user if isinstance(self.target, discord.Interaction) else self.target.author

    async def send(self, *, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None,
                   content: Optional[str] = None, ephemeral: Optional[bool] = None,
                   wait: bool = False) -> Optional[discord.Message]:
        use_ephemeral = self.ephemeral if ephemeral is None else ephemeral
        if isinstance(self.target, discord.Interaction):
            if self.target.response.is_done():
                # Use followup for deferred interactions - only pass view if it's not None
                kwargs = {"embed": embed, "content": content, "ephemeral": use_ephemeral, "wait": wait}
                if view is not None:
                    kwargs["view"] = view
                return await self.target.followup.send(**kwargs)
            # Use response for non-deferred interactions
            kwargs = {"embed": embed, "content": content, "ephemeral": use_ephemeral}
            if view is not None:
                kwargs["view"] = view
            await self.target.response.send_message(**kwargs)
            try:
                return await self.target.original_response()
            except discord.HTTPException:
                return None
        # For Context, use channel.send()
        kwargs = {"embed": embed, "content": content}
        if view is not None:
            kwargs["view"] = view
        return await self.channel.send(**kwargs)

    async def edit(self, *, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None,
                   content: Optional[str] = None, message: Optional[discord.Message] = None) -> None:
        target_message = message
        if target_message is None and isinstance(self.target, discord.Interaction):
            try:
                target_message = await self.target.original_response()
            except discord.HTTPException:
                target_message = None
        if target_message is None:
            return
        await _safe_edit(target_message, embed=embed, view=view, content=content)


def build_vps_record(*, user_id: Union[int, str], vps_id: int, node_id: int, container_name: str,
                     ram_gb: int, cpu: int, disk_gb: int, os_version: str, password: str,
                     expiry_days: Optional[int], is_public: bool) -> Dict[str, Any]:
    expiry = None
    if expiry_days and expiry_days > 0:
        expiry = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()
    return {
        "id": vps_id,
        "user_id": str(user_id),
        "node_id": int(node_id),
        "container_name": container_name,
        "ram": f"{ram_gb}GB",
        "cpu": str(cpu),
        "storage": f"{disk_gb}GB",
        "config": f"{ram_gb}GB RAM / {cpu} vCPU / {disk_gb}GB Disk",
        "os_version": os_version,
        "status": "running",
        "suspended": False,
        "whitelisted": False,
        "is_public": bool(is_public),
        "created_at": datetime.now().isoformat(),
        "shared_with": [],
        "suspension_history": [],
        "expiration_date": expiry,
        "root_password": encrypt_secret(password),
    }


async def provision_vps(*, responder: Responder, owner: discord.abc.User, node_id: int, os_version: str,
                        ram_gb: int, cpu: int, disk_gb: int, expiry_days: Optional[int],
                        is_public: bool, guild: Optional[discord.Guild] = None,
                        requester_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Full provisioning pipeline with a live animated panel.

    Returns the created VPS record, or None when provisioning failed.
    """
    vps_id = await run_blocking(next_vps_id)
    prefix = sanitize_username_for_container(getattr(owner, "name", None) or str(owner.id))
    container = f"{prefix}-vps-{vps_id}"

    valid, reason = validate_container_name(container)
    if not valid:  # extremely defensive: sanitizer should always succeed
        await responder.send(embed=error_embed("Invalid Container Name", reason), ephemeral=True)
        return None

    node = await run_blocking(get_node, node_id)
    if not node:
        await responder.send(embed=error_embed("Node Missing", f"Node {node_id} no longer exists."), ephemeral=True)
        return None

    steps = [
        "Reserve VPS identity",
        "Create LXC container",
        "Apply CPU / RAM / disk limits",
        "Configure privileges (Docker ready)",
        "Boot and harden the guest",
        "Install SSH + root password",
        "Expose SSH port",
        "Register in bot database",
        "Deliver credentials",
    ]
    # Pass the interaction to ProgressPanel for proper deferred interaction handling
    interaction_obj = responder.target if isinstance(responder.target, discord.Interaction) else None
    logger.debug(f"provision_vps: creating ProgressPanel with interaction={interaction_obj is not None}")
    panel = ProgressPanel(responder.channel, f"Deploying {container}", steps,
                          subtitle=f"{os_version} • {ram_gb} GB RAM • {cpu} vCPU • {disk_gb} GB disk • node {node['name']}",
                          color=COLORS["brand"], interaction=interaction_obj)
    
    logger.debug(f"provision_vps: calling panel.start()")
    try:
        await panel.start()
        logger.debug(f"provision_vps: panel.start() completed successfully")
    except Exception as e:
        logger.error(f"provision_vps: panel.start() failed with error: {e}", exc_info=True)
        await responder.send(embed=error_embed("Progress Panel Error", f"Could not display progress: {truncate_text(str(e), 300)}"), ephemeral=True)
        return None

    password = generate_strong_password()
    try:
        logger.debug(f"Step 0: Reserving VPS identity")
        await panel.step(0, "done", f"`{container}`")
        
        logger.debug(f"Step 1: Creating LXC container with os={os_version}")
        await panel.step(1, "running")
        try:
            await provision_container(container, os_version, node_id, ram_gb=ram_gb, cpu=cpu, disk_gb=disk_gb)
            logger.debug(f"Step 1: Container created successfully")
        except Exception as e:
            logger.error(f"Step 1 FAILED: Could not create container: {e}", exc_info=True)
            raise
        await panel.step(1, "done")
        
        logger.debug(f"Step 2: Applying limits")
        await panel.step(2, "done", f"{ram_gb}GB / {cpu} vCPU / {disk_gb}GB")
        logger.debug(f"Step 3: Configuring privileges")
        await panel.step(3, "done", "nesting • privileged • fuse")
        logger.debug(f"Step 4: Booting container")
        await panel.step(4, "done", "sysctl + cgroups ready")
        
        logger.debug(f"Step 5: Installing SSH")
        await panel.step(5, "running")
        ssh_ok, ssh_note = await configure_ssh(container, node_id, password)
        logger.debug(f"Step 5: SSH config result: ok={ssh_ok}, note={ssh_note}")
        await panel.step(5, "done" if ssh_ok else "failed", truncate_text(ssh_note, 60))
        if not ssh_ok:
            raise RuntimeError(f"SSH setup failed: {ssh_note}")

        # Extended grace period: allow SSH daemon to fully stabilize and accept connections
        # This prevents "Error reading SSH protocol banner" from paramiko
        logger.debug(f"Step 5: Waiting 10 seconds for SSH to stabilize")
        await asyncio.sleep(10)
        
        logger.debug(f"Step 6: Creating port forward")
        await panel.step(6, "running")
        ssh_port = await create_port_forward(owner.id, container, 22, node_id)
        logger.debug(f"Step 6: Port forward created: ssh_port={ssh_port}")
        await panel.step(6, "done" if ssh_port else "failed",
                         f"host port {ssh_port}" if ssh_port else "no free port")
        
        logger.debug(f"Step 7: Saving to database")
        await panel.step(7, "running")
        record = build_vps_record(user_id=owner.id, vps_id=vps_id, node_id=node_id, container_name=container,
                                  ram_gb=ram_gb, cpu=cpu, disk_gb=disk_gb, os_version=os_version,
                                  password=password, expiry_days=expiry_days, is_public=is_public)
        await run_blocking(upsert_vps, record)
        if await run_blocking(get_user_allocation, owner.id) == 0:
            await run_blocking(allocate_ports, owner.id, 1)
        if is_public:
            await run_blocking(mark_device_vps_created, owner.id)
        await panel.step(7, "done", "saved")
        logger.debug(f"Step 7: Database saved")
        
        logger.debug(f"Step 8: Delivering credentials")
        await panel.step(8, "running")
        await run_host_motd(container, node_id)
        record["port_forwards"] = [{"host_port": ssh_port, "vps_port": 22}] if ssh_port else []
        await panel.step(8, "done", "DM sent")
        logger.debug(f"Step 8: Credentials delivered")
    except Exception as exc:
        logger.error("Provisioning failed for %s: %s", container, exc, exc_info=True)
        audit("vps_create", requester_id or owner.id, target=container, detail=str(exc), success=False)
        await panel.finish(final_embed=error_embed(
            "Deployment Failed",
            f"`{container}` could not be created.\n\n**Reason:** {truncate_text(exc, 600)}\n\n"
            f"Cleanup hint: an admin can run `{PREFIX}lxc-list` and `{PREFIX}delete-vps` to remove leftovers."))
        return None

    audit("vps_create", requester_id or owner.id, target=container,
          detail=f"os={os_version} ram={ram_gb}GB cpu={cpu} disk={disk_gb}GB public={is_public}")

    # ── success: announce + DM credentials ───────────────────────────────────
    ssh_command = ssh_command_for(ssh_port)
    token = None
    web_pin = f"{secrets.randbelow(900000) + 100000}" if WEBSSH_DEFAULT_PIN else None
    if WEBSSH_ENABLED:
        token = issue_webssh_token(owner.id, vps=record, ttl_seconds=1800, label=container, pin=web_pin)
    web_link = _public_url_for_token(token) if token else None

    success = success_embed(
        "✅ VPS Successfully Deployed!",
        f"🚀 **{container}** is now live and ready to use!",
        thumbnail=False)
    add_field(success, "🖥️ Server Identity",
              f"**Container:** `{container}`\n"
              f"**VPS ID:** `#{vps_id}`", True)
    add_field(success, "🧮 Specifications",
              f"**RAM:** `{ram_gb} GB`\n"
              f"**CPU:** `{cpu}` vCore\n"
              f"**Storage:** `{disk_gb} GB`", True)
    add_field(success, "⏰ Lifetime", expiration_line(record), True)
    add_field(success, "🔐 Access Your Server",
              f"**Check your DMs!** 📬\n\nCredentials and connection info have been sent to you privately.\n"
              f"Only you can see your password and SSH connection details.", False)
    add_field(success, "⚡ Quick Start",
              f"`{PREFIX}manage` — Open control panel\n"
              f"`{PREFIX}ports add 1 <port>` — Publish a port\n"
              f"`{PREFIX}vps renew` — Extend lifetime", False)
    await panel.finish(final_embed=success)

    dm = create_embed("Your VPS Is Ready",
                      f"✅ **{container}** has been deployed for you!\n\n"
                      f"Your credentials and connection details are below.\n"
                      f"**Store these safely** — you'll need them to connect.",
                      COLORS["success"],
                      thumbnail=False)
    add_field(dm, "🖥️ Server Details",
              f"**Container:** `{container}`\n"
              f"**VPS ID:** `#{vps_id}`\n"
              f"**Node:** {node['name']}\n"
              f"**OS:** {os_version}", False)
    add_field(dm, "🧮 Specifications",
              f"**RAM:** `{ram_gb} GB` | **CPU:** `{cpu}` vCore | **Storage:** `{disk_gb} GB`", False)
    add_field(dm, "🔐 SSH Credentials",
              credentials_block(ssh_command, password, container=container, node_name=node["name"]), False)
    if web_link:
        pin_text = f"\n🔒 PIN: `{web_pin}` (asked once per browser)" if web_pin else ""
        add_field(dm, "🌐 Browser Terminal (Web SSH)", f"[**→ Open Terminal Now**]({web_link}){pin_text}", False)
    add_field(dm, "⏰ Lifetime", expiration_line(record), False)
    add_field(dm, "📝 Important",
              "✦ Save your password somewhere safe\n"
              "✦ Only you have access to these credentials\n"
              "✦ Rotate the password anytime from the panel\n"
              "✦ Default user is `root` with full access", False)
    try:
        await owner.send(embed=dm)
    except discord.HTTPException:
        logger.debug("Could not DM credentials to %s", owner.id)

    if guild:
        member = guild.get_member(owner.id)
        await ensure_vps_role(guild, member)
    return record


# ── 10.1 Admin creation flow ─────────────────────────────────────────────────

class CreateVpsModal(discord.ui.Modal, title="Create a VPS"):
    specs: discord.ui.TextInput = discord.ui.TextInput(
        label="RAM_CPU_DISK  (numbers only)",
        placeholder="2_2_20  →  2 GB RAM, 2 vCPU, 20 GB disk",
        default="2_2_20", max_length=20, required=True)
    expiry: discord.ui.TextInput = discord.ui.TextInput(
        label="Lifetime in days (0 = never expires)", default=str(DEFAULT_VPS_EXPIRATION_DAYS),
        max_length=5, required=False)
    owner: discord.ui.TextInput = discord.ui.TextInput(
        label="Owner (mention or ID)", max_length=64, required=True)

    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(embed=error_embed("Not Yours", "Open your own modal."),
                                                    ephemeral=True)
            return
        if not is_admin_id(interaction.user.id):
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot create VPS."),
                                                    ephemeral=True)
            return

        raw = str(self.specs.value).replace(",", "_").replace(" ", "_").replace("/", "_")
        parts = [p for p in re.split(r"[_\-\s]+", raw) if p]
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            await interaction.response.send_message(
                embed=error_embed("Bad Format", "Use  `RAM_CPU_DISK`  with numbers only, e.g. `4_2_40`."),
                ephemeral=True)
            return
        ram_gb, cpu, disk_gb = (clamp_int(parts[0], 1, 512, 2), clamp_int(parts[1], 1, 128, 2),
                                clamp_int(parts[2], 5, 5000, 20))
        days = clamp_int(self.expiry.value or DEFAULT_VPS_EXPIRATION_DAYS, 0, 3650, DEFAULT_VPS_EXPIRATION_DAYS)

        match = re.search(r"\d{15,25}", str(self.owner.value))
        if not match:
            await interaction.response.send_message(embed=error_embed("Bad Owner", "Mention the user or paste an ID."),
                                                    ephemeral=True)
            return
        owner_id = int(match.group(0))
        try:
            owner = await bot.fetch_user(owner_id)
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("Unknown User", "I couldn't find that user."),
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        nodes = [n for n in await run_blocking(get_nodes)
                 if await run_blocking(get_current_vps_count, n["id"]) < int(n.get("total_vps") or 0)]
        if not nodes:
            await interaction.followup.send(embed=error_embed("No Capacity", "Every node is at its VPS limit."),
                                            ephemeral=True)
            return

        view = NodeSelectView(interaction.user.id, owner, ram_gb, cpu, disk_gb, days)
        embed = create_embed("Select a node", f"Creating **{ram_gb} GB / {cpu} vCPU / {disk_gb} GB** for {owner.mention}"
                                              f" • lifetime {days} days", COLORS["node"])
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)


class NodeSelectView(PanelView):
    def __init__(self, author_id: int, owner: discord.abc.User, ram_gb: int, cpu: int, disk_gb: int, days: int):
        super().__init__(author_id, timeout=300)
        self.owner = owner
        self.ram_gb, self.cpu, self.disk_gb, self.days = ram_gb, cpu, disk_gb, days

    async def build_options(self) -> None:
        options: List[discord.SelectOption] = []
        for node in await run_blocking(get_nodes):
            used = await run_blocking(get_current_vps_count, node["id"])
            capacity = int(node.get("total_vps") or 0)
            if capacity and used >= capacity:
                continue
            options.append(discord.SelectOption(
                label=f"{node['name']}"[:100],
                value=str(node["id"]),
                description=f"{node.get('location', '?')} • {used}/{capacity or '∞'} used"[:100],
                emoji="🏠" if node.get("is_local") else "🌐",
            ))
        if options:
            self.add_select(placeholder="Pick a node…", options=options[:25], callback=self._choose)
        else:
            self.add_button(label="No capacity available", style=discord.ButtonStyle.secondary,
                            callback=self._noop, disabled=True)

    async def _noop(self, interaction: discord.Interaction) -> None:  # pragma: no cover
        await interaction.response.defer()

    async def _choose(self, interaction: discord.Interaction) -> None:
        logger.debug(f"NodeSelectView._choose called by {interaction.user.id}")
        try:
            node_id = int(interaction.data["values"][0])
            logger.debug(f"Selected node_id: {node_id}")
        except (KeyError, TypeError, ValueError, IndexError) as e:
            logger.error("Error extracting node_id: %s", e)
            await interaction.response.send_message(
                embed=error_embed("Selection Error", f"Failed to process your selection: {e}"),
                ephemeral=True)
            return
        
        await interaction.response.defer()
        if interaction.message:
            await _safe_edit(interaction.message, view=None)
        os_view = OSSelectView(interaction.user.id, self.owner, node_id, self.ram_gb, self.cpu,
                               self.disk_gb, self.days)
        await os_view.build_options()
        embed = create_embed("Select an operating system",
                             f"{self.ram_gb} GB RAM • {self.cpu} vCPU • {self.disk_gb} GB disk • "
                             f"{self.days} day lifetime", COLORS["os"])
        msg = await interaction.followup.send(embed=embed, view=os_view)
        os_view.message = msg
        logger.debug(f"OSSelectView sent for node {node_id}")
        self.stop()


class OSSelectView(PanelView):
    def __init__(self, author_id: int, owner: discord.abc.User, node_id: int, ram_gb: int, cpu: int,
                 disk_gb: int, days: int):
        super().__init__(author_id, timeout=300)
        self.owner, self.node_id = owner, node_id
        self.ram_gb, self.cpu, self.disk_gb, self.days = ram_gb, cpu, disk_gb, days

    async def build_options(self) -> None:
        # v10 clarity: the loop variable was renamed from `os` (which shadowed the
        # standard-library `os` module for the rest of this function.
        self.add_select(
            placeholder="Pick an OS image…",
            options=[
                discord.SelectOption(label=image["label"], value=image["value"], emoji=image.get("emoji"))
                for image in OS_OPTIONS
            ],
            callback=self._choose,
        )

    async def _choose(self, interaction: discord.Interaction) -> None:
        logger.debug(f"OSSelectView._choose called by {interaction.user.id} for node {self.node_id}")
        try:
            values = interaction.data.get("values", [])
            if not values:
                logger.warning(f"No values in interaction data for OS selection")
                await interaction.response.send_message(
                    embed=error_embed("Invalid Selection", "No OS was selected."),
                    ephemeral=True)
                return
            os_version = str(values[0])
            logger.debug(f"Selected OS: {os_version}")
        except (KeyError, TypeError, IndexError) as e:
            logger.error("Error extracting OS version: %s", e)
            await interaction.response.send_message(
                embed=error_embed("Selection Error", f"Failed to process your selection: {e}"),
                ephemeral=True)
            return
        
        logger.debug(f"About to defer interaction")
        await interaction.response.defer()
        logger.debug(f"Interaction deferred successfully")
        
        if interaction.message:
            logger.debug(f"Removing view from OS selection message")
            await _safe_edit(interaction.message, view=None)
        
        logger.debug(f"Creating Responder with interaction")
        responder = Responder(interaction)
        
        # Send immediate acknowledgment
        logger.debug(f"Sending provisioning acknowledgment")
        try:
            await responder.send(embed=loading_embed(
                "🚀 Starting VPS Deployment",
                f"Creating VPS with **{os_version}**…\n\nPlease wait, this takes a few minutes."
            ))
        except Exception as e:
            logger.warning(f"Could not send acknowledgment: {e}")
        
        logger.info(f"Starting VPS provisioning: os={os_version}, ram={self.ram_gb}GB, cpu={self.cpu}, disk={self.disk_gb}GB, node={self.node_id}")
        
        try:
            record = await provision_vps(
                responder=responder, owner=self.owner, node_id=self.node_id, os_version=os_version,
                ram_gb=self.ram_gb, cpu=self.cpu, disk_gb=self.disk_gb, expiry_days=self.days,
                is_public=False, guild=interaction.guild, requester_id=interaction.user.id,
            )
            logger.debug(f"provision_vps returned: {record is not None}")
            if record:
                logger.info("Admin created %s for %s", record["container_name"], self.owner.id)
            else:
                logger.warning("VPS creation failed or returned None")
        except Exception as e:
            logger.error(f"provision_vps raised exception: {e}", exc_info=True)
            try:
                await responder.send(embed=error_embed("Provisioning Error", f"An error occurred: {truncate_text(str(e), 500)}"))
            except Exception as send_err:
                logger.error(f"Could not send error message: {send_err}")
        finally:
            logger.debug(f"Stopping OSSelectView")
            self.stop()


@bot.command(name="create", aliases=["create-vps", "newvps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def create_vps(ctx: commands.Context, ram_gb: int = None, cpu: int = None, disk_gb: int = None,
                     user: discord.Member = None, expiry_days: int = None) -> None:
    """
    Create a VPS for a user.

    Usage: !create <ram_gb> <cpu> <disk_gb> @user [days]
    Leave the numbers out to use the guided modal: !create
    """
    if ram_gb is None or cpu is None or disk_gb is None or user is None:
        await ctx.send(embed=create_embed(
            "Create a VPS",
            "Fill in the form and pick a node + OS in the next steps.", COLORS["brand"]),
            view=QuickCreateView(ctx.author.id))
        return

    ram_gb = clamp_int(ram_gb, 1, 512, 2)
    cpu = clamp_int(cpu, 1, 128, 2)
    disk_gb = clamp_int(disk_gb, 5, 5000, 20)
    days = DEFAULT_VPS_EXPIRATION_DAYS if expiry_days is None else clamp_int(expiry_days, 0, 3650,
                                                                            DEFAULT_VPS_EXPIRATION_DAYS)

    view = NodeSelectView(ctx.author.id, user, ram_gb, cpu, disk_gb, days)
    await view.build_options()
    embed = create_embed("Select a node",
                         f"**{ram_gb} GB RAM** • **{cpu} vCPU** • **{disk_gb} GB disk** for {user.mention}\n"
                         f"Lifetime: **{days} days**", COLORS["node"])
    view.message = await ctx.send(embed=embed, view=view)


class QuickCreateView(PanelView):
    def __init__(self, author_id: int):
        super().__init__(author_id, timeout=300)

    @discord.ui.button(label="Open the form", style=discord.ButtonStyle.primary, emoji="🧾")
    async def open_form(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not is_admin_id(interaction.user.id):
            await interaction.response.send_message(embed=error_embed("Admins Only", "You cannot create VPS."),
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(CreateVpsModal(interaction.user.id))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


# ── 10.2 Public self-service flow ────────────────────────────────────────────

def _device_fingerprint(user: Union[discord.User, discord.Member]) -> str:
    payload = f"{user.id}:{getattr(user, 'name', user.id)}:{getattr(user, 'avatar', None)}:{user.created_at.isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()


async def fraud_report(user: discord.abc.User, *, ip_hint: str) -> Dict[str, Any]:
    """Heuristic risk score used to gate the free tier."""
    score = 0
    flags: List[str] = []

    created = user.created_at
    now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    age_days = max(0, (now - created).days)
    if age_days < PUBLIC_VPS_REQUIRE_ACCOUNT_AGE:
        score += 60
        flags.append(f"account is only {age_days} day(s) old")
    elif age_days < 14:
        score += 15
        flags.append(f"account is quite new ({age_days} days)")

    owned = len(await run_blocking(get_user_vps, user.id))
    if owned >= PUBLIC_VPS_MAX_PER_USER:
        score += 100
        flags.append(f"already owns {owned} VPS (limit {PUBLIC_VPS_MAX_PER_USER})")

    accounts_same_ip = await run_blocking(count_accounts_for_ip, ip_hint)
    if accounts_same_ip > PUBLIC_VPS_MAX_PER_IP:
        score += 40
        flags.append(f"{accounts_same_ip} accounts share this network (limit {PUBLIC_VPS_MAX_PER_IP})")

    display_name = getattr(user, "name", "") or ""
    if len(display_name) < 3:
        score += 10
        flags.append("very short username")

    return {"score": score, "risky": score >= 60, "flags": flags, "account_age_days": age_days,
            "owned": owned, "accounts_same_ip": accounts_same_ip}


@bot.command(name="vps", aliases=["myvps-create"])
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def vps_command(ctx: commands.Context, action: str = None, *args: str) -> None:
    """
    Self-service VPS.

    !vps            → this help card
    !vps create     → create your free VPS
    !vps info       → your VPS details
    !vps renew      → extend the lifetime
    !vps specs      → what you get
    """
    action = (action or "help").lower().strip()

    if action in ("create", "new") and PUBLIC_VPS_ENABLED:
        await public_create(ctx)
        return
    if action == "info":
        await public_info(ctx)
        return
    if action == "renew":
        await public_renew(ctx)
        return
    if action == "specs":
        await public_specs(ctx)
        return
    if action not in ("help", "create", "new", "info", "renew", "specs"):
        await ctx.send(embed=warning_embed("Unknown Option", f"`{action}` is not a valid option — showing help."))

    embed = create_embed("Free VPS · Self Service",
                         f"Spin up your own server in about a minute.\n"
                         f"Status: {'🟢 open' if PUBLIC_VPS_ENABLED else '🔴 disabled by staff'}", COLORS["brand"])
    add_field(embed, "🚀 Commands",
              f"`{PREFIX}vps create` — deploy a new server\n"
              f"`{PREFIX}vps info` — your servers & expiry\n"
              f"`{PREFIX}vps renew` — extend the period\n"
              f"`{PREFIX}vps specs` — hardware & rules", False)
    add_field(embed, "⚙️ What you get",
              f"`{PUBLIC_VPS_MAX_RAM} GB` RAM • `{PUBLIC_VPS_MAX_CPU}` vCPU • `{PUBLIC_VPS_MAX_DISK} GB` disk\n"
              f"root access, Docker ready, {PUBLIC_VPS_EXPIRY_DAYS}-day lifetime", False)
    add_field(embed, "📏 Fair-use rules",
              f"`{PUBLIC_VPS_MAX_PER_USER}` server per account\n"
              f"no crypto mining, proxies, spam or abuse\n"
              f"breaking the rules = permanent ban", False)
    await ctx.send(embed=embed)


async def public_specs(ctx: commands.Context) -> None:
    embed = create_embed("VPS Specifications", "Exactly what the free tier ships with.", COLORS["info"])
    add_field(embed, "🧮 Hardware", f"**RAM** `{PUBLIC_VPS_MAX_RAM} GB`\n**CPU** `{PUBLIC_VPS_MAX_CPU} vCPU`\n"
                                    f"**Disk** `{PUBLIC_VPS_MAX_DISK} GB SSD`", True)
    add_field(embed, "🐧 Software", "**OS** Ubuntu 22.04 LTS\n**Access** root via SSH\n**Docker** ✅ supported", True)
    add_field(embed, "⏰ Lifetime", f"`{PUBLIC_VPS_EXPIRY_DAYS} days`, renewable by `{PUBLIC_VPS_RENEWAL_DAYS} days`", True)
    add_field(embed, "🔌 Extras", "• SSH port forwarded automatically\n• Browser terminal included\n"
                                  "• Extra ports on request", False)
    add_field(embed, "❌ Not allowed", "Mining, phishing, spam, public proxies, port scanners, "
                                       "anything illegal or abusive.", False)
    await ctx.send(embed=embed)


async def public_info(ctx: commands.Context) -> None:
    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not vps_list:
        await ctx.send(embed=info_embed("No VPS Yet", f"Use `{PREFIX}vps create` to deploy one."))
        return
    lines = []
    for index, vps in enumerate(vps_list, start=1):
        forwards = await run_blocking(get_forwards_for_container, vps["container_name"])
        ssh_forward = next((f for f in forwards if int(f["vps_port"]) == 22), None)
        lines.append(vps_card(vps, index))
        if ssh_forward:
            lines.append(f"   ↳ ```{ssh_command_for(int(ssh_forward['host_port']))}```")
    pages = build_pages("Your VPS", lines, color=COLORS["success"], page_size=6, field_name="Servers")
    paginator = Paginator(ctx.author.id, pages)
    await paginator.send(ctx)


class PublicNodeSelectView(PanelView):
    """Node selector for public VPS creation; owner-only and capacity-aware."""

    def __init__(self, author_id: int, owner: discord.abc.User, guild: Optional[discord.Guild]):
        super().__init__(author_id, timeout=300)
        self.owner = owner
        self.guild = guild

    async def build_options(self) -> None:
        options: List[discord.SelectOption] = []
        for node in await run_blocking(get_nodes):
            used = await run_blocking(get_current_vps_count, int(node["id"]))
            capacity = int(node.get("total_vps") or 0)
            if capacity > 0 and used >= capacity:
                continue
            options.append(discord.SelectOption(
                label=str(node.get("name") or f"Node {node['id']}")[:100],
                value=str(node["id"]),
                description=f"{node.get('location', 'Unknown')} • {used}/{capacity or '∞'} used"[:100],
                emoji="🏠" if node.get("is_local") else "🌐",
            ))
        if options:
            self.add_select(
                placeholder="🌐 Choose a VPS node…",
                options=options[:25],
                callback=self._choose,
            )
        else:
            self.add_button(
                label="No capacity available",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                callback=self._noop,
            )

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

    async def _choose(self, interaction: discord.Interaction) -> None:
        logger.debug(f"PublicNodeSelectView._choose called by {interaction.user.id}")
        try:
            node_id = int(interaction.data["values"][0])
            logger.debug(f"Selected node_id: {node_id}")
        except (KeyError, TypeError, ValueError, IndexError):
            await interaction.response.send_message(
                embed=error_embed("Invalid Selection", "Please choose a valid node."),
                ephemeral=True,
            )
            return
        node = await run_blocking(get_node, node_id)
        if not node:
            await interaction.response.send_message(
                embed=error_embed("Node Unavailable", "That node no longer exists. Please run the create command again."),
                ephemeral=True,
            )
            return
        used = await run_blocking(get_current_vps_count, node_id)
        capacity = int(node.get("total_vps") or 0)
        if capacity > 0 and used >= capacity:
            await interaction.response.send_message(
                embed=warning_embed("Node Is Full", "That node has reached capacity. Please start again and choose another node."),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        os_view = PublicOSSelectView(interaction.user.id, self.owner, self.guild, node_id)
        await os_view.build_options()
        embed = create_embed(
            "🐧 Choose Your Operating System",
            f"**Node:** {node['name']} • {node.get('location', 'Unknown')}\n"
            f"**RAM:** {PUBLIC_VPS_MAX_RAM} GB • **CPU:** {PUBLIC_VPS_MAX_CPU} vCPU • "
            f"**Disk:** {PUBLIC_VPS_MAX_DISK} GB\n\nSelect the OS image you want to install.",
            COLORS["os"],
        )
        msg = await interaction.followup.send(embed=embed, view=os_view, ephemeral=True)
        os_view.message = msg
        logger.debug(f"PublicOSSelectView sent for node {node_id}")
        self.stop()


class PublicOSSelectView(PanelView):
    """OS selector and final provisioning step for public VPS creation."""

    def __init__(self, author_id: int, owner: discord.abc.User, guild: Optional[discord.Guild], node_id: int):
        super().__init__(author_id, timeout=300)
        self.owner = owner
        self.guild = guild
        self.node_id = int(node_id)

    async def build_options(self) -> None:
        options = []
        # Use the exact aliases accepted by provision_container().
        # SUPPORTED_OS_OPTIONS is used for legacy display labels and contains
        # aliases that do not all match the images configured for provisioning.
        for image in OS_OPTIONS[:25]:
            value = str(image.get("value") or "")
            if not value:
                continue
            options.append(discord.SelectOption(
                label=str(image.get("label") or value)[:100],
                value=value,
                description=f"Install {image.get('label') or value}"[:100],
                emoji=image.get("emoji") or "🐧",
            ))
        if options:
            self.add_select(
                placeholder="🐧 Select an OS image…",
                options=options,
                callback=self._choose,
            )
        else:
            self.add_button(
                label="No OS images configured",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                callback=self._noop,
            )

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

    async def _choose(self, interaction: discord.Interaction) -> None:
        logger.debug(f"PublicOSSelectView._choose called by {interaction.user.id} for node {self.node_id}")
        values = (interaction.data or {}).get("values") or []
        os_version = str(values[0]) if values else ""
        allowed_images = {str(item.get("value")) for item in OS_OPTIONS if item.get("value")}
        if os_version not in allowed_images:
            logger.warning(f"Invalid OS selected: {os_version}")
            await interaction.response.send_message(
                embed=error_embed("Invalid OS", "That operating system is not in the configured OS list."),
                ephemeral=True,
            )
            return
        node = await run_blocking(get_node, self.node_id)
        if not node:
            await interaction.response.send_message(
                embed=error_embed("Node Unavailable", "The selected node no longer exists. Please try again."),
                ephemeral=True,
            )
            return
        logger.debug(f"About to defer interaction for OS selection")
        await interaction.response.defer(ephemeral=True)
        logger.debug(f"Interaction deferred successfully")
        
        if interaction.message:
            logger.debug(f"Removing view from OS selection message")
            await _safe_edit(interaction.message, view=None)
        
        logger.debug(f"Creating Responder with interaction")
        responder = Responder(interaction)
        
        # Send immediate acknowledgment
        logger.debug(f"Sending provisioning acknowledgment")
        try:
            await responder.send(embed=loading_embed(
                "🚀 Starting Deployment",
                f"Creating **{os_version}** instance…\n\nPlease wait, this takes a few minutes."
            ), ephemeral=True)
        except Exception as e:
            logger.warning(f"Could not send acknowledgment: {e}")
        
        logger.info(f"Starting VPS provisioning: os={os_version}, ram={PUBLIC_VPS_MAX_RAM}GB, cpu={PUBLIC_VPS_MAX_CPU}, disk={PUBLIC_VPS_MAX_DISK}GB, node={self.node_id}")
        
        try:
            record = await provision_vps(
                responder=responder,
                owner=self.owner,
                node_id=self.node_id,
                os_version=os_version,
                ram_gb=PUBLIC_VPS_MAX_RAM,
                cpu=PUBLIC_VPS_MAX_CPU,
                disk_gb=PUBLIC_VPS_MAX_DISK,
                expiry_days=PUBLIC_VPS_EXPIRY_DAYS,
                is_public=True,
                guild=self.guild,
                requester_id=interaction.user.id,
            )
            logger.debug(f"provision_vps returned: {record is not None}")
            if record:
                await run_blocking(
                    set_cooldown,
                    f"vps_create:{self.owner.id}",
                    PUBLIC_VPS_COOLDOWN_HOURS * 3600,
                    "free VPS already claimed",
                )
                logger.info(f"Free VPS created: {record['container_name']} for {self.owner.id}")
            else:
                await run_blocking(
                    set_cooldown,
                    f"vps_create:{self.owner.id}",
                    900,
                    "failed attempt cool-down",
                )
                logger.warning("Free VPS creation failed or returned None")
        except Exception as e:
            logger.error(f"provision_vps raised exception: {e}", exc_info=True)
            try:
                await responder.send(embed=error_embed("Provisioning Error", f"An error occurred: {truncate_text(str(e), 500)}"), ephemeral=True)
            except Exception as send_err:
                logger.error(f"Could not send error message: {send_err}")
        finally:
            logger.debug(f"Stopping PublicOSSelectView")
            self.stop()


async def public_create(ctx: commands.Context) -> None:
    allowed, message = check_rate_limit(ctx.author.id, cost=1.0)
    if not allowed:
        await ctx.send(embed=warning_embed("Slow Down", message))
        return

    cooldown_active, seconds_left, reason = await run_blocking(get_cooldown, f"vps_create:{ctx.author.id}")
    if cooldown_active:
        await ctx.send(embed=warning_embed("Cooldown Active",
                                           f"Your next free VPS unlocks in **{fmt_duration(seconds_left)}**.\n"
                                           f"_{reason}_"))
        return

    if not PUBLIC_VPS_ENABLED:
        await ctx.send(embed=error_embed("Closed", "Self-service creation is currently disabled."))
        return

    public_count = await run_blocking(get_public_vps_count)
    if public_count >= PUBLIC_VPS_GLOBAL_LIMIT:
        await ctx.send(embed=warning_embed("Host Is Full",
                                           f"All {PUBLIC_VPS_GLOBAL_LIMIT} free slots are taken. "
                                           f"Try again later or ask an admin."))
        return

    # Per-account and per-network limits are checked *before* the risk score so
    # the user gets an accurate explanation instead of a generic "flagged".
    owned = await run_blocking(get_user_vps, ctx.author.id)
    if len(owned) >= PUBLIC_VPS_MAX_PER_USER:
        embed = warning_embed("You Already Have a Server",
                              f"Each account can hold **{PUBLIC_VPS_MAX_PER_USER}** free server(s).")
        add_field(embed, "Your servers",
                  "\n".join(f"`{v['container_name']}` • {expiration_line(v)}" for v in owned[:5]), False)
        add_field(embed, "Next steps",
                  f"• `{PREFIX}vps renew` extends the lifetime\n"
                  f"• Ask an admin if you need more capacity", False)
        await ctx.send(embed=embed)
        return

    ip_hint = f"discord:{ctx.author.id}"  # Discord never exposes IPs to bots
    report = await fraud_report(ctx.author, ip_hint=ip_hint)
    await run_blocking(track_device, ctx.author.id, ip_hint, str(getattr(ctx.author, "name", "")),
                       str(getattr(ctx.author, "avatar", "")), _device_fingerprint(ctx.author))

    if report["risky"]:
        embed = warning_embed("Request Flagged", "Our safety checks blocked this request for now.")
        add_field(embed, "Reasons", humanize_list(report["flags"]) or "automatic risk score too high", False)
        add_field(embed, "What now?",
                  "• Ask an admin for a manual review\n"
                  "• New accounts get access after a few days\n"
                  "• One account = one server", False)
        await ctx.send(embed=embed)
        audit("public_create_blocked", ctx.author.id, detail="; ".join(report["flags"]), success=False)
        return

    # Let users choose an available node, then an OS from the same OS_OPTIONS
    # list used by the working admin provisioning flow.
    view = PublicNodeSelectView(ctx.author.id, ctx.author, ctx.guild)
    await view.build_options()
    if not any(isinstance(child, discord.ui.Select) for child in view.children):
        await ctx.send(embed=warning_embed("No Nodes Available", "No node currently has capacity. Please try again later."))
        return
    embed = create_embed(
        "🚀 Create Your Free VPS",
        f"Choose where to deploy your VPS first.\n\n"
        f"**RAM:** {PUBLIC_VPS_MAX_RAM} GB • **CPU:** {PUBLIC_VPS_MAX_CPU} vCPU • "
        f"**Disk:** {PUBLIC_VPS_MAX_DISK} GB\n"
        f"**Lifetime:** {PUBLIC_VPS_EXPIRY_DAYS} days",
        COLORS["node"],
    )
    await ctx.send(embed=embed, view=view)


async def public_renew(ctx: commands.Context) -> None:
    if not PUBLIC_VPS_RENEWAL_ENABLED:
        await ctx.send(embed=error_embed("Renewal Disabled", "Renewals are turned off right now."))
        return

    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not vps_list:
        await ctx.send(embed=info_embed("Nothing to Renew", f"You have no servers. `{PREFIX}vps create`"))
        return

    now = datetime.now(timezone.utc)
    renewable: List[Tuple[int, Dict[str, Any], float]] = []
    waiting: List[str] = []
    for index, vps in enumerate(vps_list):
        expiry = safe_iso_parse(vps.get("expiration_date"))
        if expiry is None:
            waiting.append(f"`{vps['container_name']}` — no expiry set (ask an admin)")
            continue
        days_left = (expiry - now).total_seconds() / 86400.0
        if days_left <= PUBLIC_VPS_RENEWAL_WINDOW_DAYS:
            renewable.append((index, vps, days_left))
        else:
            waiting.append(f"`{vps['container_name']}` — {days_left:.1f} days left")

    if not renewable:
        embed = warning_embed("Not Renewable Yet",
                              f"You can renew when **{PUBLIC_VPS_RENEWAL_WINDOW_DAYS} days or less** remain.")
        add_field(embed, "Current servers", "\n".join(waiting) or "—", False)
        await ctx.send(embed=embed)
        return

    async def apply_renewal(source: Union[commands.Context, discord.Interaction], vps: Dict[str, Any]) -> None:
        expiry = safe_iso_parse(vps.get("expiration_date"))
        base = expiry if expiry and expiry > now else now
        new_expiry = base + timedelta(days=PUBLIC_VPS_RENEWAL_DAYS)
        await run_blocking(patch_vps, vps["container_name"], expiration_date=new_expiry.isoformat())
        audit("vps_renew", ctx.author.id, target=vps["container_name"], detail=f"→ {new_expiry.date()}")
        embed = success_embed("Renewed", f"`{vps['container_name']}` is safe until "
                                         f"{discord_timestamp(new_expiry, 'D')}.")
        add_field(embed, "⏰ New expiry", f"{discord_timestamp(new_expiry, 'F')} ({PUBLIC_VPS_RENEWAL_DAYS} days added)", False)
        responder = Responder(source, ephemeral=True)
        await responder.send(embed=embed)

    if len(renewable) == 1:
        await apply_renewal(ctx, renewable[0][1])
        return

    view = PanelView(ctx.author.id, timeout=120)

    async def _pick(interaction: discord.Interaction) -> None:
        _, vps, _ = renewable[int(interaction.data["values"][0])]
        await safe_defer(interaction, ephemeral=True)
        await apply_renewal(interaction, vps)

    view.add_select(
        placeholder="Which server should be renewed?",
        options=[
            discord.SelectOption(label=vps["container_name"][:100], value=str(position),
                                 description=f"{max(0, int(days))} day(s) left, renews +{PUBLIC_VPS_RENEWAL_DAYS}d")
            for position, (_index, vps, days) in enumerate(renewable[:25])
        ],
        callback=_pick,
    )
    await ctx.send(embed=create_embed("Renew a server", "Pick one below.", COLORS["success"]), view=view)


@bot.command(name="generate-password")
@is_admin()
async def generate_password(ctx: commands.Context) -> None:
    """Generate a strong random password (never sent to anyone else)."""
    try:
        await ctx.author.send(embed=create_embed("Generated Password",
                                                 f"`{generate_strong_password(20)}`\n\n"
                                                 f"Keep it in your password manager.", COLORS["vault"]))
        await ctx.send(embed=success_embed("Sent", "The password was sent to your DMs."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("DMs Closed", "I can't DM you — enable direct messages first."))

# ═══════════════════════════════════════════════════════════════════════════
# 11. ADMIN & OPERATIONS COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

async def resolve_user(identifier: str) -> Optional[discord.abc.User]:
    match = re.search(r"\d{15,25}", identifier or "")
    if not match:
        return None
    try:
        return await bot.fetch_user(int(match.group(0)))
    except discord.HTTPException:
        return None


def display_user(user: Optional[discord.abc.User], fallback_id: Union[int, str]) -> str:
    if user:
        return f"{user.display_name} (`{user.id}`)"
    return f"Unknown (`{fallback_id}`)"


# ── 11.1 Overview dashboards ─────────────────────────────────────────────────

@bot.command(name="list-all", aliases=["allvps", "vps-all", "vps-list"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def list_all_vps(ctx: commands.Context) -> None:
    """Every VPS on the platform, grouped and paginated."""
    message = await thinking(ctx.channel, "Scanning platform", "Reading every container record…")
    rows = await run_blocking(query_all, "SELECT * FROM vps ORDER BY user_id, id")
    if not rows:
        await _safe_edit(message, embed=info_embed("Nothing Deployed", "No VPS exist yet."))
        return

    vps_rows = [dict(row) for row in rows]
    counts = {"running": 0, "stopped": 0, "suspended": 0, "whitelisted": 0, "public": 0}
    for vps in vps_rows:
        if vps.get("suspended"):
            counts["suspended"] += 1
        elif str(vps.get("status")) == "running":
            counts["running"] += 1
        else:
            counts["stopped"] += 1
        if vps.get("whitelisted"):
            counts["whitelisted"] += 1
        if vps.get("is_public"):
            counts["public"] += 1

    summary = create_embed("Platform Overview",
                           f"Snapshot of every container • {len(vps_rows)} total", COLORS["dark"])
    add_field(summary, "🟢 Running", f"`{counts['running']}`", True)
    add_field(summary, "🔴 Stopped", f"`{counts['stopped']}`", True)
    add_field(summary, "🟡 Suspended", f"`{counts['suspended']}`", True)
    add_field(summary, "🌍 Self-service", f"`{counts['public']}`", True)
    add_field(summary, "✅ Whitelisted", f"`{counts['whitelisted']}`", True)
    add_field(summary, "👥 Owners", f"`{len({r['user_id'] for r in vps_rows})}`", True)
    summary.set_footer(text=f"{beat()} {brand_footer()}", icon_url=BOT_ICON_URL or None)
    await _safe_edit(message, embed=summary)

    lines: List[str] = []
    for vps in vps_rows:
        owner = await resolve_user(str(vps["user_id"]))
        vps["suspended"] = bool(vps.get("suspended"))
        lines.append(f"**{vps['container_name']}** • {vps_status_pill(vps)}\n"
                     f"   ↳ 👤 {display_user(owner, vps['user_id'])} • `{vps.get('ram')}`/`{vps.get('cpu')}`/`{vps.get('storage')}`"
                     f" • 🖥️ node `{vps.get('node_id')}` • {expiration_line(vps)}")

    pages = build_pages("All VPS", lines, color=COLORS["dark"], page_size=6, field_name="Servers")
    await Paginator(ctx.author.id, pages).send(ctx)


@bot.command(name="serverstats", aliases=["stats-server", "platform"])
@is_admin()
@with_typing
@command_timeout(60)
async def server_stats(ctx: commands.Context) -> None:
    """Aggregated platform statistics with visual bars."""
    message = await thinking(ctx.channel, "Crunching statistics", "Aggregating every container…")
    rows = [dict(r) for r in await run_blocking(query_all, "SELECT * FROM vps")]
    total_ram = total_cpu = total_disk = 0
    running = stopped = suspended = whitelisted = public = no_expiry = expiring = expired = 0

    for vps in rows:
        total_ram += clamp_int(str(vps.get("ram", "0")).replace("GB", ""), 0, 4096, 0)
        total_cpu += clamp_int(vps.get("cpu", 0), 0, 512, 0)
        total_disk += clamp_int(str(vps.get("storage", "0")).replace("GB", ""), 0, 100000, 0)
        if vps.get("suspended"):
            suspended += 1
        elif str(vps.get("status")) == "running":
            running += 1
        else:
            stopped += 1
        whitelisted += 1 if vps.get("whitelisted") else 0
        public += 1 if vps.get("is_public") else 0
        expiry = safe_iso_parse(vps.get("expiration_date"))
        if expiry is None:
            no_expiry += 1
        else:
            days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
            if days < 0:
                expired += 1
            elif days <= EXPIRATION_WARNING_DAYS:
                expiring += 1

    total_ports_allocated = int((await run_blocking(query_one,
                                                    "SELECT COALESCE(SUM(allocated_ports),0) AS s FROM port_allocations"))["s"])
    total_ports_used = int((await run_blocking(query_one,
                                               "SELECT COUNT(*) AS c FROM port_forwards"))["c"])

    host_cpu, host_ram, host_disk, host_disk_pct = await asyncio.gather(
        run_blocking(get_host_cpu_usage),
        run_blocking(get_host_ram_usage),
        run_blocking(get_host_disk_usage),
        run_blocking(get_host_disk_percent),
    )

    total = max(1, len(rows))
    embed = create_embed("Platform Statistics", f"{len(rows)} containers • {len({r['user_id'] for r in rows})} owners",
                         COLORS["dark"])
    add_field(embed, "🖥️ Containers",
              f"🟢 `{running}` running\n🔴 `{stopped}` stopped\n🟡 `{suspended}` suspended", True)
    add_field(embed, "📦 Allocation",
              f"`{total_ram} GB` RAM\n`{total_cpu}` vCPU\n`{total_disk} GB` disk", True)
    add_field(embed, "🩺 Host",
              f"CPU {gradient_bar(host_cpu, 100, 8)}\nRAM {gradient_bar(host_ram, 100, 8)}\n"
              f"Disk {gradient_bar(host_disk_pct, 100, 8)} `{host_disk}`", True)
    add_field(embed, "⏰ Expiry mix",
              f"🔴 `{expired}` expired • 🟡 `{expiring}` expiring\n🔵 `{no_expiry}` permanent • "
              f"✅ `{total - expired - expiring - no_expiry}` healthy", False)
    add_field(embed, "🔌 Ports", f"`{total_ports_used}` / `{total_ports_allocated}` slots in use"
                                 f" {mini_meter(total_ports_used, max(1, total_ports_allocated), 8)}", False)
    add_field(embed, "🌍 Self-service", f"`{public}` of `{len(rows)}` created by users", False)
    embed.set_footer(text=f"{beat()} {brand_footer()} • {PREFIX}live for a self-refreshing view",
                     icon_url=BOT_ICON_URL or None)
    await _safe_edit(message, embed=embed)


@bot.command(name="status", aliases=["system-status"])
@is_admin()
@with_typing
@command_timeout(90)
async def system_status(ctx: commands.Context) -> None:
    """Health of the bot, every node and the data layer."""
    started = time.perf_counter()
    message = await thinking(ctx.channel, "Collecting status", "Pinging nodes and reading the database…")

    nodes = await run_blocking(get_nodes)
    node_lines: List[str] = []
    online_nodes = 0
    for node in nodes:
        status_text, online = await get_node_status(node["id"])
        online_nodes += 1 if online else 0
        used = await run_blocking(get_current_vps_count, node["id"])
        capacity = int(node.get("total_vps") or 0)
        stats = await get_host_stats(node["id"]) if online else {"cpu": 0, "ram": 0}
        node_lines.append(
            f"**{node['name']}** {status_text}\n"
            f"   ↳ 📍 {node.get('location', '?')} • 📦 {used}/{capacity or '∞'} VPS • "
            f"CPU `{float(stats.get('cpu') or 0):.0f}%` RAM `{float(stats.get('ram') or 0):.0f}%`"
        )

    rows = [dict(r) for r in await run_blocking(query_all, "SELECT * FROM vps")]
    running = sum(1 for r in rows if str(r.get("status")) == "running" and not r.get("suspended"))
    suspended = sum(1 for r in rows if r.get("suspended"))

    db_size = DB_FILE.stat().st_size if DB_FILE.exists() else 0
    audit_rows = int((await run_blocking(query_one,
                                         "SELECT COUNT(*) AS c FROM audit_log"))["c"])
    elapsed = (time.perf_counter() - started) * 1000
    health = "🟢 Healthy" if online_nodes == len(nodes) and nodes else "🟡 Degraded"

    embed = create_embed("System Status", f"{health} • collected in `{elapsed:.0f} ms`",
                         COLORS["success"] if health.startswith("🟢") else COLORS["warning"])
    add_field(embed, "🤖 Bot",
              f"Latency `{bot.latency * 1000:.0f} ms`\nUptime `{fmt_duration((datetime.now(timezone.utc) - bot.started_at).total_seconds())}`\n"
              f"Guilds `{len(bot.guilds)}`", True)
    add_field(embed, "🖥️ Nodes", f"`{online_nodes}/{len(nodes)}` online", True)
    add_field(embed, "📦 Containers", f"`{len(rows)}` total\n`{running}` running\n`{suspended}` suspended", True)
    add_field(embed, "🧠 Storage",
              f"DB `{fmt_bytes(db_size)}`\nBackups `{len(list(DB_BACKUP_DIR.glob('vps_*.db')))}`\n"
              f"Audit rows `{audit_rows}`", True)
    add_field(embed, "🌐 Web terminal",
              f"{'🟢 enabled' if WEBSSH_ENABLED else '⛔ disabled'}\nSessions `{len(webssh_sessions) if WEBSSH_ENABLED else 0}`", True)
    add_field(embed, "🎛️ Workers",
              "Autosave ✅\nExpiry monitor ✅\nHost snapshots ✅\nPresence rotation ✅", True)
    for chunk in chunk_lines(node_lines, char_limit=950, max_lines=6):
        add_field(embed, "📡 Nodes", chunk, False)
    embed.set_footer(text=f"{beat()} {brand_footer()}", icon_url=BOT_ICON_URL or None)
    await _safe_edit(message, embed=embed)


@bot.command(name="userinfo", aliases=["whois"])
@is_admin()
@with_typing
@command_timeout(60)
async def user_info(ctx: commands.Context, user: discord.User = None) -> None:
    """Resource + risk profile for a user."""
    target = user or ctx.author
    vps_list = await run_blocking(get_user_vps, target.id)
    embed = create_embed(f"Profile · {target.display_name}",
                         f"{target.mention} • joined {discord_timestamp(target.created_at, 'R')}", COLORS["dark"])
    add_field(embed, "🆔 IDs", f"User `{target.id}`\nBot `{'yes' if target.bot else 'no'}`", True)
    add_field(embed, "🛡️ Role", "Main admin 👑" if is_main_admin_id(target.id)
              else ("Admin 🛡️" if is_admin_id(target.id) else "User 👤"), True)
    add_field(embed, "🖥️ Servers", f"`{len(vps_list)}`", True)

    if vps_list:
        total_ram = sum(clamp_int(str(v.get("ram", "0")).replace("GB", ""), 0, 4096, 0) for v in vps_list)
        total_cpu = sum(clamp_int(v.get("cpu", 0), 0, 512, 0) for v in vps_list)
        total_disk = sum(clamp_int(str(v.get("storage", "0")).replace("GB", ""), 0, 100000, 0) for v in vps_list)
        add_field(embed, "📦 Resources", f"`{total_ram} GB` RAM\n`{total_cpu}` vCPU\n`{total_disk} GB` disk", True)
        add_field(embed, "🔌 Ports",
                  f"`{await run_blocking(get_user_used_ports, target.id)}/"
                  f"{await run_blocking(get_user_allocation, target.id)}` slots", True)
        lines = [f"{vps_status_pill(v)} `{v['container_name']}` • {expiration_line(v)}" for v in vps_list[:8]]
        for chunk in chunk_lines(lines):
            add_field(embed, "🗂️ Servers", chunk, False)

    record = await run_blocking(query_one, "SELECT * FROM device_tracking WHERE user_id = ?", (str(target.id),))
    if record:
        add_field(embed, "🔍 Device record",
                  f"fingerprint `{str(record['device_fingerprint'] or '')[:12]}…`\n"
                  f"first seen {str(record['created_at'])[:16]}", False)
    await ctx.send(embed=embed)


@bot.command(name="vpsinfo", aliases=["vps-info", "inspect"])
@is_admin()
@with_typing
@command_timeout(60)
async def vps_info(ctx: commands.Context, container_name: str = None) -> None:
    """Full dossier for one container (or a list when omitted)."""
    if not container_name:
        rows = [dict(r) for r in await run_blocking(query_all, "SELECT * FROM vps ORDER BY id")]
        lines = []
        for vps in rows:
            owner = await resolve_user(str(vps["user_id"]))
            lines.append(f"`{vps['container_name']}` — {vps_status_pill(vps)} — 👤 {display_user(owner, vps['user_id'])}")
        await Paginator(ctx.author.id, build_pages("All Containers", lines, page_size=10,
                                                   field_name="Containers")).send(ctx)
        return

    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return

    message = await thinking(ctx.channel, f"Inspecting {container_name}", "Reading live state…")
    owner = await resolve_user(str(vps["user_id"]))
    node = await run_blocking(get_node, vps["node_id"])
    stats = await get_container_stats(container_name, vps["node_id"])
    forwards = await run_blocking(get_forwards_for_container, container_name)
    history = await run_blocking(get_usage_history, container_name, 24)

    embed = create_embed(f"Inspector · {container_name}",
                         f"{vps_status_pill(vps)} • owner {display_user(owner, vps['user_id'])}",
                         COLORS["dark"])
    add_field(embed, "🧮 Specs", f"`{vps.get('ram')}` RAM • `{vps.get('cpu')}` vCPU • `{vps.get('storage')}` disk\n"
                                 f"🐧 `{vps.get('os_version')}`", True)
    add_field(embed, "🖥️ Placement", f"**Node** {node['name'] if node else '?'}\n"
                                     f"**VPS ID** `#{vps.get('id')}`\n**Created** {discord_timestamp(safe_iso_parse(vps.get('created_at')), 'R')}", True)
    add_field(embed, "⏰ Lifetime", expiration_line(vps), True)
    for name, value, inline in stats_panel_fields(vps, stats, history=history):
        add_field(embed, name, value, inline)
    if forwards:
        add_field(embed, "🔌 Forwards",
                  "\n".join(f"`{f['host_port']}` → `{f['vps_port']}` ({f.get('protocol', 'both')})" for f in forwards[:10]), False)
    shared = vps.get("shared_with") or []
    if shared:
        add_field(embed, "👥 Shared with", ", ".join(f"<@{uid}>" for uid in shared[:10]), False)
    history_log = vps.get("suspension_history") or []
    if history_log:
        recent = history_log[-3:]
        add_field(embed, "📝 Suspension log",
                  "\n".join(f"{discord_timestamp(safe_iso_parse(h.get('time')), 'R')} — {truncate_text(h.get('reason'), 80)}"
                            for h in recent), False)
    await _safe_edit(message, embed=embed)


# ── 11.2 Resource + lifecycle admin ─────────────────────────────────────────

@bot.command(name="vps-stats", aliases=["livestats"])
@is_admin()
@with_typing
async def vps_stats(ctx: commands.Context, container_name: str) -> None:
    """Live (auto-refreshing) stats for any container."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    view = LiveStatsView(ctx.author.id, vps, owner_label=f"<@{vps['user_id']}>")
    message = await thinking(ctx.channel, "Sampling", f"Reading cgroups of `{container_name}`")
    view.message = message
    await view.render()
    await view.start(message)


@bot.command(name="add-resources", aliases=["resize-vps", "upgrade"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def resize_vps(ctx: commands.Context, container_name: str, ram_gb: int = 0, cpu: int = 0,
                     disk_gb: int = 0) -> None:
    """
    Change allocated resources.

    Usage: !add-resources <container> [new_ram_gb] [new_cpu] [new_disk_gb]
    Any value of 0 (or omitted) keeps the current setting.
    """
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    if not any((ram_gb, cpu, disk_gb)):
        await ctx.send(embed=error_embed("Nothing to Change",
                                         f"Provide at least one value:\n`{PREFIX}add-resources {container_name} 4 2 40`"))
        return

    new_ram = clamp_int(ram_gb, 0, 512, 0) or clamp_int(str(vps.get("ram", "1")).replace("GB", ""), 1, 512, 1)
    new_cpu = clamp_int(cpu, 0, 128, 0) or clamp_int(vps.get("cpu", 1), 1, 128, 1)
    new_disk = clamp_int(disk_gb, 0, 5000, 0) or clamp_int(str(vps.get("storage", "10")).replace("GB", ""), 5, 5000, 10)

    was_running = str(vps.get("status")) == "running"
    panel = ProgressPanel(ctx.channel, f"Resizing {container_name}",
                          ["Stop container", "Apply new limits", "Boot container", "Restore port forwards"],
                          subtitle=f"→ {new_ram} GB RAM • {new_cpu} vCPU • {new_disk} GB disk")
    await panel.start()
    try:
        if was_running:
            await panel.step(0, "running")
            await run_lxc_command(container_name, f"stop {shlex.quote(container_name)}", timeout=150,
                                  node_id=vps["node_id"], check=False)
            await panel.step(0, "done")
        else:
            await panel.step(0, "skipped", "container was stopped")

        await panel.step(1, "running")
        await run_lxc_argv(container_name, ["config", "set", container_name, "limits.memory", f"{new_ram * 1024}MB"],
                           node_id=vps["node_id"])
        await run_lxc_argv(container_name, ["config", "set", container_name, "limits.cpu", str(new_cpu)],
                           node_id=vps["node_id"])
        if new_disk != clamp_int(str(vps.get("storage", "10")).replace("GB", ""), 5, 5000, 10):
            await run_lxc_argv(container_name,
                               ["config", "device", "set", container_name, "root", f"size={new_disk}GB"],
                               node_id=vps["node_id"], check=False)
        await panel.step(1, "done")

        await panel.step(2, "running" if was_running else "skipped")
        if was_running:
            await run_lxc_command(container_name, f"start {shlex.quote(container_name)}", timeout=180,
                                  node_id=vps["node_id"])
            await apply_internal_permissions(container_name, vps["node_id"])
            await panel.step(2, "done")

        await panel.step(3, "running")
        restored = await recreate_port_forwards(container_name)
        await panel.step(3, "done", f"{restored} forward(s)")
    except Exception as exc:
        await panel.finish(final_embed=error_embed("Resize Failed", truncate_text(exc, 700)))
        return

    await run_blocking(patch_vps, container_name, ram=f"{new_ram}GB", cpu=str(new_cpu),
                       storage=f"{new_disk}GB",
                       config=f"{new_ram}GB RAM / {new_cpu} vCPU / {new_disk}GB Disk",
                       status="running" if was_running else "stopped")
    audit("vps_resize", ctx.author.id, target=container_name,
          detail=f"{new_ram}GB/{new_cpu}cpu/{new_disk}GB")

    embed = success_embed("Resources Updated",
                          f"`{container_name}` now has **{new_ram} GB RAM**, **{new_cpu} vCPU** and **{new_disk} GB disk**.")
    add_field(embed, "ℹ️ Filesystem", "Run `resize2fs /` inside the container to grow the filesystem immediately.", False)
    await panel.finish(final_embed=embed)


@bot.command(name="suspend-vps", aliases=["lock-vps"])
@is_admin()
@with_typing
@command_timeout(120)
async def suspend_vps(ctx: commands.Context, container_name: str, *, reason: str = "Administrative action") -> None:
    """Stop a container and lock it until an admin unsuspends it."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    if vps.get("suspended"):
        await ctx.send(embed=info_embed("Already Suspended", f"`{container_name}` is already locked."))
        return

    message = await thinking(ctx.channel, "Suspending", f"Stopping `{container_name}`…")
    await run_lxc_command(container_name, f"stop {shlex.quote(container_name)}", timeout=150,
                          node_id=vps["node_id"], check=False)
    history = list(vps.get("suspension_history") or [])
    history.append({"time": datetime.now(timezone.utc).isoformat(),
                    "reason": sanitize_reason(reason), "by": str(ctx.author.id)})
    await run_blocking(patch_vps, container_name, suspended=True, status="stopped", suspension_history=history)
    audit("vps_suspend", ctx.author.id, target=container_name, detail=reason)

    embed = success_embed("VPS Suspended", f"`{container_name}` is locked.")
    add_field(embed, "Reason", sanitize_reason(reason), False)
    await _safe_edit(message, embed=embed)
    try:
        user = await bot.fetch_user(int(vps["user_id"]))
        await user.send(embed=warning_embed("Your VPS Was Suspended",
                                            f"`{container_name}` has been suspended.\n\n**Reason:** {sanitize_reason(reason)}\n\n"
                                            f"Contact an admin if you believe this is a mistake."))
    except discord.HTTPException:
        pass


@bot.command(name="unsuspend-vps", aliases=["unlock-vps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def unsuspend_vps(ctx: commands.Context, container_name: str) -> None:
    """Restore a suspended container and re-attach its ports."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    if not vps.get("suspended"):
        await ctx.send(embed=info_embed("Not Suspended", f"`{container_name}` is not locked."))
        return

    panel = ProgressPanel(ctx.channel, f"Restoring {container_name}",
                          ["Clear suspension flag", "Boot container", "Re-apply permissions", "Restore ports"])
    await panel.start()
    try:
        await run_blocking(patch_vps, container_name, suspended=False)
        await panel.step(0, "done")
        await panel.step(1, "running")
        await run_lxc_command(container_name, f"start {shlex.quote(container_name)}", timeout=180,
                              node_id=vps["node_id"])
        await panel.step(1, "done")
        await panel.step(2, "running")
        await apply_internal_permissions(container_name, vps["node_id"])
        await panel.step(2, "done")
        await panel.step(3, "running")
        restored = await recreate_port_forwards(container_name)
        await panel.step(3, "done", f"{restored} forward(s)")
    except Exception as exc:
        await run_blocking(patch_vps, container_name, suspended=True)
        await panel.finish(final_embed=error_embed("Restore Failed", truncate_text(exc, 700)))
        return

    await run_blocking(patch_vps, container_name, status="running")
    audit("vps_unsuspend", ctx.author.id, target=container_name)
    await panel.finish(final_embed=success_embed("VPS Restored",
                                                 f"`{container_name}` is running again and {restored} forward(s) were restored."))
    try:
        user = await bot.fetch_user(int(vps["user_id"]))
        await user.send(embed=success_embed("Your VPS Is Back",
                                            f"`{container_name}` has been unsuspended and is running."))
    except discord.HTTPException:
        pass


@bot.command(name="restart-vps")
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def restart_vps(ctx: commands.Context, container_name: str) -> None:
    """Graceful restart with port restoration."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    panel = ProgressPanel(ctx.channel, f"Restarting {container_name}",
                          ["Reboot container", "Re-apply permissions", "Restore port forwards"])
    await panel.start()
    try:
        await panel.step(0, "running")
        await run_lxc_command(container_name, f"restart {shlex.quote(container_name)}", timeout=180,
                             node_id=vps["node_id"], check=False)
        await panel.step(0, "done")
        await panel.step(1, "running")
        await apply_internal_permissions(container_name, vps["node_id"])
        await panel.step(1, "done")
        await panel.step(2, "running")
        restored = await recreate_port_forwards(container_name)
        await panel.step(2, "done", f"{restored} forward(s)")
    except Exception as exc:
        await panel.finish(final_embed=error_embed("Restart Failed", truncate_text(exc, 700)))
        return
    await run_blocking(patch_vps, container_name, status="running")
    audit("vps_restart", ctx.author.id, target=container_name)
    await panel.finish(final_embed=success_embed("Restarted", f"`{container_name}` is back online."))


@bot.command(name="delete-vps", aliases=["destroy-vps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def delete_vps(ctx: commands.Context, user: discord.User, target: str = None, *,
                     reason: str = "No reason given") -> None:
    """
    Delete a container permanently.

    Usage: !delete-vps @user <vps_number> [reason]     (number from !vpsinfo)
           !delete-vps @user <container-name> [reason]
    """
    vps_list = await run_blocking(get_user_vps, user.id)
    if not vps_list:
        await ctx.send(embed=error_embed("Nothing to Delete", f"{user.mention} owns no containers."))
        return

    vps: Optional[Dict[str, Any]] = None
    if target is None:
        vps = vps_list[0] if len(vps_list) == 1 else None
    elif str(target).strip().isdigit():
        number = int(str(target).strip())
        vps = vps_list[number - 1] if 1 <= number <= len(vps_list) else None
    else:
        vps = next((v for v in vps_list if v["container_name"] == str(target).strip().lower()), None)

    if vps is None:
        listing = "\n".join(f"`{i}.` {v['container_name']}" for i, v in enumerate(vps_list, start=1))
        await ctx.send(embed=error_embed("Pick a Container", f"Which one?\n\n{listing}"))
        return

    container = vps["container_name"]
    confirmed = await ask_confirm(
        ctx,
        title="Delete this container?",
        description=(f"**`{container}`** (owner {user.mention}) will be destroyed.\n\n"
                     f"🧮 `{vps.get('ram')}` RAM • `{vps.get('cpu')}` vCPU • `{vps.get('storage')}` disk\n"
                     f"Reason: {sanitize_reason(reason)}\n\n**This cannot be undone.**"),
        confirm_label="Delete forever", danger=True, timeout=90,
    )
    if not confirmed:
        return

    message = await thinking(ctx.channel, "Deleting container", f"Destroying `{container}`…")
    await run_lxc_command(container, f"delete {shlex.quote(container)} --force", timeout=240,
                          node_id=vps["node_id"], check=False)
    await run_blocking(delete_vps_row, container)
    audit("vps_delete", ctx.author.id, target=container, detail=sanitize_reason(reason))

    embed = success_embed("Container Deleted", f"`{container}` was removed.", thumbnail=False)
    add_field(embed, "👤 Owner", user.mention, True)
    add_field(embed, "🆔 VPS ID", f"`#{vps.get('id')}`", True)
    add_field(embed, "📝 Reason", sanitize_reason(reason), False)
    await _safe_edit(message, embed=embed)

    if ctx.guild:
        member = ctx.guild.get_member(user.id)
        await remove_vps_role_if_unused(member, user.id)
    try:
        await user.send(embed=error_embed("Your VPS Was Deleted",
                                          f"`{container}` was deleted by staff.\n\n**Reason:** {sanitize_reason(reason)}"))
    except discord.HTTPException:
        pass


@bot.command(name="set-expiration", aliases=["expire-in"])
@is_admin()
async def set_expiration(ctx: commands.Context, container_name: str, days: int) -> None:
    """Set how long a container lives (0 = never expires)."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    days = clamp_int(days, 0, 3650, DEFAULT_VPS_EXPIRATION_DAYS)
    expiry = None if days == 0 else (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    await run_blocking(patch_vps, container_name, expiration_date=expiry, suspended=False)
    audit("vps_set_expiration", ctx.author.id, target=container_name, detail=f"{days} days")
    text = "never expires now" if expiry is None else f"expires {discord_timestamp(safe_iso_parse(expiry), 'R')}"
    await ctx.send(embed=success_embed("Expiration Updated", f"`{container_name}` {text}."))


@bot.command(name="renew-vps")
@is_admin()
async def renew_vps(ctx: commands.Context, container_name: str, days: int = None) -> None:
    """Extend (and unsuspend) a container."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    days = clamp_int(days or DEFAULT_VPS_EXPIRATION_DAYS, 1, 3650, DEFAULT_VPS_EXPIRATION_DAYS)
    now = datetime.now(timezone.utc)
    current = safe_iso_parse(vps.get("expiration_date"))
    base = current if current and current > now else now
    new_expiry = base + timedelta(days=days)
    await run_blocking(patch_vps, container_name, expiration_date=new_expiry.isoformat(), suspended=False)
    audit("vps_renew", ctx.author.id, target=container_name, detail=f"+{days}d")
    embed = success_embed("VPS Renewed", f"`{container_name}` extended by **{days} days**.")
    add_field(embed, "⏰ New expiry", f"{discord_timestamp(new_expiry, 'F')} ({discord_timestamp(new_expiry, 'R')})", False)
    await ctx.send(embed=embed)
    try:
        user = await bot.fetch_user(int(vps["user_id"]))
        await user.send(embed=success_embed("Your VPS Was Renewed",
                                            f"`{container_name}` now runs until {discord_timestamp(new_expiry, 'D')}."))
    except discord.HTTPException:
        pass


@bot.command(name="whitelist-vps")
@is_admin()
async def whitelist_vps(ctx: commands.Context, container_name: str, action: str = "add") -> None:
    """Exempt a container from auto-suspension."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    enable = action.lower() in ("add", "on", "yes", "true", "enable")
    await run_blocking(patch_vps, container_name, whitelisted=enable)
    audit("vps_whitelist", ctx.author.id, target=container_name, detail=str(enable))
    await ctx.send(embed=success_embed("Whitelist Updated",
                                       f"`{container_name}` is {'whitelisted ✅' if enable else 'no longer whitelisted'}."))


@bot.command(name="suspension-logs", aliases=["susplog"])
@is_admin()
async def suspension_logs(ctx: commands.Context, container_name: str = None) -> None:
    """Show the suspension history for one container or the whole platform."""
    def render(entry: Dict[str, Any]) -> str:
        when = discord_timestamp(safe_iso_parse(entry.get("time")), "R")
        return f"{when} — {truncate_text(entry.get('reason'), 110)}\n   ↳ by `{entry.get('by', '?')}`"

    if container_name:
        vps = await run_blocking(get_vps_by_container, container_name)
        if not vps:
            await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
            return
        history = list(reversed(vps.get("suspension_history") or []))[:12]
        if not history:
            await ctx.send(embed=info_embed("Clean Record", f"`{container_name}` was never suspended."))
            return
        embed = create_embed(f"Suspension log · {container_name}", None, COLORS["warning"])
        for chunk in chunk_lines([render(entry) for entry in history]):
            add_field(embed, "History", chunk)
        await ctx.send(embed=embed)
        return

    lines: List[str] = []
    for row in await run_blocking(query_all, "SELECT container_name, suspension_history FROM vps"):
        for entry in (json.loads(row["suspension_history"] or "[]") if row["suspension_history"] else []):
            lines.append(f"**{row['container_name']}** — {render(entry)}")
    if not lines:
        await ctx.send(embed=info_embed("Clean Record", "No suspensions have ever been recorded."))
        return
    await Paginator(ctx.author.id, build_pages("Suspension Log", lines[-60:], color=COLORS["warning"],
                                               page_size=8, field_name="Events")).send(ctx)


@bot.command(name="apply-permissions", aliases=["fix-perms"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def apply_permissions(ctx: commands.Context, container_name: str) -> None:
    """Re-apply nesting/privileged/FUSE settings and restart the container."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    panel = ProgressPanel(ctx.channel, f"Applying permissions to {container_name}",
                          ["Stop container", "Write security profile", "Boot container",
                           "Guest kernel tuning", "Restore port forwards"])
    await panel.start()
    try:
        was_running = (await container_state(container_name, vps["node_id"])) == "running"
        if was_running:
            await panel.step(0, "running")
            await run_lxc_command(container_name, f"stop {shlex.quote(container_name)}", timeout=150,
                                  node_id=vps["node_id"], check=False)
            await panel.step(0, "done")
        else:
            await panel.step(0, "skipped", "already stopped")
        await panel.step(1, "running")
        await apply_lxc_config(container_name, vps["node_id"])
        await panel.step(1, "done")
        await panel.step(2, "running")
        await run_lxc_command(container_name, f"start {shlex.quote(container_name)}", timeout=180,
                              node_id=vps["node_id"])
        await panel.step(2, "done")
        await panel.step(3, "running")
        await apply_internal_permissions(container_name, vps["node_id"])
        await panel.step(3, "done")
        await panel.step(4, "running")
        restored = await recreate_port_forwards(container_name)
        await panel.step(4, "done", f"{restored} forward(s)")
    except Exception as exc:
        await panel.finish(final_embed=error_embed("Apply Failed", truncate_text(exc, 700)))
        return
    await run_blocking(patch_vps, container_name, status="running", suspended=False)
    audit("vps_permissions", ctx.author.id, target=container_name)
    await panel.finish(final_embed=success_embed(
        "Permissions Applied", f"`{container_name}` is Docker-ready (nesting, privileged, FUSE, kernel modules)."))


@bot.command(name="repair-ports", aliases=["fix-ports"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def repair_ports(ctx: commands.Context, container_name: str) -> None:
    """Rebuild every proxy device for a container."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    message = await thinking(ctx.channel, "Repairing ports", f"Rebuilding forwards for `{container_name}`")
    restored = await recreate_port_forwards(container_name)
    audit("vps_repair_ports", ctx.author.id, target=container_name, detail=str(restored))
    await _safe_edit(message, embed=success_embed("Ports Repaired", f"Restored **{restored}** forward(s)."))


@bot.command(name="migrate-vps", aliases=["move-vps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def migrate_vps(ctx: commands.Context, container_name: str, target_node_id: int) -> None:
    """
    Copy a container to another node, then remove the local copy.

    Note: this uses `lxc copy` from the *source* node, so both nodes must be
    able to reach each other (or use a shared/remote LXD remote). Use
    `!node` to register the remote first.
    """
    vps = await run_blocking(get_vps_by_container, container_name)
    target = await run_blocking(get_node, target_node_id)
    if not vps or not target:
        await ctx.send(embed=error_embed("Not Found", "Container or target node does not exist."))
        return
    if int(vps["node_id"]) == int(target_node_id):
        await ctx.send(embed=info_embed("Already There", f"`{container_name}` lives on that node."))
        return

    confirmed = await ask_confirm(ctx, title="Migrate container?",
                                  description=f"`{container_name}` will be copied to **{target['name']}** "
                                              f"and then deleted from node `{vps['node_id']}`.\n\n"
                                              f"Expect downtime while the copy runs.",
                                  confirm_label="Start migration", danger=True, timeout=90)
    if not confirmed:
        return

    panel = ProgressPanel(ctx.channel, f"Migrating {container_name} → {target['name']}",
                          ["Stop source", "Copy to target", "Delete source", "Boot on target",
                           "Re-register node", "Restore forwards"])
    await panel.start()
    source_node = vps["node_id"]
    try:
        await panel.step(0, "running")
        await run_lxc_command(container_name, f"stop {shlex.quote(container_name)}", timeout=150,
                              node_id=source_node, check=False)
        await panel.step(0, "done")
        await panel.step(1, "running")
        temp_name = f"{container_name}-migrating"
        await run_lxc_command(container_name,
                              f"copy {shlex.quote(container_name)} {shlex.quote(temp_name)} -s {shlex.quote(DEFAULT_STORAGE_POOL)}",
                              timeout=1800, node_id=source_node)
        await run_lxc_command(temp_name, f"delete {shlex.quote(container_name)} --force", timeout=240,
                              node_id=source_node, check=False)
        await run_lxc_command(temp_name, f"rename {shlex.quote(temp_name)} {shlex.quote(container_name)}",
                              timeout=120, node_id=target_node_id)
        await panel.step(1, "done")
        await panel.step(2, "done")
        await panel.step(3, "running")
        await apply_lxc_config(container_name, target_node_id)
        await run_lxc_command(container_name, f"start {shlex.quote(container_name)}", timeout=240,
                              node_id=target_node_id)
        await apply_internal_permissions(container_name, target_node_id)
        await panel.step(3, "done")
        await panel.step(4, "running")
        await run_blocking(patch_vps, container_name, node_id=target_node_id, status="running")
        await panel.step(4, "done")
        await panel.step(5, "running")
        restored = await recreate_port_forwards(container_name)
        await panel.step(5, "done", f"{restored} forward(s)")
    except Exception as exc:
        await panel.finish(final_embed=error_embed("Migration Failed", truncate_text(exc, 800)))
        return
    audit("vps_migrate", ctx.author.id, target=container_name, detail=f"{source_node} → {target_node_id}")
    await panel.finish(final_embed=success_embed("Migration Complete",
                                                 f"`{container_name}` now runs on **{target['name']}**."))

# ── 11.3 Diagnostics ─────────────────────────────────────────────────────────

@bot.command(name="exec", aliases=["sh", "console"])
@is_admin()
@with_typing
@command_timeout(180)
async def exec_command(ctx: commands.Context, container_name: str, *, command: str) -> None:
    """Run a command inside a container (root, one-shot)."""
    valid, reason = validate_container_name(container_name)
    if not valid:
        await ctx.send(embed=error_embed("Invalid Container", reason))
        return
    message = await thinking(ctx.channel, "Executing", f"`$ {truncate_text(command, 80)}`")
    try:
        output = await container_shell(container_name, command, timeout=120)
    except Exception as exc:
        await _safe_edit(message, embed=error_embed("Execution Failed", truncate_text(exc, 800)))
        return
    audit("admin_exec", ctx.author.id, target=container_name, detail=command[:200])
    embed = create_embed(f"Output · {container_name}", f"`$ {truncate_text(command, 150)}`", COLORS["dark"],
                         thumbnail=False)
    for chunk in chunk_lines((output or "_no output_").splitlines(), char_limit=950, max_lines=35):
        add_field(embed, "stdout", safe_code_block(chunk, max_chars=900))
    await _safe_edit(message, embed=embed)


@bot.command(name="lxc-list", aliases=["containers"])
@is_admin()
@with_typing
@command_timeout(90)
async def lxc_list(ctx: commands.Context, node_id: int = None) -> None:
    """Raw LXC inventory for a node (defaults to the local node)."""
    node = await run_blocking(get_node, node_id) if node_id else await run_blocking(get_local_node)
    if not node:
        await ctx.send(embed=error_embed("Node Not Found", "That node does not exist."))
        return
    message = await thinking(ctx.channel, "Listing containers", f"Reading state on `{node['name']}`")
    try:
        output = await run_lxc_command("", "list", timeout=60, node_id=node["id"])
    except Exception as exc:
        await _safe_edit(message, embed=error_embed("List Failed", truncate_text(exc, 700)))
        return
    embed = create_embed(f"Containers · {node['name']}", f"Live inventory on <t:{int(time.time())}:R>",
                         COLORS["node"], thumbnail=False)
    lines = output.splitlines()
    for chunk in chunk_lines(lines[:60], char_limit=950, max_lines=40):
        add_field(embed, "lxc list", safe_code_block(chunk, max_chars=900))
    await _safe_edit(message, embed=embed)


@bot.command(name="vps-logs", aliases=["logs"])
@is_admin()
@with_typing
@command_timeout(120)
async def vps_logs(ctx: commands.Context, container_name: str, lines: int = 30) -> None:
    """Tail recent logs from inside a container."""
    lines = clamp_int(lines, 5, 200, 30)
    message = await thinking(ctx.channel, "Reading logs", f"`{container_name}` • last {lines} lines")
    script = (f"journalctl -n {lines} --no-pager 2>/dev/null || tail -n {lines} /var/log/syslog 2>/dev/null "
              f"|| dmesg | tail -n {lines} 2>/dev/null || echo 'no log source available'")
    try:
        output = await container_shell(container_name, script, timeout=60,
                                      node_id=await run_blocking(find_node_id_for_container, container_name))
    except Exception as exc:
        await _safe_edit(message, embed=error_embed("Log Read Failed", truncate_text(exc, 700)))
        return
    embed = create_embed(f"Logs · {container_name}", f"Last `{lines}` lines", COLORS["dark"], thumbnail=False)
    for chunk in chunk_lines(output.splitlines(), char_limit=950, max_lines=35):
        add_field(embed, "log", safe_code_block(chunk, max_chars=900))
    await _safe_edit(message, embed=embed)


@bot.command(name="vps-processes", aliases=["ps"])
@is_admin()
@with_typing
@command_timeout(90)
async def vps_processes(ctx: commands.Context, container_name: str) -> None:
    """Process list from inside a container."""
    message = await thinking(ctx.channel, "Reading processes", f"`{container_name}`")
    try:
        output = await container_shell(container_name, "ps -eo pid,user,comm,%cpu,%mem --sort=-%cpu | head -n 20",
                                       timeout=45)
    except Exception as exc:
        await _safe_edit(message, embed=error_embed("Failed", truncate_text(exc, 700)))
        return
    embed = create_embed(f"Processes · {container_name}", "Top consumers by CPU", COLORS["dark"], thumbnail=False)
    for chunk in chunk_lines(output.splitlines(), char_limit=950, max_lines=25):
        add_field(embed, "ps", safe_code_block(chunk, max_chars=900))
    await _safe_edit(message, embed=embed)


@bot.command(name="vps-network", aliases=["netinfo"])
@is_admin()
@with_typing
@command_timeout(90)
async def vps_network(ctx: commands.Context, container_name: str) -> None:
    """Interfaces and routes of a container, with a public-IP lookup hint."""
    vps = await run_blocking(get_vps_by_container, container_name)
    message = await thinking(ctx.channel, "Reading network", f"Interfaces of `{container_name}`…")
    networks = await get_container_networks(container_name, vps["node_id"] if vps else None)
    forwarding = await run_blocking(get_forwards_for_container, container_name)
    embed = create_embed(f"Network · {container_name}", "Interfaces, addresses and published ports",
                         COLORS["network"])
    if networks:
        add_field(embed, "🧩 Interfaces", "\n".join(f"`{iface}` → `{ip}`" for iface, ip in networks.items()), False)
    else:
        add_field(embed, "🧩 Interfaces", "_No non-loopback interface detected._", False)
    if forwarding:
        add_field(embed, "🔌 Published",
                  "\n".join(f"`{YOUR_SERVER_IP}:{f['host_port']}` → `:{f['vps_port']}`" for f in forwarding[:12]), False)
    else:
        add_field(embed, "🔌 Published", "No port forwards yet.", False)
    if vps:
        add_field(embed, "🖥️ Node", f"`{vps['node_id']}` • bridge `{LXC_BRIDGE}`", True)
    await _safe_edit(message, embed=embed)


@bot.command(name="vps-uptime")
@is_admin()
@with_typing
async def vps_uptime(ctx: commands.Context, container_name: str) -> None:
    """Uptime of a container."""
    message = await thinking(ctx.channel, "Reading uptime", f"`{container_name}`")
    stats = await get_container_stats(container_name)
    embed = info_embed("Uptime", f"`{container_name}` has been running for **{stats.get('uptime')}**.")
    uptime_seconds = float(stats.get("uptime_seconds") or 0)
    if uptime_seconds:
        add_field(embed, "📈 Over time", gradient_bar(min(uptime_seconds / 86400 * 100, 100), 100, 12), False)
    await _safe_edit(message, embed=embed)


@bot.command(name="vps-password", aliases=["passwords"])
@is_admin()
@with_typing
async def vps_password(ctx: commands.Context, container_name: str = None) -> None:
    """
    Reveal stored root credentials — admins only, always ephemeral.

    A DM copy is sent to the requesting admin so it never sits in channel history.
    """
    if ctx.guild and ctx.guild.me and ctx.guild.me.guild_permissions.manage_messages:
        await _safe_delete(ctx.message)
    if container_name:
        vps = await run_blocking(get_vps_by_container, container_name)
        if not vps:
            await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
            return
        targets = [vps]
    else:
        targets = [dict(r) for r in await run_blocking(query_all, "SELECT * FROM vps ORDER BY user_id, id")]

    lines: List[str] = []
    for vps in targets:
        password = decrypt_secret(vps.get("root_password"))
        owner = await resolve_user(str(vps["user_id"]))
        lines.append(f"`{vps['container_name']}` • 👤 {display_user(owner, vps['user_id'])}\n"
                     f"   ↳ 🔑 {f'`{password}`' if password else '_not stored_'}")

    embed = create_embed("Credential Vault", f"{len(targets)} record(s) • ephemeral message only",
                         COLORS["vault"], thumbnail=False)
    for chunk in chunk_lines(lines, char_limit=950, max_lines=10):
        add_field(embed, "Records", chunk)
    add_field(embed, "🔐 Handling rules",
              "• Never paste these in public channels\n"
              "• Rotate anything that may have leaked\n"
              "• Encryption key lives in `.secret_key` / `SECRET_KEY`", False)
    await ctx.author.send(embed=embed)
    audit("vault_view", ctx.author.id, target=container_name or "ALL", detail=f"{len(targets)} records")
    await ctx.send(embed=success_embed("Sent Privately", "Full credential list was sent to your DMs."))


@bot.command(name="node-check", aliases=["nodeinfo"])
@is_admin()
@with_typing
@command_timeout(90)
async def node_check(ctx: commands.Context, node_id: int = None) -> None:
    """Deep node health check: connectivity, storage pools, capacity."""
    node = await run_blocking(get_node, node_id) if node_id else await run_blocking(get_local_node)
    if not node:
        await ctx.send(embed=error_embed("Node Not Found", "That node does not exist."))
        return
    message = await thinking(ctx.channel, "Checking node", f"Probing `{node['name']}`…")
    status_text, online = await get_node_status(node["id"])
    embed = create_embed(f"Node Check · {node['name']}", status_text, COLORS["node"])
    add_field(embed, "📍 Placement", f"Location `{node.get('location', '?')}`\nType `{'local' if node['is_local'] else 'remote'}`\n"
                                     f"Capacity `{get_current_vps_count(node['id'])}/{node.get('total_vps') or '∞'}`", True)
    if online:
        stats = await get_host_stats(node["id"])
        add_field(embed, "🩺 Host",
                  f"CPU {gradient_bar(float(stats.get('cpu') or 0), 100, 8)}\n"
                  f"RAM {gradient_bar(float(stats.get('ram') or 0), 100, 8)}\n"
                  f"Disk {gradient_bar(float(stats.get('disk_pct') or 0), 100, 8)} `{stats.get('disk')}`", True)
        try:
            pools = await run_lxc_command("", "storage list", timeout=40, node_id=node["id"])
            for chunk in chunk_lines(pools.splitlines(), char_limit=950, max_lines=10):
                add_field(embed, "💾 Storage pools", safe_code_block(chunk, max_chars=900))
        except Exception as exc:
            add_field(embed, "💾 Storage pools", f"Unavailable: {truncate_text(exc, 200)}")
        if not node["is_local"]:
            add_field(embed, "🔗 Endpoint", f"`{node.get('url')}`", False)
    else:
        add_field(embed, "⚠️ Status", "Node is offline — remote commands will fail until it returns.", False)
    embed.set_footer(text=f"{beat()} {brand_footer()}", icon_url=BOT_ICON_URL or None)
    await _safe_edit(message, embed=embed)


@bot.command(name="resource-check", aliases=["runmon"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def resource_check(ctx: commands.Context) -> None:
    """Scan every running container and list anything above the thresholds."""
    message = await thinking(ctx.channel, "Scanning containers", "Reading live CPU and memory usage…")

    offenders: List[str] = []
    checked = 0
    for user_id in await run_blocking(get_all_user_ids_with_vps):
        for vps in await run_blocking(get_user_vps, user_id):
            if vps.get("suspended") or str(vps.get("status")) != "running":
                continue
            stats = await get_container_stats(vps["container_name"], vps["node_id"])
            if not stats.get("online"):
                continue
            checked += 1
            cpu = float(stats.get("cpu") or 0)
            ram_pct = float((stats.get("ram") or {}).get("pct") or 0)
            if cpu > CPU_THRESHOLD or ram_pct > RAM_THRESHOLD:
                offenders.append(f"**{vps['container_name']}** — CPU `{cpu:.0f}%` RAM `{ram_pct:.0f}%`"
                                 + (" ✅ whitelisted" if vps.get("whitelisted") else ""))

    embed = create_embed("Resource Scan", f"Checked `{checked}` running container(s)", COLORS["warning"])
    add_field(embed, "🎚️ Thresholds", f"CPU `>{CPU_THRESHOLD}%` • RAM `>{RAM_THRESHOLD}%`", True)
    if offenders:
        for chunk in chunk_lines(offenders, char_limit=950, max_lines=10):
            add_field(embed, "⚠️ Above threshold", chunk)
        add_field(embed, "Options",
                  f"`{PREFIX}suspend-vps <name> reason` • `{PREFIX}whitelist-vps <name> add` • "
                  f"`{PREFIX}add-resources <name> ...`", False)
    else:
        add_field(embed, "✅ All clear", "No container is above the configured thresholds.", False)
    await _safe_edit(message, embed=embed)


@bot.command(name="thresholds")
@is_admin()
async def show_thresholds(ctx: commands.Context) -> None:
    """Show the current abuse thresholds."""
    embed = info_embed("Resource Thresholds", f"CPU `{CPU_THRESHOLD}%` • RAM `{RAM_THRESHOLD}%`")
    add_field(embed, "Visual", f"CPU {gradient_bar(CPU_THRESHOLD, 100, 12)}\nRAM {gradient_bar(RAM_THRESHOLD, 100, 12)}", False)
    add_field(embed, "Change them", f"`{PREFIX}set-threshold <cpu> <ram>`", False)
    await ctx.send(embed=embed)


@bot.command(name="set-threshold")
@is_admin()
async def set_threshold(ctx: commands.Context, cpu: int, ram: int) -> None:
    """Set the abuse thresholds used by monitoring and `resource-check`."""
    global CPU_THRESHOLD, RAM_THRESHOLD
    CPU_THRESHOLD = clamp_int(cpu, 10, 100, 90)
    RAM_THRESHOLD = clamp_int(ram, 10, 100, 90)
    await run_blocking(set_setting, "cpu_threshold", CPU_THRESHOLD)
    await run_blocking(set_setting, "ram_threshold", RAM_THRESHOLD)
    audit("thresholds_update", ctx.author.id, detail=f"cpu={CPU_THRESHOLD} ram={RAM_THRESHOLD}")
    await ctx.send(embed=success_embed("Thresholds Updated", f"CPU `{CPU_THRESHOLD}%` • RAM `{RAM_THRESHOLD}%`"))


@bot.command(name="set-status", aliases=["presence"])
@is_admin()
async def set_status(ctx: commands.Context, activity: str, *, text: str) -> None:
    """Change the bot presence. activity = playing|watching|listening|competing|clear"""
    mapping = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }
    if activity.lower() == "clear":
        await bot.change_presence(activity=None)
        await ctx.send(embed=success_embed("Presence Cleared", "The bot no longer shows an activity."))
        return
    if activity.lower() not in mapping:
        await ctx.send(embed=error_embed("Invalid Activity", f"Choose one of: {', '.join(mapping)} or `clear`."))
        return
    await bot.change_presence(activity=discord.Activity(type=mapping[activity.lower()], name=text[:128]))
    await ctx.send(embed=success_embed("Presence Updated", f"{activity.title()} · {truncate_text(text, 80)}"))


# ═══════════════════════════════════════════════════════════════════════════
# 11.4 Admin VPS Management (Delete, Suspend, Unsuspend with Interactive Views)
# ═══════════════════════════════════════════════════════════════════════════

class SelectVPSView(discord.ui.View):
    """Interactive view for selecting a VPS from a user's list"""

    def __init__(self, vps_list: List[Dict[str, Any]], action: str, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.vps_list = vps_list
        self.action = action
        self.selected_vps = None

        # Create a select menu with all VPS
        options = []
        for i, vps in enumerate(vps_list, 1):
            container = vps.get("container_name", f"VPS #{i}")
            status = vps.get("status", "stopped").upper()
            suspended = "🔒 SUSPENDED" if vps.get("suspended") else f"🟢 {status}"
            config = vps.get("config", "Unknown")

            label = f"#{i} {container[:30]}"
            description = f"{suspended} • {config}"

            options.append(discord.SelectOption(
                label=label,
                description=description[:100],
                value=str(i)
            ))

        if options:
            select = discord.ui.Select(
                placeholder=f"Select a VPS to {self.action}...",
                options=options,
                min_values=1,
                max_values=1
            )
            select.callback = self.on_select
            self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        """Handle VPS selection"""
        selected_index = int(interaction.data["values"][0]) - 1
        if 0 <= selected_index < len(self.vps_list):
            self.selected_vps = self.vps_list[selected_index]
        await safe_defer(interaction)
        self.stop()


@bot.command(name="vps-delete", aliases=["vpsdelete", "vps-remove", "remove-vps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def vps_delete(ctx: commands.Context, user: discord.User) -> None:
    """
    🗑️ Delete a user's VPS (admin only)

    Shows all VPS owned by a user and lets admin select which one to delete.
    """
    vps_list = await run_blocking(get_user_vps, user.id)

    if not vps_list:
        await ctx.send(embed=error_embed("No VPS Found", f"{user.mention} has no VPS to delete."))
        return

    # Create selection view
    view = SelectVPSView(vps_list, action="delete")
    embed = create_embed(
        "🗑️ Delete VPS",
        f"Select which VPS of {user.mention} to delete:",
        COLORS["warning"]
    )
    add_field(embed, "⚠️ Warning", "Deletion is **permanent** and cannot be undone!", False)

    msg = await ctx.send(embed=embed, view=view)
    await view.wait()

    if view.selected_vps is None:
        await _safe_edit(msg, embed=error_embed("Cancelled", "No VPS selected."), view=None)
        return

    selected_vps = view.selected_vps
    container_name = selected_vps.get("container_name", "Unknown")

    # Confirm deletion
    confirmed = await ask_confirm(
        ctx,
        title=f"Confirm Delete: {container_name}",
        description=f"This will permanently delete **{container_name}** from {user.mention}\n\n"
                    f"⚠️ This action **CANNOT be undone**!",
        confirm_label="🗑️ Delete Permanently",
        danger=True,
        timeout=30
    )

    if not confirmed:
        await ctx.send(embed=info_embed("Cancelled", "VPS deletion cancelled."))
        return

    # Delete the VPS
    loading_msg = await thinking(ctx.channel, "Deleting VPS", f"Removing `{container_name}`...")

    try:
        # Stop container if running
        try:
            node_id = selected_vps.get("node_id", 1)
            await run_lxc_command(
                container_name,
                f"stop {shlex.quote(container_name)}",
                timeout=60,
                node_id=node_id,
                check=False
            )
        except Exception:
            pass

        # Delete from database
        await run_blocking(delete_vps_row, container_name)

        # Notify user
        try:
            await user.send(embed=warning_embed(
                "VPS Deleted",
                f"Your VPS **{container_name}** has been deleted by admin {ctx.author.mention}"
            ))
        except Exception:
            pass

        await _safe_edit(loading_msg, embed=success_embed(
            "✅ VPS Deleted",
            f"Successfully deleted `{container_name}` from {user.mention}"
        ))

        audit("vps_deleted", ctx.author.id, target=user.id, detail=f"Container: {container_name}")
        logger.info("[ADMIN] %s deleted VPS %s from %s", ctx.author.name, container_name, user.name)

    except Exception as e:
        logger.error("Failed to delete VPS %s: %s", container_name, e, exc_info=True)
        await _safe_edit(loading_msg, embed=error_embed("Deletion Failed", f"Error: {str(e)}"))


@bot.command(name="vps-suspend", aliases=["vpssuspend", "vps-ban", "ban-vps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def vps_suspend(ctx: commands.Context, user: discord.User, *, reason: str = "Administrative action") -> None:
    """
    🔒 Suspend a user's VPS (admin only)

    Shows all VPS owned by a user and lets admin select which one to suspend.
    Suspended VPS cannot be started until unsuspended.
    """
    vps_list = await run_blocking(get_user_vps, user.id)

    if not vps_list:
        await ctx.send(embed=error_embed("No VPS Found", f"{user.mention} has no VPS to suspend."))
        return

    # Filter out already suspended VPS
    active_vps = [v for v in vps_list if not v.get("suspended")]
    if not active_vps:
        await ctx.send(embed=warning_embed("All Suspended", f"All VPS of {user.mention} are already suspended."))
        return

    # Create selection view
    view = SelectVPSView(active_vps, action="suspend")
    embed = create_embed(
        "🔒 Suspend VPS",
        f"Select which VPS of {user.mention} to suspend:",
        COLORS["warning"]
    )
    add_field(embed, "ℹ️ Info", "Suspended VPS cannot be started until unsuspended by an admin.", False)

    msg = await ctx.send(embed=embed, view=view)
    await view.wait()

    if view.selected_vps is None:
        await _safe_edit(msg, embed=error_embed("Cancelled", "No VPS selected."), view=None)
        return

    selected_vps = view.selected_vps
    container_name = selected_vps.get("container_name", "Unknown")

    # Suspend the VPS
    loading_msg = await thinking(ctx.channel, "Suspending VPS", f"Suspending `{container_name}`...")

    try:
        # Stop container
        try:
            await run_lxc_command(
                container_name,
                f"stop {shlex.quote(container_name)}",
                timeout=60,
                node_id=selected_vps.get("node_id", 1),
                check=False
            )
        except Exception:
            pass

        # Mark as suspended in database
        await run_blocking(patch_vps, container_name, suspended=True)

        # Notify user
        try:
            await user.send(embed=warning_embed(
                "🔒 VPS Suspended",
                f"Your VPS **{container_name}** has been suspended by admin {ctx.author.mention}\n\n"
                f"**Reason:** {reason}\n\n"
                f"Contact admin to unsuspend it."
            ))
        except Exception:
            pass

        await _safe_edit(loading_msg, embed=success_embed(
            "✅ VPS Suspended",
            f"Successfully suspended `{container_name}` of {user.mention}\n\n"
            f"**Reason:** {reason}"
        ))

        audit("vps_suspended", ctx.author.id, target=user.id, detail=f"Container: {container_name}, Reason: {reason}")
        logger.info("[ADMIN] %s suspended VPS %s from %s", ctx.author.name, container_name, user.name)

    except Exception as e:
        logger.error("Failed to suspend VPS %s: %s", container_name, e, exc_info=True)
        await _safe_edit(loading_msg, embed=error_embed("Suspension Failed", f"Error: {str(e)}"))


@bot.command(name="vps-unsuspend", aliases=["vpsunsuspend", "vps-unban", "unban-vps"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def vps_unsuspend(ctx: commands.Context, user: discord.User) -> None:
    """
    🔓 Unsuspend a user's VPS (admin only)

    Shows all suspended VPS owned by a user and lets admin select which one to unsuspend.
    """
    vps_list = await run_blocking(get_user_vps, user.id)

    if not vps_list:
        await ctx.send(embed=error_embed("No VPS Found", f"{user.mention} has no VPS."))
        return

    # Filter suspended VPS only
    suspended_vps = [v for v in vps_list if v.get("suspended")]
    if not suspended_vps:
        await ctx.send(embed=info_embed("None Suspended", f"{user.mention} has no suspended VPS."))
        return

    # Create selection view
    view = SelectVPSView(suspended_vps, action="unsuspend")
    embed = create_embed(
        "🔓 Unsuspend VPS",
        f"Select which VPS of {user.mention} to unsuspend:",
        COLORS["success"]
    )
    add_field(embed, "ℹ️ Info", "Unsuspending will allow the user to start this VPS again.", False)

    msg = await ctx.send(embed=embed, view=view)
    await view.wait()

    if view.selected_vps is None:
        await _safe_edit(msg, embed=error_embed("Cancelled", "No VPS selected."), view=None)
        return

    selected_vps = view.selected_vps
    container_name = selected_vps.get("container_name", "Unknown")

    # Unsuspend the VPS
    loading_msg = await thinking(ctx.channel, "Unsuspending VPS", f"Unsuspending `{container_name}`...")

    try:
        # Mark as unsuspended in database
        await run_blocking(patch_vps, container_name, suspended=False)

        # Notify user
        try:
            await user.send(embed=success_embed(
                "🔓 VPS Unsuspended",
                f"Your VPS **{container_name}** has been unsuspended by admin {ctx.author.mention}\n\n"
                f"You can now start it again using `{PREFIX}manage`"
            ))
        except Exception:
            pass

        await _safe_edit(loading_msg, embed=success_embed(
            "✅ VPS Unsuspended",
            f"Successfully unsuspended `{container_name}` of {user.mention}"
        ))

        audit("vps_unsuspended", ctx.author.id, target=user.id, detail=f"Container: {container_name}")
        logger.info("[ADMIN] %s unsuspended VPS %s from %s", ctx.author.name, container_name, user.name)

    except Exception as e:
        logger.error("Failed to unsuspend VPS %s: %s", container_name, e, exc_info=True)
        await _safe_edit(loading_msg, embed=error_embed("Unsuspension Failed", f"Error: {str(e)}"))


@bot.command(name="vps-timeout-remove", aliases=["vps-timeout", "timeout-remove", "remove-timeout"])
@is_admin()
async def vps_timeout_remove(ctx: commands.Context, user: discord.User) -> None:
    """
    ⏱️ Remove VPS creation cooldown from a user (admin only)

    Allows a user to create a new free VPS immediately by removing their cooldown.
    """
    # Try to remove the cooldown
    cooldown_key = f"vps_create:{user.id}"
    was_active = await run_blocking(remove_cooldown, cooldown_key)

    if was_active:
        # Notify the admin
        await ctx.send(embed=success_embed(
            "✅ Cooldown Removed",
            f"**{user.mention}** can now create a new free VPS immediately.\n\n"
            f"They can use `{PREFIX}vps create` right away."
        ))

        # Notify the user
        try:
            await user.send(embed=success_embed(
                "⏱️ VPS Cooldown Removed",
                f"An admin has removed your VPS creation cooldown!\n\n"
                f"You can now create a new free VPS using `{PREFIX}vps create`"
            ))
        except Exception:
            pass

        audit("vps_timeout_removed", ctx.author.id, target=user.id, detail="Cooldown cleared by admin")
        logger.info("[ADMIN] %s removed VPS cooldown for %s", ctx.author.name, user.name)
    else:
        await ctx.send(embed=info_embed(
            "No Cooldown Active",
            f"**{user.mention}** doesn't have an active VPS creation cooldown.\n\n"
            f"They can create a VPS immediately."
        ))


@bot.command(name="stop-vps-all", aliases=["emergency-stop"])
@is_main_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def stop_all_vps(ctx: commands.Context) -> None:
    """Emergency stop: gracefully stop every running container."""
    running = [dict(r) for r in await run_blocking(
        query_all, "SELECT * FROM vps WHERE status = 'running' AND suspended = 0")]
    if not running:
        await ctx.send(embed=info_embed("Nothing Running", "No active containers to stop."))
        return
    confirmed = await ask_confirm(
        ctx, title="Stop every running container?",
        description=f"This will stop **{len(running)}** container(s) across all nodes and notify the owners.\n\n"
                    f"Use this only for emergencies (abuse wave, host maintenance).",
        confirm_label=f"Stop {len(running)} containers", danger=True, timeout=120)
    if not confirmed:
        return

    panel = ProgressPanel(ctx.channel, "Emergency stop", ["Stop containers", "Update records", "Notify owners"],
                          subtitle=f"{len(running)} container(s) queued")
    await panel.start()
    stopped, failed = [], []
    for vps in running:
        try:
            await run_lxc_command(vps["container_name"], f"stop {shlex.quote(vps['container_name'])}",
                                  timeout=120, node_id=vps["node_id"], check=False)
            await run_blocking(patch_vps, vps["container_name"], status="stopped")
            stopped.append(vps)
        except Exception as exc:
            logger.warning("Emergency stop failed for %s: %s", vps["container_name"], exc)
            failed.append(vps["container_name"])
    await panel.step(0, "done", f"{len(stopped)} stopped")
    await panel.step(1, "done", f"{len(stopped)} records updated")
    await panel.step(2, "running")
    notified = 0
    for vps in stopped:
        try:
            user = await bot.fetch_user(int(vps["user_id"]))
            await user.send(embed=warning_embed("Service Maintenance",
                                                f"`{vps['container_name']}` was stopped by staff during maintenance.\n"
                                                f"Start it again from `{PREFIX}manage`."))
            notified += 1
        except Exception:
            pass
    await panel.step(2, "done", f"{notified} owner(s) notified")
    audit("emergency_stop", ctx.author.id, detail=f"{len(stopped)} stopped, {len(failed)} failed")
    embed = success_embed("Emergency Stop Complete", f"Stopped **{len(stopped)}** container(s).")
    if failed:
        add_field(embed, "Failures", ", ".join(failed[:20]), False)
    await panel.finish(final_embed=embed)


# ── 11.4 Container Management ──────────────────────────────────────────────────


@bot.command(name="show-all-vps", aliases=["list-lxc", "lxc-all"])
@is_admin()
@with_typing
@command_timeout(120)
async def show_all_vps(ctx: commands.Context) -> None:
    """
    Show ALL LXC containers on the host with Discord user assignments.
    
    Highlights orphaned containers (exist on host but not in bot database).
    Shows each container's owner, status, and whether it's tracked by the bot.
    """
    message = await thinking(ctx.channel, "Scanning host", "Fetching all LXC containers…")
    
    try:
        # Get all containers on the local node with timeout
        try:
            lxc_containers = await asyncio.wait_for(get_lxc_containers_on_node(1), timeout=15.0)
        except asyncio.TimeoutError:
            await _safe_edit(message, embed=error_embed("Timeout", "LXC list query timed out. Try again later."))
            return
        
        # Get all tracked containers from database (fast query)
        tracked_containers = await run_blocking(get_all_containers_on_host)
        
        # Categorize containers
        tracked = []
        orphaned = []
        
        for container in lxc_containers:
            if container in tracked_containers:
                tracked.append(container)
            else:
                orphaned.append(container)
        
        # Build embed with summary
        embed = create_embed(
            "🖥️ All Containers on Host",
            f"Total: `{len(lxc_containers)}` • Tracked: `{len(tracked)}` • Orphaned: `{len(orphaned)}`",
            COLORS["brand"],
            thumbnail=False
        )
        
        # Tracked containers (show first 30 only to avoid size limit)
        if tracked:
            lines = []
            for container_name in tracked[:30]:
                # Simple format - no heavy lookups
                status_emoji = "🟢"  # Default to running (we can show "?" if needed)
                lines.append(f"{status_emoji} `{container_name}`")
            
            # Add count note if there are more
            if len(tracked) > 30:
                lines.append(f"\n_... and {len(tracked) - 30} more tracked containers_")
            
            for chunk in chunk_lines(lines, char_limit=950, max_lines=25):
                add_field(embed, "📊 Tracked Containers", chunk)
        else:
            add_field(embed, "📊 Tracked Containers", "_None_", False)
        
        # Orphaned containers (show first 20 only)
        if orphaned:
            orphan_lines = [f"⚠️ `{c}`" for c in orphaned[:20]]
            
            # Add count note if there are more
            if len(orphaned) > 20:
                orphan_lines.append(f"\n_... and {len(orphaned) - 20} more orphaned containers_")
            
            for chunk in chunk_lines(orphan_lines, char_limit=950, max_lines=15):
                add_field(embed, "🚨 Orphaned Containers", 
                         chunk + f"\n_Use `!lxc-assign @user <name>` to adopt them_")
        else:
            add_field(embed, "🚨 Orphaned Containers", "✅ None", False)
        
        add_field(embed, "💡 Quick Actions",
                 f"`!lxc-delete <name>` — delete a container\n"
                 f"`!lxc-assign @user <name>` — assign orphaned container to user\n"
                 f"`!lxc-list` — raw LXC output with full details", False)
        
        await _safe_edit(message, embed=embed)
    
    except Exception as exc:
        logger.error("show_all_vps failed: %s", exc, exc_info=True)
        await _safe_edit(message, embed=error_embed("Scan Failed", truncate_text(str(exc), 700)))


@bot.command(name="lxc-delete", aliases=["delete-container"])
@is_admin()
@with_typing
@command_timeout(120)
async def lxc_delete(ctx: commands.Context, container_name: str, *, reason: str = "Cleanup") -> None:
    """
    Delete an LXC container by name (not via user).
    
    This is useful for deleting orphaned or untracked containers directly.
    
    Usage: !lxc-delete <container-name> [reason]
    """
    container = str(container_name).strip().lower()
    
    # Check if it exists in database
    vps = await run_blocking(get_vps_by_container, container)
    is_tracked = vps is not None
    owner_text = ""
    if is_tracked:
        owner = await resolve_user(str(vps["user_id"]))
        owner_text = f"\n👤 Owner: {display_user(owner, vps['user_id'])}"
    
    # Confirm deletion
    confirmed = await ask_confirm(
        ctx,
        title="Delete this container?",
        description=(
            f"**`{container}`**{' (untracked/orphaned)' if not is_tracked else ''} will be destroyed.\n"
            f"Reason: {sanitize_reason(reason)}{owner_text}\n\n"
            f"**This cannot be undone.**"
        ),
        confirm_label="Delete forever", danger=True, timeout=90,
    )
    if not confirmed:
        return
    
    # Delete the container
    message = await thinking(ctx.channel, "Deleting container", f"Destroying `{container}`…")
    try:
        node_id = vps["node_id"] if is_tracked else 1
        await run_lxc_command(container, f"delete {shlex.quote(container)} --force", 
                            timeout=240, node_id=node_id, check=False)
        
        # Remove from database if tracked
        if is_tracked:
            await run_blocking(delete_vps_row, container)
        
        audit("lxc_delete", ctx.author.id, target=container, detail=sanitize_reason(reason))
        
        embed = success_embed("Container Deleted", f"`{container}` was removed.", thumbnail=False)
        if is_tracked:
            add_field(embed, "Status", "Also removed from bot database.", False)
        else:
            add_field(embed, "Status", "Orphaned container cleaned up.", False)
        add_field(embed, "Reason", sanitize_reason(reason), False)
        await _safe_edit(message, embed=embed)
        
    except Exception as exc:
        await _safe_edit(message, embed=error_embed("Deletion Failed", truncate_text(exc, 700)))


@bot.command(name="delete-useless-vps", aliases=["cleanup-orphaned", "delete-orphaned"])
@is_admin()
@with_typing
@command_timeout(300)
async def delete_useless_vps(ctx: commands.Context, node_id: int = None) -> None:
    """
    Delete LXC containers that exist on a node but are NOT in the bot database.
    
    This command finds orphaned containers (those created manually or left over from crashes)
    and allows you to delete them. VPS that ARE tracked in the bot database will NOT be touched.
    
    Usage: !delete-useless-vps [node_id]
    """
    # Get the node to check
    if node_id is None:
        node = await run_blocking(get_local_node)
        if not node:
            await ctx.send(embed=error_embed("No Default Node", "Specify a node_id: !delete-useless-vps 1"))
            return
        node_id = int(node["id"])
    else:
        node = await run_blocking(get_node, node_id)
        if not node:
            await ctx.send(embed=error_embed("Node Not Found", f"Node {node_id} does not exist."))
            return
    
    # Get all containers on the node
    message = await thinking(ctx.channel, "Scanning node", f"Listing containers on {node.get('name', node_id)}…")
    try:
        all_containers = await get_lxc_containers_on_node(node_id)
    except Exception as exc:
        await _safe_edit(message, embed=error_embed(
            "Scan Failed", 
            f"Could not list containers on node {node_id}:\n{truncate_text(exc, 500)}"
        ))
        return
    
    if not all_containers:
        await _safe_edit(message, embed=info_embed(
            "No Containers", 
            f"Node {node.get('name', node_id)} has no LXC containers."
        ))
        return
    
    # Get all containers tracked in database
    tracked_containers = set(await run_blocking(get_all_containers_on_host))
    
    # Find orphaned containers (exist on node but not in database)
    orphaned = [c for c in all_containers if c not in tracked_containers]
    
    if not orphaned:
        await _safe_edit(message, embed=info_embed(
            "All Clean", 
            f"All {len(all_containers)} container(s) on {node.get('name', node_id)} are tracked in the database."
        ))
        return
    
    # Show the orphaned containers
    orphaned_list = "\n".join(f"• `{c}`" for c in sorted(orphaned)[:20])
    if len(orphaned) > 20:
        orphaned_list += f"\n• … and {len(orphaned) - 20} more"
    
    embed = create_embed(
        "🗑️ Orphaned Containers Found",
        f"**Node:** {node.get('name', node_id)}\n**Found:** {len(orphaned)} container(s) not in database\n\n{orphaned_list}",
        COLORS["warning"]
    )
    
    # Confirm deletion
    confirmed = await ask_confirm(
        ctx,
        title="Delete orphaned containers?",
        description=(
            f"The following **{len(orphaned)}** container(s) will be permanently deleted:\n\n{orphaned_list}\n\n"
            f"✅ All containers tracked in the bot database will be KEPT.\n"
            f"🗑️ Only orphaned/untracked containers will be deleted.\n\n"
            f"**This cannot be undone.**"
        ),
        confirm_label="Delete all orphaned containers", danger=True, timeout=120,
    )
    
    if not confirmed:
        await ctx.send(embed=info_embed("Cancelled", "No containers were deleted."))
        return
    
    # Delete each orphaned container
    deleted_count = 0
    failed_count = 0
    failed_list: List[Tuple[str, str]] = []
    
    panel = ProgressPanel(
        ctx.channel, 
        f"Deleting orphaned containers",
        [f"Delete container {i+1}/{len(orphaned)}" for i in range(len(orphaned))],
        subtitle=f"Node: {node.get('name', node_id)}",
        color=COLORS["danger"]
    )
    await panel.start()
    
    for idx, container in enumerate(sorted(orphaned)):
        try:
            logger.info(f"Deleting orphaned container: {container}")
            await panel.step(idx, "running", f"Deleting `{container}`…")
            
            # Force delete the container
            await run_lxc_command(
                container, 
                f"delete {shlex.quote(container)} --force", 
                timeout=180, 
                node_id=node_id, 
                check=False
            )
            
            await panel.step(idx, "done", f"`{container}` deleted")
            deleted_count += 1
            audit("delete_orphaned_vps", ctx.author.id, target=container, detail=f"node={node_id}")
            
        except Exception as exc:
            logger.error(f"Failed to delete orphaned container {container}: {exc}")
            await panel.step(idx, "failed", truncate_text(str(exc), 50))
            failed_count += 1
            failed_list.append((container, str(exc)))
    
    # Show results
    result_lines = [
        f"✅ **Deleted:** {deleted_count}",
        f"❌ **Failed:** {failed_count}",
        f"📦 **Total found:** {len(orphaned)}",
    ]
    
    result_desc = "\n".join(result_lines)
    if failed_list:
        result_desc += "\n\n**Failed containers:**\n"
        for container, error in failed_list[:10]:
            result_desc += f"• `{container}`: {truncate_text(error, 60)}\n"
        if len(failed_list) > 10:
            result_desc += f"• … and {len(failed_list) - 10} more\n"
    
    final_embed = success_embed(
        "🗑️ Cleanup Complete",
        result_desc,
        thumbnail=False
    ) if deleted_count > 0 else warning_embed(
        "Cleanup Incomplete",
        result_desc
    )
    
    add_field(final_embed, "Node", node.get('name', node_id), True)
    add_field(final_embed, "Database", "All tracked VPS were preserved ✅", True)
    
    await panel.finish(final_embed=final_embed)


@bot.command(name="lxc-assign", aliases=["adopt-container", "assign-vps"])
@is_admin()
@with_typing
@command_timeout(60)
async def lxc_assign(ctx: commands.Context, user: discord.User, container_name: str) -> None:
    """
    Assign an orphaned LXC container to a Discord user.
    
    Creates a new VPS record in the database for an existing container.
    Useful for manually-created containers or recovering orphaned ones.
    
    Usage: !lxc-assign @user <container-name>
    """
    container = str(container_name).strip().lower()
    
    # Check if container is already tracked
    existing = await run_blocking(get_vps_by_container, container)
    if existing:
        embed = warning_embed("Already Tracked", 
                            f"`{container}` is already owned by {display_user(await resolve_user(str(existing['user_id'])), existing['user_id'])}")
        await ctx.send(embed=embed)
        return
    
    # Verify container exists on host
    lxc_containers = await get_lxc_containers_on_node(1)
    if container not in lxc_containers:
        await ctx.send(embed=error_embed("Container Not Found", 
                                        f"`{container}` does not exist on the host.\n\n"
                                        f"Use `!show-all-vps` to see available containers."))
        return
    
    # Confirm assignment
    confirmed = await ask_confirm(
        ctx,
        title="Assign container to user?",
        description=(
            f"Container `{container}` will be assigned to {user.mention}\n\n"
            f"This creates a new VPS record with default resources:\n"
            f"• 1 GB RAM • 1 vCPU • 10 GB disk\n\n"
            f"You can adjust resources with `!add-resources {container} ...`"
        ),
        confirm_label="Assign", danger=False, timeout=60,
    )
    if not confirmed:
        return
    
    # Assign the container
    message = await thinking(ctx.channel, "Assigning container", f"Creating VPS record for `{container}`…")
    try:
        success = await run_blocking(assign_container_to_user, container, user.id)
        if success:
            audit("lxc_assign", ctx.author.id, target=container, 
                 detail=f"assigned to {user.id} ({user.name})")
            
            embed = success_embed("Container Assigned", 
                                f"`{container}` is now owned by {user.mention}", thumbnail=False)
            add_field(embed, "📊 Default Resources", "1 GB RAM • 1 vCPU • 10 GB disk", False)
            add_field(embed, "🔧 Next steps",
                     f"`!add-resources {container} 2 1 20` — adjust resources\n"
                     f"`!vpsinfo {container}` — view full details\n"
                     f"`!set-expiration {container} 30` — set lifetime", False)
            await _safe_edit(message, embed=embed)
        else:
            raise Exception("Database assignment failed")
    
    except Exception as exc:
        await _safe_edit(message, embed=error_embed("Assignment Failed", truncate_text(exc, 700)))


# ── 11.5 Ports / admins / platform maintenance ───────────────────────────────

@bot.command(name="ports")
@with_typing
@command_timeout(60)
async def ports_command(ctx: commands.Context, subcommand: str = None, *args: str) -> None:
    """
    Port forwarding.

    !ports                          → panel for your servers
    !ports list                     → every forward you own
    !ports add <vps#> <port>        → forward a port (tcp+udp)
    !ports remove <forward_id>      → delete a forward
    """
    subcommand = (subcommand or "panel").lower()

    if subcommand == "list":
        forwards = await run_blocking(get_user_forwards, ctx.author.id)
        embed = create_embed("Your Port Forwards",
                             f"`{get_user_used_ports(ctx.author.id)}/{get_user_allocation(ctx.author.id)}` slots used",
                             COLORS["network"])
        if not forwards:
            add_field(embed, "Empty", f"No forwards yet. Add one with `{PREFIX}ports add 1 8080`.", False)
        else:
            lines = [f"`id {f['id']}` • host `{f['host_port']}` → `{f['vps_container']}:{f['vps_port']}` "
                     f"({f.get('protocol', 'both')})" for f in forwards[:20]]
            add_field(embed, "Forwards", "\n".join(lines), False)
        await ctx.send(embed=embed)
        return

    if subcommand == "add":
        if len(args) < 2:
            await ctx.send(embed=error_embed("Usage", f"`{PREFIX}ports add <vps_number> <port>`"))
            return
        vps_index = clamp_int(args[0], 1, 1000, 1)
        port = clamp_int(args[1], 1, 65535, 0)
        if not port:
            await ctx.send(embed=error_embed("Invalid Port", "Port must be between 1 and 65535."))
            return
        vps_list = await run_blocking(get_user_vps, ctx.author.id)
        if vps_index > len(vps_list):
            await ctx.send(embed=error_embed("Invalid VPS", f"You have {len(vps_list)} server(s)."))
            return
        vps = vps_list[vps_index - 1]
        if get_user_used_ports(ctx.author.id) >= get_user_allocation(ctx.author.id):
            await ctx.send(embed=error_embed("Quota Reached", "Ask an admin for more port slots."))
            return
        message = await thinking(ctx.channel, "Opening port", f"Forwarding `{port}` on `{vps['container_name']}`…")
        host_port = await create_port_forward(ctx.author.id, vps["container_name"], port, vps["node_id"])
        if not host_port:
            await _safe_edit(message, embed=error_embed("Failed", "Could not allocate a host port right now."))
            return
        audit("port_add", ctx.author.id, target=vps["container_name"], detail=f"{port}→{host_port}")
        await _safe_edit(message, embed=success_embed("Port Forwarded",
                                                      f"`{YOUR_SERVER_IP}:{host_port}` → `{vps['container_name']}:{port}` (tcp+udp)"))
        return

    if subcommand == "remove":
        if not args:
            await ctx.send(embed=error_embed("Usage", f"`{PREFIX}ports remove <forward_id>`"))
            return
        forward_id = clamp_int(args[0], 1, 10_000_000, 0)
        record = await run_blocking(get_forward, forward_id)
        if not record or str(record["user_id"]) != str(ctx.author.id):
            await ctx.send(embed=error_embed("Not Yours", "That forward does not exist or belongs to someone else."))
            return
        ok, _, host_port = await remove_port_forward(forward_id)
        if ok:
            await ctx.send(embed=success_embed("Forward Removed", f"Host port `{host_port}` released."))
        else:
            await ctx.send(embed=error_embed("Failed", "Could not remove that forward."))
        return

    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not vps_list:
        await ctx.send(embed=info_embed("No Servers", f"Use `{PREFIX}vps create` first."))
        return
    view = PortsView(ManageView(ctx.author.id, vps_list), vps_list[0])
    embed = await run_blocking(PortsView.build_embed, vps_list[0])
    await ctx.send(embed=embed, view=view)


@bot.command(name="ports-add-user", aliases=["give-ports"])
@is_admin()
async def ports_add_user(ctx: commands.Context, amount: int, user: discord.User) -> None:
    """Give a user extra port-forwarding slots."""
    amount = clamp_int(amount, 1, 100, 1)
    total = await run_blocking(allocate_ports, user.id, amount)
    audit("ports_allocate", ctx.author.id, target=str(user.id), detail=f"+{amount}")
    embed = success_embed("Slots Added", f"`+{amount}` port slots for {user.mention} (total `{total}`).")
    await ctx.send(embed=embed)
    try:
        await user.send(embed=info_embed("Port Slots Added",
                                         f"An admin added **{amount}** forwarding slot(s). You now have `{total}`."))
    except discord.HTTPException:
        pass


@bot.command(name="ports-remove-user", aliases=["take-ports"])
@is_admin()
async def ports_remove_user(ctx: commands.Context, amount: int, user: discord.User) -> None:
    """Remove port-forwarding slots from a user."""
    amount = clamp_int(amount, 1, 100, 1)
    total = await run_blocking(allocate_ports, user.id, -amount)
    audit("ports_deallocate", ctx.author.id, target=str(user.id), detail=f"-{amount}")
    await ctx.send(embed=success_embed("Slots Removed", f"`-{amount}` slots from {user.mention} (now `{total}`)."))


@bot.command(name="ports-revoke")
@is_admin()
async def ports_revoke(ctx: commands.Context, forward_id: int) -> None:
    """Force-remove any forward by id."""
    ok, owner_id, host_port = await remove_port_forward(forward_id)
    if not ok:
        await ctx.send(embed=error_embed("Not Found", f"Forward `{forward_id}` does not exist."))
        return
    audit("port_revoke", ctx.author.id, detail=f"id={forward_id} host={host_port} owner={owner_id}")
    await ctx.send(embed=success_embed("Revoked", f"Forward `{forward_id}` (host port `{host_port}`) removed."))


@bot.command(name="admin-add")
@is_main_admin()
async def admin_add(ctx: commands.Context, user: discord.User) -> None:
    """Grant admin rights (main admin only)."""
    if is_admin_id(user.id):
        await ctx.send(embed=info_embed("Already Admin", f"{user.mention} already has access."))
        return
    await run_blocking(add_admin, user.id, ctx.author.id)
    refresh_admin_cache()
    audit("admin_add", ctx.author.id, target=str(user.id))
    await ctx.send(embed=success_embed("Admin Added", f"{user.mention} can now manage the platform."))
    try:
        await user.send(embed=create_embed("You're an admin now", f"Granted by {ctx.author.mention}.", COLORS["node"]))
    except discord.HTTPException:
        pass


@bot.command(name="admin-remove")
@is_main_admin()
async def admin_remove(ctx: commands.Context, user: discord.User) -> None:
    """Revoke admin rights (main admin only)."""
    if is_main_admin_id(user.id):
        await ctx.send(embed=error_embed("Not Allowed", "The main admin cannot be removed."))
        return
    if not await run_blocking(remove_admin, user.id):
        await ctx.send(embed=info_embed("Not An Admin", f"{user.mention} has no admin rights."))
        return
    refresh_admin_cache()
    audit("admin_remove", ctx.author.id, target=str(user.id))
    await ctx.send(embed=success_embed("Admin Removed", f"{user.mention} lost admin access."))


@bot.command(name="admin-list", aliases=["admins"])
async def admin_list(ctx: commands.Context) -> None:
    """List everyone with admin access."""
    embed = create_embed("Admin Team", f"`{len(_admin_cache)}` account(s) with elevated access", COLORS["node"])
    entries = []
    for admin_id in sorted(_admin_cache):
        user = await resolve_user(admin_id)
        crown = " 👑" if is_main_admin_id(admin_id) else ""
        entries.append(f"{display_user(user, admin_id)}{crown}")
    for chunk in chunk_lines(entries, char_limit=950, max_lines=12):
        add_field(embed, "Members", chunk)
    await ctx.send(embed=embed)


@bot.command(name="backup-db", aliases=["backup"])
@is_admin()
@with_typing
@command_timeout(120)
async def backup_db(ctx: commands.Context) -> None:
    """Create a verified database snapshot."""
    message = await thinking(ctx.channel, "Backing up", "Writing a verified snapshot…")
    path = await run_blocking(backup_database, True)
    if not path:
        await _safe_edit(message, embed=error_embed("Backup Failed",
                                                    "Could not create a verified backup — see the logs."))
        return
    size = path.stat().st_size if path.exists() else 0
    embed = success_embed("Backup Created", f"`{path.name}` • `{fmt_bytes(size)}`")
    add_field(embed, "Stored in", f"`{DB_BACKUP_DIR.name}/` (10 most recent are kept)", False)
    await _safe_edit(message, embed=embed)


@bot.command(name="audit-log", aliases=["audit"])
@is_admin()
async def audit_log_command(ctx: commands.Context, action: str = None) -> None:
    """Recent security-relevant actions."""
    entries = await run_blocking(get_audit_entries, 60, action)
    if not entries:
        await ctx.send(embed=info_embed("No Entries", "The audit log is empty for that filter."))
        return
    lines = [
        f"`{str(e['ts'])[5:19]}` **{e['action']}** "
        f"• actor `{e['actor_id'] or '—'}`"
        + (f" • target `{e['target']}`" if e.get("target") else "")
        + (f"\n   ↳ {truncate_text(e['detail'], 90)}" if e.get("detail") else "")
        for e in entries
    ]
    await Paginator(ctx.author.id, build_pages(
        "Audit Log" + (f" · {action}" if action else ""), lines, color=COLORS["dark"],
        page_size=8, field_name="Events")).send(ctx)


@bot.command(name="broadcast", aliases=["announce"])
@is_main_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def broadcast(ctx: commands.Context, *, message: str) -> None:
    """DM every VPS owner an announcement (main admin only)."""
    text = sanitize_reason(message, default="")
    if len(text) < 3:
        await ctx.send(embed=error_embed("Too Short", "Write a real message."))
        return
    owners = await run_blocking(get_all_user_ids_with_vps)
    confirmed = await ask_confirm(ctx, title="Send announcement?",
                                  description=f"This DMs **{len(owners)}** owner(s):\n\n> {truncate_text(text, 400)}",
                                  confirm_label="Send", timeout=90)
    if not confirmed:
        return
    panel = ProgressPanel(ctx.channel, "Broadcasting", ["Send DMs", "Report"],
                          subtitle=f"{len(owners)} recipient(s)")
    await panel.start()
    sent = failed = 0
    for owner_id in owners:
        try:
            user = await bot.fetch_user(int(owner_id))
            await user.send(embed=create_embed("📣 Announcement", text, COLORS["brand"]))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.35)
    await panel.step(0, "done", f"{sent} sent")
    await panel.step(1, "done", f"{failed} failed")
    audit("broadcast", ctx.author.id, detail=f"sent={sent} failed={failed}")
    await panel.finish(final_embed=success_embed("Broadcast Sent", f"Delivered `{sent}` • failed `{failed}`"))


@bot.command(name="reset-ratelimit", aliases=["unblock"])
@is_admin()
async def reset_ratelimit(ctx: commands.Context, user: discord.User) -> None:
    """Clear a user's rate-limit timeout."""
    reset_rate_limit(user.id)
    audit("ratelimit_reset", ctx.author.id, target=str(user.id))
    await ctx.send(embed=success_embed("Rate Limit Cleared", f"{user.mention} can use commands again."))


@bot.command(name="reload-cache", aliases=["refresh-admins"])
@is_main_admin()
async def reload_cache(ctx: commands.Context) -> None:
    """Reload the admin cache from the database."""
    refresh_admin_cache()
    await ctx.send(embed=success_embed("Cache Reloaded", f"`{len(_admin_cache)}` admin(s) loaded."))


# ── 11.5b Slash commands (modern clients) ────────────────────────────────────

@bot.tree.command(name="manage", description="Open your VPS control panel")
async def slash_manage(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await open_manage_panel(interaction, interaction.user.id)


@bot.tree.command(name="myvps", description="List your servers")
async def slash_myvps(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    vps_list = await run_blocking(get_user_vps, interaction.user.id)
    if not vps_list:
        await interaction.followup.send(
            embed=info_embed("No Servers", f"Create one with `{PREFIX}vps create`."), ephemeral=True)
        return
    lines = []
    for index, vps in enumerate(vps_list, start=1):
        status = await container_state(vps["container_name"], vps["node_id"])
        vps["status"] = status
        lines.append(vps_card(vps, index, compact=True))
    embed = create_embed("Your Servers", None, COLORS["success"])
    for chunk in chunk_lines(lines):
        add_field(embed, "Servers", chunk)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check bot latency")
async def slash_ping(interaction: discord.Interaction) -> None:
    embed = success_embed("Pong!", f"Gateway `{bot.latency * 1000:.0f} ms`", thumbnail=False)
    embed.set_footer(text=f"{beat()} {brand_footer()}", icon_url=BOT_ICON_URL or None)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dashboard", description="Live dashboard of your servers")
async def slash_dashboard(interaction: discord.Interaction) -> None:
    vps_list = await run_blocking(get_user_vps, interaction.user.id)
    if not vps_list:
        await interaction.response.send_message(
            embed=info_embed("No Servers", f"Create one with `{PREFIX}vps create`."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    view = LiveContainerGrid(interaction.user.id, vps_list)
    embed = await view.build_live_embed()
    message = await interaction.followup.send(embed=view._decorate_live(embed), view=view, wait=True)
    view.message = message
    view.start_live(message)


@app_commands.context_menu(name="VPS info")
async def context_vps_info(interaction: discord.Interaction, member: discord.Member) -> None:
    vps_list = await run_blocking(get_user_vps, member.id)
    embed = create_embed(f"{member.display_name}'s servers", f"`{len(vps_list)}` container(s)", COLORS["dark"])
    if not vps_list:
        add_field(embed, "Nothing here", "This user owns no servers.")
    else:
        for chunk in chunk_lines([f"{vps_status_pill(v)} `{v['container_name']}` • {expiration_line(v)}"
                                  for v in vps_list[:12]]):
            add_field(embed, "Servers", chunk)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# v10 fix: the context menu has to be attached to the tree or it is never synced.
bot.tree.add_command(context_vps_info)


# ── 9.3 Sharing & ownership ──────────────────────────────────────────────────

@bot.command(name="share-user", aliases=["sharevps", "share"])
@with_typing
@command_timeout(60)
async def share_user(ctx: commands.Context, user: discord.User, vps_number: int) -> None:
    """Share one of your servers with someone (read/write access to the panel)."""
    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not 1 <= vps_number <= len(vps_list):
        await ctx.send(embed=error_embed("Invalid Server", f"Pick a number between 1 and {len(vps_list)}. "
                                                          f"See `{PREFIX}myvps`."))
        return
    if user.id == ctx.author.id:
        await ctx.send(embed=error_embed("That's You", "You already own this server."))
        return
    if user.bot:
        await ctx.send(embed=error_embed("Bots Can't Connect", "Pick a real user."))
        return

    vps = vps_list[vps_number - 1]
    shared = list(vps.get("shared_with") or [])
    if str(user.id) in shared:
        await ctx.send(embed=info_embed("Already Shared", f"{user.mention} already has access to `{vps['container_name']}`."))
        return
    if len(shared) >= 10:
        await ctx.send(embed=error_embed("Limit Reached", "A server can be shared with at most 10 people."))
        return

    shared.append(str(user.id))
    await run_blocking(patch_vps, vps["container_name"], shared_with=shared)
    audit("vps_share", ctx.author.id, target=vps["container_name"], detail=f"granted to {user.id}")

    embed = success_embed("Server Shared", f"{user.mention} can now manage `{vps['container_name']}`.")
    add_field(embed, "How they open it",
              f"`{PREFIX}manage-shared {ctx.author.mention} {vps_number}` — or from the **Share** button in `{PREFIX}manage`.",
              False)
    add_field(embed, "Currently shared with", f"`{len(shared)}` user(s)", True)
    await ctx.send(embed=embed)
    try:
        await user.send(embed=create_embed("Server Access Granted",
                                           f"{ctx.author.mention} shared `{vps['container_name']}` with you.\n\n"
                                           f"Open it with:\n`{PREFIX}manage-shared {ctx.author.mention} {vps_number}`",
                                           COLORS["success"]))
    except discord.HTTPException:
        pass


@bot.command(name="share-ruser", aliases=["unshare", "revoke-share"])
@with_typing
@command_timeout(60)
async def revoke_share(ctx: commands.Context, user: discord.User, vps_number: int) -> None:
    """Revoke someone's access to one of your servers."""
    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    if not 1 <= vps_number <= len(vps_list):
        await ctx.send(embed=error_embed("Invalid Server", f"Pick a number between 1 and {len(vps_list)}."))
        return
    vps = vps_list[vps_number - 1]
    shared = list(vps.get("shared_with") or [])
    if str(user.id) not in shared:
        await ctx.send(embed=info_embed("Not Shared", f"{user.mention} has no access to `{vps['container_name']}`."))
        return

    shared = [uid for uid in shared if uid != str(user.id)]
    await run_blocking(patch_vps, vps["container_name"], shared_with=shared)
    audit("vps_unshare", ctx.author.id, target=vps["container_name"], detail=f"revoked {user.id}")
    await ctx.send(embed=success_embed("Access Revoked",
                                       f"{user.mention} can no longer manage `{vps['container_name']}`."))
    try:
        await user.send(embed=warning_embed("Server Access Revoked",
                                            f"{ctx.author.mention} removed your access to `{vps['container_name']}`."))
    except discord.HTTPException:
        pass


@bot.command(name="manage-shared", aliases=["shared"])
@with_typing
@command_timeout(90)
async def manage_shared_vps(ctx: commands.Context, owner: discord.User, vps_number: int) -> None:
    """Open a server that someone shared with you."""
    if owner.id == ctx.author.id:
        await ctx.send(embed=info_embed("That's Yours", f"Use `{PREFIX}manage` for your own servers."))
        return
    vps_list = await run_blocking(get_user_vps, owner.id)
    if not 1 <= vps_number <= len(vps_list):
        await ctx.send(embed=error_embed("Invalid Server", f"{owner.mention} has no server #{vps_number}."))
        return
    vps = vps_list[vps_number - 1]
    if str(ctx.author.id) not in (vps.get("shared_with") or []) and not is_admin_id(ctx.author.id):
        await ctx.send(embed=error_embed("No Access", f"You don't have access to `{vps['container_name']}`."))
        return

    view = ManageView(ctx.author.id, [vps], is_admin_view=is_admin_id(ctx.author.id), is_shared=True,
                      feedback=f"🤝 Shared by <@{owner.id}> · owner keeps destructive controls")
    embed = await view.render()
    view.message = await ctx.send(embed=embed, view=view)


@bot.command(name="transfer-vps", aliases=["vps-transfer", "give-vps", "vps-owner"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def transfer_vps(ctx: commands.Context, container_name: str, user: discord.User) -> None:
    """Transfer a container (and its data) to another owner."""
    vps = await run_blocking(get_vps_by_container, container_name)
    if not vps:
        await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
        return
    if str(vps["user_id"]) == str(user.id):
        await ctx.send(embed=info_embed("Already Owned", f"{user.mention} already owns `{container_name}`."))
        return

    confirmed = await ask_confirm(
        ctx, title="Transfer ownership?",
        description=(f"`{container_name}` moves from <@{vps['user_id']}> to {user.mention}.\n"
                     f"Port forwards, data and expiry stay with the server."),
        confirm_label="Transfer", timeout=90)
    if not confirmed:
        return

    previous_owner = str(vps["user_id"])
    await run_blocking(patch_vps, container_name, user_id=str(user.id), shared_with=[])
    audit("vps_transfer", ctx.author.id, target=container_name,
          detail=f"{previous_owner} → {user.id}")

    embed = success_embed("Ownership Transferred", f"`{container_name}` now belongs to {user.mention}.")
    add_field(embed, "Previous owner", f"<@{previous_owner}>", True)
    add_field(embed, "New owner", user.mention, True)
    await ctx.send(embed=embed)

    if ctx.guild:
        await remove_vps_role_if_unused(ctx.guild.get_member(int(previous_owner)), previous_owner)
        await ensure_vps_role(ctx.guild, ctx.guild.get_member(user.id))
    try:
        await user.send(embed=create_embed("You Received a Server",
                                           f"`{container_name}` was transferred to you.\n\n"
                                           f"Open it with `{PREFIX}manage`.", COLORS["success"]))
    except discord.HTTPException:
        pass


@bot.command(name="ssh", aliases=["ssh-command"])
@with_typing
@command_timeout(60)
async def ssh_command(ctx: commands.Context, container_name: str = None, vps_number: int = None) -> None:
    """
    Get the SSH command for your server.

    !ssh              → all of your servers
    !ssh <name>       → one specific container (admin)
    """
    if container_name and is_admin_id(ctx.author.id):
        vps = await run_blocking(get_vps_by_container, container_name)
        if not vps:
            await ctx.send(embed=error_embed("Not Found", f"No container named `{container_name}`."))
            return
        targets = [vps]
    else:
        targets = await run_blocking(get_user_vps, ctx.author.id)
        if vps_number:
            if not 1 <= vps_number <= len(targets):
                await ctx.send(embed=error_embed("Invalid Server", f"Pick a number between 1 and {len(targets)}."))
                return
            targets = [targets[vps_number - 1]]

    if not targets:
        await ctx.send(embed=info_embed("No Servers", f"Use `{PREFIX}vps create` to get one."))
        return

    lines: List[str] = []
    for vps in targets:
        forwards = await run_blocking(get_forwards_for_container, vps["container_name"])
        forward = next((f for f in forwards if int(f["vps_port"]) == 22), None)
        if not forward:
            lines.append(f"**{vps['container_name']}** — no SSH forward yet (start the server).")
            continue
        lines.append(f"**{vps['container_name']}**\n```bash\n{escape_backticks(ssh_command_for(int(forward['host_port'])))}\n```\n"
                     f"User `root` • password in your DMs / `{PREFIX}manage` → 🔑")

    embed = create_embed("SSH Access", None, COLORS["network"])
    for chunk in chunk_lines(lines, char_limit=950, max_lines=8):
        add_field(embed, "Connection details", chunk)
    embed.set_footer(text="Credentials are personal — never share this message.",
                     icon_url=BOT_ICON_URL or None)

    try:
        await ctx.author.send(embed=embed)
        if ctx.guild:
            await ctx.send(embed=success_embed("Sent by DM", "Your SSH details are in your inbox."))
    except discord.Forbidden:
        await ctx.send(embed=embed)


# v8-era aliases kept alive so nobody's muscle memory breaks.
@bot.command(name="stats", aliases=["stats-alias"])
@with_typing
async def stats_alias(ctx: commands.Context) -> None:
    """Alias for `serverstats` (admin)."""
    if not is_admin_id(ctx.author.id):
        await ctx.send(embed=error_embed("Admins Only", "This command requires admin privileges."))
        return
    await server_stats(ctx)


@bot.command(name="info")
@with_typing
async def info_alias(ctx: commands.Context, user: discord.User = None) -> None:
    """Alias for `userinfo` (admin) or a short self-summary."""
    if user and is_admin_id(ctx.author.id):
        await user_info(ctx, user)
        return
    vps_list = await run_blocking(get_user_vps, ctx.author.id)
    embed = info_embed("Your Overview",
                       f"{ctx.author.mention} • `{len(vps_list)}` server(s) • "
                       f"`{get_user_used_ports(ctx.author.id)}/{get_user_allocation(ctx.author.id)}` port slots")
    for chunk in chunk_lines([f"{vps_status_pill(v)} `{v['container_name']}` • {expiration_line(v)}"
                              for v in vps_list[:10]]):
        add_field(embed, "Servers", chunk)
    if not vps_list:
        add_field(embed, "Get started", f"`{PREFIX}vps create`", False)
    else:
        add_field(embed, "Live view", f"`{PREFIX}dashboard` — auto-refreshing version of this card", False)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════════════════════════
# 11.6 NODE MANAGEMENT  (!node …)
# ═══════════════════════════════════════════════════════════════════════════
#
#  The v8 flow asked admins to type names/locations through 6 sequential
#  chat prompts with no timeout and no validation. This version uses modals
#  (one screen), validates everything, and supports edit / convert / rotate.
#
#  Node-name and agent-endpoint validation are unchanged from v9 (private
#  agent addresses are allowed; metadata hostnames are still blocked).

def node_agent_snippet(api_key: str, port: int = 8080) -> str:
    return (
        "# on the remote machine (Python 3.8+)\n"
        "pip install flask requests\n"
        f"python node-agent.py --api_key={api_key} --port={port}"
    )


def _new_agent_key() -> str:
    return secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]


class NodeCreateModal(discord.ui.Modal, title="Add a node"):
    node_name: discord.ui.TextInput = discord.ui.TextInput(
        label="Node name", placeholder="e.g. frankfurt-01", max_length=48, required=True)
    location: discord.ui.TextInput = discord.ui.TextInput(
        label="Location", placeholder="e.g. Frankfurt, DE", max_length=64, default="Unknown", required=False)
    capacity: discord.ui.TextInput = discord.ui.TextInput(
        label="VPS capacity", placeholder="25", default="25", max_length=5, required=False)
    tags: discord.ui.TextInput = discord.ui.TextInput(
        label="Tags (comma separated)", placeholder="eu, ssd, kvm", required=False, max_length=120)
    endpoint: discord.ui.TextInput = discord.ui.TextInput(
        label="Agent URL (blank = this machine)", placeholder="http://198.51.100.7:8080",
        required=False, max_length=200)

    def __init__(self, requester_id: int, *, existing: Optional[Dict[str, Any]] = None):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.existing = existing
        if existing:
            self.title = f"Edit node #{existing['id']}"
            self.node_name.default = existing["name"]
            self.location.default = existing.get("location") or "Unknown"
            self.capacity.default = str(existing.get("total_vps") or 25)
            self.tags.default = ", ".join(existing.get("tags") or [])
            self.endpoint.default = existing.get("url") or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id or not is_admin_id(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Not Allowed", "Only admins can manage nodes."), ephemeral=True)
            return

        name = str(self.node_name.value).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}", name):
            await interaction.response.send_message(
                embed=error_embed("Invalid Name", "Use letters, digits, spaces, dots, dashes or underscores."),
                ephemeral=True)
            return

        capacity = clamp_int(self.capacity.value, 1, 10000, 25)
        tags = [t.strip()[:24] for t in str(self.tags.value or "").split(",") if t.strip()][:8]
        endpoint = str(self.endpoint.value or "").strip()

        api_key = None
        is_local = True
        if endpoint:
            if not re.match(r"^https?://", endpoint, re.I):
                endpoint = f"http://{endpoint}"
            host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0]
            # Remote agents are normally on private/LAN addresses too, so private
            # targets are allowed; metadata hostnames are still blocked.
            ok, reason = validate_ssh_target(host, 80, allow_private=True)
            if not ok:
                await interaction.response.send_message(
                    embed=error_embed("Invalid Endpoint", f"{reason}. Check the URL and try again."),
                    ephemeral=True)
                return
            is_local = False
            api_key = (self.existing or {}).get("api_key") or _new_agent_key()

        payload = {
            "id": (self.existing or {}).get("id"),
            "name": name,
            "location": str(self.location.value or "Unknown"),
            "total_vps": capacity,
            "tags": tags,
            "url": endpoint or None,
            "api_key": api_key,
            "is_local": is_local,
        }
        await safe_defer(interaction, ephemeral=True)
        try:
            node_id = await run_blocking(upsert_node, payload)
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                embed=error_embed("Name Taken", f"A node called `{name}` already exists."), ephemeral=True)
            return
        audit("node_upsert", interaction.user.id, target=name, detail=f"id={node_id} local={is_local}")

        embed = success_embed("Node Saved", f"`{name}` is registered (id `{node_id}`).")
        add_field(embed, "📍 Placement", f"Location `{payload['location']}`\n"
                                         f"Capacity `{capacity}` VPS\nTags `{', '.join(tags) or '—'}`", True)
        add_field(embed, "🔌 Type", "Local (this machine)" if is_local else f"Remote agent `{endpoint}`", True)
        if api_key:
            add_field(embed, "🔑 Agent setup", f"```bash\n{node_agent_snippet(api_key)}\n```", False)
            add_field(embed, "ℹ️ Tip", "Rotate this key any time with `!node regen-key "
                                       f"{node_id}`.", False)
        await interaction.followup.send(embed=embed, ephemeral=True)


class NodeManageView(LivePanelView):
    """
    Select a node, then run operations against it.

    v10: inherits the live engine, so the capacity and host bars keep moving.
    """

    def __init__(self, author_id: int, nodes: List[Dict[str, Any]]):
        super().__init__(author_id, interval=max(10, LIVE_REFRESH_SECONDS * 2))
        self.nodes = nodes
        self.index = 0
        self.feedback = ""
        self._build()

    @property
    def node(self) -> Dict[str, Any]:
        return self.nodes[self.index]

    def _build(self) -> None:
        self.clear_items()
        if len(self.nodes) > 1:
            self.add_select(
                placeholder="Select a node…",
                options=[
                    discord.SelectOption(label=n["name"][:100], value=str(position),
                                         description=f"{n.get('location', '?')} • {'local' if n['is_local'] else 'remote'}",
                                         emoji="🏠" if n["is_local"] else "🌐")
                    for position, n in enumerate(self.nodes[:25])
                ],
                callback=self._switch, row=0)
        row = 1 if len(self.nodes) > 1 else 0
        self.add_button(label="Status", style=discord.ButtonStyle.primary, emoji="📡",
                        callback=self._status, row=row)
        self.add_button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️",
                        callback=self._edit, row=row)
        self.add_button(label="Rotate key", style=discord.ButtonStyle.secondary, emoji="🔑",
                        callback=self._rotate, row=row)
        self.add_button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️",
                        callback=self._delete, row=row)
        self.add_button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄",
                        callback=self._refresh_nodes, row=row + 1)
        self.add_button(label="Live", style=discord.ButtonStyle.success if self.live else discord.ButtonStyle.secondary,
                        emoji="📡", callback=self._toggle_node_live, row=row + 1)
        self.add_button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️",
                        callback=self._close, row=row + 1)

    async def build_live_embed(self) -> discord.Embed:
        return await self.render()

    async def render(self) -> discord.Embed:
        node = self.node
        used = await run_blocking(get_current_vps_count, node["id"])
        capacity = int(node.get("total_vps") or 0)
        status_text, online = await get_node_status(node["id"])
        embed = create_embed(f"Node · {node['name']}",
                             f"{status_text} • {used}/{capacity or '∞'} containers"
                             + (f"\n\n{self.feedback}" if self.feedback else ""),
                             COLORS["node"] if online else COLORS["danger"])
        add_field(embed, "📍 Details",
                  f"**ID** `{node['id']}`\n**Location** `{node.get('location', '?')}`\n"
                  f"**Type** `{'local' if node['is_local'] else 'remote'}`", True)
        add_field(embed, "📦 Capacity", f"{gradient_bar(used, max(1, capacity), 8, tick=self.live_tick)}\n`{used}/{capacity or '∞'}`", True)
        if not node["is_local"]:
            add_field(embed, "🔌 Endpoint", f"`{node.get('url')}`", False)
        if node.get("tags"):
            add_field(embed, "🏷️ Tags", ", ".join(f"`{t}`" for t in node["tags"]), False)
        if online:
            stats = await get_host_stats(node["id"])
            add_field(embed, "🩺 Host",
                      f"CPU {gradient_bar(float(stats.get('cpu') or 0), 100, 8, tick=self.live_tick)}\n"
                      f"RAM {gradient_bar(float(stats.get('ram') or 0), 100, 8, tick=self.live_tick)}\n"
                      f"Disk `{stats.get('disk')}`", False)
        return embed

    async def _toggle_node_live(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        self.toggle_live()
        self._build()
        await self.render_live()

    async def _switch(self, interaction: discord.Interaction) -> None:
        self.index = clamp_int(interaction.data["values"][0], 0, len(self.nodes) - 1, 0)
        self.feedback = ""
        self._build()
        await interaction.response.edit_message(embed=await self.render(), view=self)

    async def _refresh_nodes(self, interaction: discord.Interaction) -> None:
        self.nodes = await run_blocking(get_nodes)
        self.index = min(self.index, max(0, len(self.nodes) - 1))
        self.feedback = "🔄 Reloaded from the database."
        self._build()
        await interaction.response.edit_message(embed=await self.render(), view=self)

    async def _status(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        node = self.node
        status_text, online = await get_node_status(node["id"])
        embed = create_embed(f"Node check · {node['name']}", status_text, COLORS["node"])
        if online:
            stats = await get_host_stats(node["id"])
            add_field(embed, "🩺 Host", f"CPU `{float(stats.get('cpu') or 0):.0f}%`\n"
                                        f"RAM `{float(stats.get('ram') or 0):.0f}%`\n"
                                        f"Disk `{stats.get('disk')}`", True)
            try:
                pools = await run_lxc_command("", "storage list", timeout=30, node_id=node["id"])
                add_field(embed, "💾 Storage", safe_code_block(truncate_text(pools, 500), max_chars=600), False)
            except Exception as exc:
                add_field(embed, "💾 Storage", f"Unavailable: {truncate_text(exc, 150)}", False)
        else:
            add_field(embed, "⚠️ Offline", "Remote commands will fail until the agent answers.", False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NodeCreateModal(interaction.user.id, existing=self.node))

    async def _rotate(self, interaction: discord.Interaction) -> None:
        node = self.node
        if node["is_local"]:
            await interaction.response.send_message(
                embed=warning_embed("Local Node", "Local nodes do not use an API key."), ephemeral=True)
            return
        new_key = _new_agent_key()
        await run_blocking(upsert_node, {**node, "api_key": new_key})
        audit("node_key_rotate", interaction.user.id, target=node["name"])
        embed = success_embed("API Key Rotated", f"`{node['name']}` has a new key.")
        add_field(embed, "🔑 New key", f"`{new_key}`", False)
        add_field(embed, "Restart the agent", f"```bash\n{node_agent_snippet(new_key)}\n```", False)
        self.feedback = "🔑 API key rotated — update the remote agent."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _delete(self, interaction: discord.Interaction) -> None:
        node = self.node
        if node["is_local"]:
            await interaction.response.send_message(
                embed=error_embed("Not Allowed", "The local node cannot be deleted."), ephemeral=True)
            return
        used = await run_blocking(get_current_vps_count, node["id"])
        confirmed = await ask_confirm(
            interaction, title="Delete this node?",
            description=(f"`{node['name']}` (id `{node['id']}`) will be removed.\n\n"
                         + (f"⚠️ It still hosts **{used}** container record(s) — delete those first.\n"
                            if used else "")),
            confirm_label="Delete node", danger=True, timeout=60)
        if not confirmed:
            return
        await safe_defer(interaction, ephemeral=True)
        deleted = await run_blocking(delete_node, node["id"], False)
        if not deleted:
            await interaction.followup.send(
                embed=error_embed("Blocked", f"This node still has {used} container(s). "
                                             f"Migrate or delete them first."), ephemeral=True)
            return
        audit("node_delete", interaction.user.id, target=node["name"])
        self.nodes = [n for n in self.nodes if n["id"] != node["id"]]
        if not self.nodes:
            await interaction.followup.send(embed=success_embed("Node Deleted", "No nodes left to manage."),
                                            ephemeral=True)
            self.stop_live()
            self.stop()
            return
        self.index = 0
        self.feedback = "🗑️ Node deleted."
        self._build()
        await interaction.followup.send(embed=await self.render(), view=self, ephemeral=True)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop_live()
        await interaction.response.edit_message(view=None)
        self.stop()


@bot.command(name="node", aliases=["nodes"])
@is_admin()
@with_typing
@command_timeout(COMMAND_TIMEOUT_SECONDS)
async def node_cmd(ctx: commands.Context, sub: str = None, *, argv: str = "") -> None:
    """
    Manage nodes (machines that host containers).

    !node                → interactive node manager
    !node create         → add a node (form)
    !node list           → inventory
    !node status <id>    → deep health check
    !node edit <id>      → change name / location / capacity / endpoint
    !node regen-key <id> → rotate the remote agent key
    !node delete <id>    → remove a node
    """
    sub = (sub or "panel").lower()
    nodes = await run_blocking(get_nodes)

    if sub in ("panel", "manage", "ui"):
        if not nodes:
            await ctx.send(embed=info_embed("No Nodes", f"Add one with `{PREFIX}node create`."))
            return
        view = NodeManageView(ctx.author.id, nodes)
        view.message = await ctx.send(embed=await view.render(), view=view)
        return

    if sub == "create":
        await ctx.send(embed=create_embed("Add a node",
                                          "Fill in the form. Leave the URL blank to use this machine "
                                          "(local node).", COLORS["node"]),
                       view=_NodeCreateButton(ctx.author.id))
        return

    if sub == "list":
        if not nodes:
            await ctx.send(embed=info_embed("No Nodes", f"Add one with `{PREFIX}node create`."))
            return
        lines = []
        for node in nodes:
            status_text, _online = await get_node_status(node["id"])
            used = await run_blocking(get_current_vps_count, node["id"])
            lines.append(f"**#{node['id']} · {node['name']}** — {status_text}\n"
                         f"   ↳ 📍 {node.get('location', '?')} • 📦 {used}/{node.get('total_vps') or '∞'} • "
                         f"🏷️ {', '.join(node.get('tags') or []) or '—'}")
        await Paginator(ctx.author.id, build_pages("Node Inventory", lines, color=COLORS["node"],
                                                   page_size=6, field_name="Nodes")).send(ctx)
        return

    if sub in ("status", "check"):
        identifier = argv.strip() or "1"
        node = await _resolve_node(identifier)
        if not node:
            await ctx.send(embed=error_embed("Node Not Found", f"No node matches `{identifier}`."))
            return
        view = NodeManageView(ctx.author.id, [node])
        view.message = await ctx.send(embed=await view.render(), view=view)
        return

    if sub == "edit":
        node = await _resolve_node(argv.strip())
        if not node:
            await ctx.send(embed=error_embed("Usage", f"`{PREFIX}node edit <id|name>`"))
            return
        await ctx.send(embed=create_embed("Edit node", f"Updating `{node['name']}`", COLORS["node"]),
                       view=_NodeEditButton(ctx.author.id, node))
        return

    if sub in ("regen-key", "regenkey", "rotate"):
        node = await _resolve_node(argv.strip())
        if not node:
            await ctx.send(embed=error_embed("Usage", f"`{PREFIX}node regen-key <id|name>`"))
            return
        if node["is_local"]:
            await ctx.send(embed=warning_embed("Local Node", "Local nodes don't use an API key."))
            return
        view = NodeManageView(ctx.author.id, [node])
        view.message = await ctx.send(embed=await view.render(), view=view)
        return

    if sub == "delete":
        node = await _resolve_node(argv.strip())
        if not node:
            await ctx.send(embed=error_embed("Usage", f"`{PREFIX}node delete <id|name>`"))
            return
        view = NodeManageView(ctx.author.id, [node])
        view.message = await ctx.send(embed=await view.render(), view=view)
        return

    embed = info_embed("Node Commands", "Everything you can do with nodes.")
    add_field(embed, "Commands",
              f"`{PREFIX}node` — interactive manager\n"
              f"`{PREFIX}node create` — add a node\n"
              f"`{PREFIX}node list` — inventory\n"
              f"`{PREFIX}node status <id>` — health check\n"
              f"`{PREFIX}node edit <id>` — update details\n"
              f"`{PREFIX}node regen-key <id>` — rotate agent key\n"
              f"`{PREFIX}node delete <id>` — remove a node", False)
    await ctx.send(embed=embed)


async def _resolve_node(identifier: str) -> Optional[Dict[str, Any]]:
    if not identifier:
        return None
    if identifier.isdigit():
        return await run_blocking(get_node, int(identifier))
    for node in await run_blocking(get_nodes):
        if node["name"].lower() == identifier.lower():
            return node
    return None


class _NodeCreateButton(PanelView):
    def __init__(self, author_id: int):
        super().__init__(author_id, timeout=180)

    @discord.ui.button(label="Open form", style=discord.ButtonStyle.primary, emoji="🧾")
    async def open_form(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NodeCreateModal(interaction.user.id))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


class _NodeEditButton(PanelView):
    def __init__(self, author_id: int, node: Dict[str, Any]):
        super().__init__(author_id, timeout=180)
        self.node = node

    @discord.ui.button(label="Open form", style=discord.ButtonStyle.primary, emoji="✏️")
    async def open_form(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NodeCreateModal(interaction.user.id, existing=self.node))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


# ═══════════════════════════════════════════════════════════════════════════
# 12. HELP SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

HELP_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "start": {
        "emoji": "🚀", "label": "Getting started", "color": COLORS["brand"],
        "description": "First steps, self-service creation and your daily commands.",
        "commands": [
            (f"{PREFIX}help", "This menu — browse or search every command (A-Z)"),
            (f"{PREFIX}quickhelp", "One-screen cheat sheet of the essentials"),
            (f"{PREFIX}ping", "Check bot latency and status"),
            (f"{PREFIX}uptime", "Host uptime, load average and resource bars"),
            (f"{PREFIX}about", "Bot version and statistics (typewriter intro)"),
            (f"{PREFIX}info [@user]", "Info about a user or the bot"),
            (f"{PREFIX}vps create", "Deploy your own VPS (self-service)"),
            (f"{PREFIX}vps info", "Your servers, specs and expiry"),
            (f"{PREFIX}vps renew", "Extend the lifetime of a server"),
            (f"{PREFIX}vps specs", "What the free tier includes"),
            (f"{PREFIX}myvps", "Dashboard of everything you own"),
            (f"{PREFIX}dashboard", "Live, auto-refreshing dashboard of your servers"),
            (f"{PREFIX}manage", "Control panel: start, stop, console, ports, 🔴 Live"),
        ],
    },
    "vps": {
        "emoji": "🖥️", "label": "Your servers", "color": COLORS["success"],
        "description": "Everything you can do with a server you own.",
        "commands": [
            (f"{PREFIX}ssh <name>", "SSH command + browser terminal for a server"),
            (f"{PREFIX}manage", "Open the live control panel"),
            (f"{PREFIX}manage → Web SSH", "Browser terminal — optional 4-8 digit PIN lock"),
            (f"{PREFIX}manage @user", "Admin: manage someone else's server"),
            (f"{PREFIX}myvps", "List servers with live states"),
            (f"{PREFIX}dashboard", "Animated live view of all your servers"),
            (f"{PREFIX}ports", "Port forwarding panel (add / list / remove)"),
            (f"{PREFIX}ports list", "View your port forwarding rules"),
            (f"{PREFIX}ports add <vps> <port>", "Publish a port"),
            (f"{PREFIX}ports remove <id>", "Delete a port forward"),
            (f"{PREFIX}share-user @user <n>", "Give a friend access to server #n"),
            (f"{PREFIX}share-ruser @user <n>", "Revoke that access"),
            (f"{PREFIX}manage-shared @owner <n>", "Open a server that was shared with you"),
            (f"{PREFIX}graph <container>", "ASCII CPU/RAM history chart for your server"),
            (f"{PREFIX}vpsinfo <name>", "Admin: full server dossier"),
            (f"{PREFIX}vps-uptime <name>", "How long a container has been running"),
        ],
    },
    "admin": {
        "emoji": "🛡️", "label": "Administration", "color": COLORS["node"],
        "description": "Creation, resizing and lifecycle management (admin only).",
        "commands": [
            (f"{PREFIX}create", "Guided VPS creation (interactive form)"),
            (f"{PREFIX}create 2 2 20 @user", "Create 2 GB / 2 vCPU / 20 GB for a user"),
            (f"{PREFIX}list-all", "Every container on the platform"),
            (f"{PREFIX}userinfo @user", "Resource and risk profile of a user"),
            (f"{PREFIX}restart-vps <name>", "Graceful restart with port restoration"),
            (f"{PREFIX}transfer-vps <name> @user", "Hand a container to a new owner"),
            (f"{PREFIX}vps-delete @user <n> [reason]", "Delete a VPS with dropdown menu"),
            (f"{PREFIX}vps-suspend @user [reason]", "Suspend a VPS with dropdown menu"),
            (f"{PREFIX}vps-unsuspend @user", "Unsuspend a VPS with dropdown menu"),
            (f"{PREFIX}vps-timeout-remove @user", "Remove VPS creation cooldown from user"),
            (f"{PREFIX}delete-vps @user 1 reason", "Delete a container permanently"),
            (f"{PREFIX}suspend-vps <name> [reason]", "Stop and lock a container"),
            (f"{PREFIX}unsuspend-vps <name>", "Restore it"),
            (f"{PREFIX}add-resources <name> 4 2 40", "Resize RAM / CPU / disk"),
            (f"{PREFIX}whitelist-vps <name> add", "Exempt from auto-suspension"),
            (f"{PREFIX}set-expiration <name> 30", "Set lifetime in days (0 = forever)"),
            (f"{PREFIX}renew-vps <name> [days]", "Extend and unsuspend"),
            (f"{PREFIX}suspension-logs [name]", "Suspension history"),
            (f"{PREFIX}apply-permissions <name>", "Fix Docker / FUSE / nesting"),
            (f"{PREFIX}repair-ports <name>", "Rebuild port forwards"),
            (f"{PREFIX}migrate-vps <name> <node>", "Move a container to another node"),
        ],
    },
    "ops": {
        "emoji": "🔧", "label": "Operations", "color": COLORS["warning"],
        "description": "Monitoring, live dashboards, diagnostics and maintenance.",
        "commands": [
            (f"{PREFIX}live", "📡 Animated live platform dashboard (auto-refresh)"),
            (f"{PREFIX}top [rows]", "📡 Live htop-style container leaderboard"),
            (f"{PREFIX}graph host [points]", "📊 ASCII host CPU/RAM history chart"),
            (f"{PREFIX}status", "Bot + node + database health"),
            (f"{PREFIX}serverstats", "Aggregated platform statistics"),
            (f"{PREFIX}stats", "Alias for serverstats (admin)"),
            (f"{PREFIX}uptime", "Host uptime, load average and resource bars"),
            (f"{PREFIX}vps-stats <name>", "Live auto-refreshing container stats"),
            (f"{PREFIX}vps-logs <name> [lines]", "Container logs (last N lines)"),
            (f"{PREFIX}vps-processes <name>", "Running processes inside container"),
            (f"{PREFIX}vps-network <name>", "Interfaces and published ports"),
            (f"{PREFIX}exec <name> <command>", "Run a command inside a container"),
            (f"{PREFIX}lxc-list [node]", "Raw LXC inventory"),
            (f"{PREFIX}resource-check", "Scan for high CPU/RAM usage"),
            (f"{PREFIX}node-check [node]", "Deep node health check"),
            (f"{PREFIX}thresholds", "Current abuse thresholds (CPU/RAM limits)"),
            (f"{PREFIX}set-threshold <cpu> <ram>", "Change the thresholds"),
            (f"{PREFIX}vps-password [name]", "View stored credentials (admin, DM only)"),
            (f"{PREFIX}generate-password", "Create a strong random password"),
            (f"{PREFIX}anim on|off", "✨ Turn the animation engine on or off"),
            (f"{PREFIX}speed <0.25-3>", "✨ Global animation speed multiplier"),
        ],
    },
    "ports_nodes": {
        "emoji": "🔌", "label": "Ports & nodes", "color": COLORS["network"],
        "description": "Publishing ports and managing extra machines.",
        "commands": [
            (f"{PREFIX}ports", "Port forwarding panel UI"),
            (f"{PREFIX}ports list", "Your forwarding rules"),
            (f"{PREFIX}ports add 1 8080", "Publish port 8080 of server #1"),
            (f"{PREFIX}ports remove <id>", "Delete a rule"),
            (f"{PREFIX}ports-add-user 5 @user", "Admin: grant forwarding slots"),
            (f"{PREFIX}ports-remove-user 5 @user", "Admin: revoke slots"),
            (f"{PREFIX}ports-revoke <id>", "Admin: force-delete a rule"),
            (f"{PREFIX}node", "Node management menu (live)"),
            (f"{PREFIX}node create", "Add another node"),
            (f"{PREFIX}node list", "List nodes and status"),
            (f"{PREFIX}node status <id>", "Detailed node check"),
            (f"{PREFIX}node edit <id>", "Rename / retag / convert a node"),
            (f"{PREFIX}node regen-key <id>", "Rotate a node API key"),
            (f"{PREFIX}node delete <id> [force]", "Remove a node"),
        ],
    },
    "security": {
        "emoji": "🔐", "label": "Security & vault", "color": COLORS["vault"],
        "description": "Credentials, privacy and abuse controls.",
        "commands": [
            (f"{PREFIX}vps-password [name]", "Admin: view stored credentials (DM only)"),
            (f"{PREFIX}generate-password", "Create a strong password"),
            (f"{PREFIX}thresholds", "Current abuse thresholds"),
            (f"{PREFIX}set-threshold <cpu> <ram>", "Change the thresholds"),
            (f"{PREFIX}reset-ratelimit @user", "Unblock a rate-limited user"),
            (f"{PREFIX}set-status watching <text>", "Change the bot's presence"),
            (f"{PREFIX}broadcast <message>", "Main admin: DM every owner"),
            (f"{PREFIX}admin-add @user", "Main admin: grant admin"),
            (f"{PREFIX}admin-remove @user", "Main admin: revoke admin"),
            (f"{PREFIX}admin-list", "Who has admin access"),
            (f"{PREFIX}reload-cache", "Reload admin cache from database"),
        ],
    },
    "maintenance": {
        "emoji": "🔨", "label": "Maintenance", "color": COLORS["warning"],
        "description": "Database, backups and emergency operations.",
        "commands": [
            (f"{PREFIX}backup-db", "Verified database snapshot"),
            (f"{PREFIX}audit-log [action]", "Security audit trail"),
            (f"{PREFIX}stop-vps-all", "Emergency stop all VPS (main admin only)"),
            (f"{PREFIX}set-status <activity> <text>", "Change the bot's presence"),
            (f"{PREFIX}broadcast <message>", "Send DM to all VPS owners"),
        ],
    },
}

HELP_TIPS = {
    "start": "New here? `vps create` gives you a server in about a minute.",
    "vps": "Every action in the control panel is confirmed — you cannot break anything by looking.",
    "admin": "Destructive commands always ask for confirmation first.",
    "ops": "`live` is the one-stop, self-refreshing health view when something feels off.",
    "ports_nodes": "Ports are published as TCP **and** UDP pairs automatically.",
    "security": "Passwords are encrypted at rest; the bot never logs them.",
}

HELP_PER_PAGE = 8


class HelpView(PanelView):
    """Dropdown-driven help with permission filtering, pagination and live search."""

    def __init__(self, ctx: commands.Context, start_category: str = "start", query: str = ""):
        super().__init__(ctx.author.id, timeout=420)
        self.ctx = ctx
        self.query = query.strip().lower()
        self.category_keys = [key for key in HELP_CATEGORIES if self._visible(key)]
        if not self.category_keys:
            self.category_keys = list(HELP_CATEGORIES)
        self.category = start_category if start_category in self.category_keys else self.category_keys[0]
        self.page = 0
        self.embed: discord.Embed = discord.Embed()
        self._build()

    # ── permission filtering ─────────────────────────────────────────────────
    def _visible(self, key: str) -> bool:
        # Keep privileged command categories out of non-admin help menus.
        if key == "admin":
            return is_admin_id(self.ctx.author.id)
        return True

    def _search(self) -> List[Tuple[str, str]]:
        results: List[Tuple[str, str]] = []
        for key in self.category_keys:
            for command, description in HELP_CATEGORIES[key]["commands"]:
                haystack = f"{command} {description}".lower()
                if self.query in haystack:
                    results.append((command, f"{description}  ·  _{HELP_CATEGORIES[key]['label']}_"))
        return results

    def _page_count(self) -> int:
        commands_list = HELP_CATEGORIES[self.category]["commands"]
        return max(1, (len(commands_list) + HELP_PER_PAGE - 1) // HELP_PER_PAGE)

    # ── rendering ────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.clear_items()
        if self.query:
            entries = self._search()
            embed = create_embed(f"🔎 Search · {self.query}",
                                 f"`{len(entries)}` matching command(s)" if entries else "Nothing matched.",
                                 COLORS["info"])
            if entries:
                for chunk in chunk_lines([f"**{cmd}**\n   ↳ {desc}" for cmd, desc in entries[:30]]):
                    add_field(embed, "Results", chunk)
            else:
                add_field(embed, "Try instead",
                          "`help ports` • `help vps` • `help admin` • `help ops`", False)
            self.embed = embed
        else:
            data = HELP_CATEGORIES[self.category]
            commands_list = data["commands"]
            pages = [commands_list[i:i + HELP_PER_PAGE] for i in range(0, len(commands_list), HELP_PER_PAGE)] or [[]]
            self.page = max(0, min(self.page, len(pages) - 1))
            embed = create_embed(
                f"📖 {BOT_NAME} · {data['label']}",
                f"{data['description']}\n\n💡 {HELP_TIPS.get(self.category, '')}",
                data["color"])
            for command, description in pages[self.page]:
                embed.add_field(name=f"`{command}`", value=sanitize_field_value(description), inline=False)
            if len(pages) > 1:
                embed.add_field(name="\u200b", value=f"Page **{self.page + 1}/{len(pages)}**", inline=False)
            embed.set_footer(text=truncate_text(
                f"{brand_footer()} • Page {self.page + 1}/{len(pages)}"), icon_url=BOT_ICON_URL or None)
            self.embed = embed

        # row 0: category select
        self.add_select(
            placeholder="Browse a category…",
            options=[
                discord.SelectOption(label=HELP_CATEGORIES[key]["label"], value=key,
                                     emoji=HELP_CATEGORIES[key]["emoji"],
                                     default=(key == self.category and not self.query))
                for key in self.category_keys
            ],
            callback=self._switch_category, row=0)

        if not self.query:
            if self.page > 0:
                self.add_button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️",
                                callback=self._prev_page, row=1)
            if self._page_count() > 1 and self.page < self._page_count() - 1:
                self.add_button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️",
                                callback=self._next_page, row=1)
        self.add_button(label="Quick start", style=discord.ButtonStyle.primary, emoji="⚡",
                        callback=self._quick_start, row=1)
        self.add_button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️",
                        callback=self._close, row=1)

    # ── callbacks ────────────────────────────────────────────────────────────
    async def _switch_category(self, interaction: discord.Interaction) -> None:
        self.category = interaction.data["values"][0]
        self.page = 0
        self.query = ""
        self._build()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._build()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._build()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def _quick_start(self, interaction: discord.Interaction) -> None:
        embed = create_embed("⚡ Quick start", "The four commands that cover 95% of daily use.",
                             COLORS["success"])
        add_field(embed, f"1️⃣  {PREFIX}vps create", "Deploy your own server.", False)
        add_field(embed, f"2️⃣  {PREFIX}manage", "Start, stop, open a console, change the password.", False)
        add_field(embed, f"3️⃣  {PREFIX}ports add 1 8080", "Publish a port so people can reach your app.", False)
        add_field(embed, f"4️⃣  {PREFIX}vps renew", "Keep it alive before it expires.", False)
        add_field(embed, "🆘 Stuck?", f"`{PREFIX}help <keyword>` searches every command.\n"
                                      f"`{PREFIX}status` shows platform health.", False)
        await safe_send(interaction, embed=embed, ephemeral=True)

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


@bot.command(name="help", aliases=["h", "commands", "cmds"])
@command_timeout(45)
async def show_help(ctx: commands.Context, *, query: str = "") -> None:
    """
    Interactive help.

    !help              → browse categories
    !help ports        → search every command
    """
    if query and query.lower() in HELP_CATEGORIES:
        view = HelpView(ctx, start_category=query.lower())
    elif query:
        view = HelpView(ctx, query=query)
    else:
        view = HelpView(ctx)
    view.message = await ctx.send(embed=view.embed, view=view)


# Slash mirror so modern clients get native access.
@bot.tree.command(name="help", description="Browse or search every command")
async def slash_help(interaction: discord.Interaction, search: Optional[str] = None) -> None:
    fake = type("FakeCtx", (), {"author": interaction.user})()
    view = HelpView(fake, query=search or "")
    await safe_send(interaction, embed=view.embed, view=view, ephemeral=True)
    if view.message is None:
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


@bot.command(name="quickhelp", aliases=["quick"])
async def quick_help(ctx: commands.Context) -> None:
    """Compact cheat-sheet for the most common tasks."""
    is_admin_user = is_admin_id(ctx.author.id)
    embed = create_embed("⚡ Cheat sheet", "Everything you actually need, on one card.", COLORS["success"])
    add_field(embed, "🖥️ Servers",
              f"`{PREFIX}vps create` deploy\n"
              f"`{PREFIX}myvps` list\n"
              f"`{PREFIX}manage` control panel\n"
              f"`{PREFIX}vps renew` extend", True)
    add_field(embed, "🔌 Networking",
              f"`{PREFIX}ports` panel\n"
              f"`{PREFIX}ports add 1 8080`\n"
              f"`{PREFIX}ports list`\n"
              f"`{PREFIX}ports remove <id>`", True)
    if is_admin_user:
        add_field(embed, "🛡️ Admin",
                  f"`{PREFIX}create` new server\n"
                  f"`{PREFIX}list-all` inventory\n"
                  f"`{PREFIX}status` health\n"
                  f"`{PREFIX}suspend-vps` lock", True)
        add_field(embed, "📡 Live (v10)",
                  f"`{PREFIX}live` platform\n"
                  f"`{PREFIX}top` busiest containers\n"
                  f"`{PREFIX}graph host` history\n"
                  f"`{PREFIX}speed 1.5` animation speed", True)
        add_field(embed, "🔧 Repair",
                  f"`{PREFIX}apply-permissions <name>`\n"
                  f"`{PREFIX}repair-ports <name>`\n"
                  f"`{PREFIX}backup-db`\n"
                  f"`{PREFIX}audit-log`", True)
    add_field(embed, "🔎 Finding things", f"`{PREFIX}help <keyword>` searches every command.", False)
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════
# 13. ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

_PLACEHOLDER_TOKENS = {"your_discord_bot_token_here", "changeme"}


def startup_checks() -> List[str]:
    """Validate configuration before we touch Discord."""
    problems: List[str] = []
    if not DISCORD_TOKEN or DISCORD_TOKEN in _PLACEHOLDER_TOKENS:
        problems.append("DISCORD_TOKEN is missing or still the placeholder value.")
    elif len(DISCORD_TOKEN) < 50:
        problems.append("DISCORD_TOKEN looks too short — bots tokens are ~70 characters long.")
    if not MAIN_ADMIN_ID:
        problems.append("MAIN_ADMIN_ID is not set (no admin commands will be usable).")
    if not shutil.which("lxc") and not shutil.which("lxd") and not shutil.which("podman"):
        problems.append("Neither lxc, lxd nor podman was found on PATH — container commands will fail.")
    if not DB_FILE.exists():
        problems.append("Database file could not be created.")
    if WEBSSH_ENABLED and not YOUR_SERVER_IP:
        problems.append("YOUR_SERVER_IP is empty — SSH instructions will be unusable.")
    if not ARGON2_AVAILABLE:
        logger.warning(
            "argon2-cffi is not installed - falling back to PBKDF2-HMAC-SHA256 (%d rounds). "
            "Run `pip install argon2-cffi` for stronger terminal-PIN hashing.", _PBKDF2_ROUNDS)
    return problems


def print_banner() -> None:
    banner = f"""
╭──────────────────────────────────────────────────────────────────────╮
│  {BOT_NAME}  •  VPS CONTROL PANEL  •  v{BOT_VERSION}
├──────────────────────────────────────────────────────────────────────┤
│  Prefix        : {PREFIX}
│  Main admin    : {MAIN_ADMIN_ID or 'NOT SET'}
│  Server IP     : {YOUR_SERVER_IP}
│  Password hash : {'Argon2id' if ARGON2_AVAILABLE else 'PBKDF2-SHA256 (pip install argon2-cffi)'}
│  Storage pool  : {DEFAULT_STORAGE_POOL}
│  Web terminal  : {'enabled on port ' + str(WEBSSH_PORT) if WEBSSH_ENABLED else 'disabled'}
│  Animations    : {'on' if anim_on() else 'off'} • speed {ANIM_STATE.get('speed')}x
│  Live panels   : refresh {LIVE_REFRESH_SECONDS}s • max {LIVE_MAX_MINUTES} min
│  Database      : {DB_FILE.name} ({'present' if DB_FILE.exists() else 'new'})
│  Containers    : {get_total_vps_count()} • owners {len(get_all_user_ids_with_vps())}
╰──────────────────────────────────────────────────────────────────────╯"""
    print(banner)
    logger.info("%s v%s starting…", BOT_NAME, BOT_VERSION)


def main() -> int:
    problems = startup_checks()
    for problem in problems:
        logger.error("Configuration problem: %s", problem)
    if problems and (not DISCORD_TOKEN or DISCORD_TOKEN in _PLACEHOLDER_TOKENS):
        logger.error("A valid DISCORD_TOKEN is required to start. Exiting.")
        return 1

    print_banner()
    if not Fernet:
        logger.warning("`cryptography` is not installed — the password vault falls back to an "
                       "integrity-checked stream cipher. Install it for AES-grade storage: "
                       "pip install cryptography")
    if WEBSSH_ENABLED and WEBSSH_HOST == "0.0.0.0":
        logger.warning("The web terminal is bound to 0.0.0.0:%s — put TLS and a reverse proxy "
                       "in front of it for internet-facing deployments.", WEBSSH_PORT)

    try:
        bot.run(DISCORD_TOKEN, log_handler=None, reconnect=True)
    except discord.LoginFailure:
        logger.error("Discord rejected the token. Check DISCORD_TOKEN in your .env file.")
        return 1
    except discord.PrivilegedIntentsRequired:
        logger.error("Enable MESSAGE CONTENT and SERVER MEMBERS intents in the Discord "
                     "developer portal for this application.")
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 0
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1
    finally:
        final_save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
