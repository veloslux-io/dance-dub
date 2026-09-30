# PyInstaller spec for Dance Dub (onedir).
# Build:  pyinstaller dance-dub.spec
# Result: dist/DanceDub/ folder containing DanceDub.exe

from PyInstaller.utils.hooks import collect_all, collect_data_files

# Non-Python payloads PyInstaller's static analysis tends to miss.
datas = []
binaries = []
hiddenimports = [
    "dance_dub",
    "dance_dub.pipeline",
    "dance_dub.wix_api",
    "dance_dub.app",
    "dance_dub.cli",
]
# faster_whisper ships its Silero VAD model (assets/*.onnx) as package data.
for pkg in (
    "ctranslate2", "faster_whisper", "imageio_ffmpeg", "onnxruntime", "tokenizers", "tqdm",
):
    pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_bins
    hiddenimports += pkg_hidden

a = Analysis(
    ["src/dance_dub/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DanceDub",
    debug=False,
    bootloader_traces=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="DanceDub",
)
