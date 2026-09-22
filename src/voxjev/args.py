"""Extraction déterministe des arguments (regex + fuzzy matching). Aucun appel réseau.

Les valeurs produites ici ne sont JAMAIS exécutées telles quelles : un nom d'app doit
correspondre à une app installée, un texte libre n'est utilisé qu'encodé dans une URL ou
passé en argv, et un enum / mode renvoie une valeur tirée de la config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .config import ArgSpec, Command, Config

APP_CUTOFF = 85
MAX_TEXT_LEN = 200
_EDGE_PUNCT = " \t\n.,;:!?…\"'«»“”"


def normalize(transcript: str, strip_phrases: tuple[str, ...] = ()) -> str:
    text = transcript.replace("’", "'").strip()
    for phrase in strip_phrases:
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip(_EDGE_PUNCT)


def _word_match(key: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text) is not None


def best_alias_match(text: str, candidates: dict[str, str], cutoff: int = APP_CUTOFF) -> str | None:
    """Renvoie la valeur canonique dont une clé parlée apparaît (de façon floue) dans `text`."""
    text = text.lower()
    best: tuple[float, float, int, str] | None = None
    for key, canonical in candidates.items():
        k = key.lower()
        if len(k) <= 4:
            score = 100.0 if _word_match(k, text) else 0.0
        else:
            score = fuzz.partial_ratio(k, text)
        if score < cutoff:
            continue
        rank = (score, fuzz.ratio(k, text), len(k), canonical)
        if best is None or rank > best:
            best = rank
    return best[3] if best else None


def app_candidates(installed: tuple[str, ...], aliases: dict[str, str],
                   choices: tuple[str, ...] = ()) -> dict[str, str]:
    known = set(installed)
    cands = {name: name for name in installed}
    for alias, target in aliases.items():
        if target in known:
            cands[alias] = target
    if choices:
        cands = {k: v for k, v in cands.items() if v in choices}
    return cands


@dataclass
class ArgResult:
    values: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def _capture(spec: ArgSpec, text: str) -> str | None:
    for pattern in spec.patterns:
        m = pattern.search(text)
        if m and m.group(spec.name):
            return m.group(spec.name).strip(_EDGE_PUNCT)
    return None


def extract_args(command: Command, transcript: str, config: Config,
                 installed: tuple[str, ...]) -> ArgResult:
    s = config.settings
    text = normalize(transcript, s.strip_phrases)
    result = ArgResult()
    for name, spec in command.args.items():
        span = _capture(spec, text)
        value: str | None = None
        if spec.type == "text":
            if span:
                for pattern, replacement in spec.rewrite:
                    span = pattern.sub(replacement, span)
                value = span[:MAX_TEXT_LEN]
        elif spec.type == "app":
            cands = app_candidates(installed, s.app_aliases, spec.choices)
            value = best_alias_match(span, cands) if span else None
            if value is None and not spec.optional:
                value = best_alias_match(text, cands)  # repli : app citée ailleurs dans la phrase
        elif spec.type == "enum":
            cands = {alias: key for key, v in spec.values.items() for alias in v.get("aliases", [key])}
            key = best_alias_match(span or text, cands) or spec.default or None
            value = spec.values[key]["value"] if key else None
        elif spec.type == "mode":
            cands = {alias: m.name for m in config.modes.values() for alias in (*m.aliases, m.name)}
            value = best_alias_match(span or text, cands, cutoff=80)
        if value:
            result.values[name] = value
        elif not spec.optional:
            result.missing.append(name)
    return result
