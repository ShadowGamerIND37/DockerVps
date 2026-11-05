"""
TGD Bot — Docker container VPS platform.

This package holds the infrastructure half of the bot: the Docker Engine
backend, the database layer, the provisioning/lifecycle services and the web
terminal.  ``bot.py`` (the Discord interface) stays exactly where it was and
talks to this package.
"""

from __future__ import annotations

from . import config
from .errors import (
    BackupError,
    ContainerError,
    ContainerNotFound,
    DockerUnavailable,
    ImageError,
    ImageNotApproved,
    NoFreePort,
    NodeDisabled,
    NodeNotFound,
    PortConflict,
    PortError,
    ProvisioningError,
    QuotaUnsupported,
    SSHSetupError,
    SecurityPolicyError,
    TgdBotError,
    VolumeError,
)

__version__ = "11.0-DOCKER"

__all__ = [
    "config",
    "TgdBotError",
    "DockerUnavailable",
    "NodeNotFound",
    "NodeDisabled",
    "ImageError",
    "ImageNotApproved",
    "ContainerError",
    "ContainerNotFound",
    "PortError",
    "NoFreePort",
    "PortConflict",
    "VolumeError",
    "QuotaUnsupported",
    "BackupError",
    "ProvisioningError",
    "SSHSetupError",
    "SecurityPolicyError",
    "__version__",
]
