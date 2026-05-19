"""Replacement for ``agora.shared.identity`` on Windows.

Softplayer has no burnt-in serial number, so we generate one on first
run and persist it under ``data_dir/device_id`` with an ``sp-`` prefix
so the device is obviously a softplayer in CMS logs.
"""
from __future__ import annotations

import uuid

from agora_softplayer.shims import data_dir

_SERIAL: str | None = None


def _load_or_create_serial() -> str:
    global _SERIAL
    if _SERIAL is not None:
        return _SERIAL
    path = data_dir() / "device_id"
    if path.exists():
        s = path.read_text(encoding="utf-8").strip()
        if s:
            _SERIAL = s
            return s
    s = f"sp-{uuid.uuid4().hex[:12]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(s, encoding="utf-8")
    _SERIAL = s
    return s


def get_device_serial() -> str:
    return _load_or_create_serial()


def get_device_serial_suffix(length: int = 4) -> str:
    return _load_or_create_serial()[-length:].upper()
