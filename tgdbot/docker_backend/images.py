"""
Image catalogue, pulling and administrator-approved custom images.

Everything here is **synchronous** and must be called through
:func:`tgdbot.concurrency.run_in_thread`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..errors import ImageError, ImageNotApproved
from .client import is_transient
from . import proxmox
from .security import validate_image_reference

logger = logging.getLogger("tgdbot.docker.images")


# ── catalogue ───────────────────────────────────────────────────────────────

def catalogue(custom: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Return the selectable OS list: built-in images plus approved custom ones.

    ``custom`` is normally ``repository.list_approved_images()``.
    """
    items = [
        {
            "label": spec.label,
            "value": spec.image,
            "emoji": spec.emoji,
            "family": spec.family,
            "description": spec.description,
            "features": list(spec.features),
            "custom": False,
            "kind": spec.kind,
            "restricted": spec.kind == config.WORKLOAD_PROXMOX,
        }
        for spec in config.DEFAULT_IMAGES
    ]
    seen = {item["value"] for item in items}
    for row in custom or []:
        ref = str(row.get("reference") or "").strip()
        if not ref or ref in seen or proxmox.is_proxmox_reference(ref):
            continue
        seen.add(ref)
        items.append({
            "label": str(row.get("label") or ref),
            "value": ref,
            "emoji": str(row.get("emoji") or "🐳"),
            "family": config.family_for(ref),
            "description": str(row.get("description") or "Administrator approved image"),
            "features": ["SSH"],
            "custom": True,
            "kind": config.WORKLOAD_VPS,
            "restricted": False,
        })
    return items


def resolve(image_ref: str, custom: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Resolve a reference to a catalogue entry, honouring the approval list."""
    ref = str(image_ref or "").strip()
    ok, reason = validate_image_reference(ref)
    if not ok:
        raise ImageError(reason)
    proxmox.assert_supported_reference(ref)
    for item in catalogue(custom):
        if item["value"] == ref:
            return item
    # Unknown references are never implicitly customer-selectable.  Admins
    # approve custom images explicitly through the validated /image-add path.
    raise ImageNotApproved(
        f"`{ref}` is not in the approved image list.",
        user_message="That image is not approved. Ask an administrator to add it with /image-add.",
    )


# ── pulling ─────────────────────────────────────────────────────────────────

def validate_image_platform(attrs: Dict[str, Any], reference: str) -> None:
    """Only Linux rootfs images can be bootstrapped by the VPS supervisor."""
    platform = str(attrs.get("Os") or attrs.get("OS") or "linux").lower()
    if platform != "linux":
        raise ImageError(
            f"Image {reference} targets {platform}; the VPS backend supports Linux images only.",
            user_message="Only Linux Docker images can be added as VPS operating systems.",
        )


def image_exists(client: Any, reference: str) -> bool:
    try:
        client.images.get(reference)
        return True
    except Exception:
        return False


def pull(client: Any, reference: str, *,
         progress: Optional[Callable[[str], None]] = None,
         timeout: Optional[int] = None) -> Dict[str, Any]:
    """
    Pull ``reference`` with retries.  Returns the image metadata.

    ``progress`` receives short status lines that the Discord layer can render
    on a progress panel.
    """
    deadline = time.time() + int(timeout or config.DOCKER_IMAGE_PULL_TIMEOUT)
    last_error: Optional[Exception] = None
    attempts = max(1, config.DOCKER_MAX_RETRIES)

    for attempt in range(1, attempts + 1):
        if time.time() > deadline:
            break
        try:
            if progress:
                progress(f"Pulling {reference} (attempt {attempt}/{attempts})…")
            layers = client.api.pull(reference, stream=True, decode=True)
            seen_status: set = set()
            for chunk in layers:
                status = str(chunk.get("status") or "")
                detail = str(chunk.get("progress") or chunk.get("id") or "")
                if chunk.get("error"):
                    raise ImageError(f"Pull failed: {chunk['error']}")
                marker = status.split(" ")[0]
                if marker not in seen_status and progress:
                    seen_status.add(marker)
                    progress(f"{status} {detail}".strip()[:90])
                if time.time() > deadline:
                    raise ImageError(
                        f"Pulling {reference} exceeded {config.DOCKER_IMAGE_PULL_TIMEOUT}s")
            return dict(client.images.get(reference).attrs)
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            last_error = exc  # type: ignore[assignment]
            if not is_transient(exc) or attempt >= attempts:
                break
            time.sleep(min(10.0, config.DOCKER_RETRY_BACKOFF ** attempt))

    raise ImageError(
        f"Could not pull {reference}",
        detail=str(last_error) if last_error else "timed out",
        user_message=f"`{reference}` could not be downloaded on this node.",
    )


def ensure_image(client: Any, reference: str, *,
                 progress: Optional[Callable[[str], None]] = None,
                 always_pull: Optional[bool] = None) -> Dict[str, Any]:
    """Pull only when the image is missing locally (unless forced)."""
    force = config.DOCKER_ALWAYS_PULL if always_pull is None else bool(always_pull)
    if not force and image_exists(client, reference):
        if progress:
            progress(f"{reference} is already available locally")
        return dict(client.images.get(reference).attrs)
    return pull(client, reference, progress=progress)


def list_local(client: Any) -> List[Dict[str, Any]]:
    """Every image present on the node (short summary)."""
    out: List[Dict[str, Any]] = []
    for image in client.images.list():
        tags = list(getattr(image, "tags", []) or [])
        attrs = dict(image.attrs or {})
        out.append({
            "id": str(attrs.get("Id", ""))[:19],
            "tags": tags,
            "size": int(attrs.get("Size") or 0),
            "created": attrs.get("Created"),
        })
    return out


def disk_usage(client: Any) -> Dict[str, Any]:
    """``docker system df`` for the images layer."""
    try:
        usage = client.df()
    except Exception as exc:
        logger.debug("docker df failed: %s", exc)
        return {}
    images = usage.get("Images") or []
    total = sum(int(item.get("Size") or 0) for item in images)
    reclaimable = sum(int(item.get("Size") or 0) for item in images if not item.get("Containers"))
    return {"image_count": len(images), "total_bytes": total, "reclaimable_bytes": reclaimable}


def prune_unused(client: Any) -> Dict[str, Any]:
    """Remove dangling images only — never anything a VPS still references."""
    return dict(client.images.prune(filters={"dangling": True}))
