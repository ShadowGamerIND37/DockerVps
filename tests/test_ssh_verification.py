"""Paramiko login verification: transient failures, retries and diagnostics."""

from __future__ import annotations

import paramiko

from tgdbot.docker_backend import ssh


class _Stdout:
    def read(self):
        return b"TGDBOT_SSH_OK\n0\n"


def test_transient_eof_is_retried_with_a_fresh_client(monkeypatch):
    clients = []

    class Client:
        def __init__(self):
            self.closed = False
            clients.append(self)

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **_kwargs):
            if len(clients) == 1:
                raise EOFError()

        def exec_command(self, _command, **_kwargs):
            return None, _Stdout(), None

        def close(self):
            self.closed = True

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    monkeypatch.setattr(ssh.time, "sleep", lambda _seconds: None)

    ok, detail = ssh.verify_ssh_login(
        "127.0.0.1", 2222, "root", "generated-password", retry_delay=0)

    assert ok, detail
    assert "TGDBOT_SSH_OK" in detail
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_empty_exception_message_includes_type_target_and_attempts(monkeypatch):
    attempts = []

    class Client:
        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **_kwargs):
            attempts.append(1)
            raise EOFError()

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    monkeypatch.setattr(ssh.time, "sleep", lambda _seconds: None)

    ok, detail = ssh.verify_ssh_login(
        "127.0.0.1", 2222, "root", "generated-password", attempts=2, retry_delay=0)

    assert not ok
    assert len(attempts) == 2
    assert "root@127.0.0.1:2222" in detail
    assert "EOFError" in detail
    assert "after 2 attempt(s)" in detail
    assert not detail.endswith(": ")


def test_authentication_failure_is_not_retried(monkeypatch):
    attempts = []

    class Client:
        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **_kwargs):
            attempts.append(1)
            raise paramiko.AuthenticationException("bad credentials")

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", Client)

    ok, detail = ssh.verify_ssh_login(
        "127.0.0.1", 2222, "root", "wrong-password", attempts=3, retry_delay=0)

    assert not ok
    assert len(attempts) == 1
    assert "Authentication failed" in detail
    assert "root@127.0.0.1:2222" in detail
    assert "wrong-password" not in detail
