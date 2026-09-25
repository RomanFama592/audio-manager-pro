# -*- mode: python ; coding: utf-8 -*-
# Construir con:  pyinstaller AudioManagerPro.spec
import sys
from PyInstaller.utils.hooks import collect_all

datas = [("resources", "resources")]
binaries = []
hiddenimports = [
    "pycaw.pycaw",
    "comtypes.stream",
    "sounddevice",
]

# qfluentwidgets y PySide6 a veces requieren recolección explícita de
# datos/plugins (QSS, iconos, plugins de plataforma) para funcionar
# correctamente al empaquetar.
for pkg in ("qfluentwidgets", "PySide6"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="AudioManagerPro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # sin consola: es una app de bandeja/GUI
    icon="resources/app_icon.ico",
    onefile=True,
)
