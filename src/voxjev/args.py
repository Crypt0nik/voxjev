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
    display: dict[str, str] = field(default_factory=dict)  # version lisible (« demain à 18 h 00 »)
    data: dict[str, tuple] = field(default_factory=dict)  # ex. chemin d'un élément de menu
    destructive: bool = False  # un candidat choisi porte un nom destructeur
    sources: dict[str, str] = field(default_factory=dict)  # nom -> "regex" | "jev" (segment choisi par Jev)

    @property
    def shown(self) -> dict[str, str]:
        return {**self.values, **self.display}

    @property
    def ok(self) -> bool:
        return not self.missing


def _capture(spec: ArgSpec, text: str) -> str | None:
    for pattern in spec.patterns:
        m = pattern.search(text)
        if m and m.group(spec.name):
            return m.group(spec.name).strip(_EDGE_PUNCT)
    return None


def _jev_pick(source: str, picks: dict, cands: dict, min_p: float):
    idx, p = picks.get(source, (None, 0.0))
    pool = cands.get(source) or []
    if idx is None or p < min_p or not 0 <= idx < len(pool):
        return None
    return pool[idx]


def extract_args(command: Command, transcript: str, config: Config, installed: tuple[str, ...],
                 picks: dict | None = None, cands: dict | None = None) -> ArgResult:
    """``picks``/``cands`` : choix de Jev parmi des candidats réels (menus, Raccourcis, segments)."""
    from .when import format_duration, format_when, parse_duration, parse_when

    s = config.settings
    picks, cands = picks or {}, cands or {}
    text = normalize(transcript, s.strip_phrases)
    result = ArgResult()
    for name, spec in command.args.items():
        source_text = text
        if spec.type == "text" and spec.strip_when:
            w = parse_when(text)
            source_text = w.rest if w else text
        span = _capture(spec, source_text)
        value: str | None = None
        if spec.type == "text":
            if not span and spec.span and spec.patterns:
                chosen = _jev_pick("span", picks, cands, s.pick_min_p)
                if chosen is not None:
                    span = chosen.value
                    if spec.strip_when and (w := parse_when(span)):
                        span = w.rest
                    result.sources[name] = "jev"
            if span:
                for pattern, replacement in spec.rewrite:
                    span = pattern.sub(replacement, span)
                value = span.strip(_EDGE_PUNCT)[:MAX_TEXT_LEN]
                result.sources.setdefault(name, "regex")
        elif spec.type == "pick":
            chosen = _jev_pick(spec.source, picks, cands, s.pick_min_p)
            if chosen is not None:
                value = chosen.value
                result.data[name] = chosen.data
                result.destructive = result.destructive or chosen.destructive
        elif spec.type == "duration":
            seconds = parse_duration(text)
            if seconds:
                value = str(seconds)
                result.display[name] = format_duration(seconds)
        elif spec.type == "when":
            w = parse_when(text)
            if w:
                value = w.at.strftime("%Y-%m-%dT%H:%M")
                result.display[name] = format_when(w.at)
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
