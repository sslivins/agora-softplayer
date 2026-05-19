"""Replacement probes for ``cms_client.service`` module-level helpers."""
from __future__ import annotations

import shutil
import socket
from pathlib import Path

from agora_softplayer.shims import data_dir
from agora_softplayer.shims.identity import get_device_serial


def get_storage_mb(path: Path) -> tuple[int, int]:
    """Return ``(total_mb, used_mb)`` for the disk hosting ``path``.

    Matches the real probe's contract: cms_client computes free via
    ``total - used`` downstream.
    """
    try:
        target = Path(path) if path else data_dir()
        if not target.exists():
            target = data_dir()
        usage = shutil.disk_usage(target)
        total_mb = usage.total // (1024 * 1024)
        used_mb = usage.used // (1024 * 1024)
        return int(total_mb), int(used_mb)
    except OSError:
        return 0, 0


def get_cpu_temp() -> float | None:
    # Windows doesn't expose CPU temperature without privileged WMI/IPMI
    # access. Returning None makes the CMS heartbeat omit the field.
    return None


def is_ssh_enabled() -> bool | None:
    # The softplayer doesn't manage an SSH service.
    return None


def get_local_ip() -> str:
    """Best-effort local IP. Opens a UDP socket to a sentinel address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_device_id() -> str:
    return get_device_serial()


def get_device_type() -> str:
    return "Windows softplayer"
