"""
core/mirror_engine.py
----------------------
Captura audio y lo retransmite en tiempo real hacia uno o más dispositivos
de salida adicionales ("Audio Duplication / Mirroring") utilizando
sounddevice. La fuente puede ser:
  * SOURCE_LOOPBACK: lo que suena por un dispositivo de salida (WASAPI
    Loopback vía pyaudiowpatch).
  * SOURCE_INPUT: un micrófono / entrada WASAPI (vía pyaudiowpatch).

Cada `MirrorEngine` es una sesión independiente: se puede tener a la vez un
mirroring del audio del sistema y otro del micrófono.

Diseño:
  * Un stream de entrada (pyaudiowpatch) captura la fuente en tiempo real.
  * Por cada dispositivo destino se abre un `OutputStream` independiente
    (sounddevice) con su propio FIFO de muestras, para tolerar tamaños de
    bloque, latencias y drift distintos entre dispositivos sin que uno
    lento bloquee a los demás.
  * Los callbacks de audio NO toman ningún lock compartido con start/stop:
    leen una tupla inmutable de destinos que se reemplaza atómicamente.
    (Antes el callback de captura esperaba el mismo lock que `stop()`
    mantenía tomado mientras esperaba a que terminara ese callback ->
    deadlock y congelamiento de la app.)
  * Los dispositivos se identifican por NOMBRE: los índices de PortAudio
    cambian cada vez que se reinicializa para detectar dispositivos nuevos.
  * Los errores (dispositivo desconectado a mitad de la reproducción, etc.)
    se detectan con `check_health()` y se reportan vía callback, sin tirar
    abajo el proceso completo.

Todos los métodos públicos deben llamarse desde un mismo hilo (el worker
del backend); los callbacks de audio corren en hilos de PortAudio.
"""
# TODO: guardar esquema de mirror cuando cierto dispositivo este como predeterminado o con algun otro rol, este mismo si hace match se aplicaria
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np
import pyaudiowpatch as pyaudio
import sounddevice as sd

logger = logging.getLogger("audio_manager.mirror_engine")

LOOPBACK_SUFFIX = " [Loopback]"

SOURCE_LOOPBACK = "loopback"   # audio que suena en una salida
SOURCE_INPUT = "input"         # micrófono / entrada

# Sesiones con streams de sounddevice abiertos. Mientras haya alguna, NO se
# puede reinicializar PortAudio (eso cerraría sus streams), así que la
# lista de dispositivos se lee sin refrescar.
_running_engines: "set[int]" = set()
_registry_lock = threading.Lock()


def _other_engine_running(engine: "MirrorEngine | None" = None) -> bool:
    with _registry_lock:
        return any(eid != id(engine) for eid in _running_engines)


@dataclass
class MirrorError:
    device_name: str
    message: str


# =========================================================================
# FIFO de muestras
# =========================================================================

class _SampleFifo:
    """FIFO circular de frames (frames x canales) thread-safe.

    El productor (callback de captura) y el consumidor (callback de salida)
    piden bloques de tamaños distintos; este buffer conserva el sobrante en
    lugar de descartarlo o rellenar con ceros, que era lo que producía los
    cortes/chasquidos del audio duplicado.
    """

    def __init__(self, channels: int, capacity_frames: int, prime_frames: int):
        self._buf = np.zeros((capacity_frames, channels), dtype=np.float32)
        self._capacity = capacity_frames
        self._prime_frames = prime_frames
        self._read = 0
        self._size = 0
        self._priming = True  # acumula un mínimo antes de reproducir (jitter buffer)
        self._lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        n = len(data)
        if n == 0:
            return
        cap = self._capacity
        if n > cap:
            data = data[-cap:]
            n = cap
        with self._lock:
            overflow = self._size + n - cap
            if overflow > 0:
                # Se descarta lo más viejo: prioriza baja latencia (también
                # absorbe el drift de reloj entre dispositivos).
                self._read = (self._read + overflow) % cap
                self._size -= overflow
            w = (self._read + self._size) % cap
            first = min(n, cap - w)
            self._buf[w:w + first] = data[:first]
            if first < n:
                self._buf[:n - first] = data[first:]
            self._size += n

    def read_into(self, out: np.ndarray) -> int:
        """Copia hasta len(out) frames en `out` y completa con silencio.
        Devuelve la cantidad de frames reales copiados."""
        frames = len(out)
        with self._lock:
            if self._priming and self._size < max(self._prime_frames, frames):
                n = 0
            else:
                self._priming = False
                n = min(frames, self._size)
                cap = self._capacity
                first = min(n, cap - self._read)
                out[:first] = self._buf[self._read:self._read + first]
                if first < n:
                    out[first:n] = self._buf[:n - first]
                self._read = (self._read + n) % cap
                self._size -= n
                if n < frames:
                    self._priming = True  # underrun: volver a acumular
        if n < frames:
            out[n:] = 0
        return n

    @property
    def size(self) -> int:
        return self._size


def _adapt_channels(data: np.ndarray, out_channels: int) -> np.ndarray:
    """Adapta un bloque (frames x canales) a `out_channels` canales."""
    in_channels = data.shape[1]
    if in_channels == out_channels:
        return data
    if out_channels == 1:
        return data.mean(axis=1, keepdims=True)
    if in_channels == 1:
        return np.repeat(data, out_channels, axis=1)
    if in_channels > out_channels:
        return data[:, :out_channels]
    out = np.zeros((len(data), out_channels), dtype=np.float32)
    out[:, :in_channels] = data
    return out


# =========================================================================
# Destino de salida
# =========================================================================

class _TargetOutput:
    """Encapsula el stream de salida hacia UN dispositivo destino."""

    BUFFER_SECONDS = 0.20   # latencia máxima acumulada antes de descartar
    PRIME_SECONDS = 0.04    # colchón inicial / tras un underrun

    def __init__(self, device_index: int, device_name: str, max_channels: int,
                 samplerate: int, source_channels: int):
        self.device_index = device_index
        self.device_name = device_name
        self.max_channels = max_channels
        self.samplerate = samplerate
        self.source_channels = source_channels
        self.channels = source_channels
        self._fifo: Optional[_SampleFifo] = None
        self._stream: Optional[sd.OutputStream] = None
        self._stopped = threading.Event()
        self._finished_unexpectedly = threading.Event()

    def start(self) -> None:
        """Abre el stream. Lanza la última excepción si no pudo abrirlo
        con ninguna cantidad de canales."""
        # Se prefiere al menos estéreo: una fuente mono (micrófono) se
        # duplica a ambos canales con _adapt_channels en vez de depender de
        # cómo cada driver reproduce un stream mono.
        candidates: List[int] = []
        for c in (max(self.source_channels, 2), self.source_channels, 2, 1):
            if c and c not in candidates and (self.max_channels <= 0 or c <= self.max_channels):
                candidates.append(c)

        # auto_convert: deja que WASAPI (modo compartido) convierta la
        # frecuencia de muestreo/canales si el destino usa un formato de
        # mezcla distinto al de la fuente (p. ej. 44.1 kHz vs 48 kHz). Sin
        # esto, abrir el destino fallaba con "Invalid sample rate".
        try:
            extra = sd.WasapiSettings(auto_convert=True)
        except TypeError:  # sounddevice viejo sin auto_convert
            extra = None

        last_exc: Optional[Exception] = None
        for ch in candidates:
            fifo = _SampleFifo(
                ch,
                capacity_frames=int(self.samplerate * self.BUFFER_SECONDS),
                prime_frames=int(self.samplerate * self.PRIME_SECONDS),
            )

            def callback(outdata, frames, time_info, status, _fifo=fifo):
                _fifo.read_into(outdata)

            try:
                stream = sd.OutputStream(
                    device=self.device_index,
                    samplerate=self.samplerate,
                    channels=ch,
                    dtype="float32",
                    latency="low",
                    extra_settings=extra,
                    callback=callback,
                    finished_callback=self._on_finished,
                )
                stream.start()
            except Exception as exc:
                last_exc = exc
                logger.warning("No se pudo abrir salida hacia %s con %d canal(es): %s",
                               self.device_name, ch, exc)
                continue
            self.channels = ch
            self._fifo = fifo
            self._stream = stream
            return

        raise last_exc or RuntimeError(f"No se pudo abrir {self.device_name}")

    def push(self, data: np.ndarray) -> None:
        """Llamado desde el callback de captura. Nunca bloquea por mucho."""
        fifo = self._fifo
        if fifo is None or self._stopped.is_set():
            return
        fifo.write(_adapt_channels(data, self.channels))

    def _on_finished(self) -> None:
        # Corre en un hilo de PortAudio: solo se marca; check_health() lo
        # reporta desde el hilo del backend.
        if not self._stopped.is_set():
            self._finished_unexpectedly.set()

    def is_alive(self) -> bool:
        if self._stopped.is_set() or self._stream is None:
            return False
        if self._finished_unexpectedly.is_set():
            return False
        try:
            return bool(self._stream.active)
        except Exception:
            return False

    def stop(self) -> None:
        self._stopped.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                logger.debug("Error deteniendo salida %s", self.device_name, exc_info=True)
            try:
                stream.close()
            except Exception:
                logger.debug("Error cerrando salida %s", self.device_name, exc_info=True)


# =========================================================================
# Motor
# =========================================================================

class MirrorEngine:
    """Una sesión de mirroring: captura la fuente (loopback de una salida o
    un micrófono) mediante PyAudioWPatch y retransmite hacia N dispositivos
    destino mediante SoundDevice."""

    def __init__(self, on_error: Optional[Callable[[MirrorError], None]] = None,
                 source_kind: str = SOURCE_LOOPBACK):
        if source_kind not in (SOURCE_LOOPBACK, SOURCE_INPUT):
            raise ValueError(f"source_kind inválido: {source_kind!r}")
        self.source_kind = source_kind
        self._on_error = on_error or (lambda e: logger.warning("MirrorError: %s - %s", e.device_name, e.message))
        self._pa: Optional[pyaudio.PyAudio] = None
        self._pa_stream = None
        # Tupla inmutable leída por el callback de captura (sin locks).
        self._targets: Tuple[_TargetOutput, ...] = ()
        self._lock = threading.RLock()
        self._running = False
        self.source_name: str = ""
        self.samplerate: int = 48000
        self.channels: int = 2

    def is_running(self) -> bool:
        return self._running

    @property
    def target_names(self) -> List[str]:
        return [t.device_name for t in self._targets]

    def is_same_session(self, source_name: str, target_names: List[str]) -> bool:
        """True si ya está corriendo exactamente con esa fuente y destinos
        (sirve para ignorar un "Iniciar" repetido sin cortar el audio)."""
        return (self._running and self.source_name == source_name
                and set(self.target_names) == set(self._clean_targets(source_name, target_names)))

    def _clean_targets(self, source_name: str, target_names: List[str]) -> List[str]:
        targets = list(dict.fromkeys(target_names))  # sin duplicados, en orden
        if self.source_kind == SOURCE_LOOPBACK:
            # Evita realimentación: si la fuente también es destino, el
            # loopback vuelve a capturar lo que se reproduce -> eco infinito.
            targets = [n for n in targets if n != source_name]
        return targets

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def start(self, source_name: str, target_names: List[str]) -> List[MirrorError]:
        """Inicia el mirroring. Lanza RuntimeError si no puede capturar la
        fuente o si no pudo abrir ningún destino. Devuelve la lista de
        destinos que fallaron (si al menos uno funcionó)."""
        with self._lock:
            self._stop_locked()

            targets = self._clean_targets(source_name, target_names)
            if not targets:
                raise RuntimeError("Elegí al menos un destino distinto de la fuente.")

            # Índices frescos y dispositivos recién conectados (solo si otra
            # sesión no tiene streams abiertos).
            devices = query_wasapi_devices(refresh=True)
            by_name = {d["name"]: d for d in devices["outputs"]}

            self._pa = pyaudio.PyAudio()
            try:
                if self.source_kind == SOURCE_LOOPBACK:
                    source = self._find_loopback(source_name, devices["default_output"])
                else:
                    source = self._find_input(source_name, devices["default_input"])
                self.samplerate = int(source["defaultSampleRate"])
                self.channels = int(source["maxInputChannels"]) or 2

                opened: List[_TargetOutput] = []
                failures: List[MirrorError] = []
                for name in targets:
                    info = by_name.get(name)
                    if info is None:
                        failures.append(MirrorError(name, "El dispositivo ya no está disponible."))
                        continue
                    target = _TargetOutput(info["index"], name, info["channels"],
                                           self.samplerate, self.channels)
                    try:
                        target.start()
                    except Exception as exc:
                        logger.exception("No se pudo abrir salida hacia %s", name)
                        failures.append(MirrorError(name, str(exc)))
                        continue
                    opened.append(target)

                if not opened:
                    detail = "; ".join(f"{f.device_name}: {f.message}" for f in failures)
                    raise RuntimeError(f"No se pudo abrir ningún destino ({detail})")

                self._targets = tuple(opened)
                self._open_capture(source)
            except Exception:
                self._stop_locked()
                raise

            self.source_name = source_name
            self._running = True
            with _registry_lock:
                _running_engines.add(id(self))
            logger.info("Mirroring (%s) iniciado desde %s (sr=%d, ch=%d) hacia %d destino(s).",
                        self.source_kind, source_name, self.samplerate, self.channels, len(self._targets))
            return failures

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        was_running = self._running
        self._running = False
        targets, self._targets = self._targets, ()
        self._cleanup_pa()
        for target in targets:
            target.stop()
        with _registry_lock:
            _running_engines.discard(id(self))
        if was_running:
            logger.info("Mirroring (%s) detenido.", self.source_kind)

    def check_health(self) -> bool:
        """Detecta streams muertos (dispositivo desconectado, etc.),
        quita los destinos caídos y reporta vía `on_error`. Si la fuente
        deja de capturar o no queda ningún destino, detiene el mirroring.
        Devuelve True si el mirroring sigue activo."""
        errors: List[MirrorError] = []
        with self._lock:
            if not self._running:
                return False

            alive, dead = [], []
            for t in self._targets:
                (alive if t.is_alive() else dead).append(t)
            if dead:
                self._targets = tuple(alive)
                for t in dead:
                    t.stop()
                    errors.append(MirrorError(t.device_name, "El dispositivo se desconectó durante el mirroring."))

            capture_ok = False
            try:
                capture_ok = self._pa_stream is not None and self._pa_stream.is_active()
            except Exception:
                capture_ok = False

            if not capture_ok:
                errors.append(MirrorError(self.source_name, "La fuente dejó de capturar audio; mirroring detenido."))
                self._stop_locked()
            elif not alive:
                errors.append(MirrorError(self.source_name, "No quedan destinos activos; mirroring detenido."))
                self._stop_locked()
            running = self._running

        for err in errors:
            self._on_error(err)
        return running

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _find_loopback(self, source_name: str, default_name: Optional[str]) -> dict:
        def base(dev: dict) -> str:
            name = dev["name"]
            return name[:-len(LOOPBACK_SUFFIX)] if name.endswith(LOOPBACK_SUFFIX) else name

        found = _match_by_name(list(self._pa.get_loopback_device_info_generator()), source_name, base)
        if found is not None:
            return found
        if default_name and source_name == default_name:
            # La fuente elegida es el predeterminado: pyaudiowpatch sabe
            # resolver su loopback aunque el nombre no coincida.
            try:
                return self._pa.get_default_wasapi_loopback()
            except Exception:
                pass
        raise RuntimeError(f"No se encontró dispositivo loopback WASAPI para {source_name}")

    def _find_input(self, source_name: str, default_name: Optional[str]) -> dict:
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        inputs = []
        for i in range(self._pa.get_device_count()):
            dev = self._pa.get_device_info_by_index(i)
            if (dev["hostApi"] == wasapi and dev["maxInputChannels"] > 0
                    and not dev.get("isLoopbackDevice")):
                inputs.append(dev)
        found = _match_by_name(inputs, source_name, lambda d: d["name"])
        if found is not None:
            return found
        if default_name and source_name == default_name:
            try:
                return self._pa.get_default_wasapi_device(d_in=True)
            except Exception:
                pass
        raise RuntimeError(f"No se encontró el micrófono WASAPI {source_name}")

    def _open_capture(self, loopback: dict) -> None:
        channels = self.channels

        def pa_callback(in_data, frame_count, time_info, status):
            try:
                if in_data:
                    data = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels)
                    for target in self._targets:  # tupla: lectura atómica, sin locks
                        target.push(data)
            except Exception:
                logger.exception("Error en el callback de captura")
            return (None, pyaudio.paContinue)

        self._pa_stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=self.samplerate,
            input=True,
            input_device_index=loopback["index"],
            stream_callback=pa_callback,
        )
        self._pa_stream.start_stream()

    def _cleanup_pa(self) -> None:
        stream, self._pa_stream = self._pa_stream, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                logger.debug("Error deteniendo captura", exc_info=True)
            try:
                stream.close()
            except Exception:
                logger.debug("Error cerrando captura", exc_info=True)
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                logger.debug("Error terminando PyAudio", exc_info=True)


# =========================================================================
# Helpers de PortAudio (sounddevice)
# =========================================================================

def _match_by_name(devices: List[dict], wanted: str, name_of: Callable[[dict], str]) -> Optional[dict]:
    """Busca por nombre: exacto, luego sin mayúsculas/espacios, luego por
    prefijo (algunos host APIs truncan los nombres)."""
    for dev in devices:
        if name_of(dev) == wanted:
            return dev
    key = wanted.casefold().strip()
    for dev in devices:
        if name_of(dev).casefold().strip() == key:
            return dev
    for dev in devices:
        n = name_of(dev).casefold().strip()
        if n and key and (n.startswith(key) or key.startswith(n)):
            return dev
    return None


def query_wasapi_devices(refresh: bool = False) -> dict:
    """Devuelve las salidas y entradas WASAPI:
    {"outputs": [{name, index, channels}], "inputs": [...],
     "default_output": str|None, "default_input": str|None}.

    Con `refresh=True` reinicializa PortAudio para detectar dispositivos
    conectados después de arrancar la app (sounddevice cachea la lista desde
    el import). Si alguna sesión de mirroring tiene streams abiertos no se
    reinicializa (los cerraría) y se devuelve la lista actual."""
    if refresh and not _other_engine_running():
        _refresh_portaudio()
    return _wasapi_devices()


def _refresh_portaudio() -> None:
    """Reinicializa PortAudio de sounddevice para releer la lista de
    dispositivos. No debe haber streams de sounddevice abiertos."""
    try:
        sd._terminate()
    except Exception:
        logger.debug("sd._terminate falló", exc_info=True)
    try:
        sd._initialize()
    except Exception:
        logger.exception("No se pudo reinicializar PortAudio")
        raise


def _wasapi_devices() -> dict:
    result = {"outputs": [], "inputs": [], "default_output": None, "default_input": None}
    hostapis = sd.query_hostapis()
    wasapi_index = next((i for i, h in enumerate(hostapis) if "WASAPI" in h["name"]), None)
    if wasapi_index is None:
        return result
    default_out = hostapis[wasapi_index].get("default_output_device", -1)
    default_in = hostapis[wasapi_index].get("default_input_device", -1)
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != wasapi_index:
            continue
        if d["max_output_channels"] > 0:
            result["outputs"].append({"index": i, "name": d["name"], "channels": d["max_output_channels"]})
            if i == default_out:
                result["default_output"] = d["name"]
        if d["max_input_channels"] > 0:
            result["inputs"].append({"index": i, "name": d["name"], "channels": d["max_input_channels"]})
            if i == default_in:
                result["default_input"] = d["name"]
    return result
