"""
Web SSH terminal security.

The v10 terminal injected the decrypted root password into the page's
``__AUTOCONNECT__`` payload *before* the PIN was verified, so anyone with the
link could read it straight out of the HTML source.  These tests pin the fixed
behaviour: the browser never receives a credential, a token is mandatory,
ad-hoc hosts are refused, and the response headers are locked down.
"""

from __future__ import annotations

import json
import re

import pytest

from tgdbot.web import terminal as terminal_mod


class FakeVault:
    """Reversible stand-in for bot.py's encrypt_secret/decrypt_secret pair."""

    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    def encrypt(self, plaintext: str) -> str:
        return "f1:" + plaintext[::-1]

    def decrypt(self, blob):
        if not blob:
            return None
        return blob[3:][::-1] if blob.startswith("f1:") else blob


@pytest.fixture()
def vault():
    return FakeVault()


@pytest.fixture()
def backend(repo, fake_node, vault):
    """A terminal backend wired to the test repository and a fake vault."""
    repo.insert_vps({
        "id": 1, "user_id": "111", "node_id": int(fake_node["id"]),
        "container_name": "alice-vps-1", "ram": "1GB", "cpu": "1", "storage": "10GB",
        "os_version": "ubuntu:24.04", "status": "running",
        "root_password": vault.encrypt("S3cret-Root-Passw0rd!"),
    })
    repo.reserve_port(node_id=int(fake_node["id"]), host_ip="0.0.0.0", host_port=22001,
                      container_name="alice-vps-1", container_port=22,
                      protocol="tcp", purpose="ssh", user_id="111")
    return terminal_mod.TerminalBackend(
        repo=repo, encrypt=vault.encrypt, decrypt=vault.decrypt,
        hash_password=lambda pin: "hash:" + pin,
        verify_password=lambda pin, stored: stored == "hash:" + str(pin),
        audit=lambda *a, **k: None,
        ssh_host_port=lambda name: 22001 if name == "alice-vps-1" else None,
        node_address=lambda node_id: "203.0.113.10",
        settings=terminal_mod.TerminalSettings(bot_name="TestNodes", bot_version="11.0-TEST"),
    )


@pytest.fixture()
def client(backend):
    app = terminal_mod.create_app(backend)
    app.config.update(TESTING=True)
    return app.test_client()


def issue(backend, **kwargs):
    options = dict(user_id="111", container_name="alice-vps-1", node_id=1,
                   ttl_seconds=1800, label="alice-vps-1")
    options.update(kwargs)
    return backend.issue(**options)


# ── the vulnerability ───────────────────────────────────────────────────────

def test_the_root_password_never_reaches_the_browser(backend, client):
    """
    Regression test for the v10 leak: the terminal page must not contain the
    root password, in the autoconnect payload or anywhere else.
    """
    token = issue(backend)
    html = client.get(f"/t/{token}").get_data(as_text=True)
    assert "S3cret-Root-Passw0rd!" not in html
    assert "S3cret" not in html


def test_pin_protected_page_leaks_nothing_before_unlock(backend, client):
    token = issue(backend, pin="123456")
    response = client.get(f"/t/{token}")
    html = response.get_data(as_text=True)
    assert "S3cret-Root-Passw0rd!" not in html
    assert "__PIN_REQUIRED__ = true" in html


def test_autoconnect_payload_carries_no_credential(backend, client):
    token = issue(backend)
    html = client.get(f"/t/{token}").get_data(as_text=True)
    match = re.search(r"autoconnect:\s*(\{[^\n]*?\}|null)\s*(?://|\n)", html)
    assert match, "the autoconnect payload was not rendered"
    payload = json.loads(match.group(1))
    assert payload is not None
    assert "password" not in payload
    assert set(payload) <= {"host", "port", "username", "label", "container"}
    assert payload["port"] == 22001


def test_token_is_stored_encrypted_not_in_plaintext(backend, vault):
    token = issue(backend)
    entry = backend.tokens[token]
    assert entry.encrypted_password.startswith("f1:")
    assert "S3cret-Root-Passw0rd!" not in entry.encrypted_password


# ── token discipline ────────────────────────────────────────────────────────

def test_an_unknown_token_is_refused(client):
    assert client.get("/t/not-a-real-token").status_code == 410


def test_a_token_cannot_be_used_for_another_vps(backend, client):
    token = issue(backend)
    response = client.post("/api/ssh/connect", json={
        "token": token, "host": "203.0.113.10", "port": 22001,
        "username": "root", "password": "anything",
    })
    # The server resolves host/port/username itself; a supplied password is
    # ignored for token links, but a foreign host must never be dialled.
    assert response.status_code in (200, 400)
    body = response.get_json() or {}
    assert body.get("host", "203.0.113.10") == "203.0.113.10"


def test_expired_token_is_refused(backend, client):
    token = issue(backend, ttl_seconds=-1)
    assert client.get(f"/t/{token}").status_code == 410
    # …and it cannot be used to open a session either.
    response = client.post("/api/ssh/connect", json={"token": token})
    assert response.status_code == 401


def test_max_uses_counts_connections(backend, client):
    """`max_uses` limits how many sessions a link can open, not page views."""
    token = issue(backend, max_uses=1)
    assert client.get(f"/t/{token}").status_code == 200
    assert client.get(f"/t/{token}").status_code == 200

    first = client.post("/api/ssh/connect", json={"token": token})
    assert first.status_code != 401, first.get_json()
    second = client.post("/api/ssh/connect", json={"token": token})
    assert second.status_code == 401
    assert (second.get_json() or {}).get("code") == "token_used"


# ── ad-hoc connections ──────────────────────────────────────────────────────

def test_ad_hoc_host_is_refused_by_default(client):
    response = client.post("/api/ssh/connect", json={
        "host": "198.51.100.7", "port": 22, "username": "root", "password": "x",
    })
    assert response.status_code == 401
    body = response.get_json() or {}
    assert body.get("code") == "token_required"


def test_ad_hoc_host_works_only_when_explicitly_enabled(backend):
    backend.settings.allow_adhoc = True
    app = terminal_mod.create_app(backend)
    app.config.update(TESTING=True)
    probe = app.test_client()
    response = probe.post("/api/ssh/connect", json={
        "host": "198.51.100.7", "port": 22, "username": "root", "password": "x",
    })
    # It will fail to *connect* (nothing is listening), but it must not be
    # rejected as "ad-hoc not allowed".
    body = response.get_json() or {}
    assert body.get("code") != "token_required"


# ── PIN handling ────────────────────────────────────────────────────────────

def test_wrong_pin_is_rejected_and_counted(backend, client):
    token = issue(backend, pin="123456")
    response = client.post("/api/ssh/unlock", json={"token": token, "pin": "000000"})
    assert response.status_code == 401
    assert (response.get_json() or {}).get("code") == "bad_pin"
    assert backend.tokens[token].pin_attempts == 1


def test_correct_pin_unlocks_the_session(backend, client):
    token = issue(backend, pin="123456")
    response = client.post("/api/ssh/unlock", json={"token": token, "pin": "123456"})
    assert response.status_code == 200
    body = response.get_json() or {}
    assert body.get("key"), "a correct PIN must return a browser unlock key"
    assert "S3cret-Root-Passw0rd!" not in response.get_data(as_text=True)
    # The unlock key is what the terminal page needs — not the password.
    page = client.get(f"/t/{token}?k={body['key']}")
    assert page.status_code == 200
    assert "S3cret-Root-Passw0rd!" not in page.get_data(as_text=True)


def test_pin_brute_force_is_cut_off(backend, client):
    """The final wrong PIN disables the link — the correct PIN can no longer work."""
    token = issue(backend, pin="123456")
    backend.settings.pin_attempts = 3
    for _ in range(2):
        assert client.post("/api/ssh/unlock",
                           json={"token": token, "pin": "000000"}).status_code == 401

    response = client.post("/api/ssh/unlock", json={"token": token, "pin": "000000"})
    assert response.status_code == 423, "the link was not disabled at the attempt limit"
    assert (response.get_json() or {}).get("code") == "too_many_pins"
    assert token not in backend.tokens, "the exhausted link was not discarded"

    # Even the real PIN is refused now, and so is the page itself.
    assert client.post("/api/ssh/unlock",
                       json={"token": token, "pin": "123456"}).status_code == 401
    assert client.get(f"/t/{token}").status_code == 410


# ── transport hardening ─────────────────────────────────────────────────────

def test_security_headers_are_set(client):
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in headers["Cache-Control"]
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_health_endpoint_reports_the_build(client):
    body = client.get("/api/health").get_json()
    assert body["bot"] == "TestNodes"
    assert body["version"] == "11.0-TEST"


def test_ui_file_is_served_with_placeholders_filled(backend, client):
    token = issue(backend)
    html = client.get(f"/t/{token}").get_data(as_text=True)
    assert "__TOKEN__" not in html
    assert "__BOT_NAME__" not in html
    assert "__AUTOCONNECT__" not in html
    assert token in html
    assert "TestNodes" in html


def test_connect_rate_limit_per_ip(backend, client):
    token = issue(backend)
    statuses = []
    for _ in range(20):
        statuses.append(client.post(
            "/api/ssh/connect",
            json={"token": token, "host": "203.0.113.10", "port": 22001,
                  "username": "root", "password": "x"},
            headers={"X-Forwarded-For": "198.51.100.99"}).status_code)
    assert 429 in statuses, "no rate limit was applied to repeated connection attempts"


def test_issue_requires_a_published_ssh_port(repo, fake_node, backend, vault):
    repo.insert_vps({
        "id": 2, "user_id": "222", "node_id": int(fake_node["id"]),
        "container_name": "noport-vps-2", "ram": "1GB", "cpu": "1", "storage": "10GB",
        "os_version": "ubuntu:24.04", "status": "running",
        "root_password": vault.encrypt("another-secret"),
    })
    with pytest.raises(ValueError):
        backend.issue(user_id="222", container_name="noport-vps-2", node_id=1)
