"""
Exception hierarchy for the TGD Bot Docker infrastructure layer.

Every error raised by :mod:`tgdbot` derives from :class:`TgdBotError`, so
the Discord layer can catch infrastructure problems without ever leaking a raw
Docker SDK traceback to a customer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class TgdBotError(Exception):
    """Base class for every error raised by the infrastructure layer."""

    #: Short, customer-safe summary shown in Discord embeds.
    user_message: str = "Infrastructure operation failed."

    def __init__(self, message: str = "", *, detail: str = "", user_message: str = "") -> None:
        super().__init__(message or self.__class__.__doc__ or self.__class__.__name__)
        self.detail = detail
        if user_message:
            self.user_message = user_message


class DockerUnavailable(TgdBotError):
    """The Docker Engine could not be reached on a node."""

    user_message = "The Docker Engine on this node is unreachable."


class NodeNotFound(TgdBotError):
    """A node id/name was requested that does not exist."""

    user_message = "That node does not exist."


class NodeDisabled(TgdBotError):
    """The node exists but is disabled or out of service."""

    user_message = "That node is currently disabled."


class ImageError(TgdBotError):
    """An image could not be pulled, built or is not approved."""

    user_message = "The requested image could not be prepared."


class ImageNotApproved(ImageError):
    """An administrator has not approved this image for customer use."""

    user_message = "That image is not approved for use on this platform."


class ContainerError(TgdBotError):
    """A container level operation failed."""

    user_message = "The container operation failed."


class ContainerNotFound(ContainerError):
    """The container does not exist on the node."""

    user_message = "That VPS container does not exist."


class PortError(TgdBotError):
    """Port allocation / publishing failed."""

    user_message = "Port allocation failed."


class NoFreePort(PortError):
    """The configured host port pool is exhausted."""

    user_message = "No free host ports are available. Contact an administrator."


class PortConflict(PortError):
    """Another VPS already owns that host IP + port + protocol combination."""

    user_message = "That port is already in use on this node."


class VolumeError(TgdBotError):
    """A persistent volume operation failed."""

    user_message = "Storage volume operation failed."


class QuotaUnsupported(TgdBotError):
    """The host filesystem cannot enforce a real disk quota."""

    user_message = "This node cannot enforce hard disk quotas."


class BackupError(TgdBotError):
    """A backup or restore operation failed."""

    user_message = "The backup operation failed."


class ProvisioningError(TgdBotError):
    """Provisioning failed. ``cleanup`` describes what the rollback removed."""

    user_message = "VPS deployment failed and was rolled back."

    def __init__(self, message: str = "", *, detail: str = "", user_message: str = "",
                 cleanup: Optional[List[str]] = None,
                 cleanup_errors: Optional[List[str]] = None) -> None:
        super().__init__(message, detail=detail, user_message=user_message)
        self.cleanup: List[str] = list(cleanup or [])
        self.cleanup_errors: List[str] = list(cleanup_errors or [])


class SSHSetupError(TgdBotError):
    """SSH could not be installed, configured or reached inside a container."""

    user_message = "SSH access could not be prepared on this VPS."


class SecurityPolicyError(TgdBotError):
    """A requested operation violates the platform security policy."""

    user_message = "That operation is blocked by the platform security policy."


class QuotaSupportReport:
    """Structured description of what disk-quota enforcement a node offers."""

    __slots__ = ("supported", "method", "filesystem", "detail")

    def __init__(self, supported: bool, method: str, filesystem: str, detail: str) -> None:
        self.supported = bool(supported)
        self.method = method
        self.filesystem = filesystem
        self.detail = detail

    def as_dict(self) -> Dict[str, Any]:
        return {
            "supported": self.supported,
            "method": self.method,
            "filesystem": self.filesystem,
            "detail": self.detail,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<QuotaSupport {self.method} supported={self.supported}>"
