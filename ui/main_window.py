"""
ui/main_window.py
-------------------
Ventana principal con estética Fluent Design (Windows 11) usando
`qfluentwidgets` sobre PySide6. Tres secciones de navegación:
  1) Dispositivos  -> listar/filtrar, fijar default de sistema/comunicaciones
  2) Reglas        -> alta/baja de reglas de prioridad automática
  3) Mirroring     -> selección de destinos de duplicación de audio

La ventana NO hace llamadas COM/audio directamente: todo pasa por
`AudioBackendWorker` (ui/signals_bridge.py), que vive en su propio QThread.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QListWidgetItem,
)

from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon as FIF,
    PushButton, PrimaryPushButton, ListWidget, SwitchButton, InfoBar,
    InfoBarPosition, TableWidget, LineEdit, ComboBox, CardWidget,
    SubtitleLabel, BodyLabel, ToolButton, setTheme, Theme,
)

from config import AppConfig, PriorityRule, save_config
from core.device_manager import AudioDeviceInfo, Direction
from core.mirror_engine import MirrorEngine
from ui.signals_bridge import make_backend_thread

logger = logging.getLogger("audio_manager.ui")


# =========================================================================
# Sección: Dispositivos
# =========================================================================

class DevicesPage(QWidget):
    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.setObjectName("DevicesPage")
        self.worker = worker
        self._devices_by_id: Dict[str, AudioDeviceInfo] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("Dispositivos de audio"))

        columns = QHBoxLayout()
        # ---- Salidas ----
        out_card = CardWidget(self)
        out_layout = QVBoxLayout(out_card)
        out_layout.addWidget(BodyLabel("Salidas (altavoces / auriculares)"))
        self.output_list = ListWidget(out_card)
        out_layout.addWidget(self.output_list)
        out_btns = QHBoxLayout()
        self.btn_set_default_out = PrimaryPushButton(FIF.ACCEPT, "Predeterminado")
        self.btn_set_comm_out = PushButton(FIF.PHONE, "Comunicaciones")
        self.btn_set_both_out = ToolButton(FIF.PIN)
        self.btn_set_both_out.setFixedSize(32, 32)
        self.btn_set_both_out.setToolTip("Establecer en Predeterminado y en Comunicaciones a la vez")
        out_btns.addWidget(self.btn_set_default_out)
        out_btns.addWidget(self.btn_set_comm_out)
        out_btns.addWidget(self.btn_set_both_out)
        out_layout.addLayout(out_btns)
        columns.addWidget(out_card)

        # ---- Entradas ----
        in_card = CardWidget(self)
        in_layout = QVBoxLayout(in_card)
        in_layout.addWidget(BodyLabel("Entradas (micrófonos)"))
        self.input_list = ListWidget(in_card)
        in_layout.addWidget(self.input_list)
        in_btns = QHBoxLayout()
        self.btn_set_default_in = PrimaryPushButton(FIF.ACCEPT, "Predeterminado")
        self.btn_set_comm_in = PushButton(FIF.PHONE, "Comunicaciones")
        self.btn_set_both_in = ToolButton(FIF.PIN)
        self.btn_set_both_in.setFixedSize(32, 32)
        self.btn_set_both_in.setToolTip("Establecer en Predeterminado y en Comunicaciones a la vez")
        in_btns.addWidget(self.btn_set_default_in)
        in_btns.addWidget(self.btn_set_comm_in)
        in_btns.addWidget(self.btn_set_both_in)
        in_layout.addLayout(in_btns)
        columns.addWidget(in_card)

        layout.addLayout(columns)

        refresh_row = QHBoxLayout()
        self.btn_refresh = PushButton(FIF.SYNC, "Actualizar ahora")
        refresh_row.addStretch(1)
        refresh_row.addWidget(self.btn_refresh)
        layout.addLayout(refresh_row)
        layout.addStretch(1)

        self.btn_refresh.clicked.connect(self.worker.refresh_now)
        self.btn_set_default_out.clicked.connect(lambda: self._apply_output(comm=False))
        self.btn_set_comm_out.clicked.connect(lambda: self._apply_output(comm=True))
        self.btn_set_both_out.clicked.connect(lambda: (self._apply_output(comm=True),self._apply_output(comm=False)))
        self.btn_set_default_in.clicked.connect(lambda: self._apply_input(comm=False))
        self.btn_set_comm_in.clicked.connect(lambda: self._apply_input(comm=True))
        self.btn_set_both_in.clicked.connect(lambda: (self._apply_input(comm=True),self._apply_input(comm=False)))

    # ---------------------------------------------------------------
    @Slot(list)
    def on_devices_changed(self, devices: List[AudioDeviceInfo]) -> None:
        self._devices_by_id = {d.id: d for d in devices}
        self.output_list.clear()
        self.input_list.clear()
        for d in devices:
            label = d.name
            if d.is_default_console:
                label += "  •  Predeterminado"
            if d.is_default_comm:
                label += "  •  Comunicaciones"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, d.id)
            if d.direction == Direction.OUTPUT:
                self.output_list.addItem(item)
            else:
                self.input_list.addItem(item)

    def _selected_id(self, lst: ListWidget) -> Optional[str]:
        item = lst.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _apply_output(self, comm: bool) -> None:
        dev_id = self._selected_id(self.output_list)
        if not dev_id:
            InfoBar.warning("Sin selección", "Elegí primero un dispositivo de salida.",
                             position=InfoBarPosition.TOP, parent=self)
            return
        from core.device_manager import set_default_communications_output, set_default_output
        try:
            if comm:
                set_default_communications_output(dev_id)
            else:
                set_default_output(dev_id, also_communications=True)
            InfoBar.success("Listo", "Dispositivo de salida actualizado.",
                             position=InfoBarPosition.TOP, parent=self)
        except Exception as exc:
            InfoBar.error("Error", f"No se pudo cambiar el dispositivo: {exc}",
                           position=InfoBarPosition.TOP, parent=self)

    def _apply_input(self, comm: bool) -> None:
        dev_id = self._selected_id(self.input_list)
        if not dev_id:
            InfoBar.warning("Sin selección", "Elegí primero un micrófono.",
                             position=InfoBarPosition.TOP, parent=self)
            return
        from core.device_manager import set_default_device, Role
        try:
            if comm:
                set_default_device(dev_id, Role.COMMUNICATIONS)
            else:
                set_default_device(dev_id, Role.CONSOLE)
                set_default_device(dev_id, Role.MULTIMEDIA)
            InfoBar.success("Listo", "Micrófono predeterminado actualizado.",
                             position=InfoBarPosition.TOP, parent=self)
        except Exception as exc:
            InfoBar.error("Error", f"No se pudo cambiar el micrófono: {exc}",
                           position=InfoBarPosition.TOP, parent=self)


# =========================================================================
# Sección: Reglas de prioridad
# =========================================================================

class RulesPage(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setObjectName("RulesPage")
        self.config = config

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("Reglas automáticas de prioridad"))

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(BodyLabel("Activar lógica automática de prioridades"))
        self.switch_enabled = SwitchButton()
        self.switch_enabled.setChecked(config.auto_rules_enabled)
        toggle_row.addWidget(self.switch_enabled)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Texto a coincidir", "Rol", "Prioridad", "Activa"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        form_row = QHBoxLayout()
        self.input_match = LineEdit()
        self.input_match.setPlaceholderText("Ej: Auriculares Bluetooth")
        self.combo_role = ComboBox()
        self.combo_role.addItems(["both", "console", "communications"])
        self.btn_add = PrimaryPushButton(FIF.ADD, "Agregar regla")
        self.btn_remove = PushButton(FIF.DELETE, "Quitar seleccionada")
        form_row.addWidget(self.input_match, 2)
        form_row.addWidget(self.combo_role, 1)
        form_row.addWidget(self.btn_add)
        form_row.addWidget(self.btn_remove)
        layout.addLayout(form_row)
        layout.addStretch(1)

        self.switch_enabled.checkedChanged.connect(self._on_toggle)
        self.btn_add.clicked.connect(self._on_add_rule)
        self.btn_remove.clicked.connect(self._on_remove_rule)
        
        # Conexión del evento de edición en celdas
        self.table.itemChanged.connect(self._on_item_changed)

        self._reload_table()

    def _reload_table(self) -> None:
        # Bloquea señales para que itemChanged no se dispare al poblar la tabla
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.config.rules))
        for row, rule in enumerate(self.config.rules):
            self.table.setItem(row, 0, self._cell(rule.match_text))
            self.table.setItem(row, 1, self._cell(rule.role))
            self.table.setItem(row, 2, self._cell(str(rule.priority)))
            self.table.setItem(row, 3, self._cell("Sí" if rule.enabled else "No"))
        self.table.blockSignals(False)

    @staticmethod
    def _cell(text: str):
        from PySide6.QtWidgets import QTableWidgetItem
        return QTableWidgetItem(text)

    def _on_item_changed(self, item) -> None:
        row = item.row()
        col = item.column()
        if row < 0 or row >= len(self.config.rules):
            return

        rule = self.config.rules[row]
        text = item.text().strip()

        if col == 0:
            rule.match_text = text
        elif col == 1:
            if text in ("both", "console", "communications"):
                rule.role = text
            else:
                self.table.blockSignals(True)
                item.setText(rule.role)  # Revierte si el texto ingresado no es un rol válido
                self.table.blockSignals(False)
                return
        elif col == 2:
            if text.isdigit():
                rule.priority = int(text)
            else:
                self.table.blockSignals(True)
                item.setText(str(rule.priority))  # Revierte si no es un entero
                self.table.blockSignals(False)
                return
        elif col == 3:
            is_enabled = text.lower() in ("sí", "si", "true", "1", "yes")
            rule.enabled = is_enabled
            self.table.blockSignals(True)
            item.setText("Sí" if is_enabled else "No")
            self.table.blockSignals(False)

        save_config(self.config)

    def _on_toggle(self, checked: bool) -> None:
        self.config.auto_rules_enabled = checked
        save_config(self.config)

    def _on_add_rule(self) -> None:
        text = self.input_match.text().strip()
        if not text:
            return
        rule = PriorityRule(match_text=text, role=self.combo_role.currentText(),
                             enabled=True, priority=len(self.config.rules) * 10)
        self.config.rules.append(rule)
        save_config(self.config)
        self.input_match.clear()
        self._reload_table()

    def _on_remove_rule(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.config.rules):
            return
        del self.config.rules[row]
        save_config(self.config)
        self._reload_table()


# =========================================================================
# Sección: Mirroring / duplicación de audio
# =========================================================================

class MirrorPage(QWidget):
    def __init__(self, mirror_engine_provider, parent=None):
        super().__init__(parent)
        self.setObjectName("MirrorPage")
        self._get_engine = mirror_engine_provider  # callable -> MirrorEngine (en el worker thread)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("Duplicación de audio (mirroring)"))
        layout.addWidget(BodyLabel(
            "Captura el audio del dispositivo de salida predeterminado actual "
            "y lo reproduce simultáneamente en los destinos seleccionados."
        ))

        self.source_combo = ComboBox()
        layout.addWidget(BodyLabel("Dispositivo fuente (loopback):"))
        layout.addWidget(self.source_combo)

        layout.addWidget(BodyLabel("Destinos adicionales:"))
        self.targets_list = ListWidget(self)
        self.targets_list.setSelectionMode(ListWidget.SelectionMode.MultiSelection)
        layout.addWidget(self.targets_list)

        btn_row = QHBoxLayout()
        self.btn_reload_devices = PushButton(FIF.SYNC, "Recargar dispositivos WASAPI")
        self.btn_start = PrimaryPushButton(FIF.PLAY, "Iniciar mirroring")
        self.btn_stop = PushButton(FIF.PAUSE, "Detener")
        btn_row.addWidget(self.btn_reload_devices)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        layout.addLayout(btn_row)
        layout.addStretch(1)

        self.btn_reload_devices.clicked.connect(self._reload_wasapi_devices)
        self.btn_start.clicked.connect(self._start_mirror)
        self.btn_stop.clicked.connect(self._stop_mirror)

        self._reload_wasapi_devices()

    def _reload_wasapi_devices(self) -> None:
        try:
            devices = MirrorEngine.list_wasapi_output_devices()
        except Exception as exc:
            InfoBar.error("Error", f"No se pudieron listar dispositivos WASAPI: {exc}", parent=self)
            return
        self.source_combo.clear()
        self.targets_list.clear()
        self._device_index_map = {}
        for d in devices:
            self.source_combo.addItem(d["name"], userData=d["index"])
            item = QListWidgetItem(d["name"])
            item.setData(Qt.UserRole, d["index"])
            self.targets_list.addItem(item)

    def _start_mirror(self) -> None:
        src_idx = self.source_combo.currentData()
        if src_idx is None:
            InfoBar.warning("Falta fuente", "Elegí un dispositivo fuente.", parent=self)
            return
        target_indices = []
        names = {}
        for item in self.targets_list.selectedItems():
            idx = item.data(Qt.UserRole)
            target_indices.append(idx)
            names[idx] = item.text()
        if not target_indices:
            InfoBar.warning("Faltan destinos", "Seleccioná al menos un destino.", parent=self)
            return

        engine = self._get_engine()
        try:
            engine.start(src_idx, target_indices, names)
            InfoBar.success("Mirroring activo", f"Duplicando hacia {len(target_indices)} destino(s).", parent=self)
        except Exception as exc:
            InfoBar.error("Error", f"No se pudo iniciar el mirroring: {exc}", parent=self)

    def _stop_mirror(self) -> None:
        engine = self._get_engine()
        engine.stop()
        InfoBar.info("Detenido", "Mirroring detenido.", parent=self)


# =========================================================================
# Ventana principal
# =========================================================================

class MainWindow(FluentWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setWindowTitle("Audio Manager Pro")
        self.resize(980, 640)
        setTheme(Theme.AUTO)  # sigue el modo claro/oscuro del sistema

        self.backend_thread, self.worker = make_backend_thread(config)

        self.devices_page = DevicesPage(self.worker)
        self.rules_page = RulesPage(config)
        self.mirror_page = MirrorPage(lambda: self.worker.mirror_engine)

        self.addSubInterface(self.devices_page, FIF.SPEAKERS, "Dispositivos")
        self.addSubInterface(self.rules_page, FIF.TAG, "Reglas")
        self.addSubInterface(self.mirror_page, FIF.SHARE, "Mirroring")

        # Señales del backend -> páginas de la UI (queued connection automática
        # porque worker vive en otro QThread)
        self.worker.devices_changed.connect(self.devices_page.on_devices_changed)
        self.worker.rule_applied.connect(self._on_rule_applied)
        self.worker.mirror_error.connect(self._on_mirror_error)
        self.worker.backend_error.connect(self._on_backend_error)

        self.backend_thread.start()

    @Slot(str)
    def _on_rule_applied(self, message: str) -> None:
        InfoBar.success("Regla automática aplicada", message,
                         position=InfoBarPosition.BOTTOM_RIGHT, duration=4000, parent=self)

    @Slot(str, str)
    def _on_mirror_error(self, device_name: str, message: str) -> None:
        InfoBar.error("Error de mirroring", f"{device_name}: {message}",
                       position=InfoBarPosition.BOTTOM_RIGHT, duration=6000, parent=self)

    @Slot(str)
    def _on_backend_error(self, message: str) -> None:
        InfoBar.error("Error del backend de audio", message,
                       position=InfoBarPosition.TOP, duration=8000, parent=self)

    def shutdown(self) -> None:
        """Debe llamarse antes de cerrar la app para liberar COM/streams
        de forma prolija."""
        try:
            self.worker.stop()
        except Exception:
            logger.exception("Error deteniendo el worker de audio")
        self.backend_thread.quit()
        self.backend_thread.wait(3000)

    def closeEvent(self, event) -> None:
        # Al cerrar con la X, minimizamos a la bandeja en lugar de salir
        # (el cierre real ocurre desde el menú de la bandeja).
        event.ignore()
        self.hide()
