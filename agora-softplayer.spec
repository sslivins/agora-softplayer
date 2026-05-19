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
