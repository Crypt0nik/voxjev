"""Contexte local : app au premier plan, apps installées, mode actif et dernière commande."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

STATE_FILE = Path(os.environ.get("VOXJEV_STATE", "~/Library/Application Support/voxjev/state.json")).expanduser()

# Caractères invisibles (ex. U+200E devant « WhatsApp ») à retirer des noms d'apps.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")


def clean_app_name(name: str) -> str:
    return _INVISIBLE.sub("", name).strip()


@lru_cache(maxsize=4)
def installed_apps(app_dirs: tuple[str, ...]) -> tuple[str, ...]:
    """Noms des .app trouvés dans les dossiers (un niveau, sans parcourir les bundles)."""
    names: set[str] = {"Finder"}
    for d in app_dirs:
        path = Path(d).expanduser()
        if not path.is_dir():
            continue
        for entry in path.iterdir():
            if entry.suffix == ".app":
                names.add(clean_app_name(entry.stem))
    return tuple(sorted(names, key=str.lower))


def frontmost_app() -> str | None:
    """Nom de l'app au premier plan (NSWorkspace, repli sur lsappinfo)."""
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return clean_app_name(str(app.localizedName())) if app else None
    except Exception:
        pass
    try:
        out = subprocess.run(["lsappinfo", "info", "-only", "name", "front"],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r'"LSDisplayName"="([^"]+)"', out)
        return clean_app_name(m.group(1)) if m else None
    except Exception:
        return None


@dataclass
class Session:
    """État persistant entre deux énoncés (et entre deux lancements en mode --text)."""

    mode: str
    last_command: str | None = None
    last_args: dict = field(default_factory=dict)
    previous_mode: str | None = None
    path: Path | None = field(default=STATE_FILE, repr=False)

    @classmethod
    def load(cls, default_mode: str, known_modes: set[str], path: Path | None = STATE_FILE) -> Session:
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                mode = data.get("mode") if data.get("mode") in known_modes else default_mode
                return cls(mode=mode, last_command=data.get("last_command"),
                           last_args=data.get("last_args") or {}, previous_mode=data.get("previous_mode"), path=path)
            except (OSError, ValueError):
                pass
        return cls(mode=default_mode, path=path)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"mode": self.mode, "last_command": self.last_command,
                                         "last_args": self.last_args, "previous_mode": self.previous_mode}))


JOURNAL = Path(os.environ.get("VOXJEV_JOURNAL", "~/Library/Logs/voxjev/history.jsonl")).expanduser()


def journal(entry: dict) -> None:
    """Journal local des actions exécutées (jamais envoyé nulle part)."""
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
