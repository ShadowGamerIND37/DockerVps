"""HTTP, cookie, authorization and noVNC WebSocket tests for the PVE gateway."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from tgdbot import config
from tgdbot.web.proxmox_console import (
    SESSION_COOKIE,
    ConsoleSettings,
    ProxmoxConsoleBackend,
    create_app,
)


def pve_row(port: int = 22786) -> dict:
    return {
        "container_name": "pve-console-test",
        "docker_image": config.PROXMOX_IMAGE,
        "os_version": config.PROXMOX_IMAGE,
        "user_id": "owner-1",
        "shared_with": ["shared-1"],
        "node_id": 1,
        "ssh_host_port": port,
    }


def make_backend(port: int = 22786) -> ProxmoxConsoleBackend:
    return ProxmoxConsoleBackend(
        repo=None,
        authorize=lambda user_id, row: (
            str(user_id) == "admin-1"
            or str(user_id) == str(row.get("user_id"))
            or str(user_id) in {str(item) for item in (row.get("shared_with") or [])}
        ),
        settings=ConsoleSettings(upstream_scheme="http"),
    )


def test_token_is_proxmox_only_and_respects_owner_shared_and_admin_policy():
    backend = make_backend()
    row = pve_row()
    token = backend.issue(user_id="owner-1", vps=row)
    assert backend.issue(user_id="owner-1", vps=row) == token
    assert backend.issue(user_id="shared-1", vps=row)
    with pytest.raises(PermissionError):
        backend.issue(user_id="stranger", vps=row)
    with pytest.raises(ValueError, match="only for Proxmox"):
        backend.issue(user_id="owner-1", vps={**row, "docker_image": "ubuntu:24.04"})
    assert backend.url_for(token).startswith("http://127.0.0.1:5001/c/")
    assert "22786" not in backend.url_for(token)


def test_public_console_url_supports_a_domain_and_https_without_upstream_port():
    backend = ProxmoxConsoleBackend(
        repo=None,
        settings=ConsoleSettings(
            public_host="console.example.com",
            url_format="https://{SERVER_IP}",
            upstream_scheme="http",
        ),
    )
    token = backend.issue(user_id="owner-1", vps=pve_row())
    assert backend.url_for(token) == f"https://console.example.com/c/{token}"
    assert "22786" not in backend.url_for(token)


def test_console_public_defaults_follow_server_ip_in_a_clean_process():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PROXMOX_CONSOLE_PUBLIC_HOST", None)
    environment.pop("PROXMOX_CONSOLE_HOST", None)
    environment["YOUR_SERVER_IP"] = "public.example.com"
    result = subprocess.run(
        [sys.executable, "-c",
         "from tgdbot import config; "
         "print(config.PROXMOX_CONSOLE_HOST); "
         "print(config.PROXMOX_CONSOLE_PUBLIC_HOST)"],
        cwd=root, env=environment, check=True, capture_output=True, text=True,
    )
    assert result.stdout.splitlines() == ["0.0.0.0", "public.example.com"]


async def _upstream_app() -> web.Application:
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        response = web.Response(text="<html>native Proxmox UI + noVNC</html>",
                                content_type="text/html")
        response.headers.add(
            "Set-Cookie", "PVEAuthCookie=native-ticket; Path=/; Secure; Domain=127.0.0.1")
        return response

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        return web.json_response({
            "path": request.path,
            "query": request.query_string,
            "host": request.headers.get("Host"),
            "cookie": request.headers.get("Cookie", ""),
            "body": body.decode(),
            "csrf": request.headers.get("CSRFPreventionToken"),
        })

    async def redirect(request: web.Request) -> web.Response:
        return web.Response(status=302, headers={
            "Location": f"https://127.0.0.1:{request.transport.get_extra_info('sockname')[1]}/login?next=%2F",
        })

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=("binary",))
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await ws.send_bytes(message.data)
            elif message.type == WSMsgType.TEXT:
                await ws.send_str(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
        return ws

    app.router.add_get("/", index)
    app.router.add_route("*", "/api2/json/echo", echo)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/api2/json/nodes/pve/qemu/100/vncwebsocket", websocket)
    return app


@pytest_asyncio.fixture
async def console_setup():
    upstream = TestServer(await _upstream_app())
    await upstream.start_server()
    backend = ProxmoxConsoleBackend(
        repo=None,
        settings=ConsoleSettings(upstream_scheme="http"),
    )
    token = backend.issue(user_id="owner-1", vps=pve_row(upstream.port),
                          host_port=upstream.port)
    client = TestClient(TestServer(create_app(backend)))
    await client.start_server()
    try:
        yield client, backend, token
    finally:
        await client.close()
        await upstream.close()


@pytest.mark.asyncio
async def test_native_http_proxy_cookie_redirect_and_upstream_isolation(console_setup):
    client, _backend, token = console_setup
    response = await client.get(f"/c/{token}", allow_redirects=False)
    assert response.status == 303
    assert response.headers["Location"] == "/"
    assert SESSION_COOKIE in client.session.cookie_jar.filter_cookies(client.make_url("/"))

    # Without the gateway session, native application paths are not public.
    client.session.cookie_jar.clear()
    assert (await client.get("/")).status == 401
    response = await client.get(f"/c/{token}", allow_redirects=False)
    assert response.status == 303

    response = await client.get("/")
    assert response.status == 200
    assert "native Proxmox UI + noVNC" in await response.text()

    # Proxmox's noVNC client defaults to encrypted browser WebSockets. The
    # native query override keeps that client on ws:// for an HTTP loopback
    # gateway without changing the upstream HTTPS/WSS connection.
    novnc = await client.get(
        "/?console=kvm&vmid=100&node=pve&novnc=1", allow_redirects=False,
    )
    assert novnc.status == 307
    assert "encrypt=0" in novnc.headers["Location"]
    adapted = await client.get(novnc.headers["Location"])
    assert adapted.status == 200

    # Local HTTP links must not receive a Secure-only cookie that the browser
    # would silently discard; the upstream Domain is not copied either.
    cookie = response.headers["Set-Cookie"]
    assert "PVEAuthCookie=native-ticket" in cookie
    assert "Secure" not in cookie
    assert "Domain=" not in cookie

    response = await client.post(
        "/api2/json/echo?vmid=100", data=b"username=root%40pam",
        headers={"CSRFPreventionToken": "csrf-token", "Content-Type": "application/x-www-form-urlencoded"},
    )
    payload = json.loads(await response.text())
    assert payload["path"] == "/api2/json/echo"
    assert payload["query"] == "vmid=100"
    assert payload["body"] == "username=root%40pam"
    assert payload["csrf"] == "csrf-token"
    assert SESSION_COOKIE not in payload["cookie"]
    assert "PVEAuthCookie=native-ticket" in payload["cookie"]
    assert payload["host"].startswith("127.0.0.1:")

    response = await client.get("/redirect", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/login?next=%2F"


@pytest.mark.asyncio
async def test_binary_novnc_websocket_frames_are_relayed(console_setup):
    client, _backend, _token = console_setup
    # The fixture's token is exchanged before the WebSocket handshake.
    assert (await client.get(f"/c/{_token}", allow_redirects=False)).status == 303
    ws = await client.ws_connect(
        "/api2/json/nodes/pve/qemu/100/vncwebsocket", protocols=("binary",))
    payload = b"\x00\x01\x02novnc-boot-menu\xff"
    await ws.send_bytes(payload)
    message = await asyncio.wait_for(ws.receive(), timeout=5)
    assert message.type == WSMsgType.BINARY
    assert message.data == payload
    await ws.close()


@pytest.mark.asyncio
async def test_invalid_or_expired_console_link_is_not_an_open_proxy(console_setup):
    client, _backend, _token = console_setup
    response = await client.get("/c/not-a-real-token", allow_redirects=False)
    assert response.status == 410
    assert (await client.get("/")).status == 401
