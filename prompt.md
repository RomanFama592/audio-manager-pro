 **Rol:** Actúa como un Arquitecto de Software Senior especializado en desarrollo de aplicaciones de escritorio Python para Windows 11 y programación de sistemas de audio a bajo nivel (WASAPI / Windows CoreAudio API).
 **Objetivo:** Diseñar e implementar una aplicación moderna, ligera y elegante para Windows 11 en Python que permita la gestión inteligente de dispositivos de entrada y salida de audio.
 ---
 
 
 ### 1. Funcionalidades Clave
 
 
 1. **Gestión Avanzada de Dispositivos:**
 * Listar entradas (micrófonos) y salidas (altavoces/auriculares).
 * Filtrar e ignorar dispositivos deshabilitados (`Disabled`).
 * Monitorizar en tiempo real el estado de reconexión/desconexión (`Connected` / `Disconnected`).
 * Permitir cambiar de forma independiente el **Dispositivo Predeterminado de Sistema** y el **Dispositivo Predeterminado de Comunicaciones**.
 
 
 2. **Lógica de Prioridades y Reglas:**
 * Sistema de reglas/prioridades definible por el usuario (ej. si se conecta "Auriculares Bluetooth", pasa a ser el predeterminado automáticamente; si se desconecta, vuelve a "Altavoces").
 
 
 3. **Clonado / Mirroring de Audio (Audio Duplication):**
 * Función para capturar el flujo de audio del dispositivo predeterminado actual (vía WASAPI Loopback) y transmitirlo en tiempo real hacia otros dispositivos seleccionados simultáneamente.
 
 
 4. **System Tray (Bandeja de Sistema):**
 * Minimizarse en la bandeja del sistema.
 * Menú contextual rápido para:
 * Activar/Desactivar la lógica automática de prioridades.
 * Forzar un dispositivo predeterminado con un clic.
 * Salir de la aplicación.
 
 
 
 
 
 
 ---
 
 
 ### 2. Interfaz de Usuario y Estética
 
 
 * **Estilo:** Fluent Design nativo de Windows 11 (esquinas redondeadas, soporte para modo oscuro/claro del sistema, tipografía Segoe UI Variable, paleta de colores limpia y moderna).
 * **Framework GUI Sugerido:** Utiliza `PySide6` con `qfluentwidgets` (o en su defecto `CustomTkinter`) para garantizar una interfaz verdaderamente profesional y alineada con Windows 11.
 
 
 ---
 
 
 ### 3. Arquitectura Técnica Recomendada
 
 
 * **Control de Dispositivos Windows:** Utilizar `pycaw` o llamadas COM mediante `comtypes` a la API `IMMDeviceEnumerator` de Windows CoreAudio.
 * **Audio Mirroring / Stream Capture:** Utilizar `sounddevice` con soporte de host API WASAPI para la captura en bucle (Loopback) y reproducción multicanal de baja latencia.
 * **Procesamiento en Segundo Plano:** El motor de audio y la escucha de eventos de dispositivos deben ejecutarse en hilos (`threading` o `QThread`) independientes de la interfaz gráfica para no congelar la GUI.
 
 
 ---
 
 
 ### 4. Estructura de la Respuesta Requerida
 
 
 Por favor, proporciona:
 1. **Arquitectura del Proyecto:** Lista de dependencias (`requirements.txt`) e instructivo breve de arquitectura.
 2. **Código Fuente Completo y Modular:**
 * Módulo de gestión de audio de Windows (cambio de dispositivo por defecto, filtrado de estados, captura WASAPI).
 * Módulo de la interfaz de usuario (Ventana principal + System Tray).
 * Motor de reglas y duplicación de audio.
 
 
 3. **Manejo de Errores y Casos Borde:** Como la caída de dispositivos durante el mirroring o permisos de audio.
 4. **Instrucciones para empaquetar:** Cómo compilarlo a un `.exe` standalone con `PyInstaller`.
 
 