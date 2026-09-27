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

#TODO: que los botones de setear dispositivos en vez de ser botones sea, click izq para establecer ambos, click derecho y menu contextual para elegir que establecer

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QHeaderView, QWidget, QVBoxLayout, QHBoxLayout, QListWidgetItem,
)

from qfluentwidgets import (
    CheckBox, FluentWindow, FluentIcon as FIF,
    PushButton, PrimaryPushButton, ListWidget, SpinBox, SwitchButton, InfoBar,
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

        header_row = QHBoxLayout()
        self.btn_refresh = PushButton(FIF.SYNC, "Actualizar ahora")

        header_row.addWidget(SubtitleLabel("Dispositivos de audio"))  # 1. Izquierda
        header_row.addStretch(1)                                     # 2. Empuja lo de la derecha
        header_row.addWidget(self.btn_refresh)                       # 3. Derecha

        layout.addLayout(header_row)

        columns = QHBoxLayout()
        # ---- Salidas ----
        out_card = CardWidget(self)
        out_layout = QVBoxLayout(out_card)
        out_layout.addWidget(BodyLabel("Salidas (altavoces / auriculares)"))
        self.output_list = ListWidget(out_card)
        out_layout.addWidget(self.output_list)
        out_btns = QHBoxLayout()
        self.btn_set_default_out = PrimaryPushButton(FIF.SPEAKERS, "Predeterminado")
        self.btn_set_comm_out = PushButton(FIF.PHONE, "Comunicaciones")
        self.btn_set_both_out = ToolButton(FIF.ASTERISK)
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
        self.btn_set_default_in = PrimaryPushButton(FIF.SPEAKERS, "Predeterminado")
        self.btn_set_comm_in = PushButton(FIF.PHONE, "Comunicaciones")
        self.btn_set_both_in = ToolButton(FIF.ASTERISK)
        self.btn_set_both_in.setFixedSize(32, 32)
        self.btn_set_both_in.setToolTip("Establecer en Predeterminado y en Comunicaciones a la vez")
        in_btns.addWidget(self.btn_set_default_in)
        in_btns.addWidget(self.btn_set_comm_in)
        in_btns.addWidget(self.btn_set_both_in)
        in_layout.addLayout(in_btns)
        columns.addWidget(in_card)

        layout.addLayout(columns)
        layout.addStretch(1)

        self.btn_refresh.clicked.connect(self.worker.refresh_now)
        self.btn_set_default_out.clicked.connect(lambda: self._apply_output(comm=False))
        self.btn_set_comm_out.clicked.connect(lambda: self._apply_output(comm=True))
        self.btn_set_both_out.clicked.connect(lambda: self._apply_output(comm=False, also_communications=True))
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

    def _apply_output(self, comm: bool, also_communications: bool = False) -> None:
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
                set_default_output(dev_id, also_communications=also_communications)
            InfoBar.success("Listo", "Dispositivo de salida actualizado.",
                             position=InfoBarPosition.TOP, parent=self)
        except Exception as exc:
            InfoBar.error("Error", f"No se pudo cambiar el dispositivo: {exc}",
                           position=InfoBarPosition.TOP, parent=self)
        self.worker.refresh_now()

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
        self.worker.refresh_now()


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
        toggle_row.addStretch(1)
        toggle_row.addWidget(BodyLabel("Activar lógica automática de prioridades"))
        self.switch_enabled = SwitchButton()
        self.switch_enabled.setChecked(config.auto_rules_enabled)
        toggle_row.addWidget(self.switch_enabled)
        # toggle_row.addStretch(1) hace que este en el medio 
        layout.addLayout(toggle_row)

        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Texto a coincidir", "Rol", "Prioridad", "Activa"])
        self.table.setStyleSheet(self.table.styleSheet() + """
            QTableView::item {
                padding: 8px;
            }
        """)

        # Anchos predeterminados suficientes para ver los controles sin cortes
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(2, 170)
        self.table.setColumnWidth(3, 80)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)      # La celda de texto ocupa el espacio restante
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)  # Rol
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)  # Prioridad
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)  # Activa
        header.setSortIndicatorShown(True)
        header.setSortIndicator(2, Qt.SortOrder.DescendingOrder)
        
        layout.addWidget(self.table)

        form_row = QHBoxLayout()
        
        # --- Campo con Placeholder y Tooltip mejorado ---
        self.input_match = LineEdit()
        self.input_match.setPlaceholderText("Ej: Altavoces ! Steam ! Virtual")
        self.input_match.setToolTip(
            "Ingresa el nombre del dispositivo.\n"
            "Usa '!' para excluir palabras no deseadas.\n"
            "Ejemplo: 'Altavoces ! Steam' (coincide con Altavoces pero NO de Steam)."
        )
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
        
        # Conexión para la edición del texto plano en la columna 0
        self.table.itemChanged.connect(self._on_item_changed)

        self._reload_table()

    def _reload_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.config.rules))

        self.config.rules.sort(key=lambda x: x.priority)

        for row, rule in enumerate(self.config.rules):
            # Columna 0: Texto a coincidir (Celda editable estándar)
            self.table.setItem(row, 0, self._cell(rule.match_text))

            # Columna 1: Rol (Desplegable / ComboBox)
            combo_role = ComboBox()
            combo_role.addItems(["both", "console", "communications"])
            combo_role.setCurrentText(rule.role)
            combo_role.currentTextChanged.connect(
                lambda text, r=rule: self._on_role_changed(r, text)
            )
            combo_role.setFixedHeight(28)
            self.table.setCellWidget(row, 1, self.create_padded_widget(combo_role, margin_h=8))

            # Columna 2: Prioridad (SpinBox con saltos de a 10)
            spin_priority = SpinBox()
            spin_priority.setRange(0, 9999)
            spin_priority.setSingleStep(10)
            spin_priority.setFixedHeight(28)
            spin_priority.setValue(rule.priority)
            spin_priority.valueChanged.connect(
                lambda val, r=rule: self._on_priority_changed(r, val)
            )
            self.table.setCellWidget(row, 2, self.create_padded_widget(spin_priority, margin_h=8))

            # Columna 3: Activa (Checkbox)
            check_enabled = CheckBox()
            check_enabled.setChecked(rule.enabled)
            check_enabled.checkStateChanged.connect(
                lambda checked, r=rule: self._on_enabled_changed(r, checked)
            )
            # Layout contenedor para centrar la casilla de verificación en la celda
            check_container = QWidget()
            check_layout = QHBoxLayout(check_container)
            check_layout.addWidget(check_enabled)
            check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check_layout.setContentsMargins(0, 0, 0, 0)
            
            self.table.setCellWidget(row, 3, check_container)

        self.table.blockSignals(False)

    def create_padded_widget(self, widget, margin_v=4, margin_h=6):
        """Envuelve un widget en un contenedor con padding/márgenes."""
        container = QWidget()
        layout = QHBoxLayout(container)
        # Márgenes: (izquierda, arriba, derecha, abajo)
        layout.setContentsMargins(margin_h, margin_v, margin_h, margin_v)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(widget)
        return container

    @staticmethod
    def _cell(text: str):
        from PySide6.QtWidgets import QTableWidgetItem
        return QTableWidgetItem(text)

    def _on_item_changed(self, item) -> None:
        # Actualiza solo si cambia el texto en la columna 0
        if item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self.config.rules):
            self.config.rules[row].match_text = item.text().strip()
            save_config(self.config)

    def _on_role_changed(self, rule: PriorityRule, role: str) -> None:
        rule.role = role
        save_config(self.config)

    def _on_priority_changed(self, rule: PriorityRule, priority: int) -> None:
        rule.priority = priority
        # self.config.rules.sort(key=lambda x: x.priority)
        save_config(self.config)
        self._reload_table()

    def _on_enabled_changed(self, rule: PriorityRule, enabled: Qt.CheckState) -> None:
        rule.enabled = enabled == Qt.CheckState.Checked
        save_config(self.config)

    def _on_toggle(self, checked: bool) -> None:
        self.config.auto_rules_enabled = checked
        save_config(self.config)

    def _on_add_rule(self) -> None:
        text = self.input_match.text().strip()
        if not text:
            return
        
        maxPriority = max((rule.priority for rule in self.config.rules), default=0)
        
        rule = PriorityRule(
            match_text=text,
            role=self.combo_role.currentText(),
            enabled=True,
            priority= maxPriority + 10
        )
        
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
        # FIXME: cuando lo toco otra vez despues de haber selecionado un dispositivo y sin modificarlo, se congela la aplicacion
        # FIXME: cuando tengo selecionado el mismo dispositivo de fuente como de adicional se rompe el sonido, evitar que se pueda
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
            self._stop_mirror()
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
