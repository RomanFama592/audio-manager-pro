# -*- mode: python ; coding: utf-8 -*-
# Construir con:  pyinstaller AudioManagerPro.spec   (o mejor: build.ps1)
#
# Genera dist\AudioManagerPro\ (modo carpeta / onedir), que es lo que
# empaqueta el instalador de Inno Setup. Frente a onefile: arranca más rápido
# (no descomprime ~65 MB en %TEMP% en cada inicio de Windows) y los antivirus
# lo marcan menos.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
    VSVersionInfo,
)

sys.path.insert(0, SPECPATH)
from version import APP_EXE, APP_NAME, PUBLISHER, __version__  # noqa: E402

_nums = tuple(int(p) for p in __version__.split("."))[:4]
_nums = _nums + (0,) * (4 - len(_nums))
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_nums, prodvers=_nums),
    kids=[
        StringFileInfo([StringTable("0C0A04B0", [   # es-ES, Unicode
            StringStruct("CompanyName", PUBLISHER),
            StringStruct("FileDescription", APP_NAME),
            StringStruct("FileVersion", __version__),
            StringStruct("InternalName", APP_EXE),
            StringStruct("OriginalFilename", f"{APP_EXE}.exe"),
            StringStruct("ProductName", APP_NAME),
            StringStruct("ProductVersion", __version__),
            StringStruct("LegalCopyright", f"© {PUBLISHER}"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0C0A, 1200])]),
    ],
)

datas = [("resources", "resources")]
# qfluentwidgets compila sus íconos/QSS como módulo Python (_rc), pero por
# si alguna versión trae archivos sueltos, se incluyen sus datos.
datas += collect_data_files("qfluentwidgets")

binaries = []
hiddenimports = [
    "pycaw.pycaw",
    "pycaw.callbacks",
    "comtypes.stream",
    "sounddevice",
    "pyaudiowpatch",
]

# PySide6 ya tiene hooks oficiales en PyInstaller que copian los plugins de
# Qt necesarios. Antes se usaba collect_all("PySide6"), que metía TODO Qt
# (WebEngine, Multimedia, 3D, Quick...) y dejaba un .exe de ~260 MB que
# además tardaba en arrancar (onefile lo descomprime en cada inicio).
excludes = [
    # Submódulos opcionales de qfluentwidgets / framelesswindow no usados
    "qfluentwidgets.multimedia",
    "qframelesswindow.webengine",
    # Módulos de Qt que la app no usa
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtHttpServer",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "tkinter",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

# Modo carpeta (onedir): el EXE no lleva binaries/datas; los junta COLLECT.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_EXE,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX suele corromper DLLs de Qt y enlentece el arranque
    console=False,          # sin consola: es una app de bandeja/GUI
    icon="resources/app_icon.ico",
    version=version_info,   # "Detalles" del .exe: versión, producto, autor
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_EXE,
)
