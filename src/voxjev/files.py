"""« ouvre le fichier rapport de stage » : Spotlight trouve des candidats, Jev choisit le bon.

La requête Spotlight est construite par le code à partir de mots alphanumériques (aucun
caractère spécial ne passe) et lancée via argv (``mdfind``), jamais via un shell.
Ce qui part vers l'API TypeSafe : la phrase, puis pour chaque candidat son nom, le nom de son
dossier parent et sa date de modification (pas le chemin complet ni le contenu).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

MAX_CANDIDATES = 40
_STOP = {
    "le", "la", "les", "l", "un", "une", "des", "de", "du", "d", "mon", "ma", "mes", "ton", "ta", "fichier",
    "document", "doc", "dossier", "ouvre", "ouvrir", "montre", "affiche", "trouve", "cherche", "retrouve",
    "moi", "stp", "dans", "sur", "que", "qui", "j", "ai", "sais", "plus", "où", "est", "avec", "pour",
}
_EXCLUDE = re.compile(r"/(?:Library|\.Trash|node_modules|\.git|\.venv|venv|__pycache__|\.cache)/|\.app/")

QUESTION = (
    "Which file does the user most likely mean in `transcript`? Each option is a file name, its folder and its "
    "last modification date. Prefer names that match the spoken words; pick `none` if no file fits."
)


@dataclass(frozen=True)
class FileHit:
    path: str
    name: str
    parent: str
    mtime: float

    @property
    def label(self) -> str:
        return f"{self.name} — dossier {self.parent} — modifié le {time.strftime('%d/%m/%Y', time.localtime(self.mtime))}"


def keywords(query: str) -> list[str]:
    words = re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+", query.lower())
    return [w for w in words if w not in _STOP and len(w) > 1][:6]


def _mdfind(query: str, home: str) -> list[str]:
    try:
        out = subprocess.run(["mdfind", "-onlyin", home, query], capture_output=True, text=True, timeout=6).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in out.splitlines() if p and not _EXCLUDE.search(p + "/")][:400]


def search(query: str, home: str | None = None) -> list[FileHit]:
    words = keywords(query)
    if not words:
        return []
    home = home or str(Path.home())
    clause = lambda w: f'kMDItemFSName == "*{w}*"cd'  # w : lettres et chiffres uniquement
    paths = _mdfind(" && ".join(clause(w) for w in words), home)
    if len(paths) < 3 and len(words) > 1:  # pas assez de résultats : n'importe quel mot
        paths += [p for p in _mdfind(" || ".join(clause(w) for w in words), home) if p not in paths]
    hits = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        hits.append(FileHit(p, os.path.basename(p), os.path.basename(os.path.dirname(p)), st.st_mtime))
    # les mots trouvés d'abord, puis les plus récents
    score = lambda h: (sum(w in h.name.lower() for w in words), h.mtime)
    return sorted(hits, key=score, reverse=True)[:MAX_CANDIDATES]


def resolve(client, transcript: str, query: str, min_p: float = 0.5) -> tuple[FileHit | None, float, list[FileHit]]:
    hits = search(query)
    if not hits:
        return None, 0.0, []
    if len(hits) == 1:
        return hits[0], 1.0, hits
    idx, p, _ = client.choose({"transcript": transcript}, QUESTION, [h.label for h in hits], none="None of these files")
    if idx is None or p < min_p:
        return None, p, hits
    return hits[idx], p, hits
