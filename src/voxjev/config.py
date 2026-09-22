"""Chargement et validation de la configuration YAML (commandes, modes, réglages).

La config est la seule source de ce qui peut être exécuté : la validation refuse tout type
d'action hors liste blanche et tout placeholder qui ne correspond pas à un argument déclaré.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ACTION_TYPES = {
    "open_app",
    "quit_app",
    "open_url",
    "applescript",
    "keystroke",
    "shortcut",
    "exec",
    "set_mode",
    "sequence",
    "spotify",
    "web_task",
    "undo",
}
SPOTIFY_OPS = {"play", "like", "search"}
ARG_TYPES = {"app", "text", "enum", "mode"}
# Champs d'action dans lesquels un placeholder `{arg}` est autorisé.
TEMPLATED_FIELDS = {
    "open_app": {"app"},
    "quit_app": {"app"},
    "open_url": {"url", "app"},
    "applescript": {"args"},
    "set_mode": {"mode"},
    "spotify": {"query"},
    "web_task": {"goal", "url"},
}
URL_SCHEMES = ("https://", "http://", "mailto:")
KEY_MODIFIERS = {"command", "shift", "option", "control"}

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "commands.yaml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type: str
    patterns: tuple[re.Pattern, ...] = ()
    optional: bool = False
    choices: tuple[str, ...] = ()  # pour type app : restreint les apps acceptées
    values: dict[str, dict] = field(default_factory=dict)  # pour type enum
    path_segment: bool = False  # pour type text : encodage d'un segment de chemin
    rewrite: tuple[tuple[re.Pattern, str], ...] = ()  # pour type text : substitutions regex
    default: str = ""  # pour type enum : clé utilisée si rien ne correspond


@dataclass(frozen=True)
class Command:
    id: str
    description: str
    examples: tuple[str, ...]
    destructive: bool
    action: dict
    args: dict[str, ArgSpec] = field(default_factory=dict)
    label: str = ""  # libellé court pour l'interface, ex. « Ouvrir {app} »
    always_confirm: bool = False  # confirmation systématique (ex. agent web sur votre profil Chrome)
    undo: dict | None = None  # action inverse, pour « annule ça » (mêmes arguments + {previous_mode})

    def short(self, values: dict[str, str] | None = None) -> str:
        """Libellé lisible, arguments inclus : « Ouvrir Spotify »."""
        if not self.label:
            return self.description
        values = values or {}
        return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), "…"), self.label)


@dataclass(frozen=True)
class Mode:
    name: str
    description: str
    aliases: tuple[str, ...]
    commands: tuple[str, ...]
    on_enter: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Settings:
    model: str = "jev-latest"
    threshold: float = 0.70
    confirm_floor: float = 0.40
    addressed_threshold: float = 0.70
    addressed_floor: float = 0.35
    destructive_threshold: float = 0.50
    hotkey: str = "alt_r"
    stt_model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "fr"
    stt_prompt: str = ""
    min_record_seconds: float = 0.3
    confirm_timeout_seconds: int = 10
    fallback_min_p: float = 0.30  # 2e option de Jev tentée si la 1re n'a pas d'argument valide
    compound_threshold: float = 0.60  # Noul « plusieurs actions » au-dessus duquel on découpe
    max_plan_steps: int = 6
    step_delay_seconds: float = 0.8  # pause après l'ouverture d'une app, avant l'étape suivante
    split_model: str = "inception/mercury-2.5"  # LLM (OpenRouter) qui découpe les demandes composées
    sounds: dict[str, str] = field(default_factory=dict)
    app_dirs: tuple[str, ...] = ()
    exec_allowlist: tuple[str, ...] = ()
    strip_phrases: tuple[str, ...] = ()
    app_aliases: dict[str, str] = field(default_factory=dict)
    none_option: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    settings: Settings
    commands: dict[str, Command]
    modes: dict[str, Mode]
    common: tuple[str, ...]
    path: Path | None = None

    def commands_for_mode(self, mode: str) -> list[Command]:
        """Commandes communes + commandes propres au mode, dans l'ordre de déclaration."""
        wanted = set(self.common) | set(self.modes[mode].commands)
        return [cmd for cid, cmd in self.commands.items() if cid in wanted]

    @property
    def default_mode(self) -> str:
        return next(iter(self.modes))


def _placeholders(template: str) -> set[str]:
    names = set()
    for _, name, spec, conv in string.Formatter().parse(template):
        if name is None:
            continue
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or spec or conv:
            raise ConfigError(f"placeholder invalide {{{name}}} dans {template!r}")
        names.add(name)
    return names


def _validate_action(action: dict, where: str, args: dict[str, ArgSpec], settings: Settings,
                     mode_names: set[str]) -> None:
    kind = action.get("type")
    if kind not in ACTION_TYPES:
        raise ConfigError(f"{where}: type d'action {kind!r} hors liste blanche")
    if kind == "sequence":
        steps = action.get("steps") or []
        if not steps:
            raise ConfigError(f"{where}: sequence vide")
        for i, step in enumerate(steps):
            if step.get("type") == "sequence":
                raise ConfigError(f"{where}: sequence imbriquée interdite")
            _validate_action(step, f"{where}.steps[{i}]", args, settings, mode_names)
        return

    allowed_fields = TEMPLATED_FIELDS.get(kind, set())
    for key, value in action.items():
        if key == "type":
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if not isinstance(v, str):
                continue
            used = _placeholders(v)
            if used and key not in allowed_fields:
                raise ConfigError(f"{where}: placeholder interdit dans le champ {key!r}")
            unknown = used - set(args)
            if unknown:
                raise ConfigError(f"{where}: placeholder(s) sans argument déclaré : {sorted(unknown)}")
            if key == "args" and used and v != f"{{{next(iter(used))}}}":
                raise ConfigError(f"{where}: un argument AppleScript doit être exactement '{{nom}}'")

    if kind in ("open_app", "quit_app") and not action.get("app"):
        raise ConfigError(f"{where}: champ 'app' requis")
    if kind == "open_url":
        url = action.get("url", "")
        # Un url entièrement issu d'un enum (valeur de config) est vérifié au rendu.
        if not url.startswith(URL_SCHEMES) and not (
            _placeholders(url) and all(args[n].type == "enum" for n in _placeholders(url))
            and url.strip() == f"{{{next(iter(_placeholders(url)))}}}"
        ):
            raise ConfigError(f"{where}: l'URL doit commencer par {URL_SCHEMES} : {url!r}")
        for name in _placeholders(url):
            if url.startswith(URL_SCHEMES) and args[name].type == "app":
                raise ConfigError(f"{where}: un argument app ne peut pas aller dans une URL")
    if kind == "applescript" and not isinstance(action.get("script"), str):
        raise ConfigError(f"{where}: champ 'script' requis")
    if kind == "applescript" and _placeholders(action["script"]):
        raise ConfigError(f"{where}: le script AppleScript doit être figé (args via argv)")
    if kind == "keystroke":
        key = action.get("key")
        if not isinstance(key, str) or len(key) != 1:
            raise ConfigError(f"{where}: 'key' doit être un seul caractère")
        bad = set(action.get("modifiers", [])) - KEY_MODIFIERS
        if bad:
            raise ConfigError(f"{where}: modificateurs inconnus {bad}")
    if kind == "shortcut" and (not isinstance(action.get("name"), str) or _placeholders(action["name"])):
        raise ConfigError(f"{where}: 'name' de raccourci figé requis")
    if kind == "exec":
        argv = action.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ConfigError(f"{where}: 'argv' doit être une liste de chaînes")
        if argv[0] not in settings.exec_allowlist:
            raise ConfigError(f"{where}: {argv[0]!r} absent de settings.exec_allowlist")
        if any(_placeholders(a) for a in argv):
            raise ConfigError(f"{where}: exec n'accepte aucun placeholder")
    if kind == "spotify":
        if action.get("op") not in SPOTIFY_OPS:
            raise ConfigError(f"{where}: op Spotify doit être l'une de {sorted(SPOTIFY_OPS)}")
        for name in _placeholders(action.get("query", "")):
            if args[name].type != "text":
                raise ConfigError(f"{where}: la requête Spotify n'accepte que des arguments texte")
    if kind == "web_task":
        if _placeholders(action.get("url", "")) and not all(
                args[n].type == "enum" for n in _placeholders(action.get("url", ""))):
            raise ConfigError(f"{where}: l'URL de départ doit venir d'un enum de la config")
        for name in _placeholders(action.get("goal", "")):
            if args[name].type != "text":
                raise ConfigError(f"{where}: l'objectif n'accepte que des arguments texte")
    if kind == "set_mode":
        mode = action.get("mode", "")
        if not _placeholders(mode) and mode not in mode_names:
            raise ConfigError(f"{where}: mode inconnu {mode!r}")


def _parse_args(raw: dict, where: str) -> dict[str, ArgSpec]:
    out = {}
    for name, spec in (raw or {}).items():
        kind = spec.get("type")
        if kind not in ARG_TYPES:
            raise ConfigError(f"{where}.args.{name}: type {kind!r} inconnu")
        patterns = []
        for p in spec.get("patterns", []):
            try:
                compiled = re.compile(p, re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(f"{where}.args.{name}: regex invalide {p!r}: {exc}") from exc
            if name not in compiled.groupindex:
                raise ConfigError(f"{where}.args.{name}: la regex doit contenir (?P<{name}>...)")
            patterns.append(compiled)
        rewrite = []
        for rule in spec.get("rewrite", []):
            if not (isinstance(rule, list) and len(rule) == 2):
                raise ConfigError(f"{where}.args.{name}: rewrite attend des paires [regex, remplacement]")
            rewrite.append((re.compile(rule[0], re.IGNORECASE), rule[1]))
        if kind == "enum" and not spec.get("values"):
            raise ConfigError(f"{where}.args.{name}: enum sans 'values'")
        out[name] = ArgSpec(
            name=name,
            type=kind,
            patterns=tuple(patterns),
            optional=bool(spec.get("optional", False)),
            choices=tuple(spec.get("choices", [])),
            values=dict(spec.get("values", {})),
            path_segment=bool(spec.get("path_segment", False)),
            rewrite=tuple(rewrite),
            default=str(spec.get("default", "")),
        )
    return out


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config introuvable : {path}") from exc

    s = raw.get("settings", {}) or {}
    settings = Settings(
        **{
            k: (tuple(v) if isinstance(v, list) else v)
            for k, v in s.items()
            if k in Settings.__dataclass_fields__
        }
    )
    if not 0 <= settings.confirm_floor <= settings.threshold <= 1:
        raise ConfigError("il faut 0 <= confirm_floor <= threshold <= 1")
    if not 0 <= settings.addressed_floor <= settings.addressed_threshold <= 1:
        raise ConfigError("il faut 0 <= addressed_floor <= addressed_threshold <= 1")

    mode_names = set((raw.get("modes") or {}).keys())
    if not mode_names:
        raise ConfigError("au moins un mode est requis")

    commands: dict[str, Command] = {}
    for i, c in enumerate(raw.get("commands") or []):
        cid = c.get("id")
        where = f"commands[{i}] ({cid})"
        if not cid or not re.fullmatch(r"[a-z][a-z0-9_]*", cid):
            raise ConfigError(f"{where}: id invalide")
        if cid == "none":
            raise ConfigError(f"{where}: 'none' est réservé")
        if cid in commands:
            raise ConfigError(f"{where}: id dupliqué")
        if not c.get("description") or len(c.get("examples", [])) < 1:
            raise ConfigError(f"{where}: description et au moins un exemple requis")
        if not isinstance(c.get("destructive"), bool):
            raise ConfigError(f"{where}: 'destructive' doit être true/false")
        args = _parse_args(c.get("args"), where)
        _validate_action(c.get("action") or {}, where, args, settings, mode_names)
        if c.get("undo"):
            if c["undo"].get("type") in ("undo", "web_task", "sequence"):
                raise ConfigError(f"{where}.undo : type d'action non autorisé pour une annulation")
            undo_args = dict(args) | {"previous_mode": ArgSpec("previous_mode", "mode")}
            _validate_action(c["undo"], f"{where}.undo", undo_args, settings, mode_names)
        commands[cid] = Command(
            id=cid,
            description=c["description"],
            examples=tuple(c["examples"]),
            destructive=c["destructive"],
            action=c["action"],
            args=args,
            label=str(c.get("label", "")),
            always_confirm=bool(c.get("always_confirm", False)),
            undo=c.get("undo"),
        )

    common = tuple(raw.get("common") or [])
    modes = {}
    for name, m in raw["modes"].items():
        m = m or {}
        cmds = tuple(m.get("commands") or [])
        unknown = (set(cmds) | set(common)) - set(commands)
        if unknown:
            raise ConfigError(f"mode {name}: commandes inconnues {sorted(unknown)}")
        on_enter = tuple(m.get("on_enter") or [])
        for i, step in enumerate(on_enter):
            _validate_action(step, f"modes.{name}.on_enter[{i}]", {}, settings, mode_names)
        modes[name] = Mode(
            name=name,
            description=m.get("description", name),
            aliases=tuple(m.get("aliases") or [name]),
            commands=cmds,
            on_enter=on_enter,
        )
    return Config(settings=settings, commands=commands, modes=modes, common=common, path=path)
