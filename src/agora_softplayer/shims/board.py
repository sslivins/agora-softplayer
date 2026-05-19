"""Replacement for ``agora.shared.board`` on Windows.

The softplayer pretends to be a Raspberry Pi 5 (the multi-display target).
``hdmi_port_count()`` is driven by ``agora_softplayer.shims.available_slots()``
so the user can lie about hardware via ``--available-slots`` when testing
PR 2a's bind/unbind handlers.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from agora_softplayer.shims import available_slots


class Board(enum.StrEnum):
    ZERO_2W = "zero_2w"
    PI_4 = "pi_4"
    PI_5 = "pi_5"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HdmiPort:
    name: str
    i2c_bus: str


_HDMI_PORTS = [
    HdmiPort("HDMI-0", "softplayer"),
    HdmiPort("HDMI-1", "softplayer"),
]


def get_board() -> Board:
    return Board.PI_5


def get_i2c_bus() -> str:
    return _HDMI_PORTS[0].i2c_bus


def get_i2c_buses() -> list[HdmiPort]:
    return list(_HDMI_PORTS[: available_slots()])


def hdmi_port_count() -> int:
    return available_slots()


def supported_codecs() -> list[str]:
    return ["hevc", "h264"]


def has_wifi() -> bool:
    return False


def has_ethernet() -> bool:
    return True


def max_fps() -> int:
    return 60


def player_backend() -> str:
    return "chromium"


def alsa_card() -> str:
    return "softplayer"


def get_cpu_temp() -> float | None:
    return None
