"""
core/rules_engine.py
---------------------
Evalúa las reglas de prioridad del usuario (config.PriorityRule) contra el
estado actual de dispositivos y decide si corresponde cambiar el
predeterminado automáticamente (ej. "si se conecta 'Auriculares Bluetooth'
pasa a ser el default; si se desconecta, vuelve a 'Altavoces'").

Este módulo es puro (sin dependencias de Qt) para poder testearse
fácilmente; recibe la lista de dispositivos activos y funciones para
aplicar el cambio (inyectables; por defecto las de device_manager).

Cada rol se evalúa por separado:
  * "console"        -> lo deciden las reglas con rol "console" o "both".
  * "communications" -> lo deciden las reglas con rol "communications" o "both".
Así una regla de rol "console" de máxima prioridad no impide que otra regla
de rol "communications" fije el dispositivo de comunicaciones.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

from config import AppConfig, PriorityRule
from core.device_manager import (
    AudioDeviceInfo, Direction, set_default_output, set_default_communications_output,
)

logger = logging.getLogger("audio_manager.rules_engine")

ROLE_CONSOLE = "console"
ROLE_COMM = "communications"

# Qué valores de `PriorityRule.role` participan en la decisión de cada rol.
_RULE_ROLES_FOR = {
    ROLE_CONSOLE: ("console", "both"),
    ROLE_COMM: ("communications", "both"),
}

_ROLE_LABELS = {
    ROLE_CONSOLE: "predeterminado",
    ROLE_COMM: "comunicaciones",
}


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
        # Último dispositivo aplicado por rol: evita re-aplicar la misma
        # regla en cada evento y respeta un cambio manual del usuario hasta
        # que cambie el "mejor" dispositivo según las reglas.
        self._last_applied: Dict[str, str] = {}

    def reset(self) -> None:
        """Olvida lo aplicado previamente: la próxima evaluación vuelve a
        imponer la mejor regla (se usa al editar reglas o reactivarlas)."""
        self._last_applied.clear()

    # TIP: para reglas de entradas, pasar direction=Direction.INPUT
    def _best_rule_for(self, devices: List[AudioDeviceInfo], role: str,
                       direction: Direction = Direction.OUTPUT
                       ) -> Optional[Tuple[PriorityRule, AudioDeviceInfo]]:
        """Devuelve (regla, dispositivo) de mayor prioridad (menor
        `priority`) para `role` entre los dispositivos activos de
        `direction` que matchean alguna regla habilitada."""
        accepted_roles = _RULE_ROLES_FOR[role]
        best: Optional[Tuple[PriorityRule, AudioDeviceInfo]] = None
        # Copia de la lista: la UI puede modificar las reglas desde otro hilo.
        for rule in list(self.config.rules):
            if not rule.enabled or rule.role not in accepted_roles:
                continue
            for dev in devices:
                if dev.direction != direction or dev.state != "Active":
                    continue
                if rule.matches(dev.name) and (best is None or rule.priority < best[0].priority):
                    best = (rule, dev)
        return best

    def evaluate_and_apply(self, devices: List[AudioDeviceInfo]) -> Optional[str]:
        """Se debe llamar cada vez que cambia el set de dispositivos activos
        (disparado por DeviceWatcher). Devuelve un mensaje descriptivo si
        aplicó algún cambio, o None si no hizo nada."""
        if not self.config.auto_rules_enabled:
            return None

        messages: List[str] = []
        for role in (ROLE_CONSOLE, ROLE_COMM):
            best = self._best_rule_for(devices, role)
            if best is None:
                continue
            rule, device = best
            if self._last_applied.get(role) == device.id:
                continue  # ya está aplicado, evita loops de re-aplicación

            already_default = device.is_default_console if role == ROLE_CONSOLE else device.is_default_comm
            if already_default:
                # Nada que cambiar; solo se recuerda para no pisar luego una
                # elección manual del usuario.
                self._last_applied[role] = device.id
                continue

            try:
                if role == ROLE_CONSOLE:
                    self._apply_default_output(device.id, False)  # consola + multimedia
                else:
                    self._apply_default_comm(device.id)
            except Exception:
                logger.exception("No se pudo aplicar la regla '%s' (%s)", rule.match_text, role)
                continue

            self._last_applied[role] = device.id
            messages.append(f"'{rule.match_text}' → {device.name} ({_ROLE_LABELS[role]})")

        if not messages:
            return None
        msg = "Regla aplicada: " + "; ".join(messages)
        logger.info(msg)
        return msg
