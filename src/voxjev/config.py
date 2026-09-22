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
    # quotidien
    "menu",           # élément de menu de l'app au premier plan (choisi par Jev parmi les menus lus)
    "type_text",      # saisie de texte au clavier (jamais dans un terminal)
    "keycombo",       # raccourci clavier (grammaire stricte : cmd+shift+t, code:121…)
    "timer",          # minuteur
    "reminder",       # rappel (app Rappels)
    "calendar_add",   # événement (app Calendrier)
    "calendar_read",  # lecture de l'agenda d'un jour
    "note_add",       # note dictée (app Notes)
    "mail_draft",     # brouillon d'e-mail visible, jamais envoyé
    "info",           # heure, date, batterie, minuteurs
    "routine",        # enchaînement de commandes aux arguments figés
    "file_open",      # fichier trouvé par Spotlight, choisi par Jev
    "memory_add",     # mémoire personnelle locale
    "memory_ask",
    "memory_forget",
    "mail_triage",    # quels mails non lus demandent une action ?
    "ask",            # question générale -> réponse courte d'un LLM (OpenRouter)
    "desktop_task",   # agent bureau : plusieurs clics dans l'app au premier plan
    "page_link",      # ouvrir un lien de la page Chrome active (« le 2ᵉ lien », « le meilleur résultat »)
    "layout",         # ranger les fenêtres : grille, moitié d'écran, annuler le rangement
    "layout_pair",    # deux apps côte à côte
}
LAYOUT_ARRANGEMENTS = {"tile", "restore", "left", "right", "top", "bottom", "full", "center",
                       "top_left", "top_right", "bottom_left", "bottom_right"}
SPOTIFY_OPS = {"play", "like", "search"}
ARG_TYPES = {"app", "text", "enum", "mode", "pick", "duration", "when"}
PICK_SOURCES = {"menu", "shortcut"}
INFO_TOPICS = {"time", "date", "battery", "timers"}
# Champ d'action -> types d'argument acceptés. Le champ doit être exactement « {nom} ».
TYPED_FIELDS = {
    ("menu", "item"): {"pick"},
    ("shortcut", "name"): {"pick"},
    ("type_text", "text"): {"text"},
    ("keycombo", "combo"): {"enum"},
    ("timer", "seconds"): {"duration"},
    ("timer", "label"): {"text"},
    ("reminder", "title"): {"text"},
    ("reminder", "when"): {"when"},
    ("calendar_add", "title"): {"text"},
    ("calendar_add", "when"): {"when"},
    ("calendar_read", "when"): {"when"},
    ("note_add", "body"): {"text"},
    ("mail_draft", "to"): {"text"},
    ("mail_draft", "body"): {"text"},
    ("info", "what"): {"enum"},
    ("file_open", "query"): {"text"},
    ("memory_add", "fact"): {"text"},
    ("memory_ask", "question"): {"text"},
    ("memory_forget", "question"): {"text"},
    ("ask", "question"): {"text"},
    ("desktop_task", "goal"): {"text"},
    ("layout", "arrangement"): {"enum"},
    ("layout_pair", "left"): {"app"},
    ("layout_pair", "right"): {"app"},
}
REQUIRED_FIELDS = {
    "menu": ("item",), "type_text": ("text",), "keycombo": ("combo",), "timer": ("seconds",),
    "reminder": ("title",), "calendar_add": ("title", "when"), "note_add": ("body",),
    "info": ("what",), "file_open": ("query",), "memory_add": ("fact",), "memory_ask": ("question",),
    "memory_forget": ("question",), "ask": ("question",), "desktop_task": ("goal",),
    "layout": ("arrangement",), "layout_pair": ("left", "right"),
}
COMBO_RE = re.compile(r"(?:(?:cmd|shift|alt|ctrl)\+)*(?:[a-z0-9,.;/'\[\]=`-]|code:\d{1,3})")
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
# Réglages personnels (fenêtre Réglages) : fusionnés par-dessus commands.yaml, qui reste intact.
import os as _os

USER_CONFIG = Path(_os.environ.get("VOXJEV_USER_CONFIG",
                                   "~/Library/Application Support/voxjev/settings.yaml")).expanduser()
USER_SETTING_KEYS = {  # réglages modifiables depuis la fenêtre
    "hotkey", "threshold", "confirm_floor", "addressed_threshold", "addressed_floor", "destructive_threshold",
    "speak_answers", "voice", "quiet_mode", "safe_commands", "speculate", "hands_free", "wake_words",
    "followup_seconds", "sounds_enabled", "app_aliases", "split_model", "auto_layout",
}


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
    source: str = ""  # pour type pick : d'où viennent les candidats (menu | shortcut)
    strip_when: bool = False  # pour type text : retirer l'expression de date (« demain à 18h »)
    span: bool = True  # pour type text : si les regex échouent, segment de la phrase choisi par Jev


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
    pick_min_p: float = 0.50  # choix de Jev parmi des candidats (menu, raccourci, segment) : p minimale
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
    speak_answers: bool = True
    sounds_enabled: bool = True
    auto_layout: bool = True  # nouvelles pages web : nouvelle fenêtre rangée à côté de la fenêtre courante
    # Mode sans confirmation : les commandes sans risque s'exécutent directement, même si Jev
    # hésite un peu (au-dessus des planchers). Les actions destructives restent toujours confirmées.
    quiet_mode: bool = True
    safe_commands: tuple[str, ...] = ()
    speculate: bool = True  # transcription + appel Jev anticipés pendant l'appui
    hands_free: bool = False  # micro ouvert en continu, déclenché par le mot d'éveil
    wake_words: tuple[str, ...] = ("jarvis",)
    followup_seconds: float = 8.0  # après une commande : on peut enchaîner sans le mot d'éveil
    voice: str = ""
    terminal_apps: tuple[str, ...] = ()


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

    allowed_fields = TEMPLATED_FIELDS.get(kind, set()) | {f for (k, f) in TYPED_FIELDS if k == kind}
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

    for field_name in REQUIRED_FIELDS.get(kind, ()):
        if action.get(field_name) in (None, ""):
            raise ConfigError(f"{where}: champ {field_name!r} requis pour {kind}")
    for (k, field_name), accepted in TYPED_FIELDS.items():
        value = action.get(field_name)
        if k != kind or not isinstance(value, str):
            continue
        used = _placeholders(value)
        if not used:
            if kind == "keycombo" and not COMBO_RE.fullmatch(value):
                raise ConfigError(f"{where}: raccourci clavier invalide {value!r}")
            if kind == "info" and value not in INFO_TOPICS:
                raise ConfigError(f"{where}: info doit être l'un de {sorted(INFO_TOPICS)}")
            if kind == "layout" and value not in LAYOUT_ARRANGEMENTS:
                raise ConfigError(f"{where}: rangement inconnu {value!r}")
            if kind in ("menu", "timer", "reminder", "calendar_add", "file_open", "desktop_task", "ask"):
                raise ConfigError(f"{where}: {field_name!r} doit venir d'un argument")
            continue
        name = next(iter(used))
        if value != f"{{{name}}}" or args[name].type not in accepted:
            raise ConfigError(f"{where}: {field_name!r} doit être exactement {{arg}} de type {sorted(accepted)}")
        spec = args[name]
        if spec.type == "pick" and spec.source != {"menu": "menu", "shortcut": "shortcut"}[kind]:
            raise ConfigError(f"{where}: l'argument {name!r} doit avoir source: {kind}")
        if kind == "keycombo":
            bad = [v.get("value") for v in spec.values.values() if not COMBO_RE.fullmatch(str(v.get("value", "")))]
            if bad:
                raise ConfigError(f"{where}: raccourcis invalides dans l'enum {name!r} : {bad}")
        if kind == "info":
            bad = [v.get("value") for v in spec.values.values() if v.get("value") not in INFO_TOPICS]
            if bad:
                raise ConfigError(f"{where}: sujets inconnus dans l'enum {name!r} : {bad}")
        if kind == "layout":
            bad = [v.get("value") for v in spec.values.values() if v.get("value") not in LAYOUT_ARRANGEMENTS]
            if bad:
                raise ConfigError(f"{where}: rangements inconnus dans l'enum {name!r} : {bad}")
    if kind == "type_text" and not isinstance(action.get("submit", False), bool):
        raise ConfigError(f"{where}: 'submit' doit être true/false")
    if kind == "routine":
        steps = action.get("steps") or []
        if not steps or len(steps) > 12:
            raise ConfigError(f"{where}: une routine a 1 à 12 étapes")
        for i, st in enumerate(steps):
            if not isinstance(st, dict) or not (set(st) <= {"run", "with"} and "run" in st or set(st) == {"wait"}):
                raise ConfigError(f"{where}.steps[{i}]: attendu {{run: id, with: {{…}}}} ou {{wait: secondes}}")
            if "wait" in st and not (isinstance(st["wait"], (int, float)) and 0 < st["wait"] <= 30):
                raise ConfigError(f"{where}.steps[{i}]: wait entre 0 et 30 s")
        return  # les commandes référencées sont vérifiées une fois toutes chargées
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
    if kind == "shortcut" and not isinstance(action.get("name"), str):
        raise ConfigError(f"{where}: 'name' de raccourci requis")
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
        if kind == "pick" and spec.get("source") not in PICK_SOURCES:
            raise ConfigError(f"{where}.args.{name}: pick exige source parmi {sorted(PICK_SOURCES)}")
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
            source=str(spec.get("source", "")),
            strip_when=bool(spec.get("strip_when", False)),
            span=bool(spec.get("span", True)),
        )
    return out


def _check_routines(commands: dict[str, Command]) -> None:
    """Étapes de routine : commandes existantes, arguments déclarés ; destructive si une étape l'est."""
    for cid, cmd in list(commands.items()):
        if cmd.action.get("type") != "routine":
            continue
        destructive = cmd.destructive
        for i, st in enumerate(cmd.action["steps"]):
            if "wait" in st:
                continue
            where = f"routine {cid}.steps[{i}]"
            target = commands.get(st["run"])
            if target is None:
                raise ConfigError(f"{where}: commande inconnue {st['run']!r}")
            if target.action.get("type") in ("routine", "undo"):
                raise ConfigError(f"{where}: {target.action['type']} interdit dans une routine")
            given = st.get("with") or {}
            unknown = set(given) - set(target.args)
            if unknown:
                raise ConfigError(f"{where}: arguments inconnus {sorted(unknown)}")
            missing = [n for n, a in target.args.items() if not a.optional and n not in given]
            if missing:
                raise ConfigError(f"{where}: arguments manquants {missing}")
            if not all(isinstance(v, (str, int, float)) for v in given.values()):
                raise ConfigError(f"{where}: les valeurs de 'with' doivent être du texte")
            destructive = destructive or target.destructive
        if destructive != cmd.destructive:
            commands[cid] = Command(**{**cmd.__dict__, "destructive": destructive})


def read_user_config(path: Path | None = None) -> dict:
    path = path or USER_CONFIG
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"réglages personnels illisibles ({path}) : {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"réglages personnels invalides ({path})")
    return data


def apply_user_config(raw: dict, user: dict) -> dict:
    """Fusionne les réglages personnels : settings, commandes désactivées, routines ajoutées/remplacées."""
    raw = dict(raw)
    settings = dict(raw.get("settings") or {})
    for key, value in (user.get("settings") or {}).items():
        if key not in USER_SETTING_KEYS:
            raise ConfigError(f"réglage personnel inconnu ou non modifiable : {key!r}")
        if key == "app_aliases":
            settings[key] = {**(settings.get(key) or {}), **(value or {})}
        else:
            settings[key] = value
    raw["settings"] = settings
    disabled = set(user.get("disabled_commands") or [])
    if disabled:
        raw["common"] = [c for c in raw.get("common") or [] if c not in disabled]
        raw["modes"] = {name: {**(m or {}), "commands": [c for c in (m or {}).get("commands") or [] if c not in disabled]}
                        for name, m in (raw.get("modes") or {}).items()}
        settings["safe_commands"] = [c for c in settings.get("safe_commands") or [] if c not in disabled]
    removed = set(user.get("removed_routines") or [])
    mine = list(user.get("routines") or [])
    ids = {r.get("id") for r in mine}
    raw["routines"] = [r for r in raw.get("routines") or [] if r.get("id") not in removed | ids] + mine
    if removed - ids:
        settings["safe_commands"] = [c for c in settings.get("safe_commands") or [] if c not in removed - ids]
    return raw


def load_config(path: str | Path | None = None, user: dict | None = None) -> Config:
    """``path`` absent : commands.yaml + réglages personnels. ``user`` : réglages à essayer (validation)."""
    path = Path(path) if path else DEFAULT_CONFIG
    use_user = user is not None or path.resolve() == DEFAULT_CONFIG.resolve()
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config introuvable : {path}") from exc
    if use_user:
        raw = apply_user_config(raw, user if user is not None else read_user_config())

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
    routines = []
    for r in raw.get("routines") or []:  # sucre syntaxique : une routine = une commande de type routine
        r = dict(r)
        r.setdefault("destructive", False)
        r["action"] = {"type": "routine", "steps": r.pop("steps", None)}
        routines.append(r)
    for i, c in enumerate([*(raw.get("commands") or []), *routines]):
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

    _check_routines(commands)
    unknown = set(settings.safe_commands) - set(commands)
    if unknown:
        raise ConfigError(f"settings.safe_commands : commandes inconnues {sorted(unknown)}")
    risky = [c for c in settings.safe_commands if commands[c].destructive or commands[c].always_confirm]
    if risky:
        raise ConfigError(f"settings.safe_commands : {risky} sont destructives ou toujours confirmées")
    common = tuple(raw.get("common") or []) + tuple(r["id"] for r in routines if r["id"] not in (raw.get("common") or []))
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
