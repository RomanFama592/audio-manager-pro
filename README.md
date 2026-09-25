# Audio Manager Pro

Aplicación de escritorio para Windows 11 (Fluent Design) para gestión
inteligente de dispositivos de audio: cambio de predeterminado
(sistema/comunicaciones), reglas de prioridad automáticas y duplicación
(mirroring) de audio en tiempo real vía WASAPI.

## 1. Estructura del proyecto

```
audio_manager/
├── main.py                  # Punto de entrada (QApplication, tray, ventana)
├── config.py                 # Persistencia de configuración (JSON en %APPDATA%)
├── requirements.txt
├── AudioManagerPro.spec      # Build de PyInstaller
├── resources/
│   └── app_icon.ico          # (agregar tu propio ícono .ico aquí)
├── core/                      # Lógica pura, sin dependencias de Qt
│   ├── device_manager.py     # Enumeración WASAPI + cambio de default + hot-plug
│   ├── mirror_engine.py      # Captura loopback + duplicación multi-salida
│   └── rules_engine.py       # Evaluación de reglas de prioridad
└── ui/
    ├── signals_bridge.py     # QThread que aloja el backend y expone señales Qt
    ├── main_window.py        # FluentWindow con páginas: Dispositivos/Reglas/Mirroring
    └── tray.py                # Icono y menú de la bandeja del sistema
```

### Por qué esta separación

- **`core/` es agnóstico de UI.** Se puede probar con `pytest` sin levantar
  Qt ni depender de la bandeja del sistema. `rules_engine.py`, por ejemplo,
  recibe funciones inyectadas para aplicar cambios, lo que permite testear
  la lógica de prioridades con mocks.
- **Un solo `QThread` (`AudioBackendWorker`) concentra todo el trabajo de
  audio/COM.** COM (usado por `pycaw`/`comtypes`/`IPolicyConfig`) requiere
  inicializarse por hilo (`CoInitializeEx`); mezclar llamadas COM
  directamente en el hilo de la GUI puede congelar la interfaz o generar
  errores de apartment mismatch. El `DeviceWatcher` interno además abre
  **su propio** hilo con apartamento MTA para escuchar notificaciones de
  hot-plug sin bloquear al worker.
- **Las señales Qt son la única vía de comunicación** entre los hilos de
  audio y la GUI (`devices_changed`, `rule_applied`, `mirror_error`, etc.).
  Al conectar señales entre objetos que viven en threads distintos, Qt usa
  automáticamente `QueuedConnection`, por lo que no hay que preocuparse
  por locks en el código de la UI.

## 2. Cómo funciona el cambio de dispositivo predeterminado

La Windows API pública **no expone** una función para cambiar el
dispositivo predeterminado (por diseño, Microsoft lo reservó al Panel de
Control / Configuración). La técnica estándar —usada por utilidades como
EarTrumpet, SoundSwitch o AudioDeviceCmdlets— es invocar la interfaz COM
interna `IPolicyConfig` (`SetDefaultEndpoint`), documentada de forma
extraoficial pero estable desde Windows Vista hasta Windows 11.
Esto está implementado en `core/device_manager.py::set_default_device`.

> ⚠️ Al ser una interfaz no documentada oficialmente, Microsoft podría
> cambiarla en una futura build de Windows. Si algún método deja de
> responder, revisar si cambió el GUID de `IPolicyConfig` (hay variantes
> `IPolicyConfigVista` para Windows 7/Vista que **no** aplican aquí).

## 3. Mirroring de audio (WASAPI Loopback)

`sounddevice` permite abrir un `InputStream` sobre un dispositivo de
**salida** en modo *loopback* (`WasapiSettings(loopback=True)`), lo que
captura la mezcla final que suena por ese dispositivo. Cada destino recibe
su propio `OutputStream` con un buffer (`queue.Queue`) independiente, de
forma que:

- Un destino lento no bloquea a los demás (se descartan chunks viejos si
  el buffer se llena, priorizando baja latencia sobre completitud).
- Si un destino se desconecta a mitad de la reproducción, su callback
  `finished_callback` dispara `mirror_error`, que llega a la GUI como un
  `InfoBar` de error — el resto de los destinos sigue funcionando.

## 4. Manejo de errores y casos borde

| Caso | Manejo |
|---|---|
| Dispositivo desconectado durante el mirroring | `_TargetOutput._on_finished` detecta el cierre inesperado del stream y emite `MirrorError`; los demás destinos no se ven afectados. |
| Falta de permisos / dispositivo exclusivo tomado por otra app | `sd.OutputStream`/`InputStream` lanza excepción al abrir; se captura y se reporta vía `mirror_error` sin tirar abajo el proceso. |
| `IPolicyConfig.SetDefaultEndpoint` falla (dispositivo removido justo antes del click) | Excepción COM capturada en la UI (`DevicesPage._apply_output/_apply_input`), mostrada como `InfoBar.error`. |
| Config corrupta (`config.json` inválido) | `config.load_config` detecta `JSONDecodeError`/`OSError` y regenera una config por defecto en vez de crashear. |
| Cierre de la ventana con la "X" | Se intercepta `closeEvent` y se oculta a la bandeja en lugar de cerrar el proceso; el cierre real solo ocurre desde "Salir" en el tray, que llama a `shutdown()` (detiene el watcher, el mirror engine y libera COM). |
| Bucle de re-aplicación de reglas | `RulesEngine` guarda `_last_applied_device_id` para no reaplicar la misma regla en cada evento de `OnPropertyValueChanged`. |

## 5. Instalación (desarrollo)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

> Nota: `sounddevice` requiere que las librerías de PortAudio estén
> disponibles; el paquete de PyPI para Windows ya las incluye
> precompiladas, no hace falta instalar nada extra.

## 6. Empaquetado a `.exe` standalone

1. Colocar un ícono `app_icon.ico` en `resources/`.
2. Instalar PyInstaller (ya está en `requirements.txt`):
   ```powershell
   pip install pyinstaller
   ```
3. Compilar usando el `.spec` incluido (ya configura `collect_all` para
   `qfluentwidgets` y `PySide6`, necesario para que los estilos/plugins de
   Qt viajen dentro del `.exe`):
   ```powershell
   pyinstaller AudioManagerPro.spec
   ```
4. El ejecutable queda en `dist/AudioManagerPro.exe` (modo `onefile`,
   sin consola).

### Notas de empaquetado

- Si el `.exe` arranca pero la ventana no muestra estilos Fluent
  correctamente, verificar que `collect_all("qfluentwidgets")` haya
  incluido los `.qss` (a veces requiere agregar manualmente
  `qfluentwidgets/_rc` según la versión instalada).
- Si Windows Defender / SmartScreen marca el `.exe` como desconocido,
  es el comportamiento esperado para binarios sin firma de código; para
  distribución pública se recomienda firmar el ejecutable con un
  certificado Authenticode.
- Para reducir el tamaño del binario, se puede compilar sin `onefile`
  (carpeta `dist/AudioManagerPro/`) o excluir módulos no usados de
  `PySide6` (p. ej. `QtWebEngine`, `QtMultimedia`) en el `.spec`.

## 7. Extensión futura sugerida

- Persistir el volumen y el balance por dispositivo (pycaw expone
  `AudioUtilities.GetSpeakers().EndpointVolume`).
- Agregar un ecualizador/ganancia por destino en `mirror_engine.py`
  (multiplicar el array `numpy` antes de encolarlo).
- Perfil por aplicación (enrutar el audio de un proceso específico a un
  dispositivo distinto) requeriría `IAudioSessionManager2`, fuera del
  alcance de esta primera versión.
