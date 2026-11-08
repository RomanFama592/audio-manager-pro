"""
main.py
--------
Punto de entrada de Audio Manager Pro.

Uso:
    python main.py              abre la app (o muestra la ventana si ya corre)
    python main.py --minimized  arranca directo en la bandeja (inicio con Windows)
    python main.py --quit       cierra la instancia en ejecución (lo usa el instalador)

Requiere Windows 10/11 y las dependencias de requirements.txt.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication, QStyle
from PySide6.QtGui import QIcon

from config import get_config_dir, load_config
from version import APP_NAME, __version__

LOG_PATH = os.path.join(get_config_dir(), "app.log")


def setup_logging() -> None:
    handlers: list[logging.Handler] = [
        # Con rotación: antes el log crecía sin límite (varios MB).
        RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    # En el .exe sin consola (PyInstaller --windowed) sys.stdout es None.
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    install_exception_hooks()


def install_exception_hooks() -> None:
    """Deja en el log las excepciones no capturadas (en el .exe no hay
    consola donde verlas)."""
    logger = logging.getLogger("audio_manager.unhandled")

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Excepción no capturada", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        logger.critical("Excepción no capturada en el hilo %s",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def resource_path(relative_path: str) -> str:
    """Resuelve rutas de recursos tanto en desarrollo como empaquetado
    con PyInstaller (usa sys._MEIPASS cuando corre como .exe)."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="AudioManagerPro", add_help=False)
    parser.add_argument("--minimized", action="store_true",
                        help="arrancar en la bandeja sin mostrar la ventana")
    parser.add_argument("--quit", action="store_true",
                        help="cerrar la instancia en ejecución y salir")
    args, _unknown = parser.parse_known_args(argv)  # Qt puede recibir sus propios argumentos
    return args


def main() -> int:
    if sys.platform != "win32":
        print("Audio Manager Pro requiere Windows 10/11 (WASAPI).", file=sys.stderr)
        return 1

    args = parse_args(sys.argv[1:])
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)  # se mantiene vivo en la bandeja al cerrar la ventana

    # Instancia única: dos instancias tendrían dos iconos en la bandeja y
    # dos motores de reglas peleándose por el dispositivo predeterminado.
    from ui.single_instance import MSG_QUIT, MSG_SHOW, SingleInstance, server_name_for
    config_dir = get_config_dir()
    instance = SingleInstance(server_name_for(config_dir), os.path.join(config_dir, "app.lock"))
    if not instance.acquire():
        if args.quit:
            if instance.send(MSG_QUIT):
                instance.wait_until_released()
        elif not args.minimized:
            # Abrir la app desde el menú Inicio con la app ya en la bandeja:
            # se muestra la ventana existente. Un arranque automático
            # duplicado (--minimized) simplemente no hace nada.
            instance.send(MSG_SHOW)
        return 0
    if args.quit:
        instance.release()
        return 0  # no había nada que cerrar

    setup_logging()
    logger = logging.getLogger("audio_manager.main")
    logger.info("Iniciando %s %s...", APP_NAME, __version__)

    from core import autostart
    from ui.main_window import MainWindow
    from ui.tray import TrayIcon

    autostart.repair_if_stale()
    config = load_config()

    icon_path = resource_path(os.path.join("resources", "app_icon.ico"))
    icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
    if icon.isNull():
        # Sin ícono el QSystemTrayIcon no se muestra en absoluto.
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
    app.setWindowIcon(icon)

    window = MainWindow(config)
    window.setWindowIcon(icon)

    tray = TrayIcon(window, icon, app)
    tray.show()

    def on_instance_message(message: str) -> None:
        if message == MSG_SHOW:
            tray.show_window()
        elif message == MSG_QUIT:
            tray.quit_app()

    instance.message_received.connect(on_instance_message)

    if not (args.minimized or config.start_minimized):
        window.show()

    app.aboutToQuit.connect(window.shutdown)

    try:
        return app.exec()
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
