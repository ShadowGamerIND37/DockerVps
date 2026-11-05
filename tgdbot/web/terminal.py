"""
Web SSH terminal (Flask + Paramiko).

This is the v10 terminal, moved out of ``bot.py`` and hardened for the Docker
backend.  The UI (``webssh.html``) and its visual design are unchanged; the
connection path is:

* a **token is mandatory**.  A browser can no longer ask the terminal to dial
  an arbitrary host — the destination, the username and the password are all
  resolved server-side from the token row, which is bound to a Discord user
  and a VPS that user owns (or administers).
* the root password is **never sent to the browser**.  It is stored encrypted
  in the token row and injected only into the Paramiko connection.
* sessions are per-token, rate limited per IP, capped globally, idle-expired
  and closed when the SSH channel dies.
* nothing Docker-related is ever exposed: no container ids, no engine
  endpoints, no credentials.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config

logger = logging.getLogger("tgdbot.web.terminal")

try:
    import paramiko
except Exception:  # pragma: no cover
    paramiko = None  # type: ignore

try:
    from flask import Flask, jsonify, request
except Exception:  # pragma: no cover
    Flask = None  # type: ignore

WEBSSH_HTML_PATH = Path(__file__).resolve().parent / "webssh.html"


@dataclass
class TerminalSettings:
    """Runtime knobs (mirrors of the WEBSSH_* environment values)."""

    host: str = "0.0.0.0"
    port: int = 5000
    server_ip: str = "127.0.0.1"
    url_format: str = "http://{SERVER_IP}:{PORT}"
    session_ttl: int = 1800
    max_sessions: int = 200
    max_sessions_per_ip: int = 8
    connect_timeout: int = 15
    default_pin: bool = False
    pin_attempts: int = 5
    #: When False (default) a browser may not dial an arbitrary host.
    allow_adhoc: bool = False
    bot_name: str = "TGD Bot"
    bot_version: str = "11.0-DOCKER"


@dataclass
class Token:
    token: str
    user_id: str
    vps_id: Optional[int]
    container_name: str
    host: str
    port: int
    username: str
    encrypted_password: str
    label: str
    created_at: datetime
    expires_at: datetime
    used: int = 0
    max_uses: int = 3
    pin_hash: Optional[str] = None
    pin_attempts: int = 0
    unlock_keys: set = field(default_factory=set)

    @property
    def pin_required(self) -> bool:
        return bool(self.pin_hash)

    def is_valid(self) -> bool:
        return datetime.now() < self.expires_at and self.used < self.max_uses

    def is_unlocked(self, key: Optional[str]) -> bool:
        import hmac

        if not self.pin_required:
            return True
        if not key:
            return False
        return any(hmac.compare_digest(str(candidate), str(key))
                   for candidate in self.unlock_keys)


@dataclass
class Session:
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
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: Optional[threading.Thread] = None

    def touch(self) -> None:
        self.last_activity = datetime.now()


class TerminalBackend:
    """
    Glue between the Flask app and the rest of the platform.

    ``bot.py`` supplies the repository, the password vault and the audit hook so
    the terminal shares one source of truth with the Discord bot.
    """

    def __init__(self, *, repo: Any, encrypt: Callable[[str], str],
                 decrypt: Callable[[Optional[str]], Optional[str]],
                 hash_password: Callable[[str], str],
                 verify_password: Callable[[str, Optional[str]], bool],
                 audit: Callable[..., None],
                 ssh_host_port: Callable[[str], Optional[int]],
                 node_address: Callable[[int], str],
                 settings: Optional[TerminalSettings] = None) -> None:
        self.repo = repo
        self.encrypt = encrypt
        self.decrypt = decrypt
        self.hash_password = hash_password
        self.verify_password = verify_password
        self.audit = audit
        self.ssh_host_port = ssh_host_port
        self.node_address = node_address
        self.settings = settings or TerminalSettings()

        self.tokens: Dict[str, Token] = {}
        self.sessions: Dict[str, Session] = {}
        self.lock = threading.RLock()
        self.attempts: Dict[str, List[float]] = {}
        self._cleanup_started = False

    # ── tokens ──────────────────────────────────────────────────────────────
    def issue(self, *, user_id: Any, container_name: str, node_id: int,
              ttl_seconds: int = 1800, label: str = "", pin: Optional[str] = None,
              password: Optional[str] = None, max_uses: int = 3) -> str:
        """Create a signed, expiring, single-VPS terminal link."""
        port = self.ssh_host_port(container_name)
        if not port:
            raise ValueError(f"{container_name} has no published SSH port")
        if password is None:
            vps = self.repo.get_vps_by_container(container_name)
            password = self.decrypt((vps or {}).get("root_password")) if vps else None
        if not password:
            raise ValueError("No stored root password for that VPS")

        vps = self.repo.get_vps_by_container(container_name)
        entry = Token(
            token=secrets.token_urlsafe(24),
            user_id=str(user_id),
            vps_id=int(vps["id"]) if vps and vps.get("id") else None,
            container_name=str(container_name),
            host=self.node_address(int(node_id)),
            port=int(port),
            username="root",
            encrypted_password=self.encrypt(password),
            label=label or container_name,
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(seconds=ttl_seconds),
            max_uses=max(1, int(max_uses)),
            pin_hash=self.hash_password(str(pin)) if pin else None,
        )
        with self.lock:
            self._prune_tokens()
            self.tokens[entry.token] = entry
        self._persist(entry)
        return entry.token

    def url_for(self, token: str) -> str:
        base = self.settings.url_format.format(
            SERVER_IP=self.settings.server_ip, PORT=self.settings.port).rstrip("/")
        return f"{base}/t/{token}"

    def _persist(self, entry: Token) -> None:
        try:
            with self.repo.write() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO webssh_tokens
                       (token, user_id, vps_id, host, port, username, password, label,
                        pin_hash, created_at, expires_at, used, max_uses, pin_attempts,
                        unlock_keys)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entry.token, entry.user_id, entry.vps_id, entry.host, entry.port,
                     entry.username, entry.encrypted_password, entry.label, entry.pin_hash,
                     entry.created_at.isoformat(), entry.expires_at.isoformat(),
                     entry.used, entry.max_uses, entry.pin_attempts,
                     json.dumps(sorted(entry.unlock_keys)[-5:])))
        except Exception as exc:
            logger.debug("could not persist terminal token: %s", exc)

    def load_tokens(self) -> int:
        """Restore still-valid tokens after a bot restart."""
        loaded = 0
        try:
            rows = self.repo.query_all(
                "SELECT token, user_id, vps_id, host, port, username, password, label, "
                "pin_hash, created_at, expires_at, used, max_uses, pin_attempts, unlock_keys "
                "FROM webssh_tokens")
        except Exception as exc:
            logger.debug("could not load terminal tokens: %s", exc)
            return 0
        with self.lock:
            for row in rows:
                try:
                    entry = Token(
                        token=row["token"], user_id=str(row["user_id"]), vps_id=row["vps_id"],
                        container_name=str(row["label"] or ""), host=str(row["host"] or ""),
                        port=int(row["port"] or 22), username=str(row["username"] or "root"),
                        encrypted_password=str(row["password"] or ""),
                        label=str(row["label"] or ""),
                        created_at=datetime.fromisoformat(row["created_at"]),
                        expires_at=datetime.fromisoformat(row["expires_at"]),
                        used=int(row["used"] or 0), max_uses=int(row["max_uses"] or 3),
                        pin_hash=row["pin_hash"], pin_attempts=int(row["pin_attempts"] or 0))
                    entry.unlock_keys = set(json.loads(row["unlock_keys"] or "[]"))
                    if entry.is_valid():
                        self.tokens[entry.token] = entry
                        loaded += 1
                    else:
                        self.repo.query_all("DELETE FROM webssh_tokens WHERE token = ?",
                                            (entry.token,))
                except Exception as exc:
                    logger.debug("skipping terminal token: %s", exc)
        logger.info("restored %d web terminal token(s)", loaded)
        return loaded

    def _prune_tokens(self) -> None:
        for key, entry in list(self.tokens.items()):
            if not entry.is_valid():
                self.tokens.pop(key, None)
                try:
                    with self.repo.write() as conn:
                        conn.execute("DELETE FROM webssh_tokens WHERE token = ?", (key,))
                except Exception:
                    pass

    # ── sessions ────────────────────────────────────────────────────────────
    def rate_ok(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            history = [stamp for stamp in self.attempts.get(ip, []) if now - stamp < 60]
            history.append(now)
            self.attempts[ip] = history
            return len(history) <= 12

    def sessions_for_ip(self, ip: str) -> int:
        return sum(1 for session in self.sessions.values()
                   if session.client_ip == ip and not session.closed)

    def close_session(self, session_id: str, reason: str = "closed") -> None:
        session = self.sessions.pop(session_id, None)
        if not session:
            return
        session.closed = True
        for closer in (session.channel, session.transport, session.ssh_client):
            try:
                closer.close()
            except Exception:
                pass
        logger.info("terminal session %s closed (%s)", session_id[:8], reason)

    def cleanup(self) -> None:
        now = datetime.now()
        with self.lock:
            for session_id, session in list(self.sessions.items()):
                idle = (now - session.last_activity).total_seconds()
                if session.closed or idle > self.settings.session_ttl:
                    self.close_session(session_id, "expired" if not session.closed else "closed")
            self._prune_tokens()

    def start_cleanup_loop(self) -> None:
        if self._cleanup_started:
            return
        self._cleanup_started = True

        def loop() -> None:
            while True:
                try:
                    self.cleanup()
                except Exception as exc:  # pragma: no cover
                    logger.debug("terminal cleanup error: %s", exc)
                time.sleep(120)

        threading.Thread(target=loop, daemon=True, name="webssh-cleanup").start()

    # ── SSH ─────────────────────────────────────────────────────────────────
    def open_ssh(self, host: str, port: int, username: str, password: str) -> Any:
        if paramiko is None:
            raise RuntimeError("paramiko is not installed")
        timeout = self.settings.connect_timeout
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                client.connect(hostname=host, port=int(port), username=username,
                               password=password, timeout=timeout, banner_timeout=timeout,
                               auth_timeout=timeout, allow_agent=False, look_for_keys=False)
                return client
            except (paramiko.SSHException, EOFError, ConnectionRefusedError, OSError) as exc:
                last = exc
                if attempt < 2:
                    time.sleep(2)
        raise last or RuntimeError("SSH connection failed")


# ── Flask application ───────────────────────────────────────────────────────

def create_app(backend: TerminalBackend) -> Any:
    """Build the Flask app.  ``backend`` carries every dependency."""
    if Flask is None:  # pragma: no cover
        raise RuntimeError("Flask is not installed")
    settings = backend.settings
    app = Flask(f"{settings.bot_name.lower()}-webssh")
    app.config.update(SECRET_KEY=secrets.token_hex(32), MAX_CONTENT_LENGTH=64 * 1024)

    def client_ip() -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote_addr or "unknown"

    def load_html() -> str:
        try:
            return WEBSSH_HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("cannot read webssh.html: %s", exc)
            return ("<!doctype html><html><body><h2>Web SSH UI file is missing</h2>"
                    "<p>Place <code>webssh.html</code> next to <code>terminal.py</code>.</p>"
                    "</body></html>")

    def render(token: str, autoconnect: Optional[Dict[str, Any]], *, unlock_key: str = "") -> str:
        payload = json.dumps(autoconnect) if autoconnect else "null"
        payload = payload.replace("</", "<\\/")
        html = load_html()
        html = html.replace("__BOT_NAME__", settings.bot_name)
        html = html.replace("__BOT_VERSION__", settings.bot_version)
        html = html.replace("__TOKEN__", token or "none")
        html = html.replace("__UNLOCK_KEY__", unlock_key or "")
        html = html.replace("__AUTOCONNECT__", payload)
        return html

    def render_pin_page(token: str, *, expired: bool = False, attempts_left: int = 5) -> str:
        html = load_html()
        html = html.replace("__BOT_NAME__", settings.bot_name)
        html = html.replace("__BOT_VERSION__", settings.bot_version)
        html = html.replace("__TOKEN__", token or "none")
        html = html.replace("__UNLOCK_KEY__", "")
        html = html.replace("__AUTOCONNECT__", "null")
        snippet = f"""
        <script>
            window.__PIN_REQUIRED__ = true;
            window.__PIN_ATTEMPTS_LEFT__ = {attempts_left};
            window.__PIN_EXPIRED__ = {str(expired).lower()};
            window.addEventListener('load', function () {{
                var pinModal = document.getElementById('pinModal');
                if (pinModal) {{ pinModal.classList.add('active');
                    var inPin = document.getElementById('inPin'); if (inPin) inPin.focus(); }}
                var note = document.getElementById('pinNote');
                if (!note) return;
                if ({attempts_left} <= 0) {{
                    note.textContent = '❌ Too many failed attempts. Session expired.';
                    note.className = 'modal-note error'; note.style.display = 'block';
                }} else if ({attempts_left} <= 2) {{
                    note.textContent = '⚠️ ' + {attempts_left} + ' attempt(s) remaining';
                    note.className = 'modal-note error'; note.style.display = 'block';
                }}
            }});
        </script>
        """
        return html.replace("</body>", snippet + "</body>")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com "
            "https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com "
            "https://cdnjs.cloudflare.com; "
            "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
            "connect-src 'self'; img-src 'self' data:; form-action 'none'; base-uri 'none'")
        return response

    @app.route("/", methods=["GET"])
    def index():
        return render("none", None)

    @app.route("/webssh.html", methods=["GET"])
    def static_html():
        return load_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/t/<token>", methods=["GET"])
    def token_terminal(token: str):
        unlock_key = str(request.args.get("k") or "")[:128]
        with backend.lock:
            entry = backend.tokens.get(token)
            if not entry or not entry.is_valid():
                if entry and entry.pin_required:
                    return render_pin_page(token), 410
                return render("none", None).replace(
                    '<h2 id="loginTitle">Connect to a server</h2>',
                    '<h2 id="loginTitle">Link expired</h2>'), 410
            if entry.pin_required and not entry.is_unlocked(unlock_key):
                return render_pin_page(token,
                                       attempts_left=settings.pin_attempts - entry.pin_attempts)
            # The browser gets enough to *display* the target — never the password.
            autoconnect = {"host": entry.host, "port": entry.port,
                           "username": entry.username, "label": entry.label}
        return render(token, autoconnect, unlock_key=unlock_key)

    @app.route("/api/health", methods=["GET"])
    def health():
        with backend.lock:
            return jsonify({"status": "ok", "bot": settings.bot_name,
                            "version": settings.bot_version,
                            "sessions": len(backend.sessions)})

    @app.route("/api/ssh/unlock", methods=["POST"])
    def unlock():
        payload = request.get_json(silent=True) or {}
        token_value = str(payload.get("token") or "")[:128]
        pin = str(payload.get("pin") or "")[:64]
        ip = client_ip()
        if not backend.rate_ok(ip):
            return jsonify({"success": False, "error": "Too many attempts, wait a minute."}), 429
        with backend.lock:
            entry = backend.tokens.get(token_value)
            if not entry or not entry.is_valid():
                return jsonify({"success": False, "error": "This terminal link has expired.",
                                "code": "token_used"}), 401
            if not entry.pin_required:
                return jsonify({"success": True, "key": ""})
            if entry.pin_attempts >= settings.pin_attempts:
                backend.tokens.pop(token_value, None)
                return jsonify({"success": False, "code": "too_many_pins",
                                "error": "Too many wrong PINs — this link is now disabled."}), 423
            if not backend.verify_password(pin, entry.pin_hash):
                entry.pin_attempts += 1
                backend._persist(entry)
                backend.audit("webssh_pin_fail", entry.user_id, target=entry.label,
                              detail=f"{settings.pin_attempts - entry.pin_attempts} left",
                              success=False)
                remaining = settings.pin_attempts - entry.pin_attempts
                if remaining <= 0:
                    backend.tokens.pop(token_value, None)
                    return jsonify({"success": False, "code": "too_many_pins",
                                    "error": "Too many wrong PINs — this link is now disabled."}), 423
                return jsonify({"success": False, "code": "bad_pin",
                                "error": f"Incorrect PIN. {remaining} attempt(s) left."}), 401
            key = secrets.token_urlsafe(18)
            entry.unlock_keys.add(key)
            if len(entry.unlock_keys) > 5:
                entry.unlock_keys = set(list(entry.unlock_keys)[-5:])
            entry.pin_attempts = 0
            backend._persist(entry)
        backend.audit("webssh_pin_ok", entry.user_id, target=entry.label)
        return jsonify({"success": True, "key": key})

    @app.route("/api/ssh/connect", methods=["POST"])
    def connect():
        payload = request.get_json(silent=True) or {}
        token_value = str(payload.get("token") or "")
        ip = client_ip()
        if not backend.rate_ok(ip):
            return jsonify({"success": False,
                            "error": "Too many connection attempts, wait a minute."}), 429

        entry: Optional[Token] = None
        if token_value and token_value != "none":
            with backend.lock:
                entry = backend.tokens.get(token_value)
                if not entry or not entry.is_valid():
                    return jsonify({"success": False, "error": "This terminal link has expired.",
                                    "code": "token_used"}), 401
                if entry.pin_required and not entry.is_unlocked(str(payload.get("unlock_key") or "")):
                    return jsonify({"success": False, "code": "pin_required",
                                    "error": "This link is PIN protected. Reload and enter the PIN."}), 401
                entry.used += 1
                backend._persist(entry)

        if entry is not None:
            # Authoritative: the token decides the destination and the credential.
            host, port, username = entry.host, entry.port, entry.username
            password = backend.decrypt(entry.encrypted_password) or ""
        else:
            if not settings.allow_adhoc:
                return jsonify({"success": False, "code": "token_required",
                                "error": "A terminal link issued by the bot is required."}), 401
            host = str(payload.get("host") or "").strip()
            port = int(payload.get("port") or 22)
            username = str(payload.get("username") or "root").strip()[:32]
            password = str(payload.get("password") or "")

        if not host or not password:
            return jsonify({"success": False, "error": "Host and password are required."}), 400

        if backend.sessions_for_ip(ip) >= settings.max_sessions_per_ip:
            return jsonify({"success": False,
                            "error": "Too many open sessions from your network."}), 429
        with backend.lock:
            if len(backend.sessions) >= settings.max_sessions:
                return jsonify({"success": False,
                                "error": "Terminal is at capacity, try again shortly."}), 503

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ssh-connect") as pool:
                future = pool.submit(backend.open_ssh, host, int(port), username, password)
                try:
                    ssh_client = future.result(timeout=settings.connect_timeout + 10)
                except FutureTimeout:
                    raise TimeoutError("SSH connection attempt timed out")
        except Exception as exc:
            name = type(exc).__name__
            if paramiko is not None and isinstance(exc, paramiko.AuthenticationException):
                backend.audit("webssh_connect", entry.user_id if entry else ip,
                              target=f"{host}:{port}", success=False, detail="auth failed")
                return jsonify({"success": False,
                                "error": "Authentication failed — check the password."}), 401
            backend.audit("webssh_connect", entry.user_id if entry else ip,
                          target=f"{host}:{port}", success=False, detail=str(exc)[:120])
            return jsonify({"success": False, "error": f"Could not connect ({name})."}), 400

        transport = ssh_client.get_transport()
        transport.set_keepalive(30)
        channel = transport.open_session()
        channel.get_pty(term="xterm-256color", width=120, height=32)
        channel.invoke_shell()

        session_id = uuid.uuid4().hex
        session = Session(session_id=session_id, token=token_value, client_ip=ip,
                          ssh_client=ssh_client, transport=transport, channel=channel,
                          host=host, port=int(port), username=username,
                          created_at=datetime.now(), last_activity=datetime.now())
        with backend.lock:
            backend.sessions[session_id] = session

        def reader() -> None:
            chan = session.channel
            try:
                while not session.closed:
                    if chan.recv_ready():
                        chunk = chan.recv(65536)
                        if not chunk:
                            break
                        with session.lock:
                            session.buffer += chunk.decode("utf-8", errors="replace")
                            if len(session.buffer) > 400_000:
                                session.buffer = session.buffer[-200_000:]
                    elif chan.exit_status_ready():
                        break
                    else:
                        time.sleep(0.02)
            except Exception as exc:
                logger.debug("terminal reader stopped: %s", exc)
            finally:
                session.closed = True

        session.reader = threading.Thread(target=reader, daemon=True,
                                          name=f"ssh-{session_id[:6]}")
        session.reader.start()

        backend.audit("webssh_connect", entry.user_id if entry else ip,
                      target=f"{username}@{host}:{port}", detail=f"session {session_id[:8]}")
        return jsonify({"success": True, "session_id": session_id,
                        "target": f"{username}@{host}:{port}"})

    def guard(payload: Dict[str, Any]) -> Optional[Any]:
        session_id = str(payload.get("session_id") or "")
        token_value = str(payload.get("token") or "")
        session = backend.sessions.get(session_id)
        if not session:
            return jsonify({"success": False, "error": "Session not found or expired."}), 401
        if session.token not in ("", "none") and token_value != session.token:
            return jsonify({"success": False, "error": "Session token mismatch."}), 401
        return None

    @app.route("/api/ssh/read", methods=["POST"])
    def read():
        payload = request.get_json(silent=True) or {}
        blocked = guard(payload)
        if blocked:
            return blocked
        session = backend.sessions.get(str(payload.get("session_id")))
        if session is None:
            return jsonify({"success": False, "error": "Session expired."}), 401
        try:
            last_index = max(0, min(10_000_000, int(payload.get("last_index") or 0)))
        except (TypeError, ValueError):
            last_index = 0
        with session.lock:
            if last_index > len(session.buffer):
                last_index = 0
            chunk = session.buffer[last_index:]
            new_index = len(session.buffer)
            closed_now = session.closed and not chunk
        session.touch()
        return jsonify({"success": True, "data": chunk, "last_index": new_index,
                        "closed": closed_now})

    @app.route("/api/ssh/write", methods=["POST"])
    def write():
        payload = request.get_json(silent=True) or {}
        blocked = guard(payload)
        if blocked:
            return blocked
        session = backend.sessions.get(str(payload.get("session_id")))
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

    @app.route("/api/ssh/resize", methods=["POST"])
    def resize():
        payload = request.get_json(silent=True) or {}
        blocked = guard(payload)
        if blocked:
            return blocked
        session = backend.sessions.get(str(payload.get("session_id")))
        if session is None:
            return jsonify({"success": False, "error": "Session expired."}), 401
        try:
            cols = max(20, min(400, int(payload.get("cols") or 120)))
            rows = max(5, min(200, int(payload.get("rows") or 32)))
            session.channel.resize_pty(width=cols, height=rows)
        except (TypeError, ValueError, Exception):
            pass
        session.touch()
        return jsonify({"success": True})

    @app.route("/api/ssh/disconnect", methods=["POST"])
    def disconnect():
        payload = request.get_json(silent=True) or {}
        session_id = str(payload.get("session_id") or "")
        session = backend.sessions.get(session_id)
        if not session:
            return jsonify({"success": True})
        if session.token not in ("", "none") and str(payload.get("token") or "") != session.token:
            return jsonify({"success": False, "error": "Session token mismatch."}), 401
        backend.close_session(session_id, reason="user requested")
        return jsonify({"success": True})

    backend.start_cleanup_loop()
    return app


def serve(backend: TerminalBackend) -> None:
    """Run the terminal in the current thread (the bot starts it in a worker)."""
    app = create_app(backend)
    settings = backend.settings
    logger.info("web terminal listening on http://%s:%s", settings.host, settings.port)
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True,
            use_reloader=False)
