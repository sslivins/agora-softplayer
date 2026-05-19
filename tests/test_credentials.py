"""Tests for the softplayer credential loader.

These cover:
- happy-path parsing with and without optional keys
- missing required keys → InvalidCredentialsError
- malformed hex → InvalidCredentialsError
- short hex (below the 16-byte HMAC floor) → InvalidCredentialsError
- malformed env file (no '=' on a non-comment line, bad key chars) → InvalidCredentialsError
- comments, blank lines, and quoted values
- search-order precedence: --credentials-file > LOCALAPPDATA > exe dir > cwd
- explicit --credentials-file that doesn't exist → MissingCredentialsError
- nothing found anywhere → MissingCredentialsError
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agora_softplayer.credentials import (
    DEFAULT_FILENAME,
    Credentials,
    InvalidCredentialsError,
    MissingCredentialsError,
    load_credentials,
)

VALID_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"  # 32 bytes


def _write_env(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _minimum_body(cms_url: str = "wss://cms.example.com/ws/device") -> str:
    return (
        f"AGORA_CMS_URL={cms_url}\n"
        f"AGORA_FLEET_ID=fleet-test\n"
        f"AGORA_FLEET_SECRET_HEX={VALID_HEX}\n"
    )


def test_explicit_path_happy(tmp_path: Path):
    env_file = _write_env(tmp_path / "softplayer.env", _minimum_body())

    creds = load_credentials(env_file)

    assert isinstance(creds, Credentials)
    assert creds.cms_url == "wss://cms.example.com/ws/device"
    assert creds.fleet_id == "fleet-test"
    assert creds.fleet_secret_hex == VALID_HEX
    assert creds.cms_transport == "direct"
    assert creds.source_path == env_file


def test_explicit_path_with_optional_transport(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        _minimum_body() + "AGORA_CMS_TRANSPORT=wps\n",
    )

    creds = load_credentials(env_file)

    assert creds.cms_transport == "wps"


def test_invalid_transport_value(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        _minimum_body() + "AGORA_CMS_TRANSPORT=ftp\n",
    )

    with pytest.raises(InvalidCredentialsError, match="AGORA_CMS_TRANSPORT"):
        load_credentials(env_file)


def test_explicit_path_nonexistent(tmp_path: Path):
    missing = tmp_path / "does-not-exist.env"

    with pytest.raises(MissingCredentialsError, match="does not exist"):
        load_credentials(missing)


def test_comments_blanks_and_quotes(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        "# header comment\n"
        "\n"
        f'AGORA_CMS_URL="wss://cms.example.com/ws/device"\n'
        "  \n"
        f"AGORA_FLEET_ID='fleet-quoted'\n"
        f"AGORA_FLEET_SECRET_HEX={VALID_HEX}  \n"
        "# trailing comment\n",
    )

    creds = load_credentials(env_file)
    assert creds.cms_url == "wss://cms.example.com/ws/device"
    assert creds.fleet_id == "fleet-quoted"


@pytest.mark.parametrize("missing_key", [
    "AGORA_CMS_URL",
    "AGORA_FLEET_ID",
    "AGORA_FLEET_SECRET_HEX",
])
def test_missing_required_key(tmp_path: Path, missing_key: str):
    body = _minimum_body()
    lines = [ln for ln in body.splitlines() if not ln.startswith(missing_key + "=")]
    env_file = _write_env(tmp_path / "sp.env", "\n".join(lines) + "\n")

    with pytest.raises(InvalidCredentialsError, match=missing_key):
        load_credentials(env_file)


def test_empty_required_key(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        f"AGORA_CMS_URL=\nAGORA_FLEET_ID=fleet\nAGORA_FLEET_SECRET_HEX={VALID_HEX}\n",
    )

    with pytest.raises(InvalidCredentialsError, match="AGORA_CMS_URL"):
        load_credentials(env_file)


def test_malformed_hex(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        "AGORA_CMS_URL=wss://cms/ws/device\nAGORA_FLEET_ID=fleet\n"
        "AGORA_FLEET_SECRET_HEX=zznotvalidhex\n",
    )

    with pytest.raises(InvalidCredentialsError, match="not valid hex"):
        load_credentials(env_file)


def test_short_hex(tmp_path: Path):
    # 8 bytes (16 hex chars) is below the 16-byte floor.
    env_file = _write_env(
        tmp_path / "sp.env",
        "AGORA_CMS_URL=wss://cms/ws/device\nAGORA_FLEET_ID=fleet\n"
        "AGORA_FLEET_SECRET_HEX=0011223344556677\n",
    )

    with pytest.raises(InvalidCredentialsError, match="too short"):
        load_credentials(env_file)


def test_missing_equals_on_value_line(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        _minimum_body() + "BARE_LINE_NO_EQUALS\n",
    )

    with pytest.raises(InvalidCredentialsError, match="KEY=VALUE"):
        load_credentials(env_file)


def test_invalid_key_characters(tmp_path: Path):
    env_file = _write_env(
        tmp_path / "sp.env",
        _minimum_body() + "lower-case-key=value\n",
    )

    with pytest.raises(InvalidCredentialsError, match="invalid key"):
        load_credentials(env_file)


def test_implicit_search_local_app_data_wins(tmp_path: Path):
    """LOCALAPPDATA is first in the implicit search order."""
    lad = tmp_path / "localappdata"
    exe = tmp_path / "exe"
    cwd = tmp_path / "cwd"
    _write_env(lad / "agora-softplayer" / DEFAULT_FILENAME, _minimum_body("wss://lad/ws/device"))
    _write_env(exe / DEFAULT_FILENAME, _minimum_body("wss://exe/ws/device"))
    _write_env(cwd / DEFAULT_FILENAME, _minimum_body("wss://cwd/ws/device"))

    creds = load_credentials(
        None,
        local_app_data=lad / "agora-softplayer",
        exe_dir=exe,
        cwd=cwd,
    )
    assert creds.cms_url == "wss://lad/ws/device"


def test_implicit_search_falls_through_to_exe_dir(tmp_path: Path):
    lad = tmp_path / "localappdata" / "agora-softplayer"  # doesn't exist
    exe = tmp_path / "exe"
    cwd = tmp_path / "cwd"
    _write_env(exe / DEFAULT_FILENAME, _minimum_body("wss://exe/ws/device"))
    _write_env(cwd / DEFAULT_FILENAME, _minimum_body("wss://cwd/ws/device"))

    creds = load_credentials(None, local_app_data=lad, exe_dir=exe, cwd=cwd)
    assert creds.cms_url == "wss://exe/ws/device"


def test_implicit_search_falls_through_to_cwd(tmp_path: Path):
    lad = tmp_path / "localappdata" / "agora-softplayer"
    exe = tmp_path / "exe"  # doesn't exist
    cwd = tmp_path / "cwd"
    _write_env(cwd / DEFAULT_FILENAME, _minimum_body("wss://cwd/ws/device"))

    creds = load_credentials(None, local_app_data=lad, exe_dir=exe, cwd=cwd)
    assert creds.cms_url == "wss://cwd/ws/device"


def test_implicit_search_nothing_found(tmp_path: Path):
    lad = tmp_path / "lad"
    exe = tmp_path / "exe"
    cwd = tmp_path / "cwd"

    with pytest.raises(MissingCredentialsError, match="No softplayer.env"):
        load_credentials(None, local_app_data=lad, exe_dir=exe, cwd=cwd)


def test_implicit_search_malformed_file_does_not_fall_through(tmp_path: Path):
    """A found-but-broken file is an error, not a hint to try the next path."""
    lad = tmp_path / "localappdata" / "agora-softplayer"
    exe = tmp_path / "exe"
    cwd = tmp_path / "cwd"
    # LOCALAPPDATA file exists but is missing AGORA_CMS_URL.
    _write_env(
        lad / DEFAULT_FILENAME,
        f"AGORA_FLEET_ID=fleet\nAGORA_FLEET_SECRET_HEX={VALID_HEX}\n",
    )
    # CWD has a valid file -- but we should never fall through to it.
    _write_env(cwd / DEFAULT_FILENAME, _minimum_body("wss://cwd/ws/device"))

    with pytest.raises(InvalidCredentialsError, match="AGORA_CMS_URL"):
        load_credentials(None, local_app_data=lad, exe_dir=exe, cwd=cwd)


def test_credentials_error_carries_hint(tmp_path: Path):
    """The user-facing CLI relies on the .hint attribute to point at the imager."""
    missing = tmp_path / "does-not-exist.env"
    try:
        load_credentials(missing)
    except MissingCredentialsError as exc:
        assert exc.hint  # non-empty
    else:
        pytest.fail("MissingCredentialsError was not raised")
