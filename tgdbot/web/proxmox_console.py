"""Token-protected gateway for the native Proxmox VE web console.

This is deliberately a reverse proxy, not a second Proxmox UI.  Proxmox already
provides the management UI, API, noVNC client and guest boot menus.  Keeping its
application at the root of this service preserves those paths while the gateway
adds short-lived console links and a loopback-only upstream default. The
browser-facing listener and advertised host are independently configurable.

The browser never receives a Proxmox password.  It logs in to Proxmox directly;
the gateway only holds an expiring, random session cookie and forwards Proxmox's
own cookies, API requests and binary WebSocket frames.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .. import config
from ..docker_backend import proxmox

logger = logging.getLogger("tgdbot.web.proxmox_console")

SESSION_COOKIE = "tgdbot_pve_session"
_CHUNK_SIZE = 64 * 1024
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
})
_SKIP_RESPONSE = _HOP_BY_HOP | {"set-cookie"}


@dataclass
class ConsoleSettings:
    """Runtime settings for the Proxmox console service."""

    host: str = "0.0.0.0"
    port: int = 5001
    #: Defaults to the configured public server address; never the upstream.
    public_host: str = config.PROXMOX_CONSOLE_PUBLIC_HOST
    url_format: str = "http://{SERVER_IP}:{PORT}"
    upstream_host: str = "127.0.0.1"
    upstream_scheme: str = "https"
    session_ttl: int = 1800
    max_sessions: int = 200


@dataclass
class ConsoleToken:
    token: str
    user_id: str
    container_name: str
    node_id: int
    host: str
    port: int
    expires_at: float


@dataclass
class ConsoleSession:
    session_id: str
    token: str
    expires_at: float


class ProxmoxConsoleBackend:
    """Issue and resolve short-lived console links without storing credentials."""

    def __init__(self, *, repo: Any,
                 authorize: Optional[Callable[[Any, Dict[str, Any]], bool]] = None,
                 upstream_host: Optional[Callable[[int], str]] = None,
                 audit: Optional[Callable[..., None]] = None,
                 settings: Optional[ConsoleSettings] = None) -> None:
        self.repo = repo
        self.authorize = authorize
        self.upstream_host = upstream_host
        self.audit = audit or (lambda *args, **kwargs: None)
        self.settings = settings or ConsoleSettings()
        self.tokens: Dict[str, ConsoleToken] = {}
        self.sessions: Dict[str, ConsoleSession] = {}
        # Discord issues links on the bot thread; aiohttp consumes them on its
        # own thread. Keep token exchange and session lookup atomic across both.
        self.lock = threading.RLock()

    def _prune(self) -> None:
        with self.lock:
            now = time.time()
            expired = {key for key, entry in self.tokens.items() if entry.expires_at <= now}
            for key in expired:
                self.tokens.pop(key, None)
            for key, session in list(self.sessions.items()):
                if session.expires_at <= now or session.token in expired:
                    self.sessions.pop(key, None)

    def _allowed(self, user_id: Any, vps: Dict[str, Any]) -> bool:
        if self.authorize is not None:
            return bool(self.authorize(user_id, vps))
        return str(vps.get("user_id")) == str(user_id)

    def _port_for(self, vps: Dict[str, Any], supplied: Optional[int]) -> int:
        if supplied:
            return int(supplied)
        if vps.get("ssh_host_port"):
            return int(vps["ssh_host_port"])
        name = str(vps.get("container_name") or "")
        if self.repo is not None and name:
            for row in self.repo.active_ports_for_container(name):
                if int(row["container_port"]) == config.PROXMOX_CONTAINER_PORT:
                    return int(row["host_port"])
        raise ValueError(f"{name or 'Proxmox VPS'} has no published HTTPS console port")

    def _host_for(self, node_id: int) -> str:
        host = (self.upstream_host(node_id) if self.upstream_host
                else self.settings.upstream_host)
        host = str(host or "").strip()
        if not host or "://" in host or "/" in host:
            raise ValueError("The Proxmox console upstream host is invalid")
        return host

    def issue(self, *, user_id: Any, vps: Optional[Dict[str, Any]] = None,
              container_name: Optional[str] = None, node_id: Optional[int] = None,
              host_port: Optional[int] = None, ttl_seconds: Optional[int] = None) -> str:
        """Create (or reuse) a short-lived link for one authorized Proxmox VPS."""
        record = vps
        if record is None and self.repo is not None and container_name:
            record = self.repo.get_vps_by_container(container_name)
        if not record:
            raise ValueError("Proxmox console token needs a VPS record")
        if user_id is None or not str(user_id).strip():
            raise PermissionError("A user is required to open the Proxmox console")
        image = record.get("docker_image") or record.get("os_version")
        if not proxmox.is_image(image):
            raise ValueError("The browser console gateway is only for Proxmox VE")
        if not self._allowed(user_id, record):
            raise PermissionError("You are not allowed to open this Proxmox console")

        name = str(record.get("container_name") or container_name or "")
        node = int(node_id or record.get("node_id") or 1)
        port = self._port_for(record, host_port)
        host = self._host_for(node)
        ttl = int(ttl_seconds or self.settings.session_ttl)
        with self.lock:
            now = time.time()
            self._prune()
            # Rendering a management panel repeatedly must not create a new
            # token on every refresh. Reuse the same live link for this user
            # and endpoint when the caller uses the normal configured TTL.
            if ttl_seconds is None:
                for entry in self.tokens.values():
                    if (entry.user_id == str(user_id) and entry.container_name == name
                            and entry.node_id == node and entry.host == host
                            and entry.port == port):
                        return entry.token
            if len(self.tokens) >= self.settings.max_sessions:
                raise RuntimeError("Too many active Proxmox console links")
            token = secrets.token_urlsafe(32)
            self.tokens[token] = ConsoleToken(
                token=token, user_id=str(user_id), container_name=name, node_id=node,
                host=host, port=port, expires_at=now + max(1, ttl))
            return token

    def exchange(self, token: str) -> Optional[str]:
        """Exchange a URL token for a separate browser-session identifier."""
        with self.lock:
            self._prune()
            entry = self.tokens.get(str(token or ""))
            if entry is None:
                return None
            if len(self.sessions) >= self.settings.max_sessions:
                raise RuntimeError("Too many active Proxmox console sessions")
            session_id = secrets.token_urlsafe(32)
            self.sessions[session_id] = ConsoleSession(
                session_id=session_id, token=entry.token, expires_at=entry.expires_at)
            return session_id

    def session(self, session_id: str) -> Optional[ConsoleToken]:
        with self.lock:
            self._prune()
            session = self.sessions.get(str(session_id or ""))
            if session is None:
                return None
            return self.tokens.get(session.token)

    def url_for(self, token: str) -> str:
        base = self.settings.url_format.format(
            SERVER_IP=self.settings.public_host, PORT=self.settings.port).rstrip("/")
        return f"{base}/c/{token}"


def _host_with_brackets(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _upstream_origin(entry: ConsoleToken, settings: ConsoleSettings) -> str:
    return (f"{settings.upstream_scheme}://{_host_with_brackets(entry.host)}:"
            f"{int(entry.port)}")


def _upstream_url(request: web.Request, entry: ConsoleToken,
                  settings: ConsoleSettings, *, websocket: bool = False) -> str:
    origin = _upstream_origin(entry, settings)
    if websocket:
        origin = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    # Use the original request target so encoded storage IDs and ticket query
    # values are not decoded/re-encoded before they reach pveproxy.
    path_qs = request.raw_path or "/"
    if not path_qs.startswith("/"):
        path_qs = "/" + path_qs
    return origin + path_qs


def _relative_location(value: str) -> str:
    """Keep upstream absolute redirects on the gateway origin."""
    parsed = urlsplit(value)
    if parsed.netloc:
        return urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    return value


def _request_headers(request: web.Request, entry: ConsoleToken,
                     settings: ConsoleSettings) -> Dict[str, str]:
    origin = _upstream_origin(entry, settings)
    headers: Dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if (lower in _HOP_BY_HOP or lower == "cookie"
                or lower.startswith("sec-websocket-")
                or lower.startswith("x-forwarded-")):
            continue
        headers[key] = value
    headers["Host"] = f"{_host_with_brackets(entry.host)}:{int(entry.port)}"
    # Preserve a known length while streaming large multipart ISO uploads. An
    # async generator without this header would otherwise force chunked upload.
    if request.content_length is not None:
        headers["Content-Length"] = str(request.content_length)
    # The gateway cookie is only for this service. Never leak it to Proxmox.
    cookies = [f"{key}={value}" for key, value in request.cookies.items()
               if key != SESSION_COOKIE]
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    if "Origin" in request.headers:
        headers["Origin"] = origin
    if "Referer" in request.headers:
        relative = _relative_location(request.headers["Referer"])
        headers["Referer"] = origin + (relative if relative.startswith("/") else "/" + relative)
    return headers


def _response_cookie(value: str, *, secure: bool) -> str:
    parts = []
    for part in value.split(";"):
        stripped = part.strip()
        lower = stripped.lower()
        if not stripped or lower.startswith("domain="):
            continue
        if lower == "secure" and not secure:
            continue
        if lower == "samesite=none" and not secure:
            # Browsers reject SameSite=None without Secure on local HTTP.
            parts.append("SameSite=Lax")
            continue
        parts.append(stripped)
    return "; ".join(parts)


def _browser_secure(request: web.Request) -> bool:
    settings = request.app["console_settings"]
    return request.secure or str(settings.url_format).lower().startswith("https://")


def _novnc_http_redirect(request: web.Request) -> Optional[web.Response]:
    """Make Proxmox's HTTPS-oriented noVNC client work on local HTTP.

    The pinned Proxmox noVNC client defaults its ``encrypt`` setting to true,
    but its native query settings deliberately allow ``encrypt=0``. Add that
    setting to the iframe URL instead of patching the upstream JavaScript.
    """
    if (_browser_secure(request) or request.method != "GET"
            or request.path != "/" or "novnc" not in request.query
            or "encrypt" in request.query):
        return None
    query = request.query.copy()
    query.add("encrypt", "0")
    location = str(request.rel_url.with_query(query))
    return web.Response(status=307, headers={
        "Location": location, "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    })


def _error(status: int, message: str) -> web.Response:
    return web.Response(status=status, text=message, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


async def _proxy_http(request: web.Request, entry: ConsoleToken) -> web.StreamResponse:
    settings = request.app["console_settings"]
    client: ClientSession = request.app["client_session"]
    url = _upstream_url(request, entry, settings)
    headers = _request_headers(request, entry, settings)
    body = request.content.iter_chunked(_CHUNK_SIZE) if request.can_read_body else None
    try:
        async with client.request(
                request.method, url, headers=headers, data=body,
                allow_redirects=False, ssl=False) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                lower = key.lower()
                if lower in _SKIP_RESPONSE:
                    continue
                response.headers[key] = _relative_location(value) if lower == "location" else value
            for value in upstream.headers.getall("Set-Cookie", []):
                response.headers.add(
                    "Set-Cookie", _response_cookie(value, secure=_browser_secure(request)))
            response.headers["Referrer-Policy"] = "no-referrer"
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(_CHUNK_SIZE):
                await response.write(chunk)
            await response.write_eof()
            return response
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Proxmox HTTP upstream request failed: %s", exc)
        # Once headers/body bytes have reached the browser, a second Response
        # cannot be returned. Close that stream instead of masking the error.
        if "response" in locals() and response.prepared:
            try:
                await response.write_eof()
            except Exception:
                pass
            return response
        return _error(502, "The Proxmox console is unavailable. Is the VPS running?")


def _websocket_protocols(request: web.Request) -> tuple:
    """Preserve the native client's offered subprotocols (usually ``binary``)."""
    offered = request.headers.get("Sec-WebSocket-Protocol", "")
    return tuple(part.strip() for part in offered.split(",") if part.strip())


async def _proxy_websocket(request: web.Request, entry: ConsoleToken) -> web.StreamResponse:
    settings = request.app["console_settings"]
    client: ClientSession = request.app["client_session"]
    url = _upstream_url(request, entry, settings, websocket=True)
    headers = _request_headers(request, entry, settings)
    protocols = _websocket_protocols(request)
    try:
        upstream = await client.ws_connect(
            url, headers=headers, protocols=protocols, ssl=False, heartbeat=30)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Proxmox WebSocket upstream connection failed: %s", exc)
        return _error(502, "The Proxmox VM console is unavailable.")

    browser = web.WebSocketResponse(protocols=protocols, heartbeat=30)
    await browser.prepare(request)

    async def browser_to_upstream() -> None:
        async for message in browser:
            if message.type == WSMsgType.BINARY:
                await upstream.send_bytes(message.data)
            elif message.type == WSMsgType.TEXT:
                await upstream.send_str(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                break

    async def upstream_to_browser() -> None:
        async for message in upstream:
            if message.type == WSMsgType.BINARY:
                await browser.send_bytes(message.data)
            elif message.type == WSMsgType.TEXT:
                await browser.send_str(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                break

    tasks = [asyncio.create_task(browser_to_upstream()),
             asyncio.create_task(upstream_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        await browser.close()
    return browser


def create_app(backend: ProxmoxConsoleBackend) -> web.Application:
    """Create the root-path native Proxmox proxy application."""
    # Proxmox's own UI can upload installation ISOs. Do not impose aiohttp's
    # default 1 MiB request limit; the request body is streamed to the upstream.
    app = web.Application(client_max_size=0)
    app["console_backend"] = backend
    app["console_settings"] = backend.settings

    async def startup(application: web.Application) -> None:
        application["client_session"] = ClientSession(
            timeout=ClientTimeout(total=None, connect=10, sock_connect=10),
            auto_decompress=False,
        )

    async def cleanup(application: web.Application) -> None:
        session = application.get("client_session")
        if session is not None:
            await session.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    async def exchange(request: web.Request) -> web.Response:
        try:
            session_id = backend.exchange(request.match_info["token"])
        except RuntimeError as exc:
            return _error(503, str(exc))
        if session_id is None:
            return _error(410, "This Proxmox console link has expired. Open a new one from the bot.")
        response = web.Response(status=303, headers={
            "Location": "/", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        })
        response.set_cookie(
            SESSION_COOKIE, session_id, path="/", max_age=backend.settings.session_ttl,
            httponly=True, secure=_browser_secure(request), samesite="Lax")
        return response

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "proxmox-console"})

    async def proxy(request: web.Request) -> web.StreamResponse:
        entry = backend.session(request.cookies.get(SESSION_COOKIE, ""))
        if entry is None:
            return _error(401, "Open a fresh Proxmox console link from the bot first.")
        novnc_redirect = _novnc_http_redirect(request)
        if novnc_redirect is not None:
            return novnc_redirect
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(request, entry)
        return await _proxy_http(request, entry)

    app.router.add_get("/c/{token}", exchange)
    app.router.add_get("/api/health", health)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


async def run(app: web.Application, host: str, port: int) -> None:
    """Run the service until its hosting thread's event loop is cancelled."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=int(port))
    try:
        await site.start()
        logger.info("Proxmox console listening on http://%s:%s", host, port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
