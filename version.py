"""
version.py
----------
Única fuente de verdad de la versión y los datos de la app. La usan la app
(pantalla de Configuración), el .spec de PyInstaller (recurso de versión del
.exe) y build.ps1 (versión del instalador).

Al publicar una versión nueva solo hay que cambiar `__version__`.
"""
APP_NAME = "Audio Manager Pro"
APP_EXE = "AudioManagerPro"
PUBLISHER = "Famá Román"
__version__ = "1.1.0"
