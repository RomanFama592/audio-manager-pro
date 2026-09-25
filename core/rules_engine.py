"""
core/rules_engine.py
---------------------
Evalúa las reglas de prioridad del usuario (config.PriorityRule) contra el
estado actual de dispositivos y decide si corresponde cambiar el
predeterminado automáticamente (ej. "si se conecta 'Auriculares Bluetooth'
pasa a ser el default; si se desconecta, vuelve a 'Altavoces'").

Este módulo es puro (sin dependencias de Qt/COM) para poder testearse
fácilmente; recibe la lista de dispositivos activos y una función para
aplicar el cambio (inyectada desde device_manager).
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from config import AppConfig, PriorityRule
from core.device_manager import AudioDeviceInfo, Direction, set_default_output, set_default_communications_output

logger = logging.getLogger("audio_manager.rules_engine")


class RulesEngine:
    def __init__(self, config: AppConfig,
                 apply_default_output: Callable[[str, bool], None] = None,
                 apply_default_comm: Callable[[str], None] = None):
        self.config = config
        # Se permite inyectar las funciones de aplicación (para tests);
        # por defecto usan las reales de device_manager.
        self._apply_default_output = apply_default_output or (
            lambda dev_id, also_comm: set_default_output(dev_id, also_communications=also_comm)
        )
        self._apply_default_comm = apply_default_comm or set_default_communications_output
        self._last_applied_device_id: Optional[str] = None

    def _best_rule_for(self, devices: List[AudioDeviceInfo]) -> Optional[tuple]:
        """Devuelve (regla, dispositivo) de mayor prioridad (menor `priority`)
        entre los dispositivos de SALIDA activos que matchean alguna regla
        habilitada."""
        candidates = []
        for rule in self.config.rules:
            if not rule.enabled:
                continue
            for dev in devices:
                if dev.direction != Direction.OUTPUT or dev.state != "Active":
                    continue
                if rule.matches(dev.name):
                    candidates.append((rule, dev))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0].priority)
        return candidates[0]

    def evaluate_and_apply(self, devices: List[AudioDeviceInfo]) -> Optional[str]:
        """Se debe llamar cada vez que cambia el set de dispositivos activos
        (disparado por DeviceWatcher). Devuelve un mensaje descriptivo si
        aplicó un cambio, o None si no hizo nada."""
        if not self.config.auto_rules_enabled:
            return None

        best = self._best_rule_for(devices)
        if best is None:
            return None

        rule, device = best
        if device.id == self._last_applied_device_id:
            return None  # ya está aplicado, evita loops de re-aplicación

        try:
            if rule.role in ("console", "both"):
                self._apply_default_output(device.id, rule.role == "both")
            if rule.role in ("communications", "both") and rule.role != "both":
                self._apply_default_comm(device.id)
            self._last_applied_device_id = device.id
            msg = f"Regla aplicada: '{rule.match_text}' -> {device.name} predeterminado ({rule.role})"
            logger.info(msg)
            return msg
        except Exception:
            logger.exception("No se pudo aplicar la regla de prioridad")
            return None
