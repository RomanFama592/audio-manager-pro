"""
core/device_manager.py
-----------------------
Capa de acceso a Windows CoreAudio (WASAPI) mediante `pycaw` + `comtypes`.

Responsabilidades:
  * Enumerar dispositivos de entrada/salida activos (ignorando los `Disabled`).
  * Cambiar el dispositivo predeterminado de "Consola" (System) y de
    "Comunicaciones" de forma independiente (usando la interfaz COM privada
    IPolicyConfig, ya que la API pública de Windows NO expone esta función:
    es la misma técnica que usan utilidades como EarTrumpet o AudioSwitch).
  * Suscribirse a eventos de conexión/desconexión/cambio de default mediante
    IMMNotificationClient, corriendo en un hilo dedicado para no bloquear la GUI.

Nota de compatibilidad: `pycaw` ha cambiado su API pública varias veces
(GUIDs y clases que antes vivían como atributos de `AudioUtilities` se
movieron a `pycaw.constants` / `pycaw.callbacks`, y `SetDefaultDevice`
directamente desapareció). Este módulo usa exclusivamente los puntos de
entrada de alto nivel y estables: `AudioUtilities.GetDeviceEnumerator()`,
`AudioUtilities.GetAllDevices(...)` y `pycaw.callbacks.MMNotificationClient`.

Este módulo NO depende de PySide6: expone callbacks planos para poder
integrarse tanto con Qt (vía señales) como con cualquier otro framework.
"""
from __future__ import annotations

import threading
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, List, Optional

import comtypes
from comtypes import GUID, COMMETHOD, HRESULT, IUnknown
from ctypes import c_void_p, c_wchar_p, c_int, POINTER
from ctypes.wintypes import BOOL

from pycaw.pycaw import AudioUtilities
from pycaw.callbacks import MMNotificationClient

try:
    # Ubicación moderna (pycaw >= ~2023).
    from pycaw.constants import EDataFlow
except ImportError:  # pragma: no cover - compatibilidad con versiones viejas
    from pycaw.pycaw import EDataFlow

logger = logging.getLogger("audio_manager.device_manager")


# =========================================================================
# 1) Definición de roles / direcciones
# =========================================================================

class Role(IntEnum):
    CONSOLE = 0          # eRender/eCapture "System" default
    MULTIMEDIA = 1       # rara vez usado en apps modernas (heredado de Vista)
    COMMUNICATIONS = 2   # default usado para llamadas/VoIP


class Direction(IntEnum):
    OUTPUT = 0  # eRender  -> altavoces / auriculares
    INPUT = 1   # eCapture -> micrófonos


@dataclass
class AudioDeviceInfo:
    id: str
    name: str
    direction: Direction
    state: str            # "Active" | "Disabled" | "NotPresent" | "Unplugged"
    is_default_console: bool = False
    is_default_comm: bool = False

    def __repr__(self) -> str:
        tag = []
        if self.is_default_console:
            tag.append("DEFAULT")
        if self.is_default_comm:
            tag.append("COMM")
        tag_str = f" [{'/'.join(tag)}]" if tag else ""
        return f"<{self.name} ({self.direction.name}, {self.state}){tag_str}>"


# =========================================================================
# 2) IPolicyConfig: interfaz COM privada para fijar el dispositivo default
#    (GUID/vtable documentados públicamente y usados por herramientas OSS
#    como EarTrumpet, SoundSwitch, AudioDeviceCmdlets, NirCmd, etc.)
#    pycaw eliminó su antiguo `SetDefaultDevice` sin reemplazo, así que esta
#    sigue siendo la única forma de fijar el default por rol.
# =========================================================================

CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
IID_IPolicyConfig = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")


class IPolicyConfig(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IPolicyConfig
    _methods_ = (
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["out"], POINTER(c_void_p), "ppFormat")),
        COMMETHOD([], HRESULT, "GetDeviceFormat",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], BOOL, "bDefault"),
                  (["out"], POINTER(c_void_p), "ppFormat")),
        COMMETHOD([], HRESULT, "ResetDeviceFormat",
                  (["in"], c_wchar_p, "pszDeviceName")),
        COMMETHOD([], HRESULT, "SetDeviceFormat",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_void_p, "pEndpointFormat"),
                  (["in"], c_void_p, "pMixFormat")),
        COMMETHOD([], HRESULT, "GetProcessingPeriod",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], BOOL, "bDefault"),
                  (["out"], POINTER(c_int), "pmftDefaultPeriod"),
                  (["out"], POINTER(c_int), "pmftMinimumPeriod")),
        COMMETHOD([], HRESULT, "SetProcessingPeriod",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_int, "pmftPeriod")),
        COMMETHOD([], HRESULT, "GetShareMode",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["out"], c_void_p, "pMode")),
        COMMETHOD([], HRESULT, "SetShareMode",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_void_p, "mode")),
        COMMETHOD([], HRESULT, "GetPropertyValue",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], BOOL, "bFxStore"),
                  (["in"], c_void_p, "key"),
                  (["out"], c_void_p, "pv")),
        COMMETHOD([], HRESULT, "SetPropertyValue",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], BOOL, "bFxStore"),
                  (["in"], c_void_p, "key"),
                  (["in"], c_void_p, "pv")),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_int, "role")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], BOOL, "bVisible")),
    )


def _get_policy_config() -> IPolicyConfig:
    """Crea (o reutiliza) la instancia COM de PolicyConfig en el hilo actual.
    Debe llamarse tras `comtypes.CoInitialize()` en ese hilo."""
    return comtypes.CoCreateInstance(
        CLSID_PolicyConfigClient, IPolicyConfig, comtypes.CLSCTX_ALL
    )


def set_default_device(device_id: str, role: Role) -> None:
    """Fija `device_id` como predeterminado para el `role` indicado.
    Lanza OSError/COMError si el dispositivo no existe o no tiene permisos."""
    policy = _get_policy_config()
    policy.SetDefaultEndpoint(device_id, int(role))


def set_default_output(device_id: str, also_communications: bool = True) -> None:
    set_default_device(device_id, Role.CONSOLE)
    set_default_device(device_id, Role.MULTIMEDIA)
    if also_communications:
        set_default_device(device_id, Role.COMMUNICATIONS)


def set_default_communications_output(device_id: str) -> None:
    set_default_device(device_id, Role.COMMUNICATIONS)


# =========================================================================
# 3) Enumeración de dispositivos (usa pycaw sobre IMMDeviceEnumerator)
# =========================================================================

# Valores de bit de DEVICE_STATE_XXX documentados por Microsoft
# (mmdeviceapi.h). Se usan como literales en vez de depender de un enum de
# pycaw cuyo nombre/ubicación cambió entre versiones.
_STATE_ACTIVE = 0x1
_STATE_DISABLED = 0x2
_STATE_NOTPRESENT = 0x4
_STATE_UNPLUGGED = 0x8

_STATE_LABELS = {
    _STATE_ACTIVE: "Active",
    _STATE_DISABLED: "Disabled",
    _STATE_NOTPRESENT: "NotPresent",
    _STATE_UNPLUGGED: "Unplugged",
}


def _default_id_for(direction: Direction, role: Role) -> Optional[str]:
    flow = EDataFlow.eRender.value if direction == Direction.OUTPUT else EDataFlow.eCapture.value
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        endpoint = enumerator.GetDefaultAudioEndpoint(flow, int(role))
        return endpoint.GetId()
    except Exception as exc:
        logger.debug("No se pudo obtener default id para %s/%s: %s", direction, role, exc)
        return None


def _devices_for_flow(direction: Direction, state_filter: Optional[int]):
    """Pide a pycaw los dispositivos de una dirección concreta. Usa el
    filtrado nativo por `data_flow`/`device_state` cuando está disponible
    (pycaw reciente); si la versión instalada es más vieja y no acepta esos
    kwargs, hace un fallback pidiendo todo y filtrando a mano."""
    flow = EDataFlow.eRender.value if direction == Direction.OUTPUT else EDataFlow.eCapture.value
    try:
        if state_filter is not None:
            return list(AudioUtilities.GetAllDevices(data_flow=flow, device_state=state_filter))
        return list(AudioUtilities.GetAllDevices(data_flow=flow))
    except TypeError:
        # Versión de pycaw sin soporte de kwargs: filtramos nosotros mismos
        # a partir del listado completo, usando dataFlow si está expuesto.
        all_devices = list(AudioUtilities.GetAllDevices())
        result = []
        for dev in all_devices:
            dev_flow = getattr(dev, "dataFlow", None)
            if dev_flow is not None and dev_flow != flow:
                continue
            if state_filter is not None and getattr(dev, "state", _STATE_ACTIVE) != state_filter:
                continue
            result.append(dev)
        return result


def list_devices(include_disabled: bool = False) -> List[AudioDeviceInfo]:
    """Devuelve todos los dispositivos de entrada y salida. Por defecto
    ignora los deshabilitados (`Disabled`) y los no presentes, tal como pide
    el requerimiento de filtrado."""
    results: List[AudioDeviceInfo] = []

    default_out_console = _default_id_for(Direction.OUTPUT, Role.CONSOLE)
    default_out_comm = _default_id_for(Direction.OUTPUT, Role.COMMUNICATIONS)
    default_in_console = _default_id_for(Direction.INPUT, Role.CONSOLE)
    default_in_comm = _default_id_for(Direction.INPUT, Role.COMMUNICATIONS)

    state_filter = None if include_disabled else _STATE_ACTIVE

    for direction in (Direction.OUTPUT, Direction.INPUT):
        try:
            devices = _devices_for_flow(direction, state_filter)
        except Exception:
            logger.exception("Error obteniendo dispositivos de %s", direction.name)
            continue

        for dev in devices:
            # pycaw expone `state` como enum `AudioDeviceState` (no como int);
            # se normaliza a su valor numérico para poder etiquetarlo.
            state_value = getattr(dev, "state", _STATE_ACTIVE)
            state_value = getattr(state_value, "value", state_value)
            default_label = "Active" if state_filter is not None else "Unknown"
            state_name = _STATE_LABELS.get(state_value, default_label)

            try:
                name = dev.FriendlyName
            except Exception:
                name = str(dev)

            info = AudioDeviceInfo(id=dev.id, name=name, direction=direction, state=state_name)
            info.is_default_console = dev.id in (default_out_console, default_in_console)
            info.is_default_comm = dev.id in (default_out_comm, default_in_comm)
            results.append(info)

    return results


# =========================================================================
# 4) Notificaciones en tiempo real (IMMNotificationClient) en hilo propio
#    Se usa la clase base de pycaw (ya implementa correctamente la vtable
#    COM) en vez de reimplementar IMMNotificationClient a mano.
# =========================================================================

class DeviceEvent:
    """Tipos de evento que se reenvían a `on_change`."""
    STATE = "state"        # conectado/desconectado/deshabilitado
    ADDED = "added"
    REMOVED = "removed"
    DEFAULT = "default"    # cambió el predeterminado de algún rol


class _NotificationClient(MMNotificationClient):
    """Sink COM que Windows invoca ante cambios de estado/conexión de
    endpoints o cambio de dispositivo predeterminado.

    Se sobreescriben directamente los métodos de la interfaz COM
    (`OnDeviceStateChanged`, ...), cuyas firmas define Windows y no cambian,
    en lugar de los wrappers en minúscula de pycaw: pycaw cambió la firma de
    esos wrappers (p. ej. `on_default_device_changed` pasó a recibir 5
    argumentos) y eso hacía que cada evento lanzara TypeError dentro del
    callback COM, por lo que la app nunca se enteraba de los cambios.

    IMPORTANTE: estos métodos corren en un hilo del pool RPC de Windows.
    Microsoft indica no hacer trabajo pesado ni llamar al enumerador desde
    aquí (riesgo de deadlock), así que solo se notifica y el trabajo real
    se hace en el hilo del backend.
    """

    def __init__(self, on_change: Callable[[str], None]):
        super().__init__()
        self._on_change = on_change

    def OnDeviceStateChanged(self, device_id, new_state):
        logger.info("Cambio de estado: %s -> %s", device_id, _STATE_LABELS.get(new_state, new_state))
        self._safe_notify(DeviceEvent.STATE)

    def OnDeviceAdded(self, device_id):
        logger.info("Dispositivo agregado: %s", device_id)
        self._safe_notify(DeviceEvent.ADDED)

    def OnDeviceRemoved(self, device_id):
        logger.info("Dispositivo removido: %s", device_id)
        self._safe_notify(DeviceEvent.REMOVED)

    def OnDefaultDeviceChanged(self, flow, role, default_device_id):
        logger.debug("Default cambiado: flow=%s role=%s id=%s", flow, role, default_device_id)
        self._safe_notify(DeviceEvent.DEFAULT)

    def OnPropertyValueChanged(self, device_id, key):
        pass  # ruido de bajo interés (nombres/iconos); se ignora a propósito

    def _safe_notify(self, kind: str):
        try:
            self._on_change(kind)
        except Exception:
            logger.exception("Error en callback de notificación de audio")


class DeviceWatcher:
    """Ejecuta un hilo dedicado con su propio apartamento COM (MTA) que
    escucha eventos de hot-plug / cambio de default y dispara
    `on_change(kind)` (pensado para reenviarse como señal Qt desde la capa
    de UI). `on_change` se invoca desde hilos de COM: debe ser liviano."""

    def __init__(self, on_change: Callable[[str], None]):
        self._on_change = on_change
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._enumerator = None
        self._client = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="AudioDeviceWatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                logger.warning("DeviceWatcher: el hilo no terminó a tiempo")

    def _run(self) -> None:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            self._enumerator = AudioUtilities.GetDeviceEnumerator()
            self._client = _NotificationClient(self._on_change)
            self._enumerator.RegisterEndpointNotificationCallback(self._client)
            logger.info("DeviceWatcher: callback registrado, escuchando eventos.")
            # Bucle de espera pasiva: los callbacks llegan de forma asíncrona
            # desde el subsistema de audio; solo necesitamos mantener vivo
            # el hilo y su apartamento COM.
            while not self._stop_event.is_set():
                self._stop_event.wait(0.5)
        except Exception:
            logger.exception("DeviceWatcher: fallo iniciando el listener de audio")
        finally:
            try:
                if self._enumerator and self._client:
                    self._enumerator.UnregisterEndpointNotificationCallback(self._client)
            except Exception:
                logger.debug("DeviceWatcher: error al desregistrar el callback", exc_info=True)
            # Liberar los punteros COM ANTES de CoUninitialize: si quedaran
            # vivos, su Release() ocurriría más tarde (al recolectar el
            # objeto) sobre un apartamento ya cerrado -> crash al salir.
            self._client = None
            self._enumerator = None
            comtypes.CoUninitialize()
