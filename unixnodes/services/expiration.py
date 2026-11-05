"""
Expiration monitoring.

The Discord bot owns the *notification* side (DMs, embeds, cooldowns); this
module owns the *infrastructure* side: which VPS are due, how far away they
are, and the actual stop/restart of a container when it expires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import lifecycle

logger = logging.getLogger("unixnodes.services.expiration")


@dataclass
class ExpiryStatus:
    container_name: str
    user_id: str
    node_id: int
    expires_at: Optional[datetime]
    seconds_left: Optional[float]
    expired: bool
    warning: bool
    suspended: bool
    whitelisted: bool
    backend: str

    @property
    def actionable(self) -> bool:
        """True when the bot may suspend this VPS automatically."""
        return self.expired and not self.suspended and not self.whitelisted


def parse_expiry(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment
    return None


def evaluate(vps: Dict[str, Any], *, now: Optional[datetime] = None,
             warning_days: int = 3) -> ExpiryStatus:
    moment = parse_expiry(vps.get("expiration_date"))
    current = now or datetime.now(timezone.utc)
    seconds_left = (moment - current).total_seconds() if moment else None
    return ExpiryStatus(
        container_name=str(vps.get("container_name")),
        user_id=str(vps.get("user_id")),
        node_id=int(vps.get("node_id") or 1),
        expires_at=moment,
        seconds_left=seconds_left,
        expired=bool(moment and seconds_left is not None and seconds_left <= 0),
        warning=bool(moment and seconds_left is not None and 0 < seconds_left <= warning_days * 86400),
        suspended=bool(vps.get("suspended")),
        whitelisted=bool(vps.get("whitelisted")),
        backend=str(vps.get("backend") or "docker"),
    )


def scan(repo: Any, *, warning_days: int = 3) -> Dict[str, List[ExpiryStatus]]:
    """Bucket every VPS by expiry state."""
    buckets: Dict[str, List[ExpiryStatus]] = {"expired": [], "warning": [], "active": [], "none": []}
    for vps in repo.list_vps():
        status = evaluate(vps, warning_days=warning_days)
        if status.expires_at is None:
            buckets["none"].append(status)
        elif status.expired:
            buckets["expired"].append(status)
        elif status.warning:
            buckets["warning"].append(status)
        else:
            buckets["active"].append(status)
    return buckets


def suspend_expired(repo: Any, status: ExpiryStatus, *, reason: str) -> Dict[str, Any]:
    """Stop an expired VPS and record the suspension."""
    vps = repo.get_vps_by_container(status.container_name)
    if not vps:
        return {"ok": False, "error": "VPS record is missing"}
    if str(vps.get("backend") or "docker") != "docker":
        # Legacy LXC-era record: the Docker backend must not touch it.
        logger.warning("skipping auto-suspend of legacy record %s (backend=%s)",
                       status.container_name, vps.get("backend"))
        return {"ok": False, "error": f"legacy record ({vps.get('backend')}) — not a Docker VPS"}
    try:
        lifecycle.suspend(repo, vps, reason=reason, actor="expiration-monitor")
    except Exception as exc:
        logger.error("auto-suspend failed for %s: %s", status.container_name, exc)
        repo.patch_vps(status.container_name, suspended=True, status="stopped")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "container": status.container_name}


def renew(repo: Any, container_name: str, days: int) -> Optional[str]:
    """Extend the expiration date.  Returns the new ISO timestamp."""
    from datetime import timedelta

    if days <= 0:
        repo.patch_vps(container_name, expiration_date=None)
        return None
    new_expiry = (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat()
    repo.patch_vps(container_name, expiration_date=new_expiry)
    return new_expiry


def set_expiration(repo: Any, container_name: str, days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    return renew(repo, container_name, int(days))
