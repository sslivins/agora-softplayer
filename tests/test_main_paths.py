"""Tests for the path helpers in agora_softplayer.__main__.

These cover the portable-layout contract: the default data dir lives in
a ``data/`` folder next to the executable, so zipping up the install
folder moves both the .exe and its state together.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from agora_softplayer.__main__ import _default_data_dir, _exe_dir


def test_exe_dir_falls_back_to_argv0_dir_when_not_frozen(tmp_path: Path):
    fake_exe = tmp_path / "agora-softplayer.exe"
    fake_exe.write_text("", encoding="utf-8")

    with patch.object(sys, "frozen", False, create=True), \
         patch.object(sys, "argv", [str(fake_exe)]):
        assert _exe_dir() == tmp_path


def test_exe_dir_uses_sys_executable_when_frozen(tmp_path: Path):
    fake_exe = tmp_path / "agora-softplayer.exe"
    fake_exe.write_text("", encoding="utf-8")

    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(fake_exe)):
        assert _exe_dir() == tmp_path


def test_default_data_dir_is_data_subfolder_next_to_exe(tmp_path: Path):
    fake_exe = tmp_path / "agora-softplayer.exe"
    fake_exe.write_text("", encoding="utf-8")

    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(fake_exe)):
        assert _default_data_dir() == tmp_path / "data"
