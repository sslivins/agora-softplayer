"""Lifecycle tests for CMSRunner.

These don't require a running CMS — we monkey-patch CMSClient.run with a
sleep so the runner's start/stop/thread-management can be exercised in
isolation. End-to-end CMS validation lives in the m2-e2e-smoke step
(docker compose).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

AGORA_ROOT = Path(__file__).resolve().parent.parent / "agora"


@pytest.fixture
def shimmed_agora(tmp_path: Path):
    """Make the agora cms_client importable on Windows for this test."""
    if not AGORA_ROOT.exists():
        pytest.skip("agora submodule not checked out")
    if str(AGORA_ROOT) not in sys.path:
        sys.path.insert(0, str(AGORA_ROOT))

    import agora_softplayer.shims as shims
    shims._APPLIED = False
    shims._DATA_DIR = None
    shims.configure(data_dir=tmp_path, available_slots=1)
    shims.apply_shims()

    yield tmp_path


def test_cms_runner_starts_and_stops(shimmed_agora: Path, monkeypatch):
    from cms_client import service as cms_service

    from agora_softplayer.cms_runner import CMSRunner

    ran_event = asyncio.Event()
    stop_event = asyncio.Event()

    async def fake_run(self):
        ran_event.set()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass

    async def fake_stop(self):
        stop_event.set()

    monkeypatch.setattr(cms_service.CMSClient, "run", fake_run)
    monkeypatch.setattr(cms_service.CMSClient, "stop", fake_stop)

    runner = CMSRunner(
        cms_url="ws://localhost:8080/ws/device",
        data_dir=shimmed_agora,
        fleet_id="fleet-test",
        fleet_secret_hex="00" * 32,
    )
    runner.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ran_event.is_set():
            break
        time.sleep(0.05)
    else:
        runner.stop()
        raise AssertionError("CMSClient.run was never entered")

    runner.stop()
    assert runner._stopped.wait(timeout=5)


def test_cms_runner_settings_carry_cms_url(shimmed_agora: Path):
    from agora_softplayer.cms_runner import CMSRunner

    runner = CMSRunner(
        cms_url="ws://example.com:9000/ws/device",
        data_dir=shimmed_agora,
        fleet_id="fleet-test",
        fleet_secret_hex="ab" * 32,
        cms_transport="direct",
    )
    settings = runner._build_settings()
    assert settings.cms_url == "ws://example.com:9000/ws/device"
    assert settings.cms_transport == "direct"
    assert str(settings.agora_base) == str(shimmed_agora)
    assert settings.bootstrap_v2 is True
    assert settings.fleet_id == "fleet-test"
    assert settings.fleet_secret_hex == "ab" * 32


def test_cms_runner_stop_without_start_is_noop(shimmed_agora: Path):
    from agora_softplayer.cms_runner import CMSRunner

    runner = CMSRunner(
        cms_url="ws://localhost:8080/ws/device",
        data_dir=shimmed_agora,
        fleet_id="fleet-test",
        fleet_secret_hex="00" * 32,
    )
    runner.stop()
