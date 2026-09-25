"""
ui/tray.py
-----------
Icono de bandeja del sistema con menú contextual:
  * Mostrar/ocultar ventana principal
  * Activar/desactivar la lógica automática de prioridades
  * Forzar rápidamente un dispositivo predeterminado (submenú con
    los dispositivos de salida activos)
  * Salir de la aplicación (apaga hilos/streams de forma prolija)
"""
from __future__ import annotations

from typing import List

from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from config import save_config
from core.device_manager import AudioDeviceInfo, Direction, set_default_output


class TrayIcon(QSystemTrayIcon):
    def __init__(self, main_window, icon: QIcon, app: QApplication):
        super().__init__(icon, main_window)
        self.main_window = main_window
        self.app = app
        self._devices: List[AudioDeviceInfo] = []
        self.setToolTip("Audio Manager Pro")

        self.menu = QMenu()
        self.action_toggle_window = self.menu.addAction("Mostrar / Ocultar")
        self.action_toggle_window.triggered.connect(self._toggle_window)

        self.menu.addSeparator()
        self.action_toggle_rules = self.menu.addAction("Reglas automáticas activas")
        self.action_toggle_rules.setCheckable(True)
        self.action_toggle_rules.setChecked(main_window.config.auto_rules_enabled)
        self.action_toggle_rules.triggered.connect(self._toggle_rules)

        self.menu.addSeparator()
        self.quick_devices_menu = self.menu.addMenu("Forzar dispositivo predeterminado")

        self.menu.addSeparator()
        self.action_quit = self.menu.addAction("Salir")
        self.action_quit.triggered.connect(self._quit)

        self.setContextMenu(self.menu)
        self.activated.connect(self._on_activated)

        main_window.worker.devices_changed.connect(self._on_devices_changed)

    # ---------------------------------------------------------------
    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_window()

    def _toggle_window(self) -> None:
        if self.main_window.isVisible():
            self.main_window.hide()
        else:
            self.main_window.show()
            self.main_window.raise_()
            self.main_window.activateWindow()

    def _toggle_rules(self, checked: bool) -> None:
        self.main_window.config.auto_rules_enabled = checked
        save_config(self.main_window.config)
        # Mantiene sincronizado el switch de la página de Reglas si está abierta
        self.main_window.rules_page.switch_enabled.setChecked(checked)

    def _on_devices_changed(self, devices: List[AudioDeviceInfo]) -> None:
        self._devices = devices
        self.quick_devices_menu.clear()
        for d in devices:
            if d.direction != Direction.OUTPUT:
                continue
            label = d.name + ("  ✓" if d.is_default_console else "")
            action = QAction(label, self.quick_devices_menu)
            action.triggered.connect(lambda checked=False, dev_id=d.id: self._force_device(dev_id))
            self.quick_devices_menu.addAction(action)

    def _force_device(self, device_id: str) -> None:
        try:
            set_default_output(device_id, also_communications=True)
            self.showMessage("Audio Manager Pro", "Dispositivo predeterminado actualizado.",
                              QSystemTrayIcon.MessageIcon.Information, 2000)
        except Exception as exc:
            self.showMessage("Audio Manager Pro", f"Error: {exc}",
                              QSystemTrayIcon.MessageIcon.Warning, 3000)

    def _quit(self) -> None:
        self.main_window.shutdown()
        self.app.quit()
