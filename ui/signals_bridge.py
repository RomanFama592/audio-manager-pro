"""
ui/signals_bridge.py
----------------------
Puente entre la capa `core` (que corre en hilos nativos de Python /
callbacks de COM y de sounddevice) y la GUI Qt. Todo el trabajo pesado
(enumeración de dispositivos, apertura de streams, llamadas COM) se
dispara en un QThread dedicado; los resultados vuelven a la GUI
exclusivamente vía señales (Qt serializa la entrega en el hilo principal).
"""
from __future__ import annotations

import logging
from typing import List

from PySide6.QtCore import QObject, QThread, Signal

import comtypes

from core.device_manager import DeviceWatcher, list_devices, AudioDeviceInfo
from core.mirror_engine import MirrorEngine, MirrorError
from core.rules_engine import RulesEngine
from config import AppConfig

logger = logging.getLogger("audio_manager.bridge")


class AudioBackendWorker(QObject):
    """Vive en un QThread propio. Mantiene el DeviceWatcher (que a su vez
    usa su propio hilo con apartamento COM) y el MirrorEngine, y expone
    señales para refrescar la GUI."""

    devices_changed = Signal(list)          # List[AudioDeviceInfo]
    rule_applied = Signal(str)              # mensaje descriptivo
    mirror_error = Signal(str, str)         # (device_name, message)
    backend_ready = Signal()
    backend_error = Signal(str)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.watcher: DeviceWatcher | None = None
        self.rules_engine: RulesEngine | None = None
        self.mirror_engine: MirrorEngine | None = None

    def start(self) -> None:
        """Se ejecuta ya dentro del QThread (conectado a `started`)."""
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self.rules_engine = RulesEngine(self.config)
            self.mirror_engine = MirrorEngine(on_error=self._on_mirror_error)
            self.watcher = DeviceWatcher(on_change=self._on_devices_changed)
            self.watcher.start()
            self._on_devices_changed()  # primer refresco inmediato
            self.backend_ready.emit()
        except Exception as exc:
            logger.exception("Error inicializando el backend de audio")
            self.backend_error.emit(str(exc))

    def _on_devices_changed(self) -> None:
        try:
            devices: List[AudioDeviceInfo] = list_devices(include_disabled=False)
        except Exception:
            logger.exception("Error listando dispositivos")
            return
        self.devices_changed.emit(devices)
        if self.rules_engine:
            msg = self.rules_engine.evaluate_and_apply(devices)
            if msg:
                self.rule_applied.emit(msg)

    def _on_mirror_error(self, err: MirrorError) -> None:
        self.mirror_error.emit(err.device_name, err.message)

    # ---- acciones invocadas desde la GUI (via slots con queued connection) ----
    def refresh_now(self) -> None:
        self._on_devices_changed()

    def stop(self) -> None:
        if self.watcher:
            self.watcher.stop()
        if self.mirror_engine:
            self.mirror_engine.stop()
        comtypes.CoUninitialize()


def make_backend_thread(config: AppConfig):
    """Crea el QThread + worker y los deja listos para `thread.start()`."""
    thread = QThread()
    worker = AudioBackendWorker(config)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    return thread, worker
