"""
Service layer: the transactional, node-aware operations the Discord bot calls.

Each service is synchronous and returns plain dictionaries; the async facade in
:mod:`tgdbot.platform` wraps them so the Discord event loop is never
blocked.
"""

from __future__ import annotations

from . import backup_service, cloudflared, expiration, lifecycle, node_service, provisioning

__all__ = [
    "backup_service", "cloudflared", "expiration", "lifecycle", "node_service", "provisioning",
]
