"""
ui/signals_bridge.py
----------------------
Puente entre la capa `core` (que corre en hilos nativos de Python /
callbacks de COM y de PortAudio) y la GUI Qt. Todo el trabajo pesado
(enumeración de dispositivos, apertura de streams, reglas automáticas)
se ejecuta en un QThread dedicado; los resultados vuelven a la GUI
exclusivamente vía señales (Qt serializa la entrega en el hilo principal).

Para PEDIR algo al backend desde la GUI se emite una de las señales
`*_requested` del worker: como el worker vive en otro hilo, Qt encola la
llamada y el slot se ejecuta en el hilo del backend (nunca en el de la GUI).
"""
from __future__ import annotations

import logging
from typing import List

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

import comtypes

from core.device_manager import DeviceEvent, DeviceWatcher, list_devices, AudioDeviceInfo
from core.mirror_engine import (
    MirrorEngine, MirrorError, SOURCE_INPUT, SOURCE_LOOPBACK, query_wasapi_devices,
)
from core.rules_engine import RulesEngine
from config import AppConfig

logger = logging.getLogger("audio_manager.bridge")

# Un enchufe/desenchufe dispara varias notificaciones seguidas (estado +
# default x3 roles); se agrupan para refrescar una sola vez.
DEVICE_EVENT_DEBOUNCE_MS = 300
MIRROR_HEALTH_INTERVAL_MS = 2000

# Sesiones de mirroring independientes (pueden correr a la vez).
MIRROR_SYSTEM = "system"   # audio que suena en una salida (loopback)
MIRROR_MIC = "mic"         # micrófono
_MIRROR_SOURCE_KINDS = {MIRROR_SYSTEM: SOURCE_LOOPBACK, MIRROR_MIC: SOURCE_INPUT}


class AudioBackendWorker(QObject):
    """Vive en un QThread propio. Mantiene el DeviceWatcher (que a su vez
    usa su propio hilo con apartamento COM) y las sesiones de mirroring, y
    expone señales para refrescar la GUI."""

    # ---- backend -> GUI ----
    devices_changed = Signal(list)          # List[AudioDeviceInfo]
    rule_applied = Signal(str)              # mensaje descriptivo
    mirror_error = Signal(str, str)         # (device_name, message)
    # {"outputs": [...], "inputs": [...], "default_output": str, "default_input": str}
    mirror_devices_changed = Signal(dict)
    # {"kind", "running", "message", "source", "targets"}
    mirror_state_changed = Signal(dict)
    backend_ready = Signal()
    backend_error = Signal(str)

    # ---- GUI -> backend (se ejecutan en el hilo del worker) ----
    refresh_requested = Signal()
    rules_changed = Signal()
    mirror_start_requested = Signal(str, str, list)  # (tipo, fuente, [destinos])
    mirror_stop_requested = Signal(str)              # (tipo)
    mirror_devices_requested = Signal()
    stop_requested = Signal()

    # ---- interno: hilo COM -> worker ----
    _device_event = Signal(str)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.watcher: DeviceWatcher | None = None
        self.rules_engine: RulesEngine | None = None
        self.mirror_engines: dict[str, MirrorEngine] = {}
        self._com_initialized = False
        self._topology_dirty = False
        self._debounce: QTimer | None = None
        self._health_timer: QTimer | None = None

        self.refresh_requested.connect(self.refresh_now)
        self.rules_changed.connect(self.on_rules_changed)
        self.mirror_start_requested.connect(self.start_mirror)
        self.mirror_stop_requested.connect(self.stop_mirror)
        self.mirror_devices_requested.connect(self.reload_mirror_devices)
        self.stop_requested.connect(self.stop)
        self._device_event.connect(self._on_device_event)

    @Slot()
    def start(self) -> None:
        """Se ejecuta ya dentro del QThread (conectado a `started`)."""
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self._com_initialized = True

            # Los timers se crean acá para que pertenezcan al hilo del worker.
            self._debounce = QTimer(self)
            self._debounce.setSingleShot(True)
            self._debounce.setInterval(DEVICE_EVENT_DEBOUNCE_MS)
            self._debounce.timeout.connect(self._process_device_changes)

            self._health_timer = QTimer(self)
            self._health_timer.setInterval(MIRROR_HEALTH_INTERVAL_MS)
            self._health_timer.timeout.connect(self._check_mirror_health)

            self.rules_engine = RulesEngine(self.config)
            self.mirror_engines = {
                kind: MirrorEngine(on_error=self._on_mirror_error, source_kind=source_kind)
                for kind, source_kind in _MIRROR_SOURCE_KINDS.items()
            }
            # El callback corre en un hilo de COM: solo emite una señal, que
            # Qt encola hacia este hilo.
            self.watcher = DeviceWatcher(on_change=self._device_event.emit)
            self.watcher.start()
            self._process_device_changes()  # primer refresco inmediato
            self._emit_mirror_devices(refresh=False)
            self.backend_ready.emit()
        except Exception as exc:
            logger.exception("Error inicializando el backend de audio")
            self.backend_error.emit(str(exc))

    # ------------------------------------------------------------------
    # Dispositivos y reglas
    # ------------------------------------------------------------------
    @Slot(str)
    def _on_device_event(self, kind: str) -> None:
        if kind != DeviceEvent.DEFAULT:
            self._topology_dirty = True
        if self._debounce is not None:
            self._debounce.start()  # reinicia la espera

    @Slot()
    def _process_device_changes(self) -> None:
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

        if self._topology_dirty:
            self._topology_dirty = False
            self._check_mirror_health()
            self._emit_mirror_devices(refresh=True)

    @Slot()
    def refresh_now(self) -> None:
        if self._debounce is not None:
            self._debounce.stop()
        self._process_device_changes()

    @Slot()
    def on_rules_changed(self) -> None:
        """Las reglas cambiaron en la UI: se re-evalúan ya, imponiendo la
        mejor regla aunque antes se hubiera aplicado otra."""
        if self.rules_engine:
            self.rules_engine.reset()
        self.refresh_now()

    # ------------------------------------------------------------------
    # Mirroring
    # ------------------------------------------------------------------
    @property
    def mirror_engine(self) -> MirrorEngine | None:
        """Sesión de audio del sistema (compatibilidad)."""
        return self.mirror_engines.get(MIRROR_SYSTEM)

    def _emit_mirror_devices(self, refresh: bool) -> None:
        if not self.mirror_engines:
            return
        try:
            devices = query_wasapi_devices(refresh=refresh)
        except Exception as exc:
            logger.exception("Error listando dispositivos WASAPI")
            self.mirror_error.emit("WASAPI", f"No se pudieron listar dispositivos: {exc}")
            return
        self.mirror_devices_changed.emit(devices)

    def _emit_mirror_state(self, kind: str, message: str = "") -> None:
        engine = self.mirror_engines.get(kind)
        running = bool(engine and engine.is_running())
        self.mirror_state_changed.emit({
            "kind": kind,
            "running": running,
            "message": message,
            "source": engine.source_name if running else "",
            "targets": engine.target_names if running else [],
        })

    def _update_health_timer(self) -> None:
        if self._health_timer is None:
            return
        if any(e.is_running() for e in self.mirror_engines.values()):
            if not self._health_timer.isActive():
                self._health_timer.start()
        else:
            self._health_timer.stop()

    @Slot()
    def reload_mirror_devices(self) -> None:
        self._emit_mirror_devices(refresh=True)

    @Slot(str, str, list)
    def start_mirror(self, kind: str, source_name: str, target_names: list) -> None:
        engine = self.mirror_engines.get(kind)
        if engine is None:
            self.mirror_error.emit("Mirroring", "El backend de audio no está inicializado.")
            self._emit_mirror_state(kind)
            return
        if engine.is_same_session(source_name, list(target_names)):
            # Pedido repetido (p. ej. doble clic): ya está corriendo con esa
            # configuración, no se corta ni se reabre nada.
            logger.info("Mirroring (%s): pedido repetido ignorado", kind)
            self._emit_mirror_state(kind)
            return
        try:
            failures = engine.start(source_name, list(target_names))
        except Exception as exc:
            if isinstance(exc, RuntimeError):  # error esperable (validación / dispositivo)
                logger.warning("No se pudo iniciar el mirroring (%s): %s", kind, exc)
            else:
                logger.exception("No se pudo iniciar el mirroring (%s)", kind)
            self._update_health_timer()
            self.mirror_error.emit(source_name or "Mirroring", f"No se pudo iniciar el mirroring: {exc}")
            self._emit_mirror_state(kind)
            self._emit_mirror_devices(refresh=False)  # la lista pudo cambiar al reinicializar
            return

        for err in failures:
            self._on_mirror_error(err)
        self._update_health_timer()
        self._emit_mirror_state(kind, f"Duplicando hacia {len(engine.target_names)} destino(s).")
        self._emit_mirror_devices(refresh=False)

    @Slot(str)
    def stop_mirror(self, kind: str) -> None:
        engine = self.mirror_engines.get(kind)
        was_running = bool(engine and engine.is_running())
        if engine:
            engine.stop()
        self._update_health_timer()
        self._emit_mirror_state(kind, "Mirroring detenido." if was_running else "")

    @Slot()
    def _check_mirror_health(self) -> None:
        for kind, engine in self.mirror_engines.items():
            if engine.is_running() and not engine.check_health():
                self._emit_mirror_state(kind)  # los motivos ya llegaron por mirror_error
        self._update_health_timer()

    def _on_mirror_error(self, err: MirrorError) -> None:
        self.mirror_error.emit(err.device_name, err.message)

    # ------------------------------------------------------------------
    # Apagado
    # ------------------------------------------------------------------
    @Slot()
    def stop(self) -> None:
        """Se ejecuta en el hilo del worker (vía `stop_requested`) para que
        CoUninitialize ocurra en el mismo hilo que CoInitializeEx. Al
        terminar, cierra el event loop del QThread."""
        try:
            for timer in (self._debounce, self._health_timer):
                if timer is not None:
                    timer.stop()
            if self.watcher:
                self.watcher.stop()
            for engine in self.mirror_engines.values():
                engine.stop()
        except Exception:
            logger.exception("Error deteniendo el backend de audio")
        finally:
            if self._com_initialized:
                self._com_initialized = False
                comtypes.CoUninitialize()
            thread = QThread.currentThread()
            if thread is not None:
                thread.quit()


def make_backend_thread(config: AppConfig):
    """Crea el QThread + worker y los deja listos para `thread.start()`."""
    thread = QThread()
    thread.setObjectName("AudioBackendThread")
    worker = AudioBackendWorker(config)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    return thread, worker
