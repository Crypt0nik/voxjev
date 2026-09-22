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

from .config import URL_SCHEMES, Command, Config


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


def plan_action(action: dict, command: Command | None, values: dict[str, str], config: Config,
                installed: tuple[str, ...], depth: int = 0) -> list[Step]:
    kind = action["type"]
    args = command.args if command else {}

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

    if kind == "shortcut":
        return [Step("run", ("shortcuts", "run", action["name"]), label=f"raccourci {action['name']}")]

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
                 installed: tuple[str, ...]) -> list[Step]:
    return plan_action(command.action, command, values, config, installed)
