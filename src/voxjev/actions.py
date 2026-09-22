"""Traduction d'une action de la config en étapes concrètes (listes argv), puis exécution.

Garanties de sécurité :
- seuls les types d'action de la liste blanche (config.ACTION_TYPES) existent ;
- tout est lancé via ``subprocess.run(argv_list)`` : jamais ``shell=True``, jamais de
  chaîne issue du transcript ou de Jev interprétée comme du code ;
- les scripts AppleScript sont figés dans la config ; les valeurs variables (nom d'app)
  leur sont passées en ``argv`` ;
- un texte libre n'entre que dans une URL, encodé (``quote``), et l'URL finale doit
  commencer par un schéma autorisé.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from .config import COMBO_RE, INFO_TOPICS, URL_SCHEMES, Command, Config


def _placeholders_in(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class ActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    kind: str  # "run" (argv) | "set_mode" | "note" (information, rien n'est exécuté)
    argv: tuple[str, ...] = ()
    mode: str = ""
    label: str = ""

    def __str__(self) -> str:
        if self.kind == "set_mode":
            return f"[mode -> {self.mode}]"
        if self.kind == "note":
            return f"[{self.label}]"
        if self.kind == "web":
            return f"[agent web depuis {self.argv[1]} : {self.argv[0]!r}]"
        if self.kind == "spotify":
            op, query = self.argv
            return f"[spotify {op}" + (f" {query!r}]" if query else "]")
        if self.kind != "run":
            return f"[{self.label}]"
        return " ".join(_shell_repr(a) for a in self.argv)


def _shell_repr(arg: str) -> str:
    """Affichage lisible uniquement (rien n'est passé à un shell)."""
    return arg if arg and all(c.isalnum() or c in "-_./:=?&%+@" for c in arg) else repr(arg)


def _render(template: str, values: dict[str, str], encoder=None) -> str:
    out = []
    for literal, name, _, _ in string.Formatter().parse(template):
        out.append(literal)
        if name is not None:
            value = values.get(name, "")
            out.append(encoder(name, value) if encoder else value)
    return "".join(out)


def _resolve_app(name: str, fallbacks: list[str], installed: tuple[str, ...]) -> str:
    for candidate in [name, *fallbacks]:
        if candidate and (candidate in installed or candidate == "Finder"):
            return candidate
    raise ActionError(f"application introuvable : {name!r}" + (f" (ni {fallbacks})" if fallbacks else ""))


_MODIFIER_AS = {"command": "command down", "shift": "shift down", "option": "option down", "control": "control down"}


def _keystroke_argv(key: str, modifiers: list[str], app: str | None) -> tuple[str, ...]:
    key_literal = key.replace("\\", "\\\\").replace('"', '\\"')
    using = ", ".join(_MODIFIER_AS[m] for m in modifiers)
    stroke = f'tell application "System Events" to keystroke "{key_literal}"' + (f" using {{{using}}}" if using else "")
    if app:
        script = ["on run argv", "tell application (item 1 of argv) to activate", "delay 0.4", stroke, "end run"]
        return ("osascript", *[x for line in script for x in ("-e", line)], app)
    return ("osascript", "-e", stroke)


_AS_MODS = {"cmd": "command down", "shift": "shift down", "alt": "option down", "ctrl": "control down"}


def keycombo_argv(combo: str) -> tuple[str, ...]:
    """« cmd+shift+t » / « code:121 » -> osascript System Events. Grammaire stricte (config.COMBO_RE)."""
    if not COMBO_RE.fullmatch(combo):
        raise ActionError(f"raccourci clavier invalide : {combo!r}")
    *mods, key = combo.split("+")
    using = ", ".join(_AS_MODS[m] for m in mods)
    if key.startswith("code:"):
        stroke = f"key code {int(key[5:])}"
    else:
        stroke = 'keystroke "' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'
    line = f'tell application "System Events" to {stroke}' + (f" using {{{using}}}" if using else "")
    return ("osascript", "-e", line)


def _argv_script(lines: list[str], *argv: str) -> tuple[str, ...]:
    """Script AppleScript FIGÉ ; toutes les valeurs variables passent en argv (jamais interpolées)."""
    return ("osascript", *[x for line in lines for x in ("-e", line)], *argv)


def _date_argv(iso: str) -> list[str]:
    from datetime import datetime

    at = datetime.strptime(iso, "%Y-%m-%dT%H:%M")
    return [str(at.year), str(at.month), str(at.day), str(at.hour), str(at.minute)]


# Construit une date AppleScript à partir de argv[i..i+4] (année, mois, jour, heure, minute).
_AS_DATE = [
    "set d to current date",
    "set day of d to 1",
    "set year of d to (item {0} of argv) as integer",
    "set month of d to (item {1} of argv) as integer",
    "set day of d to (item {2} of argv) as integer",
    "set hours of d to (item {3} of argv) as integer",
    "set minutes of d to (item {4} of argv) as integer",
    "set seconds of d to 0",
]


def _as_date(first: int) -> list[str]:
    return [line.format(*range(first, first + 5)) for line in _AS_DATE]


REMINDER_SCRIPT = ["on run argv", *_as_date(2),
                   'tell application "Reminders" to make new reminder with properties {name:(item 1 of argv), remind me date:d}',
                   "end run"]
REMINDER_NODATE_SCRIPT = ["on run argv", 'tell application "Reminders" to make new reminder with properties {name:(item 1 of argv)}',
                          "end run"]
CALENDAR_ADD_SCRIPT = [
    "on run argv", *_as_date(2),
    "set m to (item 7 of argv) as integer",
    'tell application "Calendar"',
    "set wanted to item 8 of argv",
    'if wanted is "" then',
    "set cal to first calendar whose writable is true",
    "else",
    "set cal to first calendar whose name is wanted",
    "end if",
    "tell cal to make new event with properties {summary:(item 1 of argv), start date:d, end date:(d + m * minutes)}",
    "end tell",
    "end run",
]
NOTE_SCRIPT = ["on run argv", 'tell application "Notes"',
               "make new note at folder 1 of default account with properties {body:(item 1 of argv)}",
               "activate", "end tell", "end run"]


def routine_values(target: Command, given: dict, config: Config) -> dict[str, str]:
    """Valeurs figées d'une étape de routine, converties comme si elles avaient été dites."""
    from .when import parse_duration, parse_when

    out = {}
    for name, raw in given.items():
        value = str(raw)
        spec = target.args[name]
        if spec.type == "app":
            value = config.settings.app_aliases.get(value.lower(), value)
        elif spec.type == "enum":
            key = value if value in spec.values else next(
                (k for k, v in spec.values.items() if value in v.get("aliases", [])), None)
            if key is None:
                raise ActionError(f"valeur {value!r} inconnue pour {name}")
            value = spec.values[key]["value"]
        elif spec.type == "duration":
            seconds = parse_duration(value)
            if not seconds:
                raise ActionError(f"durée illisible : {value!r}")
            value = str(seconds)
        elif spec.type == "when":
            w = parse_when(value)
            if not w:
                raise ActionError(f"date illisible : {value!r}")
            value = w.at.strftime("%Y-%m-%dT%H:%M")
        out[name] = value
    return out


def plan_action(action: dict, command: Command | None, values: dict[str, str], config: Config,
                installed: tuple[str, ...], depth: int = 0, data: dict | None = None) -> list[Step]:
    kind = action["type"]
    args = command.args if command else {}
    data = data or {}

    def val(field_name: str) -> str:
        return _render(str(action.get(field_name, "")), values)

    if kind == "sequence":
        steps: list[Step] = []
        for sub in action["steps"]:
            steps += plan_action(sub, command, values, config, installed, depth + 1)
        return steps

    if kind in ("open_app", "quit_app"):
        app = _resolve_app(_render(action["app"], values), action.get("fallbacks", []), installed)
        if kind == "open_app":
            return [Step("run", ("open", "-a", app), label=f"ouvrir {app}")]
        script = ["on run argv", "tell application (item 1 of argv) to quit", "end run"]
        return [Step("run", ("osascript", *[x for line in script for x in ("-e", line)], app), label=f"quitter {app}")]

    if kind == "open_url":
        def encode(name: str, value: str) -> str:
            spec = args.get(name)
            if spec is None or spec.type == "enum":
                return value  # valeur issue de la config
            if spec.type == "text":
                return quote(value.lower(), safe="") if spec.path_segment else quote_plus(value)
            raise ActionError(f"argument {name!r} de type {spec.type} interdit dans une URL")

        url = _render(action["url"], values, encode)
        if not url.startswith(URL_SCHEMES):
            raise ActionError(f"URL refusée (schéma non autorisé) : {url!r}")
        app_tpl = action.get("app", "")
        app = _render(app_tpl, values) if app_tpl else ""
        if app:
            app = _resolve_app(app, [], installed)
            return [Step("run", ("open", "-a", app, url), label=f"ouvrir {url} dans {app}")]
        return [Step("run", ("open", url), label=f"ouvrir {url}")]

    if kind == "applescript":
        argv_args = [_render(a, values) for a in action.get("args", [])]
        return [Step("run", ("osascript", "-e", action["script"], *argv_args), label="applescript")]

    if kind == "keystroke":
        app = action.get("app")
        if app:
            app = _resolve_app(app, action.get("fallbacks", []), installed)
        return [Step("run", _keystroke_argv(action["key"], list(action.get("modifiers", [])), app),
                     label="raccourci clavier")]

    if kind == "spotify":
        query = _render(action.get("query", ""), values)
        if action["op"] in ("play", "search") and not query:
            raise ActionError("rien à chercher sur Spotify")
        label = {"play": f"Spotify : lancer « {query} »", "like": "Spotify : liker le morceau en cours",
                 "search": f"Spotify : chercher « {query} »"}[action["op"]]
        return [Step("spotify", (action["op"], query), label=label)]

    if kind == "web_task":
        goal = _render(action.get("goal", ""), values)
        url = _render(action.get("url", ""), values)
        if not goal:
            raise ActionError("objectif web vide")
        if not url.startswith(("https://", "http://")):
            raise ActionError(f"URL de départ refusée : {url!r}")
        return [Step("web", (goal, url), label=f"Agent web : « {goal[:60]} »")]

    if kind == "undo":
        raise ActionError("l'annulation se planifie dans le pipeline (voir Launcher.plan)")

    if kind == "shortcut":
        name = val("name")
        if _placeholders_in(action["name"]):
            from .candidates import list_shortcuts

            if name not in list_shortcuts():  # uniquement un raccourci qui existe vraiment
                raise ActionError(f"raccourci introuvable : {name!r}")
        return [Step("run", ("shortcuts", "run", name), label=f"Raccourci « {name} »")]

    if kind == "menu":
        name = next(iter(_placeholders_in(action["item"])))
        path = data.get(name) or tuple(p.strip() for p in values.get(name, "").split("›"))
        if not path or not all(path):
            raise ActionError("élément de menu inconnu")
        return [Step("menu", tuple(path), label=f"Menu « {' › '.join(path)} »")]

    if kind == "type_text":
        text = val("text")
        if not text:
            raise ActionError("rien à taper")
        submit = bool(action.get("submit", False))
        return [Step("type", (text, "1" if submit else ""), label=f"Taper « {text[:60]} »" + (" + Entrée" if submit else ""))]

    if kind == "keycombo":
        combo = val("combo")
        return [Step("run", keycombo_argv(combo), label=f"Raccourci clavier {combo}")]

    if kind == "timer":
        from .when import format_duration

        seconds = int(val("seconds") or 0)
        if not 0 < seconds <= 24 * 3600:
            raise ActionError("durée de minuteur invalide")
        label = val("label") if action.get("label") else ""
        return [Step("timer", (str(seconds), label), label=f"Minuteur {format_duration(seconds)}"
                     + (f" « {label} »" if label else ""))]

    if kind == "reminder":
        title = val("title")
        when = val("when") if action.get("when") else ""
        if not title:
            raise ActionError("rappel sans titre")
        if when:
            return [Step("run", _argv_script(REMINDER_SCRIPT, title, *_date_argv(when)), label=f"Rappel « {title} »")]
        return [Step("run", _argv_script(REMINDER_NODATE_SCRIPT, title), label=f"Rappel « {title} »")]

    if kind == "calendar_add":
        title, when = val("title"), val("when")
        if not title or not when:
            raise ActionError("événement sans titre ou sans date")
        minutes = str(int(action.get("minutes", 60)))
        calendar = str(action.get("calendar", ""))
        return [Step("run", _argv_script(CALENDAR_ADD_SCRIPT, title, *_date_argv(when), minutes, calendar),
                     label=f"Agenda « {title} »")]

    if kind == "calendar_read":
        when = val("when") if action.get("when") else ""
        return [Step("agenda", (when[:10],), label="Lire l'agenda")]

    if kind == "note_add":
        body = val("body")
        if not body:
            raise ActionError("note vide")
        return [Step("run", _argv_script(NOTE_SCRIPT, body), label=f"Note « {body[:60]} »")]

    if kind == "mail_draft":
        return [Step("mail", (val("to") if action.get("to") else "", val("body")), label="Brouillon d'e-mail (non envoyé)")]

    if kind == "info":
        topic = val("what")
        if topic not in INFO_TOPICS:
            raise ActionError(f"information inconnue : {topic!r}")
        return [Step("info", (topic,), label={"time": "Heure", "date": "Date", "battery": "Batterie",
                                                "timers": "Minuteurs en cours"}[topic])]

    if kind == "page_link":
        return [Step("page_link", (), label="Lien de la page Chrome")]

    if kind == "routine":
        steps = []
        for st in action["steps"]:
            if "wait" in st:
                steps.append(Step("wait", (str(st["wait"]),), label=f"pause {st['wait']} s"))
                continue
            target = config.commands[st["run"]]
            tvalues = routine_values(target, st.get("with") or {}, config)
            steps += plan_action(target.action, target, tvalues, config, installed, depth + 1)
            if target.action.get("type") in ("open_app", "set_mode"):
                steps.append(Step("wait", (str(config.settings.step_delay_seconds),), label="pause"))
        while steps and steps[-1].kind == "wait":
            steps.pop()
        return steps

    if kind in ("memory_add", "memory_ask", "memory_forget", "ask", "desktop_task", "file_open", "mail_triage"):
        field_name = {"memory_add": "fact", "memory_ask": "question", "memory_forget": "question", "ask": "question",
                      "desktop_task": "goal", "file_open": "query", "mail_triage": ""}[kind]
        text = val(field_name) if field_name else ""
        if field_name and not text:
            raise ActionError("rien à traiter")
        extra = ("1",) if kind == "file_open" and action.get("reveal") else ()
        labels = {"memory_add": f"Retenir « {text[:60]} »", "memory_ask": f"Chercher dans la mémoire : « {text[:50]} »",
                  "memory_forget": f"Oublier : « {text[:50]} »", "ask": f"Question : « {text[:60]} »",
                  "desktop_task": f"Agent bureau : « {text[:60]} »", "file_open": f"Fichier : « {text[:60]} »",
                  "mail_triage": "Trier les mails non lus"}
        return [Step(kind, (text, *extra), label=labels[kind])]

    if kind == "exec":
        argv = tuple(action["argv"])
        if argv[0] not in config.settings.exec_allowlist:  # double vérification
            raise ActionError(f"{argv[0]!r} hors liste blanche")
        return [Step("run", argv, label=" ".join(argv))]

    if kind == "set_mode":
        mode = _render(action["mode"], values)
        if mode not in config.modes:
            raise ActionError(f"mode inconnu : {mode!r}")
        steps = [Step("set_mode", mode=mode, label=f"mode {mode}")]
        for sub in config.modes[mode].on_enter:
            try:
                steps += plan_action(sub, None, {}, config, installed, depth + 1)
            except ActionError as exc:  # une app absente ne bloque pas le changement de mode
                steps.append(Step("note", label=f"ignoré : {exc}"))
        return steps

    raise ActionError(f"type d'action {kind!r} hors liste blanche")


def plan_command(command: Command, values: dict[str, str], config: Config,
                 installed: tuple[str, ...], data: dict | None = None) -> list[Step]:
    return plan_action(command.action, command, values, config, installed, data=data)
