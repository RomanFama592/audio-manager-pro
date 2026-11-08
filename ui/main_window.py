"""
ui/main_window.py
-------------------
Ventana principal con estética Fluent Design (Windows 11) usando
`qfluentwidgets` sobre PySide6. Secciones de navegación:
  1) Dispositivos            -> listar/filtrar, fijar default de sistema/comunicaciones
  2) Reglas                  -> alta/baja de reglas de prioridad automática
  3) Mirroring de audio      -> duplicar lo que suena en una salida
  4) Mirroring de micrófono  -> duplicar un micrófono hacia salidas (en paralelo al 3)

El trabajo de audio (enumeración, reglas automáticas, streams de mirroring)
corre en `AudioBackendWorker` (ui/signals_bridge.py), en su propio QThread;
la ventana le pide cosas emitiendo sus señales `*_requested`. La única
llamada COM que se hace desde la GUI es `SetDefaultEndpoint` (instantánea),
para poder mostrar el resultado en el momento.
"""

#TODO: que los botones de setear dispositivos en vez de ser botones sea, click izq para establecer ambos, click derecho y menu contextual para elegir que establecer

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QHeaderView, QWidget,
    QVBoxLayout, QHBoxLayout, QListWidgetItem, QTableWidgetItem,
)

from qfluentwidgets import (
    CheckBox, FluentWindow, FluentIcon as FIF,
    PushButton, PrimaryPushButton, ListWidget, SpinBox, SwitchButton, InfoBar,
    InfoBarPosition, TableWidget, LineEdit, ComboBox, CardWidget,
    SubtitleLabel, BodyLabel, CaptionLabel, ToolButton, setTheme, Theme,
    NavigationItemPosition,
)

from config import AppConfig, PriorityRule, save_config
from core.device_manager import AudioDeviceInfo, Direction, Role, set_default_device
from ui.settings_page import SettingsPage
from ui.signals_bridge import MIRROR_MIC, MIRROR_SYSTEM, make_backend_thread
from version import APP_NAME

logger = logging.getLogger("audio_manager.ui")

ROLE_CHOICES = (
    ("both", "Ambos"),
    ("console", "Predeterminado"),
    ("communications", "Comunicaciones"),
)


def _fill_role_combo(combo: ComboBox, current: str = "both") -> None:
    for value, label in ROLE_CHOICES:
        combo.addItem(label, userData=value)
    idx = combo.findData(current)
    combo.setCurrentIndex(idx if idx >= 0 else 0)


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
        self.btn_set_default_in = PrimaryPushButton(FIF.MICROPHONE, "Predeterminado")
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

        out, inp = Direction.OUTPUT, Direction.INPUT
        self.btn_refresh.clicked.connect(lambda: self.worker.refresh_requested.emit())
        self.btn_set_default_out.clicked.connect(lambda: self._apply(out, console=True, comm=False))
        self.btn_set_comm_out.clicked.connect(lambda: self._apply(out, console=False, comm=True))
        self.btn_set_both_out.clicked.connect(lambda: self._apply(out, console=True, comm=True))
        self.btn_set_default_in.clicked.connect(lambda: self._apply(inp, console=True, comm=False))
        self.btn_set_comm_in.clicked.connect(lambda: self._apply(inp, console=False, comm=True))
        self.btn_set_both_in.clicked.connect(lambda: self._apply(inp, console=True, comm=True))

    # ---------------------------------------------------------------
    @Slot(list)
    def on_devices_changed(self, devices: List[AudioDeviceInfo]) -> None:
        # Se conserva la selección: antes cada refresco (que ocurre en cada
        # evento de audio) la borraba.
        selected = {
            Direction.OUTPUT: self._selected_id(self.output_list),
            Direction.INPUT: self._selected_id(self.input_list),
        }
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
            target = self.output_list if d.direction == Direction.OUTPUT else self.input_list
            target.addItem(item)
            if d.id == selected[d.direction]:
                target.setCurrentItem(item)
        # El ListWidget de qfluentwidgets pinta la selección con una copia
        # propia que solo se actualiza con mouse/teclado: tras un clear() puede
        # quedar resaltada una fila que ya no está seleccionada.
        self.output_list.updateSelectedRows()
        self.input_list.updateSelectedRows()

    def _selected_id(self, lst: ListWidget) -> Optional[str]:
        item = lst.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _apply(self, direction: Direction, console: bool, comm: bool) -> None:
        is_output = direction == Direction.OUTPUT
        dev_id = self._selected_id(self.output_list if is_output else self.input_list)
        if not dev_id:
            InfoBar.warning("Sin selección",
                            "Elegí primero un dispositivo de salida." if is_output else "Elegí primero un micrófono.",
                            position=InfoBarPosition.TOP, parent=self)
            return
        try:
            if console:
                set_default_device(dev_id, Role.CONSOLE)
                set_default_device(dev_id, Role.MULTIMEDIA)
            if comm:
                set_default_device(dev_id, Role.COMMUNICATIONS)
            InfoBar.success("Listo",
                            "Dispositivo de salida actualizado." if is_output else "Micrófono predeterminado actualizado.",
                            position=InfoBarPosition.TOP, parent=self)
        except Exception as exc:
            logger.exception("No se pudo cambiar el dispositivo predeterminado")
            InfoBar.error("Error", f"No se pudo cambiar el dispositivo: {exc}",
                          position=InfoBarPosition.TOP, parent=self)
        # El DeviceWatcher también refresca al cambiar el default; esto es
        # un refuerzo (se ejecuta en el hilo del backend, no en la GUI).
        self.worker.refresh_requested.emit()


# =========================================================================
# Sección: Reglas de prioridad
# =========================================================================

class RulesPage(QWidget):
    rules_changed = Signal()  # el backend debe re-evaluar las reglas

    PRIORITY_SETTLE_MS = 700

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setObjectName("RulesPage")
        self.config = config
        self._reloading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("Reglas automáticas de prioridad"))

        toggle_row = QHBoxLayout()
        toggle_row.addStretch(1)
        toggle_row.addWidget(BodyLabel("Activar lógica automática de prioridades"))
        self.switch_enabled = SwitchButton()
        self.switch_enabled.setOnText("Activado")    # por defecto muestra "On"/"Off"
        self.switch_enabled.setOffText("Desactivado")
        self.switch_enabled.setChecked(config.auto_rules_enabled)
        toggle_row.addWidget(self.switch_enabled)
        layout.addLayout(toggle_row)

        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Texto a coincidir", "Rol", "Prioridad", "Activa"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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
        # Las reglas se muestran de menor a mayor prioridad numérica
        header.setSortIndicator(2, Qt.SortOrder.AscendingOrder)

        layout.addWidget(self.table)
        layout.addWidget(CaptionLabel("Menor número = mayor prioridad. Para quitar una regla, "
                                      "seleccioná su fila haciendo clic en el texto."))

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
        _fill_role_combo(self.combo_role)
        self.btn_add = PrimaryPushButton(FIF.ADD, "Agregar regla")
        self.btn_remove = PushButton(FIF.DELETE, "Quitar seleccionada")
        form_row.addWidget(self.input_match, 2)
        form_row.addWidget(self.combo_role, 1)
        form_row.addWidget(self.btn_add)
        form_row.addWidget(self.btn_remove)
        layout.addLayout(form_row)
        layout.addStretch(1)

        # Al cambiar una prioridad no se reordena la tabla en cada paso del
        # SpinBox (eso destruía el widget mientras se editaba y hacía
        # imposible tipear "150"): se espera a que el valor se asiente.
        self._priority_timer = QTimer(self)
        self._priority_timer.setSingleShot(True)
        self._priority_timer.setInterval(self.PRIORITY_SETTLE_MS)
        self._priority_timer.timeout.connect(self._on_priority_settled)

        self.switch_enabled.checkedChanged.connect(self._on_toggle)
        self.btn_add.clicked.connect(self._on_add_rule)
        self.input_match.returnPressed.connect(self._on_add_rule)
        self.btn_remove.clicked.connect(self._on_remove_rule)

        # Conexión para la edición del texto plano en la columna 0
        self.table.itemChanged.connect(self._on_item_changed)

        self._reload_table()

    def _reload_table(self) -> None:
        # Se reemplaza la lista en vez de ordenarla in-place: list.sort()
        # vacía temporalmente la lista y el backend la lee desde otro hilo.
        self.config.rules = sorted(self.config.rules, key=lambda x: x.priority)

        self._reloading = True
        self.table.blockSignals(True)
        try:
            self.table.clearSelection()
            self.table.setRowCount(len(self.config.rules))

            for row, rule in enumerate(self.config.rules):
                # Columna 0: Texto a coincidir (Celda editable estándar)
                self.table.setItem(row, 0, QTableWidgetItem(rule.match_text))

                # Columna 1: Rol (Desplegable / ComboBox)
                combo_role = ComboBox()
                _fill_role_combo(combo_role, rule.role)
                combo_role.currentIndexChanged.connect(
                    lambda idx, r=rule, c=combo_role: self._on_role_changed(r, c.itemData(idx))
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
                spin_priority.editingFinished.connect(self._resort_if_needed)
                self.table.setCellWidget(row, 2, self.create_padded_widget(spin_priority, margin_h=8))

                # Columna 3: Activa (Checkbox)
                check_enabled = CheckBox()
                check_enabled.setChecked(rule.enabled)
                check_enabled.checkStateChanged.connect(
                    lambda state, r=rule: self._on_enabled_changed(r, state)
                )
                # Layout contenedor para centrar la casilla de verificación en la celda
                check_container = QWidget()
                check_layout = QHBoxLayout(check_container)
                check_layout.addWidget(check_enabled)
                check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                check_layout.setContentsMargins(0, 0, 0, 0)

                self.table.setCellWidget(row, 3, check_container)
        finally:
            self.table.blockSignals(False)
            self._reloading = False

    def create_padded_widget(self, widget, margin_v=4, margin_h=6):
        """Envuelve un widget en un contenedor con padding/márgenes."""
        container = QWidget()
        layout = QHBoxLayout(container)
        # Márgenes: (izquierda, arriba, derecha, abajo)
        layout.setContentsMargins(margin_h, margin_v, margin_h, margin_v)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(widget)
        return container

    # ---------------------------------------------------------------
    def _on_item_changed(self, item) -> None:
        # Actualiza solo si cambia el texto en la columna 0
        if item.column() != 0:
            return
        row = item.row()
        if not 0 <= row < len(self.config.rules):
            return
        rule = self.config.rules[row]
        text = item.text().strip()
        if not text:
            # Una regla vacía nunca coincide: se restaura el texto anterior.
            self.table.blockSignals(True)
            item.setText(rule.match_text)
            self.table.blockSignals(False)
            return
        rule.match_text = text
        save_config(self.config)
        self.rules_changed.emit()

    def _on_role_changed(self, rule: PriorityRule, role: Optional[str]) -> None:
        if not role or role == rule.role:
            return
        rule.role = role
        save_config(self.config)
        self.rules_changed.emit()

    def _on_priority_changed(self, rule: PriorityRule, priority: int) -> None:
        rule.priority = priority
        save_config(self.config)
        self._priority_timer.start()

    def _on_priority_settled(self) -> None:
        self.rules_changed.emit()
        self._resort_if_needed()

    def _resort_if_needed(self) -> None:
        if self._reloading:
            return
        rules = self.config.rules
        if all(a.priority <= b.priority for a, b in zip(rules, rules[1:])):
            return
        focus = QApplication.focusWidget()
        if isinstance(focus, QAbstractSpinBox) and self.table.isAncestorOf(focus):
            return  # se está editando: se reordena al terminar (editingFinished)
        self._reload_table()

    def _on_enabled_changed(self, rule: PriorityRule, state: Qt.CheckState) -> None:
        rule.enabled = state == Qt.CheckState.Checked
        save_config(self.config)
        self.rules_changed.emit()

    def _on_toggle(self, checked: bool) -> None:
        self.config.auto_rules_enabled = checked
        save_config(self.config)
        self.rules_changed.emit()

    def set_auto_rules_enabled(self, enabled: bool) -> None:
        """Usado por la bandeja: cambia el switch (que a su vez guarda y
        notifica al backend)."""
        if self.switch_enabled.isChecked() != enabled:
            self.switch_enabled.setChecked(enabled)
        elif self.config.auto_rules_enabled != enabled:
            self._on_toggle(enabled)

    def _on_add_rule(self) -> None:
        text = self.input_match.text().strip()
        if not text:
            return

        max_priority = max((rule.priority for rule in self.config.rules), default=0)

        rule = PriorityRule(
            match_text=text,
            role=self.combo_role.currentData() or "both",
            enabled=True,
            priority=min(max_priority + 10, 9999),
        )

        self.config.rules.append(rule)
        save_config(self.config)
        self.input_match.clear()
        self._reload_table()
        self.rules_changed.emit()

    def _on_remove_rule(self) -> None:
        # Se usa la fila SELECCIONADA (no currentRow, que puede apuntar a
        # una fila vieja o a la 0 si se tocaron los controles de la celda).
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        rows = [r for r in rows if 0 <= r < len(self.config.rules)]
        if not rows:
            InfoBar.warning("Sin selección", "Seleccioná la regla a quitar haciendo clic en su texto.",
                            position=InfoBarPosition.TOP, parent=self)
            return
        for row in reversed(rows):
            del self.config.rules[row]
        save_config(self.config)
        self._reload_table()
        self.rules_changed.emit()


# =========================================================================
# Sección: Mirroring / duplicación de audio
# =========================================================================

_MIRROR_PAGES = {
    MIRROR_SYSTEM: {
        "object_name": "MirrorPage",
        "title": "Duplicación de audio (mirroring)",
        "description": ("Captura el audio que suena en el dispositivo fuente (por defecto, "
                        "el de salida predeterminado) y lo reproduce simultáneamente en los "
                        "destinos seleccionados."),
        "source_label": "Dispositivo fuente (loopback):",
        "sources_key": "outputs",
        "default_key": "default_output",
        # La fuente no puede ser destino: el loopback capturaría lo que se
        # reproduce en él mismo y se generaría realimentación (eco infinito).
        "exclude_source": True,
        "active_title": "Mirroring activo",
    },
    MIRROR_MIC: {
        "object_name": "MicMirrorPage",
        "title": "Mirroring de micrófono",
        "description": ("Captura un micrófono y lo reproduce en tiempo real en las salidas "
                        "seleccionadas (por ejemplo, para escucharte o enviarlo a un cable "
                        "virtual). Funciona a la vez que el mirroring de audio. Si el destino "
                        "son parlantes cerca del micrófono puede acoplarse: usá auriculares."),
        "source_label": "Micrófono fuente:",
        "sources_key": "inputs",
        "default_key": "default_input",
        "exclude_source": False,
        "active_title": "Mirroring de micrófono activo",
    },
}


class MirrorPage(QWidget):
    """Una sesión de mirroring (audio del sistema o micrófono). La página
    solo arma el pedido; la apertura de streams ocurre en el hilo del
    backend y el resultado vuelve por `mirror_state_changed`."""

    def __init__(self, worker, kind: str = MIRROR_SYSTEM, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._spec = _MIRROR_PAGES[kind]
        self.setObjectName(self._spec["object_name"])
        self.worker = worker
        self._running = False
        self._busy = False
        self._running_source = ""
        self._running_targets: frozenset = frozenset()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel(self._spec["title"]))
        description = BodyLabel(self._spec["description"])
        description.setWordWrap(True)
        layout.addWidget(description)

        self.source_combo = ComboBox()
        layout.addWidget(BodyLabel(self._spec["source_label"]))
        layout.addWidget(self.source_combo)

        layout.addWidget(BodyLabel("Destinos adicionales:"))
        self.targets_list = ListWidget(self)
        self.targets_list.setSelectionMode(ListWidget.SelectionMode.MultiSelection)
        layout.addWidget(self.targets_list)

        btn_row = QHBoxLayout()
        self.btn_reload_devices = PushButton(FIF.SYNC, "Recargar dispositivos WASAPI")
        self.status_label = CaptionLabel("Estado: detenido")
        self.btn_start = PrimaryPushButton(FIF.PLAY, "Iniciar mirroring")
        self.btn_stop = PushButton(FIF.PAUSE, "Detener")
        btn_row.addWidget(self.btn_reload_devices)
        btn_row.addStretch(1)
        btn_row.addWidget(self.status_label)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        layout.addLayout(btn_row)
        layout.addStretch(1)

        self.btn_reload_devices.clicked.connect(lambda: self.worker.mirror_devices_requested.emit())
        self.btn_start.clicked.connect(self._start_mirror)
        self.btn_stop.clicked.connect(self._stop_mirror)
        self.source_combo.currentIndexChanged.connect(lambda _idx: self._on_selection_changed())
        self.targets_list.itemSelectionChanged.connect(self._on_targets_selection_changed)

        worker.mirror_devices_changed.connect(self.on_mirror_devices_changed)
        worker.mirror_state_changed.connect(self.on_mirror_state_changed)
        self._update_controls()

    @staticmethod
    def _name(item: QListWidgetItem) -> str:
        # El nombre real va en UserRole: el texto visible puede llevar "• fuente".
        return item.data(Qt.UserRole)

    def _on_targets_selection_changed(self) -> None:
        # El ListWidget de qfluentwidgets pinta la selección con una copia
        # propia que solo se actualiza con mouse/teclado; sin esto, una
        # selección restaurada o quitada por código no se vería.
        self.targets_list.updateSelectedRows()
        self._update_controls()

    @Slot(dict)
    def on_mirror_devices_changed(self, devices: dict) -> None:
        sources = [d["name"] for d in devices.get(self._spec["sources_key"], [])]
        outputs = [d["name"] for d in devices.get("outputs", [])]
        default_name = devices.get(self._spec["default_key"]) or ""
        prev_source = self.source_combo.currentText()
        prev_targets = {self._name(item) for item in self.targets_list.selectedItems()}

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for name in sources:
            self.source_combo.addItem(name, userData=name)
        if prev_source in sources:
            self.source_combo.setCurrentIndex(sources.index(prev_source))
        elif default_name in sources:
            self.source_combo.setCurrentIndex(sources.index(default_name))
        self.source_combo.blockSignals(False)

        self.targets_list.blockSignals(True)
        self.targets_list.clear()
        for name in outputs:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            self.targets_list.addItem(item)
            if name in prev_targets:
                item.setSelected(True)
        self.targets_list.blockSignals(False)
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        self._sync_target_availability()
        self.targets_list.updateSelectedRows()
        self._update_controls()

    def _sync_target_availability(self) -> None:
        if not self._spec["exclude_source"]:
            return
        source = self.source_combo.currentText()
        selectable = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        for i in range(self.targets_list.count()):
            item = self.targets_list.item(i)
            name = self._name(item)
            if name == source:
                item.setSelected(False)
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setText(f"{name}  •  fuente")  # el delegate no lo pinta en gris
                item.setToolTip("Es la fuente: no puede ser también destino.")
            else:
                item.setFlags(selectable)
                item.setText(name)
                item.setToolTip("")

    def _selection(self):
        source = self.source_combo.currentText()
        targets = [self._name(item) for item in self.targets_list.selectedItems()]
        if self._spec["exclude_source"]:
            targets = [t for t in targets if t != source]
        return source, targets

    def _selection_is_running(self) -> bool:
        source, targets = self._selection()
        return self._running and source == self._running_source and frozenset(targets) == self._running_targets

    def _start_mirror(self) -> None:
        # Un segundo toque mientras se procesa el primero, o con la misma
        # configuración ya activa, no hace nada.
        if self._busy or self._selection_is_running():
            return
        source, targets = self._selection()
        if not source:
            InfoBar.warning("Falta fuente", "Elegí un dispositivo fuente.", parent=self)
            return
        if not targets:
            InfoBar.warning("Faltan destinos", "Seleccioná al menos un destino distinto de la fuente.", parent=self)
            return
        self._busy = True
        self._update_controls()
        self.status_label.setText("Estado: aplicando…" if self._running else "Estado: iniciando…")
        self.worker.mirror_start_requested.emit(self.kind, source, targets)

    def _stop_mirror(self) -> None:
        if self._busy or not self._running:
            return
        self._busy = True
        self._update_controls()
        self.status_label.setText("Estado: deteniendo…")
        self.worker.mirror_stop_requested.emit(self.kind)

    @Slot(dict)
    def on_mirror_state_changed(self, state: dict) -> None:
        if state.get("kind") != self.kind:
            return
        self._busy = False
        self._running = bool(state.get("running"))
        self._running_source = state.get("source", "") if self._running else ""
        self._running_targets = frozenset(state.get("targets", [])) if self._running else frozenset()
        self._update_controls()
        message = state.get("message")
        if not message:
            return
        if self._running:
            InfoBar.success(self._spec["active_title"], message, parent=self.window())
        else:
            InfoBar.info("Detenido", message, parent=self.window())

    def _update_controls(self) -> None:
        if self._running:
            self.btn_start.setText("Aplicar cambios")
            self.btn_start.setEnabled(not self._busy and not self._selection_is_running())
        else:
            self.btn_start.setText("Iniciar mirroring")
            self.btn_start.setEnabled(not self._busy)
        self.btn_stop.setEnabled(self._running and not self._busy)
        self.btn_reload_devices.setEnabled(not self._busy)
        if not self._busy:
            if self._running:
                n = len(self._running_targets)
                self.status_label.setText(f"Estado: activo ({n} destino{'s' if n != 1 else ''})")
            else:
                self.status_label.setText("Estado: detenido")


# =========================================================================
# Ventana principal
# =========================================================================

_THEMES = {"light": Theme.LIGHT, "dark": Theme.DARK, "auto": Theme.AUTO}


class MainWindow(FluentWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self._shut_down = False
        self.setWindowTitle(APP_NAME)
        self.resize(980, 640)
        setTheme(_THEMES.get(config.theme, Theme.AUTO))  # "auto" sigue el modo del sistema

        self.backend_thread, self.worker = make_backend_thread(config)

        self.devices_page = DevicesPage(self.worker)
        self.rules_page = RulesPage(config)
        self.mirror_page = MirrorPage(self.worker, MIRROR_SYSTEM)
        self.mic_mirror_page = MirrorPage(self.worker, MIRROR_MIC)
        self.settings_page = SettingsPage(config)

        self.addSubInterface(self.devices_page, FIF.SPEAKERS, "Dispositivos")
        self.addSubInterface(self.rules_page, FIF.TAG, "Reglas")
        self.addSubInterface(self.mirror_page, FIF.SHARE, "Mirroring de audio")
        self.addSubInterface(self.mic_mirror_page, FIF.MICROPHONE, "Mirroring de micrófono")
        self.addSubInterface(self.settings_page, FIF.SETTING, "Configuración",
                             NavigationItemPosition.BOTTOM)

        # Señales del backend -> páginas de la UI (queued connection automática
        # porque worker vive en otro QThread)
        self.worker.devices_changed.connect(self.devices_page.on_devices_changed)
        self.worker.rule_applied.connect(self._on_rule_applied)
        self.worker.mirror_error.connect(self._on_mirror_error)
        self.worker.backend_error.connect(self._on_backend_error)

        # UI -> backend
        self.rules_page.rules_changed.connect(lambda: self.worker.rules_changed.emit())

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
        """Libera COM/streams de forma prolija. Es idempotente (la llaman
        tanto "Salir" de la bandeja como `aboutToQuit`). La limpieza corre
        en el hilo del backend, que es donde se inicializó COM."""
        if self._shut_down:
            return
        self._shut_down = True
        if self.backend_thread.isRunning():
            self.worker.stop_requested.emit()
            if not self.backend_thread.wait(5000):
                logger.warning("El hilo del backend de audio no terminó a tiempo")

    def closeEvent(self, event) -> None:
        # Al cerrar con la X, minimizamos a la bandeja en lugar de salir
        # (el cierre real ocurre desde el menú de la bandeja).
        event.ignore()
        self.hide()
