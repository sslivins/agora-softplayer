"""Tests for ``agora_softplayer._slideshow.SlideshowSequencer``.

Uses a fake :class:`threading.Timer` (``FakeTimer``) so slide-advance
timers can be fired synchronously without real clocks. Mocks
``ChromiumPlayer`` as a :class:`unittest.mock.MagicMock` and asserts
on the dispatched calls.
"""
from __future__ import annotations

import json
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


# -- Video dispatch (PR-2) ----------------------------------------------------


def test_video_slide_without_play_to_end_uses_timer_fallback(
    tmp_path: Path,
) -> None:
    """Video without ``play_to_end`` -> loop=True + duration-driven advance."""
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = None  # force fallback path
    _seed_image(tmp_path / "assets", "a.jpg")
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "v.mp4", "asset_type": "video", "duration_ms": 3000},
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    seq.start("show", loop_count=None)
    # Video dispatched with loop=True, NOT skipped.
    player.show_video.assert_called_once()
    assert player.show_video.call_args.kwargs["loop"] is True
    assert FakeTimer.instances[-1].interval == pytest.approx(3.0)
    # Firing the timer advances to the image slide.
    FakeTimer.instances[-1].fire()
    player.show_image.assert_called_once()


def test_video_slide_with_play_to_end_arms_watchdog(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "v.mp4", "asset_type": "video",
             "duration_ms": 5000, "play_to_end": True},
        ],
    )
    seq.start("show", loop_count=None)
    player.show_video.assert_called_once()
    assert player.show_video.call_args.kwargs["loop"] is False
    # Watchdog armed: max(5000*2, 60_000) = 60_000 ms = 60 s.
    assert len(FakeTimer.instances) == 1
    assert FakeTimer.instances[0].interval == pytest.approx(60.0)


def test_play_to_end_watchdog_uses_cap_when_no_duration(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "v.mp4", "asset_type": "video", "play_to_end": True}],
    )
    seq.start("show", loop_count=None)
    # No duration hint -> watchdog at the hard cap (300s).
    assert FakeTimer.instances[0].interval == pytest.approx(300.0)


def test_play_to_end_watchdog_capped_for_huge_duration(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "v.mp4", "asset_type": "video",
          "duration_ms": 600_000, "play_to_end": True}],
    )
    seq.start("show", loop_count=None)
    # 2 * 600_000 = 1_200_000 ms, capped at 300_000 ms.
    assert FakeTimer.instances[0].interval == pytest.approx(300.0)


def test_on_shell_ended_advances_when_armed(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _seed_image(tmp_path / "assets", "next.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "v.mp4", "asset_type": "video",
             "duration_ms": 5000, "play_to_end": True},
            {"name": "next.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    seq.start("show", loop_count=None)
    watchdog = FakeTimer.instances[-1]
    assert seq.on_shell_ended("/assets/videos/v.mp4") is True
    # Watchdog cancelled, image slide dispatched.
    assert watchdog.cancelled is True
    player.show_image.assert_called_once()


def test_on_shell_ended_ignores_mismatched_url(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "v.mp4", "asset_type": "video",
          "duration_ms": 5000, "play_to_end": True}],
    )
    seq.start("show", loop_count=None)
    assert seq.on_shell_ended("/assets/videos/other.mp4") is False
    # Watchdog still armed.
    assert FakeTimer.instances[0].cancelled is False


def test_on_shell_ended_returns_false_when_no_pending(tmp_path: Path) -> None:
    """Image slides don't arm pending claims; ``ended`` is a no-op."""
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    assert seq.on_shell_ended("/assets/images/a.jpg") is False


def test_on_shell_ended_returns_false_when_not_running(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    assert seq.on_shell_ended("/assets/anything") is False


def test_on_shell_ended_returns_false_for_empty_url(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "v.mp4", "asset_type": "video",
          "duration_ms": 5000, "play_to_end": True}],
    )
    seq.start("show", loop_count=None)
    assert seq.on_shell_ended(None) is False
    assert seq.on_shell_ended("") is False


def test_watchdog_advances_when_ended_never_arrives(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _seed_image(tmp_path / "assets", "next.jpg")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "v.mp4", "asset_type": "video",
             "duration_ms": 5000, "play_to_end": True},
            {"name": "next.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    seq.start("show", loop_count=None)
    watchdog = FakeTimer.instances[-1]
    watchdog.fire()
    player.show_image.assert_called_once()
    # Stale ``ended`` afterward is a no-op (pending was cleared).
    assert seq.on_shell_ended("/assets/videos/v.mp4") is False


def test_play_to_end_with_asset_url_none_falls_back(tmp_path: Path) -> None:
    """Pi parity: missing asset_url -> loop=True + duration timer."""
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = None
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "v.mp4", "asset_type": "video",
          "duration_ms": 3000, "play_to_end": True}],
    )
    seq.start("show", loop_count=None)
    player.show_video.assert_called_once()
    assert player.show_video.call_args.kwargs["loop"] is True
    # No watchdog -- normal slide-duration timer is in play.
    assert FakeTimer.instances[0].interval == pytest.approx(3.0)


def test_stop_during_video_cancels_watchdog(tmp_path: Path) -> None:
    seq, player = _make_sequencer(tmp_path)
    player.asset_url.return_value = "/assets/videos/v.mp4"
    _seed_video(tmp_path / "assets", "v.mp4")
    _write_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "v.mp4", "asset_type": "video",
          "duration_ms": 5000, "play_to_end": True}],
    )
    seq.start("show", loop_count=None)
    watchdog = FakeTimer.instances[0]
    seq.stop()
    assert watchdog.cancelled is True
    # Stale ``ended`` after stop is a no-op.
    assert seq.on_shell_ended("/assets/videos/v.mp4") is False


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


# -- PR-3: stable-state helpers --

def test_manifest_unchanged_true_for_unmodified_file(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets", "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    assert seq.manifest_unchanged() is True


def test_manifest_unchanged_false_when_file_rewritten(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _seed_image(tmp_path / "assets", "b.jpg")
    _write_manifest(
        tmp_path / "assets", "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    _write_manifest(
        tmp_path / "assets", "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 1000},
            {"name": "b.jpg", "asset_type": "image", "duration_ms": 1000},
        ],
    )
    assert seq.manifest_unchanged() is False


def test_manifest_unchanged_false_when_file_deleted(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets", "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    (tmp_path / "assets" / "slideshows" / "show.json").unlink()
    assert seq.manifest_unchanged() is False


def test_manifest_unchanged_false_when_not_running(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    assert seq.manifest_unchanged() is False


def test_matches_loop_count(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets", "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=5)
    assert seq.matches_loop_count(5) is True
    assert seq.matches_loop_count(None) is False
    assert seq.matches_loop_count(7) is False


def test_matches_loop_count_none(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    _seed_image(tmp_path / "assets", "a.jpg")
    _write_manifest(
        tmp_path / "assets", "show",
        [{"name": "a.jpg", "asset_type": "image", "duration_ms": 1000}],
    )
    seq.start("show", loop_count=None)
    assert seq.matches_loop_count(None) is True
    assert seq.matches_loop_count(1) is False


def test_matches_loop_count_false_when_not_running(tmp_path: Path) -> None:
    seq, _ = _make_sequencer(tmp_path)
    assert seq.matches_loop_count(None) is False


# -- Wall-clock anchored resume (agora#226 Phase 2 port) -----------------------


def _write_anchored_manifest(
    assets_dir: Path, name: str, slides: list[dict], started_at: str,
) -> Path:
    """Like ``_write_manifest`` but emits a schema 1.1 manifest with
    a ``started_at`` so the sequencer turns on the anchored path."""
    sdir = assets_dir / "slideshows"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{name}.json"
    path.write_text(
        json.dumps({
            "name": name,
            "checksum": "x",
            "manifest_schema_version": "1.1",
            "started_at": started_at,
            "slides": slides,
        }),
        encoding="utf-8",
    )
    return path


def test_anchored_start_resumes_mid_cycle(tmp_path: Path) -> None:
    """A schema 1.1 manifest with ``started_at`` 25s in the past should
    dispatch the slide that should be on screen at 25s into the cycle
    (slide index 2 -- 10s + 10s + 5s in), NOT slide 0."""
    seq, player = _make_sequencer(tmp_path)
    for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
        _seed_image(tmp_path / "assets", n)
    # 10s + 10s + 10s + 10s = 40s cycle. Anchor 25s ago -> target idx=2.
    from datetime import datetime, timedelta, timezone
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=25)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _write_anchored_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 10_000},
            {"name": "b.jpg", "asset_type": "image", "duration_ms": 10_000},
            {"name": "c.jpg", "asset_type": "image", "duration_ms": 10_000},
            {"name": "d.jpg", "asset_type": "image", "duration_ms": 10_000},
        ],
        started_at=started_at,
    )
    assert seq.start("show", loop_count=None) is True
    # Anchored path picked target idx=2 (slide c.jpg) instead of 0.
    player.show_image.assert_called_once()
    assert player.show_image.call_args.args[0].name == "c.jpg"
    # Timer armed at min(remaining_ms, RESYNC_CAP_MS=5s). Remaining is
    # ~5s into a 10s slide, so timer should be at most 5s (the cap).
    assert FakeTimer.instances[-1].interval <= 5.01


def test_legacy_manifest_starts_at_slide_zero(tmp_path: Path) -> None:
    """Schema 1.0 manifest (no ``started_at``) keeps legacy behaviour:
    always start at slide 0 regardless of wall clock."""
    seq, player = _make_sequencer(tmp_path)
    for n in ("a.jpg", "b.jpg"):
        _seed_image(tmp_path / "assets", n)
    _write_manifest(
        tmp_path / "assets",
        "show",
        [
            {"name": "a.jpg", "asset_type": "image", "duration_ms": 5000},
            {"name": "b.jpg", "asset_type": "image", "duration_ms": 5000},
        ],
    )
    assert seq.start("show", loop_count=None) is True
    player.show_image.assert_called_once()
    assert player.show_image.call_args.args[0].name == "a.jpg"
    # Legacy path uses the slide's full duration_ms, not capped.
    assert FakeTimer.instances[-1].interval == pytest.approx(5.0)


def test_anchored_resync_tick_does_not_redispatch_same_slide(tmp_path: Path) -> None:
    """If a resync tick fires while the wall clock still says we're on
    the slide we already dispatched, we must re-arm the timer WITHOUT
    re-calling show_image / show_video (which would visibly reload the
    video element)."""
    seq, player = _make_sequencer(tmp_path)
    _seed_video(tmp_path / "assets", "a.mp4")
    from datetime import datetime, timedelta, timezone
    # 30s-long single-slide cycle, started 2s ago -> 28s remaining,
    # capped to 5s by RESYNC_CAP. Looped video (no play_to_end) so the
    # slide stays the same across resync ticks.
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _write_anchored_manifest(
        tmp_path / "assets",
        "show",
        [{"name": "a.mp4", "asset_type": "video", "duration_ms": 30_000}],
        started_at=started_at,
    )
    assert seq.start("show", loop_count=None) is True
    assert player.show_video.call_count == 1
    # Fire the resync timer -- wall clock has barely moved, still slide 0.
    FakeTimer.instances[-1].fire()
    assert player.show_video.call_count == 1, (
        "Resync tick should not have re-dispatched the same video slide"
    )


def test_anchored_video_passes_start_offset_ms(tmp_path: Path) -> None:
    """A video slide dispatched under the anchored path must include
    ``start_offset_ms`` so the shell can seek into the asset."""
    seq, player = _make_sequencer(tmp_path)
    _seed_video(tmp_path / "assets", "a.mp4")
    from datetime import datetime, timedelta, timezone
    # 60s video, 25s elapsed -> start_offset_ms = 25_000.
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=25)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _write_anchored_manifest(
        tmp_path / "assets",
        "show",
        [{
            "name": "a.mp4", "asset_type": "video",
            "duration_ms": 60_000, "play_to_end": True,
        }],
        started_at=started_at,
    )
    # Set player.asset_url so play_to_end is taken.
    player.asset_url.return_value = "/assets/videos/a.mp4"
    assert seq.start("show", loop_count=None) is True
    player.show_video.assert_called_once()
    offset = player.show_video.call_args.kwargs.get("start_offset_ms")
    assert offset is not None and 24_000 <= offset <= 26_000, offset
