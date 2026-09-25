"""
config.py
---------
Manejo de configuración persistente de la aplicación (reglas de prioridad,
dispositivos de mirroring, preferencias de UI). Se guarda como JSON en
%APPDATA%\\AudioManagerPro\\config.json
"""
from __future__ import annotations

import json
import os
import dataclasses
from dataclasses import dataclass, field
from typing import List, Dict, Any

APP_NAME = "AudioManagerPro"


def get_config_dir() -> str:
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def get_config_path() -> str:
    return os.path.join(get_config_dir(), "config.json")


@dataclass
class PriorityRule:
    """Regla: si un dispositivo cuyo nombre contiene `match_text` se conecta,
    se convierte automáticamente en predeterminado (según `role`).
    `priority` más bajo = mayor prioridad (se evalúa primero)."""
    match_text: str
    role: str = "both"          # "console" | "communications" | "both"
    enabled: bool = True
    priority: int = 100

    def matches(self, device_name: str) -> bool:
        return self.match_text.lower() in device_name.lower()


@dataclass
class AppConfig:
    auto_rules_enabled: bool = True
    rules: List[PriorityRule] = field(default_factory=list)
    mirror_enabled: bool = False
    mirror_targets: List[str] = field(default_factory=list)  # device ids
    start_minimized: bool = False
    theme: str = "auto"  # auto | light | dark

    # ---------- serialización ----------
    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AppConfig":
        rules = [PriorityRule(**r) for r in d.get("rules", [])]
        cfg = AppConfig(
            auto_rules_enabled=d.get("auto_rules_enabled", True),
            rules=rules,
            mirror_enabled=d.get("mirror_enabled", False),
            mirror_targets=d.get("mirror_targets", []),
            start_minimized=d.get("start_minimized", False),
            theme=d.get("theme", "auto"),
        )
        return cfg


def load_config() -> AppConfig:
    path = get_config_path()
    if not os.path.exists(path):
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return AppConfig.from_dict(data)
    except (json.JSONDecodeError, OSError):
        # Config corrupta: se regenera para no romper el arranque de la app
        cfg = AppConfig()
        save_config(cfg)
        return cfg


def save_config(cfg: AppConfig) -> None:
    path = get_config_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)  # escritura atómica
