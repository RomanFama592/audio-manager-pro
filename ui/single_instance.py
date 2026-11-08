"""
ui/single_instance.py
----------------------
Garantiza una sola instancia por usuario y permite que una segunda
instancia le mande órdenes a la primera:

  * "show": abrir la app desde el menú Inicio mientras ya corre en la
    bandeja muestra su ventana (en vez de un cartel de error).
  * "quit": `AudioManagerPro.exe --quit` cierra la instancia en ejecución
    de forma prolija (lo usa el instalador antes de actualizar/desinstalar).

El lock (QLockFile) es la fuente de verdad; el canal de mensajes es un
QLocalServer (named pipe) cuyo nombre depende de la carpeta de config, así
que es distinto por usuario.
"""
from __future__ import annotations

import hashlib
import logging
import time

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger("audio_manager.single_instance")

MSG_SHOW = "show"
MSG_QUIT = "quit"
_VALID = {MSG_SHOW, MSG_QUIT}


def server_name_for(config_dir: str) -> str:
    digest = hashlib.sha1(config_dir.lower().encode("utf-8")).hexdigest()[:12]
    return f"AudioManagerPro-{digest}"


class SingleInstance(QObject):
    message_received = Signal(str)

    def __init__(self, server_name: str, lock_path: str, parent=None):
        super().__init__(parent)
        self._name = server_name
        self._lock = QLockFile(lock_path)
        self._lock.setStaleLockTime(0)  # el lock de un proceso muerto se detecta por PID
        self._server: QLocalServer | None = None

    # ---- instancia principal ----
    def acquire(self) -> bool:
        """True si esta es la instancia principal (y queda escuchando)."""
        if not self._lock.tryLock(100):
            return False
        QLocalServer.removeServer(self._name)  # restos de un proceso que murió
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(self._name):
            logger.warning("No se pudo abrir el canal de instancia única: %s", self._server.errorString())
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self._lock.unlock()

    def _on_new_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            conn.readyRead.connect(self._on_ready_read)
            conn.disconnected.connect(conn.deleteLater)
            self._read(conn)

    @Slot()
    def _on_ready_read(self) -> None:
        conn = self.sender()
        if isinstance(conn, QLocalSocket):
            self._read(conn)

    def _read(self, conn: QLocalSocket) -> None:
        while conn.canReadLine():
            message = bytes(conn.readLine()).decode("utf-8", "ignore").strip()
            if message in _VALID:
                logger.info("Orden recibida de otra instancia: %s", message)
                # Diferido: "quit" cierra el servidor y destruye este socket;
                # no puede hacerse mientras se está atendiendo su señal.
                QTimer.singleShot(0, lambda m=message: self.message_received.emit(m))

    # ---- instancia secundaria ----
    def send(self, message: str, timeout_ms: int = 2000) -> bool:
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write((message + "\n").encode("utf-8"))
        sock.flush()
        sock.waitForBytesWritten(timeout_ms)
        sock.disconnectFromServer()
        if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            sock.waitForDisconnected(timeout_ms)
        return True

    def wait_until_released(self, timeout_s: float = 15.0) -> bool:
        """Espera a que la instancia principal termine (libere el lock).
        Lo usa `--quit` para que el instalador no encuentre archivos en uso."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._lock.tryLock(200):
                self._lock.unlock()
                time.sleep(0.5)  # margen para que el proceso termine de salir
                return True
        return False
