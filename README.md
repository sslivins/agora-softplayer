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
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Drop a softplayer.env file (downloaded from the CMS imager) next to the
# project, then run:
python -m agora_softplayer --credentials-file .\softplayer.env --data-dir .\data
```

A Chromium window opens. The device shows up as PENDING in the CMS;
adopt it from the Devices page like you would any other Pi.

> **Already cloned without `--recurse-submodules`?** You'll hit
> `ERROR: the agora/ submodule isn't checked out`. Run
> `git submodule update --init --recursive` from the repo root and retry.

> **Why explicit `--credentials-file` / `--data-dir` when running from
> source?** The implicit search keys off the location of the running
> `python.exe` (the venv binary), not the repo root. Passing them
> explicitly avoids surprises. The implicit search is for the
> `.exe` distribution case below, where the layout is unambiguous.

Both Python 3.12 amd64 and arm64 are supported. PyPI ships precompiled
`cryptography` wheels for `win_arm64` so installation is a single
`pip install` on either architecture.

## Quickstart (binary release)

1. Download the matching `.exe` for your architecture from the
   [Releases page](https://github.com/sslivins/agora-softplayer/releases).
2. In the CMS, go to the **Imager** tab, click **Provision Softplayer**,
   pick the fleet you want this softplayer to join, and download
   `softplayer.env`.
3. Drop `softplayer.env` next to the `.exe`.
4. Run the `.exe`.

```powershell
.\agora-softplayer-amd64.exe
```

No Python install required, but you do need Microsoft Edge or Google
Chrome installed for the player window.

**Portable layout.** Everything the softplayer needs lives next to the
`.exe`:

```
my-softplayer/
├── agora-softplayer.exe
├── softplayer.env        ← from the CMS imager
└── data/                 ← created on first run; assets, browser profile, state
```

Zip that folder and it moves between machines as a single self-contained
unit. There is no machine-wide install path and no environment-variable
override -- the goal is "drop folder anywhere, double-click .exe, it
works" with no machine-wide footprint.

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
2. `softplayer.env` in the same folder as the running `.exe`.

## Configuration

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `--credentials-file PATH` | _(none)_ | `<exe-dir>\softplayer.env` | softplayer.env from the CMS imager |
| `--data-dir PATH` | _(none)_ | `<exe-dir>\data` | Persistent state (device key, pairing secret, downloaded assets, browser profile) |
| `--browser-path PATH` | `AGORA_SOFTPLAYER_BROWSER` | auto-detect | Override for Chromium / Edge / Chrome binary |
| `--shell-port PORT` | `AGORA_SOFTPLAYER_SHELL_PORT` | `8780` | Local FastAPI shell server port |
| `--available-slots N` | `AGORA_SOFTPLAYER_AVAILABLE_SLOTS` | `1` | Advertise N HDMI slots to the CMS (use `2` to exercise multi-display) |

`--credentials-file` and `--data-dir` are CLI-only on purpose: they
control where the install lives, and an env var elsewhere on the
machine would defeat the portable-folder contract.

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

## Troubleshooting

### `ERROR: the agora/ submodule isn't checked out`

You cloned without `--recurse-submodules`. From the repo root:

```powershell
git submodule update --init --recursive
```

### `No module named agora_softplayer`

You ran a system Python that doesn't have the project installed. Either
activate the venv first:

```powershell
.\.venv\Scripts\Activate.ps1
python -m agora_softplayer ...
```

Or call the venv's interpreter explicitly:

```powershell
.\.venv\Scripts\python.exe -m agora_softplayer ...
```

`where.exe python` should list the venv's `python.exe` first when the
venv is active.

### `.exe` fails to launch with "Bad Image" / `0xc0e90002`

On Microsoft-managed Windows devices (and some other strictly
locked-down enterprise machines), Windows Defender Application Control
(WDAC) enforces a User-Mode Code Integrity policy at "Enterprise
signing level". The `.exe` is currently unsigned, so on those machines
the loader rejects the bundled `python312.dll` at startup with the
"Bad Image" dialog and error status `0xc0e90002`.

You can confirm by looking at the Windows Event Log
(`Microsoft-Windows-CodeIntegrity/Operational`) — events 3033 and 3077
will name the rejected DLL and the signing level requirement.

Three workarounds, in order of effort:

1. **Run from source instead.** Python from python.org and the Microsoft
   Store is signed by `Python Software Foundation`, which corporate
   WDAC policies allow-list. Follow the developer quickstart above —
   you'll get the same softplayer, just launched through `python.exe`
   instead of a frozen `.exe`.
2. **Run in a Hyper-V VM** on the same laptop. Corporate WDAC scopes to
   the host, not nested guests. A fresh Windows 11 dev VM (`Hyper-V
   Manager > Quick Create`) runs the `.exe` without complaint.
3. **Run on a non-managed device.** A personal Windows machine or the
   actual target hardware (an agora-style kiosk before it's joined to
   the org) has no such WDAC policy.

Tracking proper Authenticode signing in
[#9](https://github.com/sslivins/agora-softplayer/issues/9) (Azure
Trusted Signing). Once that lands the `.exe` will satisfy SmartScreen
and most enterprise WDAC policies — though Microsoft-corp-managed
devices specifically may still reject it, since they typically allow
only `Microsoft Corporation`-signed binaries.

### SmartScreen prompts "Windows protected your PC"

Expected for unsigned binaries downloaded from the internet. Click
**More info > Run anyway**. This warning goes away once the binary is
signed (issue #9).

### Browser doesn't open / wrong browser used

The softplayer auto-detects Chromium, Edge, and Chrome in standard
install locations. If yours is non-standard, set
`--browser-path "C:\path\to\chrome.exe"` or the
`AGORA_SOFTPLAYER_BROWSER` env var.

### Running the source clone from a different folder than the venv

If your softplayer.env and `data/` live in some other directory (e.g.
a "portable folder" like `C:\Users\me\softplayer\my-fleet\`) and the
repo + venv are elsewhere, just pass both paths explicitly:

```powershell
cd C:\Users\me\softplayer\my-fleet
C:\path\to\agora-softplayer\.venv\Scripts\python.exe `
  -m agora_softplayer `
  --credentials-file .\softplayer.env `
  --data-dir .\data
```

The implicit `softplayer.env`-next-to-the-exe search only kicks in for
the frozen `.exe` distribution.
