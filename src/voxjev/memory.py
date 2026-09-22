"""Mémoire personnelle locale : « retiens que mon dentiste c'est le Dr Martin ».

Les souvenirs sont stockés dans ~/Library/Application Support/voxjev/memory.json (chmod 600).
Pour répondre à « c'est qui mon dentiste ? », Jev choisit le souvenir pertinent parmi ceux
enregistrés : ils sont alors envoyés à l'API TypeSafe (voir README). N'y mettez pas de secrets.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MEMORY_FILE = Path(os.environ.get("VOXJEV_MEMORY", "~/Library/Application Support/voxjev/memory.json")).expanduser()
MAX_FACTS = 240

QUESTION = (
    "Which remembered fact answers or matches the user's request in `question`? Facts are things the user "
    "asked the assistant to remember. Pick `none` if no fact is relevant."
)


@dataclass
class Fact:
    text: str
    at: float


class Memory:
    def __init__(self, path: Path | None = None):
        self.path = path or MEMORY_FILE

    def load(self) -> list[Fact]:
        try:
            return [Fact(**f) for f in json.loads(self.path.read_text())]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self, facts: list[Fact]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([f.__dict__ for f in facts], ensure_ascii=False, indent=1))
        os.chmod(self.path, 0o600)

    def add(self, text: str) -> int:
        facts = [f for f in self.load() if f.text.lower() != text.lower()]
        facts.append(Fact(text.strip(), time.time()))
        self._save(facts[-MAX_FACTS:])
        return len(facts)

    def remove(self, text: str) -> bool:
        facts = self.load()
        kept = [f for f in facts if f.text != text]
        self._save(kept)
        return len(kept) != len(facts)

    def find(self, client, question: str, min_p: float = 0.5) -> tuple[str | None, float]:
        facts = self.load()
        if not facts:
            return None, 0.0
        recent = facts[-MAX_FACTS:][::-1]
        idx, p, _ = client.choose({"question": question}, QUESTION, [f.text for f in recent], none="No fact is relevant")
        if idx is None or p < min_p:
            return None, p
        return recent[idx].text, p
