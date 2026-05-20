# PyInstaller build spec for agora-softplayer.
# Build with: pyinstaller agora-softplayer.spec
# Output:     dist\agora-softplayer.exe
#
# Architecture is implicit: PyInstaller targets whichever Python it runs
# under. CI invokes this spec on windows-latest (amd64) and windows-11-arm
# (arm64) to produce the two release binaries; the workflow renames them
# to agora-softplayer-<arch>.exe at upload time.
# ruff: noqa: F821

block_cipher = None

a = Analysis(
    ["src/agora_softplayer/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        # uvicorn picks these up lazily; PyInstaller can't see the import.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Drop Windows OS-shipped runtime DLLs from the bundle. PyInstaller's
# default behavior on Windows is to pull the Universal C Runtime
# (`ucrtbase.dll`, `api-ms-win-*.dll`) and `vcruntime*.dll` from the
# Python install into the bundle, where they get extracted to a temp
# `_MEI*` directory at startup. On Windows-on-ARM the kernel's stricter
# Code Integrity rejects the loose, untrusted copy with
# `STATUS_INVALID_IMAGE_HASH (0xC0E90002)`, so the .exe fails to start
# with "ucrtbase.dll is either not designed to run on Windows or it
# contains an error". These DLLs are guaranteed to exist in
# `C:\Windows\System32` on every supported Windows version, so we
# excise them from the bundle and let the loader resolve them from the
# OS at runtime.
_OS_DLL_PREFIXES = ("ucrtbase", "vcruntime", "api-ms-win-")


def _is_os_runtime_dll(name: str) -> bool:
    lower = name.lower()
    return any(lower.startswith(p) or ("/" + p) in lower or ("\\" + p) in lower for p in _OS_DLL_PREFIXES)


a.binaries = [b for b in a.binaries if not _is_os_runtime_dll(b[0])]
a.datas    = [d for d in a.datas    if not _is_os_runtime_dll(d[0])]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="agora-softplayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # keep a console window for now; useful for logs in M1
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
