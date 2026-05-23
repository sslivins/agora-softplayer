"""Test-suite-wide setup for agora-softplayer.

Places the vendored ``agora/`` submodule on ``sys.path`` so tests can
import the shared ``player.slideshow_engine`` helpers without having
to run the production ``shims.apply_shims()`` bootstrap.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo layout: tests/conftest.py -> repo-root -> repo-root/agora
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGORA_SUBMODULE = _REPO_ROOT / "agora"
if _AGORA_SUBMODULE.exists() and str(_AGORA_SUBMODULE) not in sys.path:
    sys.path.insert(0, str(_AGORA_SUBMODULE))
