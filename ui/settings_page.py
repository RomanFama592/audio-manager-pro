"""
ui/settings_page.py
--------------------
Página de Configuración: inicio con Windows, inicio minimizado y datos de
la app (versión, carpeta de configuración/logs).
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QVBoxLayout, QWidget

from qfluentwidgets import (
    FluentIcon as FIF, InfoBar, InfoBarPosition, PushSettingCard, SettingCard,
    SettingCardGroup, SubtitleLabel, SwitchSettingCard,
)

from config import AppConfig, get_config_dir, save_config
from core import autostart
from version import APP_NAME, PUBLISHER, __version__

logger = logging.getLogger("audio_manager.settings")


class _SwitchCard(SwitchSettingCard):
    """SwitchSettingCard con los textos en español (la original muestra
    "On"/"Off")."""

    def __init__(self, icon, title, content=None, parent=None):
        super().__init__(icon, title, content, parent=parent)
        self.setValue(False)

    def setValue(self, isChecked: bool) -> None:
        self.switchButton.setChecked(isChecked)
        self.switchButton.setText("Activado" if isChecked else "Desactivado")

    def set_silently(self, isChecked: bool) -> None:
        """Actualiza el estado sin disparar `checkedChanged`."""
        self.switchButton.blockSignals(True)
        try:
            self.setValue(isChecked)
        finally:
            self.switchButton.blockSignals(False)


class SettingsPage(QWidget):
    autostart_changed = Signal(bool)  # para sincronizar la bandeja

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsPage")
        self.config = config

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("Configuración"))

        # ---- Inicio ----
        startup = SettingCardGroup("Inicio", self)
        self.card_autostart = _SwitchCard(
            FIF.POWER_BUTTON, "Iniciar con Windows",
            "Arranca en la bandeja del sistema al iniciar sesión (sin acceso directo manual).",
            parent=startup,
        )
        self.card_minimized = _SwitchCard(
            FIF.MINIMIZE, "Iniciar minimizado",
            "Al abrir la app manualmente, quedarse en la bandeja sin mostrar la ventana.",
            parent=startup,
        )
        startup.addSettingCard(self.card_autostart)
        startup.addSettingCard(self.card_minimized)
        layout.addWidget(startup)

        # ---- Acerca de ----
        about = SettingCardGroup("Acerca de", self)
        self.card_version = SettingCard(FIF.INFO, f"{APP_NAME} {__version__}", f"© {PUBLISHER}", about)
        self.card_folder = PushSettingCard(
            "Abrir carpeta", FIF.FOLDER, "Configuración y registros", get_config_dir(), about,
        )
        about.addSettingCard(self.card_version)
        about.addSettingCard(self.card_folder)
        layout.addWidget(about)
        layout.addStretch(1)

        self.card_minimized.set_silently(config.start_minimized)
        self.refresh_autostart()

        self.card_autostart.checkedChanged.connect(self.set_autostart)
        self.card_minimized.checkedChanged.connect(self._on_minimized_changed)
        self.card_folder.clicked.connect(lambda: os.startfile(get_config_dir()))

    # ---------------------------------------------------------------
    def autostart_enabled(self) -> bool:
        return autostart.is_enabled()

    @Slot()
    def refresh_autostart(self) -> None:
        """Relee el registro (el usuario pudo cambiarlo desde Windows)."""
        enabled = autostart.is_enabled()
        self.card_autostart.set_silently(enabled)
        self.autostart_changed.emit(enabled)

    @Slot(bool)
    def set_autostart(self, enabled: bool) -> None:
        try:
            autostart.set_enabled(enabled)
        except OSError as exc:
            logger.exception("No se pudo cambiar el inicio con Windows")
            InfoBar.error("Error", f"No se pudo cambiar el inicio con Windows: {exc}",
                          position=InfoBarPosition.TOP, parent=self.window())
        self.refresh_autostart()

    def _on_minimized_changed(self, checked: bool) -> None:
        self.config.start_minimized = checked
        save_config(self.config)

    def showEvent(self, event) -> None:
        self.refresh_autostart()
        super().showEvent(event)
