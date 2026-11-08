"""
config.py
---------
Manejo de configuración persistente de la aplicación (reglas de prioridad,
dispositivos de mirroring, preferencias de UI). Se guarda como JSON en
%APPDATA%\\AudioManagerPro\\config.json
"""
from __future__ import annotations

import json
import logging
import os
import dataclasses
from dataclasses import dataclass, field
from typing import List, Dict, Any

APP_NAME = "AudioManagerPro"

VALID_ROLES = ("both", "console", "communications")

logger = logging.getLogger("audio_manager.config")


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
    `priority` más bajo = mayor prioridad (se evalúa primero).

    Formato de `match_text`:
      * "Auriculares Sony"     -> el nombre debe contener TODAS las palabras
                                  (en cualquier orden).
      * "Altavoces ! Steam"    -> contiene "altavoces" y NO contiene "steam".
      * "! Virtual ! Steam"    -> cualquier dispositivo que no contenga
                                  "virtual" ni "steam".
    """
    match_text: str
    role: str = "both"          # "console" | "communications" | "both"
    enabled: bool = True
    priority: int = 100

    def matches(self, device_name: str) -> bool:
        text = self.match_text.lower().strip()
        name = device_name.lower()

        if not text:
            return False

        # Si empieza con '!', todo lo que sigue son condiciones de negación
        if text.startswith("!"):
            negations = [p.strip() for p in text.split("!") if p.strip()]
            return not any(neg in name for neg in negations)

        # La primera parte es la condición positiva y las siguientes,
        # las negaciones.
        parts = [p.strip() for p in text.split("!") if p.strip()]
        pos_term = parts[0]
        neg_terms = parts[1:]

        # Todas las palabras de la parte positiva deben estar en el nombre
        if not all(word in name for word in pos_term.split()):
            return False

        return not any(neg in name for neg in neg_terms)


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
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AppConfig":
        """Construye la config de forma tolerante: ignora claves
        desconocidas y descarta reglas inválidas en vez de romper el
        arranque. Lanza ValueError si la raíz ni siquiera es un objeto."""
        if not isinstance(d, dict):
            raise ValueError("La raíz de config.json no es un objeto JSON")

        rules: List[PriorityRule] = []
        for raw in d.get("rules") or []:
            rule = _rule_from_dict(raw)
            if rule is not None:
                rules.append(rule)

        targets = d.get("mirror_targets") or []
        theme = d.get("theme", "auto")
        return AppConfig(
            auto_rules_enabled=bool(d.get("auto_rules_enabled", True)),
            rules=rules,
            mirror_enabled=bool(d.get("mirror_enabled", False)),
            mirror_targets=[str(t) for t in targets] if isinstance(targets, list) else [],
            start_minimized=bool(d.get("start_minimized", False)),
            theme=theme if theme in ("auto", "light", "dark") else "auto",
        )


def _rule_from_dict(raw: Any) -> "PriorityRule | None":
    if not isinstance(raw, dict):
        return None
    text = raw.get("match_text")
    if not isinstance(text, str) or not text.strip():
        return None
    role = raw.get("role", "both")
    try:
        priority = int(raw.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    return PriorityRule(
        match_text=text,
        role=role if role in VALID_ROLES else "both",
        enabled=bool(raw.get("enabled", True)),
        priority=priority,
    )


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
    except (ValueError, OSError) as exc:  # JSONDecodeError/UnicodeDecodeError heredan de ValueError
        # Config corrupta: se guarda una copia para no perder las reglas del
        # usuario y se regenera una por defecto para no romper el arranque.
        logger.error("config.json inválido (%s); se regenera una configuración por defecto", exc)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            logger.exception("No se pudo respaldar la config corrupta")
        cfg = AppConfig()
        save_config(cfg)
        return cfg


def save_config(cfg: AppConfig) -> bool:
    """Guarda la config de forma atómica. Devuelve False (y lo deja en el
    log) si no se pudo escribir, en vez de propagar la excepción a la UI."""
    path = get_config_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)  # escritura atómica
        return True
    except OSError:
        logger.exception("No se pudo guardar la configuración en %s", path)
        return False
