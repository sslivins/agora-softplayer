"""CLI entry point.

Resolves config (CLI args > env vars > defaults), then runs the softplayer
event loop:

  * loads fleet/HMAC credentials from a CMS-generated env file
  * starts the FastAPI shell server on 127.0.0.1:<shell-port>
  * launches a Chromium / Edge window pointed at the shell URL
  * spins up the agora CMSClient to talk to the CMS as a real device

There is no built-in CMS URL; the env file is the single source of truth
for ``AGORA_CMS_URL`` + ``AGORA_FLEET_ID`` + ``AGORA_FLEET_SECRET_HEX``.
Without those the process refuses to start.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

import click

from agora_softplayer import __version__
from agora_softplayer.browser import find_browser, launch_browser
from agora_softplayer.credentials import (
    CredentialsError,
    load_credentials,
)
from agora_softplayer.shell_server import ShellServer

logger = logging.getLogger("agora_softplayer")


def _default_data_dir() -> Path:
    """Per-Windows convention: persistent state under %APPDATA%."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "agora-softplayer"
    # Sensible fallback for non-Windows dev (run under WSL etc).
    return Path.home() / ".agora-softplayer"


@click.command()
@click.version_option(__version__, prog_name="agora-softplayer")
@click.option(
    "--credentials-file",
    "credentials_file",
    envvar="AGORA_SOFTPLAYER_CREDENTIALS_FILE",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a softplayer.env file from the CMS imager. If unset, search "
        "(in order): %LOCALAPPDATA%\\agora-softplayer\\softplayer.env, the "
        "directory next to the .exe, and the current directory."
    ),
)
@click.option(
    "--data-dir",
    envvar="AGORA_SOFTPLAYER_DATA_DIR",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Persistent state directory. Defaults to %APPDATA%\\agora-softplayer.",
)
@click.option(
    "--browser-path",
    envvar="AGORA_SOFTPLAYER_BROWSER",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override path to Chromium / Edge / Chrome executable.",
)
@click.option(
    "--shell-port",
    envvar="AGORA_SOFTPLAYER_SHELL_PORT",
    type=int,
    default=8780,
    show_default=True,
    help="Port for the local FastAPI shell server.",
)
@click.option(
    "--available-slots",
    envvar="AGORA_SOFTPLAYER_AVAILABLE_SLOTS",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Lie about hardware: how many HDMI ports to advertise. "
    "Use 2 to exercise PR 2a's slot-B reconciliation path against a CMS.",
)
@click.option(
    "-v", "--verbose", is_flag=True, help="Enable debug logging."
)
def main(
    credentials_file: Path | None,
    data_dir: Path | None,
    browser_path: Path | None,
    shell_port: int,
    available_slots: int,
    verbose: bool,
) -> None:
    """Run the agora softplayer."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        credentials = load_credentials(credentials_file)
    except CredentialsError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        if exc.hint:
            click.echo(f"HINT: {exc.hint}", err=True)
        sys.exit(2)

    data_dir = (data_dir or _default_data_dir()).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "agora-softplayer %s starting (data_dir=%s, creds=%s, fleet=%s)",
        __version__,
        data_dir,
        credentials.source_path,
        credentials.fleet_id,
    )

    # Install the agora shims so any subsequent cms_client / shared.*
    # import resolves to Windows-friendly stand-ins.
    from agora_softplayer import shims
    shims.configure(data_dir=data_dir, available_slots=available_slots)
    try:
        shims.apply_shims()
    except ModuleNotFoundError as exc:
        click.echo(
            "ERROR: the agora/ submodule isn't checked out. "
            "Run `git submodule update --init --recursive` and retry.",
            err=True,
        )
        logger.debug("shim install failed: %s", exc)
        sys.exit(2)

    from agora_softplayer.cms_runner import CMSRunner
    cms_runner = CMSRunner(
        cms_url=credentials.cms_url,
        data_dir=data_dir,
        fleet_id=credentials.fleet_id,
        fleet_secret_hex=credentials.fleet_secret_hex,
        cms_transport=credentials.cms_transport,
    )
    cms_runner.start()
    logger.info(
        "CMSRunner started against %s (transport=%s)",
        credentials.cms_url,
        credentials.cms_transport,
    )

    browser = browser_path or find_browser()
    if browser is None:
        click.echo(
            "ERROR: could not find Microsoft Edge or Google Chrome. Install one, "
            "or pass --browser-path explicitly.",
            err=True,
        )
        cms_runner.stop()
        sys.exit(2)
    logger.info("Using browser: %s", browser)

    shell_url = f"http://127.0.0.1:{shell_port}/"
    server = ShellServer(
        host="127.0.0.1",
        port=shell_port,
        data_dir=data_dir,
        cms_url=credentials.cms_url,
        available_slots=available_slots,
    )
    server.start()
    logger.info("Shell server listening on %s", shell_url)

    browser_proc = launch_browser(
        browser,
        url=shell_url,
        user_data_dir=data_dir / "browser-profile",
    )
    logger.info("Browser process PID %d", browser_proc.pid)

    def _shutdown(*_args):
        logger.info("Shutting down")
        try:
            browser_proc.terminate()
        except Exception:
            logger.exception("Failed to terminate browser process")
        try:
            cms_runner.stop()
        except Exception:
            logger.exception("Failed to stop CMSRunner")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGBREAK"):  # Windows ctrl-break
        signal.signal(signal.SIGBREAK, _shutdown)

    rc = browser_proc.wait()
    logger.info("Browser exited with code %d", rc)
    cms_runner.stop()
    server.stop()


if __name__ == "__main__":
    main()
