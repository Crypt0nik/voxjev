"""Candidats réels parmi lesquels Jev choisit, dans le MÊME appel que la commande.

« Sélectionner plutôt que générer » (idée de kevinbadi/jev-voice, MIT) : le code produit des
candidats qui existent vraiment, Jev choisit un indice, le code utilise le candidat.

- ``menu``     : éléments de menu de l'app au premier plan (axmenu) ;
- ``shortcut`` : vos Raccourcis macOS (``shortcuts list``) ;
- ``span``     : segments de la phrase elle-même (requête, texte à taper, titre d'un rappel…),
                 utilisés quand les regex de la config n'extraient rien.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

MAX_SPANS = 200
SHORTCUTS_TTL = 120.0
MENU_TTL = 8.0


@dataclass(frozen=True)
class Candidate:
    label: str  # ce que Jev voit (et ce qui s'affiche)
    value: str  # ce que le code utilise
    data: tuple = ()  # ex. chemin de menu
    destructive: bool = False


# ------------------------------------------------------------------ segments de phrase
_EDGE_WORDS = {
    "de", "d'", "du", "des", "la", "le", "les", "l'", "un", "une", "et", "ou", "puis", "sur", "dans", "avec",
    "pour", "à", "a", "au", "aux", "en", "que", "qu'", "qui", "me", "moi", "te", "se", "s'", "ce", "cette",
    "mon", "ma", "mes", "son", "sa", "ses", "par", "stp", "euh",
}
MAX_SPAN_WORDS = 14


def _tokens(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[\w'’\-]+|[«»\"“”]", text)]


def spans(text: str, limit: int = MAX_SPANS) -> list[str]:
    """Segments contigus de la phrase (sans mot-outil aux bords), texte entre guillemets en premier."""
    text = re.sub(r"\s+", " ", text.replace("’", "'")).strip(" .,;:!?")
    out: list[str] = [q for q in re.findall(r"[«\"“]\s*([^»\"”]+?)\s*[»\"”]", text)]
    toks = [t for t in _tokens(text) if t[2] not in "«»\"“”"]
    edge = lambda w: w.lower() in _EDGE_WORDS or w.lower().endswith("'")
    for n in range(len(toks), 0, -1):
        if n > MAX_SPAN_WORDS:
            continue
        for i in range(len(toks) - n + 1):
            first, last = toks[i], toks[i + n - 1]
            if edge(first[2]) or edge(last[2]):
                continue
            seg = text[first[0]:last[1]]
            if seg not in out:
                out.append(seg)
    return out[:limit]


# ------------------------------------------------------------------ Raccourcis
_shortcuts_cache: tuple[float, list[str]] = (0.0, [])


def list_shortcuts() -> list[str]:
    global _shortcuts_cache
    at, names = _shortcuts_cache
    if time.monotonic() - at < SHORTCUTS_TTL and names:
        return names
    try:
        out = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=5).stdout
        names = [n.strip() for n in out.splitlines() if n.strip()][:240]
    except (OSError, subprocess.SubprocessError):
        names = []
    _shortcuts_cache = (time.monotonic(), names)
    return names


# ------------------------------------------------------------------ fournisseur
@dataclass
class Provider:
    """Rassemble les candidats ; ``prefetch()`` (à l'appui sur la touche) les prépare pendant qu'on parle."""

    menu_reader: object = None  # callable(pid|None) -> list[MenuItem] ; None = axmenu.read_menus
    shortcut_lister: object = None
    _menu: tuple[float, int | None, list[Candidate]] = field(default=(0.0, None, []), repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def menus(self, fresh: bool = False) -> list[Candidate]:
        pid = None
        reader = self.menu_reader
        if reader is None:
            from .axmenu import _front_pid, read_menus as reader

            try:
                pid = _front_pid()
            except Exception:
                pid = None
        with self._lock:
            at, cached_pid, cached = self._menu
            if not fresh and cached_pid == pid and time.monotonic() - at < MENU_TTL:
                return cached
        items = reader(pid)
        cands = [Candidate(i.label + (f" ({i.shortcut})" if i.shortcut else ""), i.label, tuple(i.path),
                           i.destructive) for i in items]
        with self._lock:
            self._menu = (time.monotonic(), pid, cands)
        return cands

    def shortcuts(self) -> list[Candidate]:
        names = (self.shortcut_lister or list_shortcuts)()
        from .axmenu import DESTRUCTIVE

        return [Candidate(n, n, (n,), bool(DESTRUCTIVE.search(n))) for n in names]

    def gather(self, sources: set[str], transcript: str) -> dict[str, list[Candidate]]:
        out: dict[str, list[Candidate]] = {}
        if "menu" in sources:
            out["menu"] = self.menus()
        if "shortcut" in sources:
            out["shortcut"] = self.shortcuts()
        if "span" in sources:
            out["span"] = [Candidate(s, s) for s in spans(transcript)]
        return {k: v for k, v in out.items() if v}

    def prefetch(self, sources: set[str]) -> None:
        def work():
            try:
                if "menu" in sources:
                    self.menus(fresh=True)
                if "shortcut" in sources:
                    self.shortcuts()
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="voxjev-prefetch").start()
