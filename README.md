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

# Drop a softplayer.env file (downloaded from the CMS imager) next to the
# project, then run:
python -m agora_softplayer --credentials-file .\softplayer.env
```

A Chromium window opens. The device shows up as PENDING in the CMS;
adopt it from the Devices page like you would any other Pi.

## Quickstart (binary release)

1. Download the matching `.exe` for your architecture from the
   [Releases page](https://github.com/sslivins/agora-softplayer/releases).
2. In the CMS, go to the **Imager** tab, click **Provision Softplayer**,
   pick the fleet you want this softplayer to join, and download
   `softplayer.env`.
3. Drop `softplayer.env` next to the `.exe` (or under
   `%LOCALAPPDATA%\agora-softplayer\softplayer.env`).
4. Run the `.exe`.

```powershell
.\agora-softplayer-amd64.exe
```

No Python install required, but you do need Microsoft Edge or Google
Chrome installed for the player window.

## Credentials

The softplayer is **bootstrap-v2-only**: it authenticates to the CMS via
the same fleet-HMAC pairing flow Pis use in production. There is no
legacy "register and let the CMS mint a token" path.

The CMS imager generates a `softplayer.env` file containing:

```
AGORA_CMS_URL=wss://cms.example.com/ws/device
AGORA_FLEET_ID=<fleet>
AGORA_FLEET_SECRET_HEX=<64 hex chars>
AGORA_CMS_TRANSPORT=direct
```

The fleet secret authorizes any device to enroll into that fleet, so
**keep the file private**. Future work moves these values into Windows
DPAPI / Credential Manager via a first-run wizard so they aren't sitting
in plaintext on disk.

Search order (first hit wins):

1. `--credentials-file PATH` (explicit; missing → hard error).
2. `%LOCALAPPDATA%\agora-softplayer\softplayer.env`.
3. The directory containing the running `.exe`.
4. The current working directory.

## Configuration

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `--credentials-file PATH` | `AGORA_SOFTPLAYER_CREDENTIALS_FILE` | (search order above) | softplayer.env from the CMS imager |
| `--data-dir PATH` | `AGORA_SOFTPLAYER_DATA_DIR` | `%APPDATA%\agora-softplayer` | Persistent state (device key, pairing secret, downloaded assets) |
| `--browser-path PATH` | `AGORA_SOFTPLAYER_BROWSER` | auto-detect | Override for Chromium / Edge / Chrome binary |
| `--shell-port PORT` | `AGORA_SOFTPLAYER_SHELL_PORT` | `8780` | Local FastAPI shell server port |
| `--available-slots N` | `AGORA_SOFTPLAYER_AVAILABLE_SLOTS` | `1` | Advertise N HDMI slots to the CMS (use `2` to exercise multi-display) |

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
