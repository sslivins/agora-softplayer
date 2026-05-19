"""Tests for the agora-softplayer shim layer.

These prove that:
  1. ``apply_shims()`` doesn't blow up on Windows.
  2. After shimming, ``from cms_client.service import CMSClient`` works
     (i.e. nothing in the agora import graph hits a Linux-only path
     during module load).
  3. The board / identity / probe shims return sensible values.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

AGORA_ROOT = Path(__file__).resolve().parent.parent / "agora"


@pytest.fixture(autouse=True)
def _agora_on_path():
    """Put agora/ on sys.path for this test session, then clean up."""
    inserted = False
    if str(AGORA_ROOT) not in sys.path:
        sys.path.insert(0, str(AGORA_ROOT))
        inserted = True
    yield
    if inserted:
        try:
            sys.path.remove(str(AGORA_ROOT))
        except ValueError:
            pass


def test_configure_and_apply_shims(tmp_path: Path) -> None:
    if not AGORA_ROOT.exists():
        pytest.skip("agora submodule not checked out")
    # apply_shims is global state; we have to allow re-entry in this test.
    import agora_softplayer.shims as shims
    shims._APPLIED = False
    shims._DATA_DIR = None
    shims.configure(data_dir=tmp_path, available_slots=2)
    shims.apply_shims()

    assert "shared.board" in sys.modules
    assert "shared.identity" in sys.modules

    from cms_client import service as cms_service
    assert hasattr(cms_service, "CMSClient")
    # Probes were patched in place
    assert cms_service._get_device_type() == "Windows softplayer"
    assert cms_service._is_ssh_enabled() is None
    assert cms_service._get_cpu_temp() is None
    assert cms_service._get_device_id().startswith("sp-")


def test_board_shim_uses_configured_available_slots(tmp_path: Path) -> None:
    if not AGORA_ROOT.exists():
        pytest.skip("agora submodule not checked out")
    import agora_softplayer.shims as shims
    shims._APPLIED = False
    shims._DATA_DIR = None
    shims.configure(data_dir=tmp_path, available_slots=1)
    shims.apply_shims()

    from shared import board
    assert board.hdmi_port_count() == 1
    assert len(board.get_i2c_buses()) == 1

    # Bump to 2 and re-import: shims.available_slots() reads the module
    # global so a fresh call should pick up the new value.
    shims._AVAILABLE_SLOTS = 2
    assert board.hdmi_port_count() == 2
    assert len(board.get_i2c_buses()) == 2


def test_identity_shim_persists_serial(tmp_path: Path) -> None:
    import agora_softplayer.shims as shims
    shims._APPLIED = False
    shims._DATA_DIR = None
    shims.configure(data_dir=tmp_path, available_slots=1)

    # Reset cached serial in case another test populated it.
    from agora_softplayer.shims import identity
    identity._SERIAL = None

    first = identity.get_device_serial()
    assert first.startswith("sp-")
    assert (tmp_path / "device_id").read_text(encoding="utf-8").strip() == first

    # Second call returns the same value -- persists across "reboots".
    identity._SERIAL = None
    second = identity.get_device_serial()
    assert second == first
