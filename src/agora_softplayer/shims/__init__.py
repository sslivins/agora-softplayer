"""Hardware/identity shims for running agora's cms_client on Windows.

The agora codebase assumes a Raspberry Pi running Linux:
    * ``shared.board`` reads ``/proc/asound``, ``/sys/class/thermal``, etc.
    * ``shared.identity`` reads ``/sys/firmware/devicetree/base/serial-number``.
    * ``cms_client.service`` module-level helpers shell out to ``sudo``,
      ``systemctl``, ``timedatectl``, ``dpkg-query``, ``iw``.

This package replaces the Linux-only bits with pure-Python Windows
equivalents. ``apply_shims()`` MUST be called exactly once, BEFORE any
``cms_client.service`` / ``shared.board`` / ``shared.identity`` import.
The softplayer ``__main__`` calls it after putting ``agora/`` on
``sys.path``.

Modelled on ``agora-device-simulator/sim/shims/`` but stripped of the
multi-instance ``DeviceProfile`` abstraction since the softplayer is a
single device.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("agora_softplayer.shims")

_APPLIED = False
_DATA_DIR: Path | None = None
_AVAILABLE_SLOTS: int = 1


def configure(*, data_dir: Path, available_slots: int) -> None:
    """Record state that the shims need before they're installed."""
    global _DATA_DIR, _AVAILABLE_SLOTS
    _DATA_DIR = data_dir
    _AVAILABLE_SLOTS = max(1, int(available_slots))


def data_dir() -> Path:
    if _DATA_DIR is None:
        raise RuntimeError(
            "agora_softplayer.shims.configure() was not called before use."
        )
    return _DATA_DIR


def available_slots() -> int:
    return _AVAILABLE_SLOTS


def apply_shims(agora_root: Path | None = None) -> None:
    """Replace Linux-only agora modules with Windows-friendly shims.

    Idempotent. Must be called after :func:`configure`. If ``agora_root``
    is provided (or the default ``<repo>/agora`` exists), it is added to
    ``sys.path`` so that ``from cms_client import service`` resolves to
    the submodule.
    """
    global _APPLIED
    if _APPLIED:
        return
    if _DATA_DIR is None:
        raise RuntimeError("call configure(data_dir=...) before apply_shims()")

    # Resolve agora_root, defaulting to <repo-root>/agora (sibling of src/).
    if agora_root is None:
        # __file__ -> <repo>/src/agora_softplayer/shims/__init__.py
        agora_root = Path(__file__).resolve().parents[3] / "agora"
    if agora_root.exists() and str(agora_root) not in sys.path:
        sys.path.insert(0, str(agora_root))

    from agora_softplayer.shims import board as shim_board
    from agora_softplayer.shims import identity as shim_identity

    sys.modules["shared.board"] = shim_board
    sys.modules["shared.identity"] = shim_identity

    # Now safe to import cms_client (it imports shared.board / shared.identity).
    from cms_client import service as cms_service

    from agora_softplayer.shims import probes

    cms_service._get_storage_mb = probes.get_storage_mb
    cms_service._get_cpu_temp = probes.get_cpu_temp
    cms_service._is_ssh_enabled = probes.is_ssh_enabled
    cms_service._get_local_ip = probes.get_local_ip
    cms_service._get_device_id = probes.get_device_id
    cms_service._get_device_type = probes.get_device_type
    cms_service.CMSClient._apply_timezone = lambda self, tz_name: None

    # Destructive directives become loud no-ops on Windows. The real
    # firmware reboots, runs an apt upgrade, etc -- none of which make
    # sense for a softplayer.
    async def _reboot_noop(self, *_args, **_kwargs):
        logger.warning("device.reboot ignored on softplayer; exiting cleanly")
        sys.exit(0)

    async def _upgrade_noop(self, *_args, **_kwargs):
        logger.warning("device.upgrade ignored on softplayer (no A/B slots)")

    if hasattr(cms_service.CMSClient, "_handle_reboot"):
        cms_service.CMSClient._handle_reboot = _reboot_noop
    if hasattr(cms_service.CMSClient, "_handle_upgrade"):
        cms_service.CMSClient._handle_upgrade = _upgrade_noop

    _APPLIED = True
    logger.info("agora shims applied (available_slots=%d)", _AVAILABLE_SLOTS)
