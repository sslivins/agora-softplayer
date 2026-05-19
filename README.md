# agora-softplayer

A Windows-native "softplayer" for the Agora digital-signage CMS. It talks to
the CMS exactly like a real Pi would — registration, adoption, sync,
play/stop, asset fetch — and renders the assigned content in a local
Chromium / Edge window. No physical hardware required.

Useful for:

- Demoing the product without a Pi on hand.
- Iterating on the shell SPA without flashing a Pi.
- Validating CMS-side changes end-to-end before they hit hardware.

This repo vendors [`sslivins/agora`](https://github.com/sslivins/agora) as a
submodule so it can reuse `cms_client` and the shell-server SPA verbatim. The
only Windows-specific code lives in `src/agora_softplayer/`.

## Quickstart (developer / source)

```powershell
git clone --recurse-submodules https://github.com/sslivins/agora-softplayer
cd agora-softplayer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m agora_softplayer --cms-url http://localhost:8000
```

A Chromium window opens. Adopt the device from the CMS Devices page like
you would any other Pi.

## Quickstart (binary release)

Download the matching `.exe` for your architecture from the
[Releases page](https://github.com/sslivins/agora-softplayer/releases) and
run it. No Python install required, but you do need Microsoft Edge or
Google Chrome installed for the player window.

```powershell
.\agora-softplayer-amd64.exe --cms-url http://localhost:8000
```

## Configuration

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `--cms-url URL` | `AGORA_SOFTPLAYER_CMS_URL` | (required) | CMS WebSocket base URL |
| `--data-dir PATH` | `AGORA_SOFTPLAYER_DATA_DIR` | `%APPDATA%\agora-softplayer` | Persistent state (device_id, api_key, downloaded assets) |
| `--browser-path PATH` | `AGORA_SOFTPLAYER_BROWSER` | auto-detect | Override for Chromium / Edge / Chrome binary |
| `--shell-port PORT` | `AGORA_SOFTPLAYER_SHELL_PORT` | `8780` | Local FastAPI shell server port |

## Architecture

```
+------------------------------+
| Windows host                 |
|                              |
|  +-------------------------+ |
|  | agora-softplayer.exe    | |
|  | ├── CMSClient (WS→CMS)  | |
|  | ├── Shell server (FastAPI on 127.0.0.1:8780)
|  | └── Chromium subprocess (--app=http://localhost:8780)
|  +-------------------------+ |
+------------------------------+
```

No sway, no systemd-run, no Wayland. The Chromium window is a normal Windows
process that happens to point at a local web server. The rest of the agora
codepaths (asset fetch, sync, ack messages, status heartbeat) are reused
unchanged.

Multi-display is intentionally out of scope for the softplayer — that
remains a hardware-side feature.

## License

MIT. See [LICENSE](LICENSE).
