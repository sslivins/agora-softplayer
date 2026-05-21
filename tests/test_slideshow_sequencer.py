"""Tests for ``agora_softplayer._slideshow.SlideshowSequencer``.

Uses a fake :class:`threading.Timer` (``FakeTimer``) so slide-advance
timers can be fired synchronously without real clocks. Mocks
``ChromiumPlayer`` as a :class:`unittest.mock.MagicMock` and asserts
on the dispatched calls.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agora_softplayer import _slideshow
from agora_softplayer._slideshow import (
    DEFAULT_SLIDE_DURATION_MS,
    DEFAULT_TRANSITION,
    DEFAULT_TRANSITION_MS,
    SlideshowSequencer,
)

# -- Fake timer infrastructure -------------------------------------------------


class FakeTimer:
    """Stand-in for :class:`threading.Timer` that captures the callback.

    Tracks all instances on a class-level list so tests can address
    them in construction order. ``fire()`` invokes the captured
    function synchronously (no thread), which lets us exercise the
    full state machine without sleeps.
    """

    instances: list[FakeTimer] = []

    def __init__(self, interval: float, fn, args=(), kwargs=None) -> None:
        self.interval = interval
        self.fn = fn
        self.args = tuple(args or ())
        self.kwargs = dict(kwargs or {})
        self.started = False
        self.cancelled = False
        self.daemon = False
        FakeTimer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        """Synchronously invoke the captured callback."""
        if self.cancelled:
            raise RuntimeError("FakeTimer.fire() called on cancelled timer")
        self.fn(*self.args, **self.kwargs)


@pytest.fixture(autouse=True)
def fake_timer(monkeypatch):
    FakeTimer.instances = []
    monkeypatch.setattr(_slideshow, "Timer", FakeTimer)
    yield FakeTimer
    FakeTimer.instances = []


# -- Helpers -------------------------------------------------------------------


def _write_manifest(assets_dir: Path, name: str, slides: list[dict]) -> Path:
    """Write a slideshow manifest. ``slides`` is the raw per-slide dict
    list -- the wrapper ``{"name", "checksum", "slides": ...}`` is
    added here so tests can stay readable."""
    sdir = assets_dir / "slideshows"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{name}.json"
    path.write_text(
        json.dumps({"name": name, "checksum": "x", "slides": slides}),
        encoding="utf-8",
    )
    return path


def _seed_image(assets_dir: Path, name: str) -> Path:
    img = assets_dir / "images" / name
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\xff\xd8\xff fake")
    return img


def _seed_video(assets_dir: Path, name: str) -> Path:
    v = assets_dir / "videos" / name
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_bytes(b"\x00\x00\x00 fake")
    return v


def _make_sequencer(
    tmp_path: Path,
    on_done=None,
) -> tuple[SlideshowSequencer, MagicMock]:
    assets = tmp_path / "assets"
    assets.mkdir()
    player = MagicMock(name="ChromiumPlayer")
    seq = SlideshowSequencer(
        player=player,
        assets_dir=assets,
        on_done=on_done,
    )
    return seq, player


# -- Manifest loading ----------------------------------------------------------


def test_start_with_missing_manifest_returns_false(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    assert seq.start("nope", loop_count=1) is False
    assert not seq.is_running()


def test_start_with_malformed_manifest_returns_false(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    sdir = tmp_path / "assets" / "slideshows"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "bad.json").write_text("{not json", encoding="utf-8")
    assert seq.start("bad", loop_count=1) is False
    assert not seq.is_running()


def test_start_with_empty_slides_returns_false(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _write_manifest(tmp_path / "assets", "empty", [])
    assert seq.start("empty", loop_count=1) is False


def test_start_with_non_object_manifest_returns_false(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    sdir = tmp_path / "assets" / "slideshows"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "list.json").write_text("[1,2,3]", encoding="utf-8")
    assert seq.start("list", loop_count=1) is False


# -- Single-slide image dispatch ----------------------------------------------


def test_start_dispatches_first_slide_immediately(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 5000}],
    )
    assert seq.start("show", loop_count=1) is True
    player.show_image.assert_called_once()
    call = player.show_image.call_args
    assert call.args[0].name == "a.jpg"
    assert call.kwargs["transition"] == DEFAULT_TRANSITION
    assert call.kwargs["duration_ms"] == DEFAULT_TRANSITION_MS


def test_first_slide_schedules_timer_with_slide_duration(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 5000}],
    )
    seq.start("show", loop_count=1)
    assert len(FakeTimer.instances) == 1
    assert FakeTimer.instances[0].interval == pytest.approx(5.0)
    assert FakeTimer.instances[0].started is True


def test_slide_missing_duration_uses_default(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image"}],
    )
    seq.start("show", loop_count=1)
    assert FakeTimer.instances[0].interval == pytest.approx(
        DEFAULT_SLIDE_DURATION_MS / 1000.0
    )


# -- Multi-slide cycling ------------------------------------------------------


def test_timer_fires_dispatches_next_slide(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    for n in ("a.jpg", "b.jpg"):
        _seed_image(tmp_path / "assets", n)
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "b.jpg", "asset_type": "image", "duration_ms": 2000},
        ],
    )
    seq.start("show", loop_count=None)
    assert player.show_image.call_count == 1
    FakeTimer.instances[-1].fire()
    assert player.show_image.call_count == 2
    assert player.show_image.call_args_list[1].args[0].name == "b.jpg"


def test_loop_count_1_completes_after_one_cycle(tmp_path: Path) -> None:
    done = MagicMock()
    seq, player = _make_sequencer(tmp_path, on_done=done)
    for n in ("a.jpg", "b.jpg"):
        _seed_image(tmp_path / "assets", n)
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "b.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    seq.start("show", loop_count=1)
    # First slide dispatched on start.
    FakeTimer.instances[-1].fire()  # -> slide 2
    assert player.show_image.call_count == 2
    FakeTimer.instances[-1].fire()  # cycle wraps -> loops_completed=1 -> done
    assert player.show_image.call_count == 2
    done.assert_called_once()
    assert not seq.is_running()


def test_loop_count_2_completes_after_two_cycles(tmp_path: Path) -> None:
    done = MagicMock()
    seq, player = _make_sequencer(tmp_path, on_done=done)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=2)
    FakeTimer.instances[-1].fire()  # cycle 2 begins -> dispatch slide 1 again
    assert player.show_image.call_count == 2
    FakeTimer.instances[-1].fire()  # cycle 2 ends -> done
    done.assert_called_once()


def test_loop_count_none_is_infinite(tmp_path: Path) -> None:
    done = MagicMock()
    seq, player = _make_sequencer(tmp_path, on_done=done)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    for _ in range(50):
        FakeTimer.instances[-1].fire()
    done.assert_not_called()
    assert seq.is_running()
    assert player.show_image.call_count == 51  # 1 on start + 50 firings


# -- Miss handling -------------------------------------------------------------


def test_missing_slide_skipped_partial(tmp_path: Path) -> None:
    """One missing slide in the middle gets skipped, others still play."""
    seq, player = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _seed_image(tmp_path / "assets", "c.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "b-missing.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "c.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    seq.start("show", loop_count=1)
    assert player.show_image.call_args.args[0].name == "a.jpg"
    FakeTimer.instances[-1].fire()
    # b missing -> skipped -> c dispatched in same _advance call.
    assert player.show_image.call_count == 2
    assert player.show_image.call_args.args[0].name == "c.jpg"


def test_all_slides_missing_aborts(tmp_path: Path) -> None:
    done = MagicMock()
    seq, player = _make_sequencer(tmp_path, on_done=done)
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "missing-a.jpg", "asset_type": "image"},
            {"name": "missing-b.jpg", "asset_type": "image"},
        ],
    )
    seq.start("show", loop_count=None)
    player.show_image.assert_not_called()
    done.assert_called_once()
    assert not seq.is_running()


def test_partial_miss_resets_counter_after_hit(tmp_path: Path) -> None:
    """A real slide between misses should reset the abort guard."""
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "ok.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "miss-1.jpg"},
            {"name": "ok.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "miss-2.jpg"},
            {"name": "miss-3.jpg"},
        ],
    )
    seq.start("show", loop_count=None)
    # Cycle: miss, hit (dispatched), miss, miss. Three misses are fine
    # because the hit reset the counter at index 1 to 0 -- so only 2
    # post-hit misses, < 4 slides -> no abort, just wraps.
    assert seq.is_running()


# -- Video deferral (PR-1 only) -----------------------------------------------


def test_video_slide_skipped_in_pr1(tmp_path: Path, caplog) -> None:
    seq, player = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "v.mp4", "asset_type": "video", "duration_ms": 5000},
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    with caplog.at_level(logging.WARNING):
        seq.start("show", loop_count=1)
    # Video skipped, image dispatched.
    player.show_image.assert_called_once()
    assert player.show_image.call_args.args[0].name == "a.jpg"
    player.show_video.assert_not_called()
    assert any("video slide" in r.message and "deferred" in r.message
               for r in caplog.records)


def test_all_video_slideshow_aborts_in_pr1(tmp_path: Path) -> None:
    done = MagicMock()
    seq, player = _make_sequencer(tmp_path, on_done=done)
    _seed_video(tmp_path / "assets", "a.mp4")
    _seed_video(tmp_path / "assets", "b.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.mp4", "asset_type": "video"},
            {"name": "b.mp4", "asset_type": "video"},
        ],
    )
    seq.start("show", loop_count=None)
    player.show_image.assert_not_called()
    done.assert_called_once()


# -- Stop semantics -----------------------------------------------------------


def test_stop_cancels_timer_and_clears_state(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    t = FakeTimer.instances[-1]
    seq.stop()
    assert t.cancelled is True
    assert not seq.is_running()
    assert seq.current_name() is None


def test_stop_during_iteration_no_further_dispatches(tmp_path: Path) -> None:
    """A timer that fires AFTER ``stop`` is a no-op (epoch mismatch)."""
    seq, player = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _seed_image(tmp_path / "assets", "b.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "b.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    seq.start("show", loop_count=None)
    t = FakeTimer.instances[-1]
    seq.stop()
    # Simulate the timer's thread firing *after* cancel was called.
    t.cancelled = False  # bypass our own safety so we can test the epoch guard
    t.fire()
    # Only the initial dispatch happened.
    assert player.show_image.call_count == 1


def test_restart_after_stop_works(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    seq.stop()
    seq.start("show", loop_count=None)
    assert seq.is_running()
    assert player.show_image.call_count == 2  # one per start


# -- Public API ----------------------------------------------------------------


def test_current_name_reflects_running_slideshow(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show1",
        [{"name": "a.jpg", "asset_type": "image"}],
    )
    assert seq.current_name() is None
    seq.start("show1", loop_count=None)
    assert seq.current_name() == "show1"
    seq.stop()
    assert seq.current_name() is None


def test_manifest_digest_captured_at_start(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image"}],
    )
    assert seq.manifest_digest() is None
    seq.start("show", loop_count=None)
    digest = seq.manifest_digest()
    assert digest is not None and len(digest) == 64  # SHA-256 hex
