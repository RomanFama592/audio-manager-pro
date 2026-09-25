"""
core/mirror_engine.py
----------------------
Captura el audio que suena por el dispositivo de salida predeterminado
actual mediante WASAPI Loopback (pyaudiowpatch) y lo retransmite en tiempo
real hacia uno o más dispositivos de salida adicionales seleccionados
por el usuario ("Audio Duplication / Mirroring") utilizando sounddevice.

Diseño:
  * Un stream de entrada en modo WASAPI Loopback (pyaudiowpatch) captura 
    el mix en tiempo real del dispositivo fuente.
  * Por cada dispositivo destino se abre un `OutputStream` independiente 
    (sounddevice), cada uno con su propio buffer (ring buffer simple vía 
    `queue.Queue`) para tolerar latencias/drift distintos entre dispositivos 
    sin que uno lento bloquee a los demás.
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
import pyaudiowpatch as pyaudio
import sounddevice as sd

logger = logging.getLogger("audio_manager.mirror_engine")


@dataclass
class MirrorError:
    device_name: str
    message: str


class _TargetOutput:
    """Encapsula el stream de salida hacia UN dispositivo destino."""

    MAX_QUEUE_CHUNKS = 32

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
                outdata.fill(0)
                return

            n = min(len(chunk), frames)
            ch = min(chunk.shape[1], outdata.shape[1]) if chunk.ndim > 1 else outdata.shape[1]
            outdata[:n, :ch] = chunk[:n, :ch]
            if ch < outdata.shape[1]:
                outdata[:n, ch:] = 0
            if n < frames:
                outdata[n:].fill(0)

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
            logger.exception("No se pudo abrir salida hacia %s", self.device_name)
            self._on_error(MirrorError(self.device_name, str(last_exc)))

    def push(self, data: np.ndarray) -> None:
        if self._stopped.is_set():
            return
        try:
            self._q.put_nowait(data.copy())
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(data.copy())
            except queue.Empty:
                pass

    def _on_finished(self) -> None:
        if not self._stopped.is_set():
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
    """Orquesta la captura loopback WASAPI mediante PyAudioWPatch y retransmite

    hacia N dispositivos destino mediante SoundDevice."""

    def __init__(self, on_error: Optional[Callable[[MirrorError], None]] = None):
        self._on_error = on_error or (lambda e: logger.warning("MirrorError: %s - %s", e.device_name, e.message))
        self._pa: Optional[pyaudio.PyAudio] = None
        self._pa_stream = None
        self._targets: Dict[int, _TargetOutput] = {}
        self._lock = threading.Lock()
        self._running = False
        self.samplerate: int = 48000
        self.channels: int = 2

    def is_running(self) -> bool:
        return self._running

    def start(self, source_device_index: int, target_device_indices: List[int],
              device_names: Dict[int, str]) -> None:
        with self._lock:
            if self._running:
                self.stop()

            self._pa = pyaudio.PyAudio()

            # Obtener información del dispositivo de salida de sounddevice para mapear en pyaudiowpatch
            sd_src_info = sd.query_devices(source_device_index)
            target_name = sd_src_info["name"]

            # Buscar el dispositivo Loopback correspondiente en PyAudioWPatch
            wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            loopback_device = None

            for dev in self._pa.get_loopback_device_info_generator():
                if target_name in dev["name"]:
                    loopback_device = dev
                    break

            if not loopback_device:
                # Si no coincide por nombre exacto, toma el loopback por defecto de WASAPI
                default_speakers = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                for dev in self._pa.get_loopback_device_info_generator():
                    if default_speakers["name"] in dev["name"]:
                        loopback_device = dev
                        break

            if not loopback_device:
                self._pa.terminate()
                err_msg = f"No se encontró dispositivo loopback WASAPI para {target_name}"
                self._on_error(MirrorError(target_name, err_msg))
                raise RuntimeError(err_msg)

            self.samplerate = int(loopback_device["defaultSampleRate"])
            self.channels = loopback_device["maxInputChannels"]

            def pa_callback(in_data, frame_count, time_info, status):
                # Convertir bytes crudos de PyAudio a ndarray float32
                data = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self.channels)
                with self._lock:
                    for target in self._targets.values():
                        target.push(data)
                return (None, pyaudio.paContinue)

            try:
                self._pa_stream = self._pa.open(
                    format=pyaudio.paFloat32,
                    channels=self.channels,
                    rate=self.samplerate,
                    input=True,
                    input_device_index=loopback_device["index"],
                    stream_callback=pa_callback,
                )
                self._pa_stream.start_stream()
            except Exception as exc:
                logger.exception("Error al iniciar captura WASAPI loopback con PyAudioWPatch")
                self._cleanup_pa()
                self._on_error(MirrorError(target_name, str(exc)))
                raise exc

            # Inicializar los dispositivos destino de salida
            self._targets = {}
            for idx in target_device_indices:
                name = device_names.get(idx, f"device#{idx}")
                target = _TargetOutput(idx, name, self.samplerate, self.channels, self._on_error)
                target.start()
                self._targets[idx] = target

            self._running = True
            logger.info("Mirroring iniciado desde %s (sr=%d, ch=%d) hacia %d destino(s).",
                        target_name, self.samplerate, self.channels, len(self._targets))

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
            self._cleanup_pa()
            self._cleanup_targets()

    def _cleanup_pa(self) -> None:
        if self._pa_stream is not None:
            try:
                self._pa_stream.stop_stream()
                self._pa_stream.close()
            except Exception:
                pass
            self._pa_stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def _cleanup_targets(self) -> None:
        for target in self._targets.values():
            target.stop()
        self._targets.clear()

    @staticmethod
    def list_wasapi_output_devices() -> List[dict]:
        """Devuelve los dispositivos de salida disponibles bajo WASAPI."""
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_index = next((i for i, h in enumerate(hostapis) if "WASAPI" in h["name"]), None)
        result = []
        for i, d in enumerate(devices):
            if wasapi_index is not None and d["hostapi"] == wasapi_index and d["max_output_channels"] > 0:
                result.append({"index": i, "name": d["name"], "channels": d["max_output_channels"]})
        return result