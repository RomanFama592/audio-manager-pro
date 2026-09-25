"""
core/mirror_engine.py
----------------------
Captura el audio que suena por el dispositivo de salida predeterminado
actual mediante WASAPI Loopback (sounddevice) y lo retransmite en tiempo
real hacia uno o más dispositivos de salida adicionales seleccionados
por el usuario ("Audio Duplication / Mirroring").

Diseño:
  * Un `InputStream` en modo loopback captura el mix del dispositivo fuente.
  * Por cada dispositivo destino se abre un `OutputStream` independiente,
    cada uno con su propio buffer (ring buffer simple vía `queue.Queue`)
    para tolerar latencias/drift distintos entre dispositivos sin que uno
    lento bloquee a los demás.
  * Todo corre en threads separados de la GUI. Los errores (dispositivo
    desconectado a mitad de la reproducción, etc.) se capturan y se
    reportan vía callback, sin tirar abajo el proceso completo.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger("audio_manager.mirror_engine")


@dataclass
class MirrorError:
    device_name: str
    message: str


class _TargetOutput:
    """Encapsula el stream de salida hacia UN dispositivo destino, con su
    propio buffer para absorber diferencias de reloj/latencia."""

    MAX_QUEUE_CHUNKS = 32  # ~ backpressure: descarta si el consumidor se atrasa mucho

    def __init__(self, device_index: int, device_name: str, samplerate: int, channels: int,
                 on_error: Callable[[MirrorError], None]):
        self.device_index = device_index
        self.device_name = device_name
        self.samplerate = samplerate
        self.channels = channels
        self._on_error = on_error
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=self.MAX_QUEUE_CHUNKS)
        self._stream: Optional[sd.OutputStream] = None
        self._stopped = threading.Event()

    def start(self) -> None:
        def callback(outdata, frames, time_info, status):
            if status:
                logger.debug("Output status [%s]: %s", self.device_name, status)
            try:
                chunk = self._q.get_nowait()
            except queue.Empty:
                outdata.fill(0)  # silencio si aún no hay datos (evita clicks/pops)
                return
            # El chunk puede tener una cantidad de canales distinta a la del
            # stream de salida (p. ej. la fuente es estéreo pero el destino
            # solo abrió en mono tras el fallback de negociación).
            n = min(len(chunk), frames)
            ch = min(chunk.shape[1], outdata.shape[1]) if chunk.ndim > 1 else outdata.shape[1]
            outdata[:n, :ch] = chunk[:n, :ch]
            if ch < outdata.shape[1]:
                outdata[:n, ch:] = 0
            if n < frames:
                outdata[n:].fill(0)

        # Distintos dispositivos (sobre todo auriculares Bluetooth en perfil
        # manos-libres) solo aceptan ciertas combinaciones de canales bajo
        # WASAPI. Se prueba con la cantidad pedida y se va bajando.
        candidates = []
        for c in (self.channels, 2, 1):
            if c and c not in candidates:
                candidates.append(c)

        last_exc: Optional[Exception] = None
        for ch in candidates:
            try:
                self._stream = sd.OutputStream(
                    device=self.device_index,
                    samplerate=self.samplerate,
                    channels=ch,
                    dtype="float32",
                    callback=callback,
                    finished_callback=self._on_finished,
                )
                self._stream.start()
                self.channels = ch
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("No se pudo abrir salida hacia %s con %d canal(es): %s",
                                self.device_name, ch, exc)
                self._stream = None

        if last_exc is not None:
            logger.exception("No se pudo abrir salida hacia %s con ninguna combinación de canales",
                              self.device_name)
            self._on_error(MirrorError(self.device_name, str(last_exc)))

    def push(self, data: np.ndarray) -> None:
        if self._stopped.is_set():
            return
        try:
            self._q.put_nowait(data.copy())
        except queue.Full:
            # El destino no consume tan rápido como llega el audio: se
            # descarta el chunk más viejo para no acumular latencia infinita.
            try:
                self._q.get_nowait()
                self._q.put_nowait(data.copy())
            except queue.Empty:
                pass

    def _on_finished(self) -> None:
        if not self._stopped.is_set():
            # El stream terminó solo -> normalmente indica que el
            # dispositivo fue desconectado/deshabilitado en caliente.
            self._on_error(MirrorError(self.device_name, "El dispositivo se desconectó durante el mirroring."))

    def stop(self) -> None:
        self._stopped.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


class MirrorEngine:
    """Orquesta la captura loopback de UN dispositivo fuente y su
    retransmisión hacia N dispositivos destino."""

    def __init__(self, on_error: Optional[Callable[[MirrorError], None]] = None):
        self._on_error = on_error or (lambda e: logger.warning("MirrorError: %s - %s", e.device_name, e.message))
        self._input_stream: Optional[sd.InputStream] = None
        self._targets: Dict[int, _TargetOutput] = {}
        self._lock = threading.Lock()
        self._running = False
        self.samplerate: int = 48000
        self.channels: int = 2

    # ---------------------------------------------------------------
    def is_running(self) -> bool:
        return self._running

    def start(self, source_device_index: int, target_device_indices: List[int],
              device_names: Dict[int, str]) -> None:
        """Inicia la captura loopback del `source_device_index` (una salida,
        capturada vía WASAPI loopback) y comienza a duplicarla hacia todos
        los `target_device_indices`."""
        with self._lock:
            if self._running:
                self.stop()

            src_info = sd.query_devices(source_device_index)
            default_sr = int(src_info["default_samplerate"]) if src_info.get("default_samplerate") else 48000
            out_channels = src_info.get("max_output_channels") or 2

            wasapi_settings = sd.WasapiSettings(loopback=True)

            def input_callback(indata, frames, time_info, status):
                if status:
                    logger.debug("Input(loopback) status: %s", status)
                data = indata.copy()
                with self._lock:
                    for target in self._targets.values():
                        target.push(data)

            # Algunos dispositivos (típicamente auriculares Bluetooth en
            # perfil manos-libres, o dispositivos virtuales) no aceptan la
            # combinación "obvia" de canales/sample rate para loopback bajo
            # WASAPI y devuelven PaErrorCode -9998 (Invalid number of
            # channels). Se prueban combinaciones decrecientes hasta que
            # PortAudio acepte una.
            channel_candidates = []
            for c in (out_channels, 2, 1):
                if c and c not in channel_candidates:
                    channel_candidates.append(c)
            samplerate_candidates = []
            for sr in (default_sr, 48000, 44100):
                if sr and sr not in samplerate_candidates:
                    samplerate_candidates.append(sr)

            self._input_stream = None
            last_exc: Optional[Exception] = None
            for sr in samplerate_candidates:
                for ch in channel_candidates:
                    try:
                        stream = sd.InputStream(
                            device=source_device_index,
                            samplerate=sr,
                            channels=ch,
                            dtype="float32",
                            callback=input_callback,
                            extra_settings=wasapi_settings,
                        )
                        stream.start()
                        self._input_stream = stream
                        self.samplerate = sr
                        self.channels = ch
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                        logger.warning(
                            "Loopback: combinación samplerate=%s channels=%s rechazada para %s: %s",
                            sr, ch, src_info.get("name"), exc,
                        )
                if self._input_stream is not None:
                    break

            if self._input_stream is None:
                logger.exception("No se pudo iniciar la captura loopback (ninguna combinación válida)")
                self._on_error(MirrorError(src_info.get("name", "fuente"), str(last_exc)))
                self._cleanup_targets()
                self._running = False
                raise last_exc if last_exc else RuntimeError("No se pudo abrir el loopback")

            # Recién ahora que se conoce el samplerate/canales reales que
            # aceptó la fuente, se abren los destinos con esos valores.
            self._targets = {}
            for idx in target_device_indices:
                name = device_names.get(idx, f"device#{idx}")
                target = _TargetOutput(idx, name, self.samplerate, self.channels, self._on_error)
                target.start()
                self._targets[idx] = target

            self._running = True
            logger.info("Mirroring iniciado desde %s (sr=%d, ch=%d) hacia %d destino(s).",
                        src_info["name"], self.samplerate, self.channels, len(self._targets))

    def add_target(self, device_index: int, device_name: str) -> None:
        with self._lock:
            if device_index in self._targets or not self._running:
                return
            target = _TargetOutput(device_index, device_name, self.samplerate, self.channels, self._on_error)
            target.start()
            self._targets[device_index] = target

    def remove_target(self, device_index: int) -> None:
        with self._lock:
            target = self._targets.pop(device_index, None)
        if target:
            target.stop()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._input_stream is not None:
                try:
                    self._input_stream.stop()
                    self._input_stream.close()
                except Exception:
                    pass
                self._input_stream = None
            self._cleanup_targets()

    def _cleanup_targets(self) -> None:
        for target in self._targets.values():
            target.stop()
        self._targets.clear()

    @staticmethod
    def list_wasapi_output_devices() -> List[dict]:
        """Utilidad para la UI: devuelve los dispositivos de salida
        disponibles bajo el host API WASAPI (necesario para loopback)."""
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_index = next((i for i, h in enumerate(hostapis) if "WASAPI" in h["name"]), None)
        result = []
        for i, d in enumerate(devices):
            if wasapi_index is not None and d["hostapi"] == wasapi_index and d["max_output_channels"] > 0:
                result.append({"index": i, "name": d["name"], "channels": d["max_output_channels"]})
        return result
