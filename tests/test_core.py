"""
Pruebas de la capa `core` y de la configuración (sin Qt ni audio real).

Ejecutar desde la raíz del proyecto:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from config import AppConfig, PriorityRule  # noqa: E402
from core.device_manager import AudioDeviceInfo, DeviceEvent, Direction, _NotificationClient  # noqa: E402
import core.mirror_engine as mirror_engine  # noqa: E402
from core.mirror_engine import (  # noqa: E402
    MirrorEngine, SOURCE_INPUT, SOURCE_LOOPBACK, _SampleFifo, _adapt_channels, _match_by_name,
)
from core.rules_engine import RulesEngine  # noqa: E402


def dev(dev_id, name, direction=Direction.OUTPUT, console=False, comm=False, state="Active"):
    return AudioDeviceInfo(id=dev_id, name=name, direction=direction, state=state,
                           is_default_console=console, is_default_comm=comm)


class PriorityRuleMatchTests(unittest.TestCase):
    def test_all_words_any_order(self):
        rule = PriorityRule("sony auriculares")
        self.assertTrue(rule.matches("Auriculares (WH-1000XM4 Sony)"))
        self.assertFalse(rule.matches("Auriculares (Genéricos)"))

    def test_negation(self):
        rule = PriorityRule("Altavoces ! Steam")
        self.assertTrue(rule.matches("Altavoces (High Definition Audio Device)"))
        self.assertFalse(rule.matches("Altavoces (Steam Streaming Speakers)"))

    def test_only_negations(self):
        rule = PriorityRule("! virtual ! steam")
        self.assertTrue(rule.matches("HDMI (NVIDIA High Definition Audio)"))
        self.assertFalse(rule.matches("Virtual Speakers"))

    def test_empty_never_matches(self):
        self.assertFalse(PriorityRule("   ").matches("Altavoces"))


class ConfigTests(unittest.TestCase):
    def test_from_dict_is_tolerant(self):
        cfg = AppConfig.from_dict({
            "auto_rules_enabled": False,
            "rules": [
                {"match_text": "HDMI", "role": "both", "priority": 20, "unknown_key": 1},
                {"match_text": "", "priority": 5},          # inválida: texto vacío
                {"role": "console"},                         # inválida: sin texto
                "basura",                                    # inválida: no es objeto
                {"match_text": "Auris", "role": "raro", "priority": "x"},
            ],
            "theme": "neon",
            "future_option": True,
        })
        self.assertFalse(cfg.auto_rules_enabled)
        self.assertEqual([r.match_text for r in cfg.rules], ["HDMI", "Auris"])
        self.assertEqual(cfg.rules[1].role, "both")
        self.assertEqual(cfg.rules[1].priority, 100)
        self.assertEqual(cfg.theme, "auto")

    def test_from_dict_rejects_non_object(self):
        with self.assertRaises(ValueError):
            AppConfig.from_dict([1, 2, 3])

    def test_corrupt_config_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"APPDATA": tmp}):
            path = config.get_config_path()
            with open(path, "w", encoding="utf-8") as f:
                f.write("[1, 2")  # JSON roto
            cfg = config.load_config()
            self.assertEqual(cfg.rules, [])
            self.assertTrue(os.path.exists(path + ".corrupt"))
            with open(path, encoding="utf-8") as f:
                self.assertIsInstance(json.load(f), dict)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"APPDATA": tmp}):
            cfg = AppConfig(rules=[PriorityRule("HDMI", "console", True, 20)])
            self.assertTrue(config.save_config(cfg))
            loaded = config.load_config()
            self.assertEqual(loaded.rules, cfg.rules)


class RulesEngineTests(unittest.TestCase):
    def make(self, rules, enabled=True):
        cfg = AppConfig(auto_rules_enabled=enabled, rules=rules)
        self.applied = []
        engine = RulesEngine(
            cfg,
            apply_default_output=lambda dev_id, also: self.applied.append(("console", dev_id, also)),
            apply_default_comm=lambda dev_id: self.applied.append(("comm", dev_id)),
        )
        return engine

    def test_highest_priority_active_device_wins(self):
        engine = self.make([PriorityRule("Altavoces", priority=30), PriorityRule("Auriculares", priority=10)])
        msg = engine.evaluate_and_apply([dev("spk", "Altavoces"), dev("hp", "Auriculares BT")])
        self.assertIn(("console", "hp", False), self.applied)
        self.assertIn(("comm", "hp"), self.applied)
        self.assertIsNotNone(msg)

    def test_roles_are_evaluated_independently(self):
        # Antes solo se aplicaba la regla de mayor prioridad global, así que
        # la regla de comunicaciones nunca se aplicaba.
        engine = self.make([
            PriorityRule("HDMI", role="console", priority=10),
            PriorityRule("Auriculares", role="communications", priority=20),
        ])
        engine.evaluate_and_apply([dev("hdmi", "HDMI"), dev("hp", "Auriculares")])
        self.assertEqual(sorted(self.applied), [("comm", "hp"), ("console", "hdmi", False)])

    def test_no_reapply_and_respects_manual_choice(self):
        engine = self.make([PriorityRule("Auriculares", priority=10)])
        devices = [dev("spk", "Altavoces"), dev("hp", "Auriculares")]
        engine.evaluate_and_apply(devices)
        self.applied.clear()
        # El usuario eligió a mano los altavoces: no se debe pisar.
        manual = [dev("spk", "Altavoces", console=True, comm=True), dev("hp", "Auriculares")]
        self.assertIsNone(engine.evaluate_and_apply(manual))
        self.assertEqual(self.applied, [])

    def test_already_default_is_not_reapplied(self):
        engine = self.make([PriorityRule("Auriculares", priority=10)])
        msg = engine.evaluate_and_apply([dev("hp", "Auriculares", console=True, comm=True)])
        self.assertIsNone(msg)
        self.assertEqual(self.applied, [])

    def test_fallback_when_best_disconnects(self):
        engine = self.make([PriorityRule("Auriculares", priority=10), PriorityRule("Altavoces", priority=30)])
        engine.evaluate_and_apply([dev("spk", "Altavoces"), dev("hp", "Auriculares")])
        self.applied.clear()
        engine.evaluate_and_apply([dev("spk", "Altavoces"), dev("hp", "Auriculares", state="Unplugged")])
        self.assertIn(("console", "spk", False), self.applied)

    def test_reset_reapplies(self):
        engine = self.make([PriorityRule("Auriculares", priority=10)])
        devices = [dev("spk", "Altavoces", console=True, comm=True), dev("hp", "Auriculares")]
        engine.evaluate_and_apply(devices)
        self.applied.clear()
        engine.reset()
        engine.evaluate_and_apply(devices)
        self.assertIn(("console", "hp", False), self.applied)

    def test_disabled(self):
        engine = self.make([PriorityRule("Auriculares")], enabled=False)
        self.assertIsNone(engine.evaluate_and_apply([dev("hp", "Auriculares")]))
        self.assertEqual(self.applied, [])

    def test_inputs_are_ignored(self):
        engine = self.make([PriorityRule("Micrófono")])
        self.assertIsNone(engine.evaluate_and_apply([dev("mic", "Micrófono", direction=Direction.INPUT)]))

    def test_failed_apply_is_retried(self):
        engine = self.make([PriorityRule("Auriculares", role="console")])
        engine._apply_default_output = mock.Mock(side_effect=OSError("COM"))
        with self.assertLogs("audio_manager.rules_engine", level="ERROR"):
            self.assertIsNone(engine.evaluate_and_apply([dev("hp", "Auriculares")]))
        engine._apply_default_output = lambda dev_id, also: self.applied.append(("console", dev_id, also))
        engine.evaluate_and_apply([dev("hp", "Auriculares")])
        self.assertEqual(self.applied, [("console", "hp", False)])


class NotificationClientTests(unittest.TestCase):
    """Reproduce el bug principal: pycaw cambió la firma de sus wrappers y
    cada evento lanzaba TypeError dentro del callback COM."""

    def test_python_level_calls(self):
        events = []
        client = _NotificationClient(events.append)
        client.OnDeviceStateChanged("id", 1)
        client.OnDeviceAdded("id")
        client.OnDeviceRemoved("id")
        client.OnDefaultDeviceChanged(0, 0, "id")
        client.OnPropertyValueChanged("id", None)
        self.assertEqual(events, [DeviceEvent.STATE, DeviceEvent.ADDED, DeviceEvent.REMOVED, DeviceEvent.DEFAULT])

    def test_calls_through_com_vtable(self):
        from pycaw.api.mmdeviceapi import IMMNotificationClient
        events = []
        client = _NotificationClient(events.append)
        ptr = client.QueryInterface(IMMNotificationClient)
        ptr.OnDefaultDeviceChanged(0, 2, "{dev}")
        ptr.OnDeviceStateChanged("{dev}", 8)
        self.assertEqual(events, [DeviceEvent.DEFAULT, DeviceEvent.STATE])


class SampleFifoTests(unittest.TestCase):
    def test_mismatched_block_sizes_keep_every_sample(self):
        # Productor de 441 frames, consumidor de 480: antes se perdían o se
        # rellenaban con ceros muestras en cada bloque (cortes de audio).
        fifo = _SampleFifo(channels=2, capacity_frames=48000, prime_frames=960)
        signal = np.arange(441 * 40 * 2, dtype=np.float32).reshape(-1, 2)
        for i in range(0, len(signal), 441):
            fifo.write(signal[i:i + 441])
        out_blocks = []
        while fifo.size >= 480:
            out = np.empty((480, 2), dtype=np.float32)
            self.assertEqual(fifo.read_into(out), 480)
            out_blocks.append(out)
        played = np.concatenate(out_blocks)
        np.testing.assert_array_equal(played, signal[:len(played)])

    def test_priming_outputs_silence_until_enough_data(self):
        fifo = _SampleFifo(channels=1, capacity_frames=1000, prime_frames=100)
        fifo.write(np.ones((50, 1), dtype=np.float32))
        out = np.full((10, 1), 7, dtype=np.float32)
        self.assertEqual(fifo.read_into(out), 0)
        self.assertTrue((out == 0).all())

    def test_overflow_drops_oldest(self):
        fifo = _SampleFifo(channels=1, capacity_frames=10, prime_frames=0)
        fifo.write(np.arange(8, dtype=np.float32).reshape(-1, 1))
        fifo.write(np.arange(8, 14, dtype=np.float32).reshape(-1, 1))
        out = np.empty((10, 1), dtype=np.float32)
        self.assertEqual(fifo.read_into(out), 10)
        np.testing.assert_array_equal(out[:, 0], np.arange(4, 14, dtype=np.float32))

    def test_adapt_channels(self):
        stereo = np.array([[1, 3], [2, 4]], dtype=np.float32)
        np.testing.assert_array_equal(_adapt_channels(stereo, 1), [[2], [3]])
        self.assertEqual(_adapt_channels(stereo, 6).shape, (2, 6))
        mono = np.array([[1], [2]], dtype=np.float32)
        np.testing.assert_array_equal(_adapt_channels(mono, 2), [[1, 1], [2, 2]])


class MirrorEngineLogicTests(unittest.TestCase):
    """Lógica de sesiones sin abrir streams reales."""

    def test_loopback_never_targets_its_own_source(self):
        engine = MirrorEngine(source_kind=SOURCE_LOOPBACK)
        self.assertEqual(engine._clean_targets("HDMI", ["HDMI", "Auris", "Auris"]), ["Auris"])
        with self.assertRaises(RuntimeError):
            engine.start("HDMI", ["HDMI"])  # falla antes de tocar PortAudio

    def test_microphone_keeps_all_targets(self):
        engine = MirrorEngine(source_kind=SOURCE_INPUT)
        self.assertEqual(engine._clean_targets("Mic", ["Mic", "Auris"]), ["Mic", "Auris"])

    def test_invalid_kind(self):
        with self.assertRaises(ValueError):
            MirrorEngine(source_kind="otro")

    def test_same_session_detects_repeated_start(self):
        engine = MirrorEngine(source_kind=SOURCE_LOOPBACK)
        engine._running = True
        engine.source_name = "HDMI"
        engine._targets = (mock.Mock(device_name="A"), mock.Mock(device_name="B"))
        self.assertTrue(engine.is_same_session("HDMI", ["B", "A", "HDMI"]))
        self.assertFalse(engine.is_same_session("HDMI", ["A"]))
        self.assertFalse(engine.is_same_session("Otro", ["A", "B"]))

    def test_no_portaudio_refresh_while_another_session_runs(self):
        # Reinicializar PortAudio cerraría los streams de la otra sesión.
        with mock.patch.object(mirror_engine, "_refresh_portaudio") as refresh, \
                mock.patch.object(mirror_engine, "_wasapi_devices", return_value={}):
            mirror_engine.query_wasapi_devices(refresh=True)
            self.assertEqual(refresh.call_count, 1)
            other = object()
            mirror_engine._running_engines.add(id(other))
            try:
                mirror_engine.query_wasapi_devices(refresh=True)
            finally:
                mirror_engine._running_engines.discard(id(other))
            self.assertEqual(refresh.call_count, 1)

    def test_match_by_name(self):
        devs = [{"name": "Micrófono (USB)"}, {"name": "Virtual Mic"}]
        name = lambda d: d["name"]  # noqa: E731
        self.assertIs(_match_by_name(devs, "Micrófono (USB)", name), devs[0])
        self.assertIs(_match_by_name(devs, "virtual mic ", name), devs[1])
        self.assertIsNone(_match_by_name(devs, "Otro", name))


class AutostartTests(unittest.TestCase):
    """Usa claves de prueba bajo HKCU\\Software\\AudioManagerProTests: no
    toca el inicio automático real del usuario."""

    BASE = r"Software\AudioManagerProTests"
    RUN = BASE + r"\Run"
    APPROVED = BASE + r"\StartupApproved"

    def setUp(self):
        from core import autostart
        self.autostart = autostart
        self.keys = dict(run_key=self.RUN, approved_key=self.APPROVED, value_name="Test")

    def tearDown(self):
        import winreg
        for key in (self.RUN, self.APPROVED, self.BASE):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass

    def test_enable_disable_roundtrip(self):
        a = self.autostart
        self.assertFalse(a.is_enabled(**self.keys))
        a.set_enabled(True, command='"C:\\x\\App.exe" --minimized', **self.keys)
        self.assertTrue(a.is_enabled(**self.keys))
        self.assertEqual(a.get_command(self.RUN, "Test"), '"C:\\x\\App.exe" --minimized')
        a.set_enabled(False, **self.keys)
        self.assertFalse(a.is_enabled(**self.keys))
        a.set_enabled(False, **self.keys)  # idempotente

    def test_respects_windows_startup_toggle(self):
        import winreg
        a = self.autostart
        a.set_enabled(True, command="x", **self.keys)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.APPROVED) as key:
            winreg.SetValueEx(key, "Test", 0, winreg.REG_BINARY, b"\x03" + b"\x00" * 11)
        self.assertFalse(a.is_enabled(**self.keys))   # deshabilitado desde Windows
        a.set_enabled(True, command="x", **self.keys)  # activarlo desde la app lo limpia
        self.assertTrue(a.is_enabled(**self.keys))

    def test_launch_command_from_source_uses_pythonw_and_minimized(self):
        cmd = self.autostart.launch_command()
        self.assertIn("main.py", cmd)
        self.assertTrue(cmd.endswith("--minimized"))


class CommandLineTests(unittest.TestCase):
    def test_parse_args(self):
        import main
        self.assertTrue(main.parse_args(["--minimized"]).minimized)
        self.assertTrue(main.parse_args(["--quit", "-platform", "offscreen"]).quit)
        args = main.parse_args([])
        self.assertFalse(args.minimized or args.quit)


if __name__ == "__main__":
    unittest.main()
