"""
main.py
--------
Punto de entrada de Audio Manager Pro.

Uso:
    python main.py

Requiere Windows 10/11 y las dependencias de requirements.txt.
"""
from __future__ import annotations

import logging
import os
import sys

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from config import load_config
from ui.main_window import MainWindow
from ui.tray import TrayIcon

LOG_PATH = os.path.join(os.environ.get("APPDATA", "."), "AudioManagerPro", "app.log")


def setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def resource_path(relative_path: str) -> str:
    """Resuelve rutas de recursos tanto en desarrollo como empaquetado
    con PyInstaller (usa sys._MEIPASS cuando corre como .exe)."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def main() -> int:
    if sys.platform != "win32":
        print("Audio Manager Pro requiere Windows 10/11 (WASAPI).", file=sys.stderr)
        return 1

    setup_logging()
    logger = logging.getLogger("audio_manager.main")
    logger.info("Iniciando Audio Manager Pro...")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # se mantiene vivo en la bandeja al cerrar la ventana

    config = load_config()

    icon_path = resource_path(os.path.join("resources", "app_icon.ico"))
    icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon.fromTheme("audio-speakers")

    window = MainWindow(config)
    if icon_path and os.path.exists(icon_path):
        window.setWindowIcon(icon)

    tray = TrayIcon(window, icon, app)
    tray.show()

    if not config.start_minimized:
        window.show()

    def on_about_to_quit():
        window.shutdown()

    app.aboutToQuit.connect(on_about_to_quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
