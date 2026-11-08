"""
core/autostart.py
------------------
Inicio automático con Windows mediante la clave del usuario
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (no requiere
permisos de administrador). Es el mismo valor que crea el instalador, así
que la app y el instalador comparten el estado.

También se respeta el interruptor de Windows (Configuración > Aplicaciones >
Inicio / Administrador de tareas), que guarda su estado en
...\\Explorer\\StartupApproved\\Run: si el usuario lo desactivó ahí, la app
lo muestra como desactivado, y al activarlo desde la app se limpia esa marca.
"""
from __future__ import annotations

import logging
import os
import sys
import winreg
from typing import Optional

logger = logging.getLogger("audio_manager.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "AudioManagerPro"
MINIMIZED_ARG = "--minimized"


def launch_command() -> str:
    """Comando que Windows ejecuta al iniciar sesión (arranca en la bandeja)."""
    if getattr(sys, "frozen", False):  # .exe de PyInstaller
        return f'"{sys.executable}" {MINIMIZED_ARG}'
    # Desde el código fuente: pythonw.exe (sin ventana de consola) + main.py
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "main.py"))
    return f'"{exe}" "{main_py}" {MINIMIZED_ARG}'


def get_command(run_key: str = RUN_KEY, value_name: str = VALUE_NAME) -> Optional[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value)
    except OSError:
        return None


def _disabled_by_windows(approved_key: str, value_name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_key) as key:
            data, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return False
    # Primer byte par (02/06) = habilitado, impar (03/07) = deshabilitado.
    return bool(data) and bool(data[0] & 1)


def is_enabled(run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY,
               value_name: str = VALUE_NAME) -> bool:
    return get_command(run_key, value_name) is not None and not _disabled_by_windows(approved_key, value_name)


def set_enabled(enabled: bool, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY,
                value_name: str = VALUE_NAME, command: Optional[str] = None) -> None:
    """Activa/desactiva el inicio con Windows. Lanza OSError si no se pudo
    escribir el registro."""
    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command or launch_command())
        _delete_value(approved_key, value_name)  # quita un "deshabilitado" de Windows
        logger.info("Inicio con Windows activado")
    else:
        _delete_value(run_key, value_name)
        _delete_value(approved_key, value_name)
        logger.info("Inicio con Windows desactivado")


def repair_if_stale(run_key: str = RUN_KEY, value_name: str = VALUE_NAME) -> bool:
    """Si el inicio automático apunta a un .exe que ya no existe (se movió
    la instalación) y estamos corriendo como .exe, lo actualiza. Devuelve
    True si lo reparó."""
    if not getattr(sys, "frozen", False):
        return False
    command = get_command(run_key, value_name)
    if not command:
        return False
    target = command.split('"')[1] if command.startswith('"') else command.split(" ")[0]
    if os.path.exists(target):
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, launch_command())
        logger.info("Inicio con Windows apuntaba a %s (inexistente); actualizado", target)
        return True
    except OSError:
        logger.exception("No se pudo actualizar el inicio con Windows")
        return False


def _delete_value(key_path: str, value_name: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass
