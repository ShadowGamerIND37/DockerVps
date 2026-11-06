"""Cloudflare quick-tunnel setup for Proxmox VE VPS instances.

The commands run *inside* the selected Proxmox container through the normal
node-aware lifecycle executor.  No package installation or tunnel process is
run on the bot host.  The public URL is parsed conservatively from cloudflared's
log so an unrelated or malformed log line cannot become a clickable link.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from ..docker_backend import proxmox
from ..errors import ContainerError
from . import lifecycle

Progress = Callable[[int, str, str], None]

SETUP_STEPS = (
    "Update package lists",
    "Install sudo and Cloudflare repository",
    "Install cloudflared",
    "Start Cloudflare quick tunnel",
    "Find quick tunnel URL",
)

LOG_PATH = "/root/cloudflared.log"
_URL_RE = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def quick_tunnel_url(log_text: Any) -> Optional[str]:
    """Return the first safe HTTPS quick-tunnel URL in cloudflared output."""
    text = _ANSI_RE.sub("", str(log_text or ""))
    for match in _URL_RE.finditer(text):
        # Log timestamps and punctuation are not part of the URL.
        candidate = match.group(0).rstrip(".,;:!?)]}")
        try:
            parsed = urlsplit(candidate)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            continue
        if (parsed.scheme.lower() == "https"
                and hostname.endswith(".trycloudflare.com")
                and not parsed.username and not parsed.password
                and port in (None, 443)
                and parsed.path in ("", "/")
                and not parsed.query and not parsed.fragment):
            return candidate
    return None


def _report(progress: Optional[Progress], index: int, state: str, detail: str = "") -> None:
    if progress is not None:
        progress(index, state, detail)


def _run(repo: Any, vps: Dict[str, Any], command: str, label: str,
         *, timeout: int = 600) -> Tuple[str, str]:
    """Run one setup command and turn a non-zero exit into a useful error."""
    code, stdout, stderr = lifecycle.exec_command(repo, vps, command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "no command output").strip()
        raise ContainerError(f"{label} failed (exit {code}): {detail[-700:]}")
    return stdout, stderr


def _stage(repo: Any, vps: Dict[str, Any], progress: Optional[Progress],
           index: int, command: str, detail: str) -> None:
    _report(progress, index, "running", detail)
    _run(repo, vps, command, SETUP_STEPS[index])
    _report(progress, index, "done", detail)


def setup_quick_tunnel(repo: Any, vps: Dict[str, Any], *,
                       progress: Optional[Progress] = None,
                       tunnel_timeout: float = 60.0,
                       poll_interval: float = 2.0) -> Dict[str, str]:
    """Install cloudflared and start a quick tunnel to local Proxmox HTTPS.

    The returned URL is public by design.  Proxmox still requires its own
    login; the URL is delivered only through the authorized Discord workflow.
    """
    image = vps.get("docker_image") or vps.get("os_version")
    if not proxmox.is_image(image):
        raise ContainerError("Cloudflare setup is available only for Proxmox VE VPS instances.")
    if vps.get("suspended"):
        raise ContainerError("Unsuspend and start the Proxmox VPS before creating a Cloudflare tunnel.")
    if lifecycle.state(repo, vps) != "running":
        raise ContainerError("Start the Proxmox VPS before creating a Cloudflare tunnel.")

    _stage(repo, vps, progress, 0, "apt update", "Running apt update")
    _stage(
        repo, vps, progress, 1,
        "set -e\n"
        "apt install sudo -y\n"
        "sudo mkdir -p --mode=0755 /usr/share/keyrings\n"
        "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | "
        "sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null\n"
        "echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] "
        "https://pkg.cloudflare.com/cloudflared any main' | "
        "sudo tee /etc/apt/sources.list.d/cloudflared.list",
        "Installing sudo and adding the Cloudflare repository",
    )
    _stage(
        repo, vps, progress, 2,
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y cloudflared",
        "Installing the cloudflared package",
    )
    _stage(
        repo, vps, progress, 3,
        "set -e\n"
        f"rm -f {LOG_PATH}\n"
        "nohup cloudflared tunnel --url https://localhost:8006 --no-tls-verify "
        f"> {LOG_PATH} 2>&1 &",
        "Starting cloudflared in the background",
    )

    _report(progress, 4, "running", "Waiting for the trycloudflare.com URL")
    deadline = time.monotonic() + max(0.0, float(tunnel_timeout))
    last_log = ""
    while True:
        # The log may not exist for the first few polls; that is normal while
        # cloudflared negotiates the quick tunnel.
        code, stdout, stderr = lifecycle.exec_command(
            repo, vps, f"cat {LOG_PATH}", timeout=30)
        last_log = (stdout or stderr or "").strip()
        if code == 0:
            url = quick_tunnel_url(last_log)
            if url:
                _report(progress, 4, "done", "Quick tunnel URL received")
                return {"url": url, "log": last_log}
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.0, float(poll_interval)))

    raise ContainerError(
        "cloudflared started but no quick tunnel URL appeared in "
        f"{LOG_PATH} within {int(max(0.0, float(tunnel_timeout)))} seconds",
        detail=last_log[-1000:],
    )
