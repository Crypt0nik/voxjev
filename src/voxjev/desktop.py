"""Agent bureau : plusieurs clics / saisies dans l'app au premier plan, via l'accessibilité macOS.

Idée reprise de kevinbadi/jev-voice (MIT) — la boucle de jev-ultrafast portée du DOM à l'arbre
d'accessibilité. À chaque tour :

    arbre AX de la fenêtre -> éléments indexés -> UN appel Jev -> le code agit sur l'élément choisi

Garde-fous (tous dans le code) :
- Jev ne choisit qu'une opération sur un élément OBSERVÉ (jamais de coordonnées, script ou shell) ;
- le texte tapé est un segment de votre phrase choisi par Jev (rien n'est généré) ;
- avant chaque clic, l'élément est relu (existe, actif) ; un libellé sensible (envoyer, payer,
  supprimer, acheter…) arrête l'agent et vous rend la main ;
- refusé dans les terminaux ; budget de 12 actions et 60 s ; arrêt si l'écran ne change plus.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from .candidates import spans

MAX_ACTIONS = 12
TIMEOUT_S = 60.0
MAX_ELEMENTS = 150
CLICKABLE = {"AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXLink",
             "AXDisclosureTriangle", "AXTab", "AXMenuItem"}
TYPABLE = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}

QUESTION = (
    "You operate the frontmost macOS app (`app`, window `window`) for the user, one step at a time, to reach "
    "`goal`. `history` lists the steps already done. Choose the single next step: click an element, type the "
    "goal's text into a field (optionally pressing Enter), `done` if the goal is visibly reached, or `blocked` "
    "if no listed step helps."
)
TEXT_QUESTION = "Which fragment of `goal` is the exact text to type into a field? Pick `none` if nothing must be typed."


class DesktopError(RuntimeError):
    pass


@dataclass
class Element:
    ref: object
    role: str
    label: str

    @property
    def describe(self) -> str:
        kind = {"AXButton": "bouton", "AXLink": "lien", "AXCheckBox": "case", "AXRadioButton": "option",
                "AXPopUpButton": "menu déroulant", "AXMenuButton": "menu", "AXTab": "onglet",
                "AXTextField": "champ", "AXTextArea": "zone de texte", "AXComboBox": "champ",
                "AXSearchField": "recherche", "AXMenuItem": "élément de menu"}.get(self.role, self.role)
        return f"{kind} « {self.label} »"


@dataclass
class DesktopResult:
    status: str  # done | blocked | stopped | timeout
    summary: str
    actions: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def _ax():
    import ApplicationServices as AS

    return AS


def _attr(el, name):
    err, value = _ax().AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def _label(el, role: str) -> str:
    for name in ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp"):
        v = _attr(el, name)
        if v and str(v).strip():
            return " ".join(str(v).split())[:80]
    if role in TYPABLE:
        return "champ sans nom"
    v = _attr(el, "AXValue")
    return " ".join(str(v).split())[:80] if isinstance(v, str) and v.strip() else ""


def observe(pid: int) -> tuple[str, list[Element], str]:
    """(titre de fenêtre, éléments actionnables, empreinte de l'écran)."""
    AS = _ax()
    app = AS.AXUIElementCreateApplication(pid)
    win = _attr(app, "AXFocusedWindow") or next(iter(_attr(app, "AXWindows") or []), None)
    if win is None:
        raise DesktopError("aucune fenêtre accessible (autorisation Accessibilité ?)")
    title = str(_attr(win, "AXTitle") or "")
    found: list[Element] = []
    stack, seen = [(win, 0)], 0
    while stack and len(found) < MAX_ELEMENTS and seen < 4000:
        el, depth = stack.pop()
        seen += 1
        role = str(_attr(el, "AXRole") or "")
        if role in CLICKABLE or role in TYPABLE:
            enabled = _attr(el, "AXEnabled")
            label = _label(el, role)
            if enabled is not False and label:
                found.append(Element(el, role, label))
        if depth < 25:
            kids = _attr(el, "AXChildren") or []
            stack.extend((k, depth + 1) for k in reversed(list(kids)))
    digest = hashlib.sha1(("|".join(f"{e.role}:{e.label}" for e in found) + title).encode()).hexdigest()
    return title, found, digest


def _type(text: str, submit: bool) -> None:
    lines = ["on run argv", 'tell application "System Events" to keystroke (item 1 of argv)']
    if submit:
        lines.append('tell application "System Events" to key code 36')
    lines.append("end run")
    subprocess.run(["osascript", *[x for ln in lines for x in ("-e", ln)], text], check=True, timeout=15,
                   capture_output=True)


def run_desktop_task(client, goal: str, *, app_name: str, pid: int, terminal_apps: tuple[str, ...] = (),
                     on_progress: Callable[[str], None] | None = None, risky=None) -> DesktopResult:
    from .jev_client import NONE

    if app_name in terminal_apps:
        raise DesktopError("l'agent bureau est désactivé dans les terminaux")
    progress = on_progress or (lambda m: None)
    started = time.monotonic()
    history: list[str] = []
    text_options = spans(goal)
    last_digest, unchanged = "", 0
    AS = _ax()
    while True:
        if time.monotonic() - started > TIMEOUT_S:
            return DesktopResult("timeout", f"temps écoulé ({TIMEOUT_S:.0f} s)", history, time.monotonic() - started)
        if len(history) >= MAX_ACTIONS:
            return DesktopResult("stopped", f"budget de {MAX_ACTIONS} actions atteint", history, time.monotonic() - started)
        title, elements, digest = observe(pid)
        unchanged = unchanged + 1 if digest == last_digest else 0
        if unchanged >= 2:
            return DesktopResult("blocked", "l'écran ne change plus", history, time.monotonic() - started)
        last_digest = digest
        options = {}
        for i, el in enumerate(elements):
            if el.role in TYPABLE:
                options[f"t{i}"] = {"what": f"taper le texte dans le {el.describe}"}
                options[f"e{i}"] = {"what": f"taper le texte dans le {el.describe} puis Entrée"}
            else:
                options[f"c{i}"] = {"what": f"cliquer sur le {el.describe}"}
        options = dict(list(options.items())[:236])
        options["done"] = {"what": "l'objectif est atteint, rien d'autre à faire"}
        options["blocked"] = {"what": "impossible d'avancer avec ces éléments"}
        questions = {"next": {"type": "choice", "instructions": QUESTION, "criteria": options}}
        if text_options and any(k[0] in "te" for k in options):
            questions["text"] = {"type": "choice", "instructions": TEXT_QUESTION,
                                 "criteria": {f"s{i}": {"what": s} for i, s in enumerate(text_options[:200])}
                                 | {NONE: {"what": "nothing to type"}}}
        state = {"goal": goal, "app": app_name, "window": title, "history": history or ["(rien encore)"]}
        r = client.raw(state, questions)
        choice = r.choices["next"].choice
        if choice == "done":
            return DesktopResult("done", "objectif atteint", history, time.monotonic() - started)
        if choice == "blocked":
            return DesktopResult("blocked", "l'agent ne trouve pas comment avancer", history, time.monotonic() - started)
        el = elements[int(choice[1:])]
        if risky is not None and risky.search(el.label):
            return DesktopResult("stopped", f"arrêt avant une action sensible : {el.describe}. Terminez vous-même.",
                                 history, time.monotonic() - started)
        if _attr(el.ref, "AXRole") is None:  # élément disparu entre-temps : on réobserve
            continue
        if choice[0] == "c":
            progress(f"clic : {el.describe}")
            if AS.AXUIElementPerformAction(el.ref, "AXPress") != 0:
                return DesktopResult("blocked", f"clic refusé sur {el.describe}", history, time.monotonic() - started)
            history.append(f"cliqué sur {el.describe}")
        else:
            t = r.choices.get("text")
            if t is None or t.choice == NONE:
                return DesktopResult("blocked", "aucun texte à taper trouvé dans la demande", history,
                                     time.monotonic() - started)
            text = text_options[int(t.choice[1:])]
            progress(f"saisie dans {el.describe} : « {text[:40]} »")
            AS.AXUIElementSetAttributeValue(el.ref, "AXFocused", True)
            time.sleep(0.15)
            _type(text, submit=choice[0] == "e")
            history.append(f"tapé « {text} » dans {el.describe}" + (" + Entrée" if choice[0] == "e" else ""))
        time.sleep(0.6)
