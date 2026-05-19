"""CLI entry point.

Resolves config (CLI args > env vars > defaults), then runs the softplayer
event loop:

  * starts the FastAPI shell server on 127.0.0.1:<shell-port>
  * launches a Chromium / Edge window pointed at the shell URL
  * (todo) connects to CMS as a normal device

For milestone 1 the CMS connection is stubbed: we just want to prove the
window appears with the local shell SPA rendered inside it. That part
lands in milestone 2.
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
    "--cms-url",
    envvar="AGORA_SOFTPLAYER_CMS_URL",
    default=None,
    help="CMS WebSocket base URL (e.g. http://localhost:8000). Required in M2+.",
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
    cms_url: str | None,
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

    data_dir = (data_dir or _default_data_dir()).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("agora-softplayer %s starting (data_dir=%s)", __version__, data_dir)

    # Install the agora shims so any subsequent cms_client / shared.*
    # import resolves to Windows-friendly stand-ins. Cheap and idempotent;
    # we do it even in M1-standalone mode so the shim state (data_dir,
    # available_slots) is always in sync with the CLI flags.
    from agora_softplayer import shims
    shims.configure(data_dir=data_dir, available_slots=available_slots)
    try:
        shims.apply_shims()
    except ModuleNotFoundError as exc:
        # Bare scaffold without the agora/ submodule checked out is fine
        # for M1; we only need shims when we wire CMSClient.
        logger.warning("agora submodule unavailable, shims not installed: %s", exc)

    if cms_url:
        from agora_softplayer.cms_runner import CMSRunner
        cms_runner = CMSRunner(cms_url=cms_url, data_dir=data_dir)
        cms_runner.start()
        logger.info("CMSRunner started against %s", cms_url)
    else:
        cms_runner = None
        logger.warning("No --cms-url provided. M1 standalone mode -- shell SPA only.")

    browser = browser_path or find_browser()
    if browser is None:
        click.echo(
            "ERROR: could not find Microsoft Edge or Google Chrome. Install one, "
            "or pass --browser-path explicitly.",
            err=True,
        )
        sys.exit(2)
    logger.info("Using browser: %s", browser)

    shell_url = f"http://127.0.0.1:{shell_port}/"
    server = ShellServer(host="127.0.0.1", port=shell_port, data_dir=data_dir)
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
        if cms_runner is not None:
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
    if cms_runner is not None:
        cms_runner.stop()
    server.stop()


if __name__ == "__main__":
    main()
