"""Exécution des étapes planifiées + retours (sons, dialogue de confirmation)."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from .actions import ActionError, Step

STEP_TIMEOUT = 15


TERMINAL_APPS = ("Terminal", "iTerm2", "iTerm", "Ghostty", "Warp", "kitty", "Alacritty", "WezTerm", "Hyper",
                 "Tabby", "Visual Studio Code", "Cursor")


class Timers:
    """Minuteurs en mémoire (perdus si voxjev s'arrête)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active: dict[int, tuple[float, str, threading.Timer]] = {}
        self._next = 1

    def start(self, seconds: int, label: str, on_done) -> int:
        with self._lock:
            tid = self._next
            self._next += 1

        def fire():
            with self._lock:
                self.active.pop(tid, None)
            on_done(label)

        t = threading.Timer(seconds, fire)
        t.daemon = True
        with self._lock:
            self.active[tid] = (time.time() + seconds, label, t)
        t.start()
        return tid

    def describe(self) -> str:
        from .when import format_duration

        with self._lock:
            items = sorted(self.active.values(), key=lambda x: x[0])
        if not items:
            return "Aucun minuteur en cours."
        return "Minuteurs : " + " ; ".join(
            f"{format_duration(max(0, int(end - time.time())))} restantes" + (f" ({label})" if label else "")
            for end, label, _ in items) + "."


def notify(title: str, text: str, sound: str = "Glass") -> None:
    script = ["on run argv", 'display notification (item 2 of argv) with title (item 1 of argv) sound name (item 3 of argv)',
              "end run"]
    subprocess.run(["osascript", *[x for ln in script for x in ("-e", ln)], title, text, sound],
                   capture_output=True, timeout=10)


def speak(text: str, voice: str = "") -> None:
    """Lecture à voix haute (say), non bloquante. Le texte passe en argv."""
    from .audio import SPEAKING_UNTIL

    text = text[:600]
    SPEAKING_UNTIL[0] = time.time() + 1.0 + len(text) / 13  # le mode mains libres n'écoute pas sa propre voix
    argv = ["say"] + (["-v", voice] if voice else []) + ["--", text]
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SubprocessExecutor:
    """Lance chaque étape via ``subprocess.run(argv)`` ou du code Python — jamais de shell.

    ``run`` renvoie les messages à afficher / lire (réponses, agenda, tri des mails…).
    """

    timers = Timers()

    def __init__(self, progress=None, client=None, settings=None, speak_answers: bool | None = None):
        self.progress = progress or (lambda msg: print(f"  {msg}", flush=True))
        self.client = client  # client Jev (tri des mails, agent bureau), injecté par le Launcher
        self.settings = settings
        self.speak_answers = speak_answers
        self.on_timer = None  # rappel GUI quand un minuteur sonne

    # -------------------------------------------------------------- utilitaires
    def _say(self, text: str) -> None:
        enabled = self.speak_answers if self.speak_answers is not None else getattr(self.settings, "speak_answers", False)
        if enabled:
            speak(text, getattr(self.settings, "voice", ""))

    def _front(self) -> tuple[str | None, int | None]:
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return (str(app.localizedName()), int(app.processIdentifier())) if app else (None, None)
        except Exception:
            return None, None

    def _terminals(self) -> tuple[str, ...]:
        return tuple(getattr(self.settings, "terminal_apps", ()) or TERMINAL_APPS)

    def _timer_done(self, label: str) -> None:
        text = f"Minuteur terminé" + (f" : {label}" if label else "")
        notify("voxjev", text)
        speak(text, getattr(self.settings, "voice", ""))
        if self.on_timer:
            self.on_timer(text)

    # -------------------------------------------------------------- exécution
    def run(self, steps: list[Step]) -> list[str]:
        messages: list[str] = []
        for step in steps:
            msg = self._run_one(step)
            if msg:
                messages.append(msg)
                print(f"  → {msg}", flush=True)
        if messages:
            self._say(" ".join(messages))
        return messages

    def _run_one(self, step: Step) -> str | None:
        k = step.kind
        if k == "spotify":
            from .spotify import SpotifyError, run_op

            try:
                print(f"  {run_op(*step.argv)}", flush=True)
            except (SpotifyError, subprocess.SubprocessError) as exc:
                raise ActionError(f"Spotify : {exc}") from exc
            return None
        if k == "web":
            from .webagent import WebAgentError, run_web_task

            try:
                res = run_web_task(*step.argv, on_progress=lambda m: self.progress(f"Agent web — {m}"))
            except WebAgentError as exc:
                raise ActionError(str(exc)) from exc
            print(f"  agent web : {res.status} — {res.summary} ({len(res.actions)} actions, "
                  f"{res.elapsed_s:.1f} s) {res.url}", flush=True)
            if res.status != "done":
                raise ActionError(f"agent web : {res.summary}")
            return None
        if k == "wait":
            time.sleep(min(float(step.argv[0]), 30))
            return None
        if k == "menu":
            from .axmenu import press

            try:
                press(tuple(step.argv))
            except Exception as exc:
                raise ActionError(f"menu : {exc}") from exc
            return None
        if k == "type":
            text, submit = step.argv
            app, _ = self._front()
            if app in self._terminals():
                raise ActionError(f"saisie refusée dans {app} (un texte dicté n'est jamais tapé dans un terminal)")
            lines = ["on run argv", 'tell application "System Events" to keystroke (item 1 of argv)']
            if submit:
                lines.append('tell application "System Events" to key code 36')
            lines.append("end run")
            self._subprocess(Step("run", ("osascript", *[x for ln in lines for x in ("-e", ln)], text), label=step.label))
            return None
        if k == "timer":
            from .when import format_duration

            seconds, label = int(step.argv[0]), step.argv[1]
            self.timers.start(seconds, label, self._timer_done)
            return f"Minuteur lancé : {format_duration(seconds)}" + (f" ({label})" if label else "") + "."
        if k == "info":
            return self._info(step.argv[0])
        if k == "agenda":
            from datetime import datetime

            from .apple import AppleDataError, agenda, describe_agenda

            day = datetime.strptime(step.argv[0], "%Y-%m-%d") if step.argv[0] else datetime.now()
            try:
                return describe_agenda(day, agenda(day))
            except AppleDataError as exc:
                raise ActionError(str(exc)) from exc
        if k == "mail":
            return self._mail(*step.argv)
        if k == "memory_add":
            from .memory import Memory

            n = Memory().add(step.argv[0])
            return f"C'est noté ({n} souvenir{'s' if n > 1 else ''})."
        if k == "memory_forget":
            from .memory import Memory

            Memory().remove(step.argv[0])
            return f"Oublié : « {step.argv[0]} »."
        if k == "say":
            return step.argv[0]
        if k == "ask":
            from . import llm

            try:
                return llm.ask(step.argv[0])
            except llm.LLMError as exc:
                raise ActionError(str(exc)) from exc
        if k == "mail_triage":
            from .apple import AppleDataError, describe_triage, triage, unread_mails

            if self.client is None:
                raise ActionError("client Jev indisponible")
            try:
                self.progress("lecture des mails non lus…")
                mails = unread_mails()
                self.progress(f"tri de {len(mails)} mail(s) par Jev…")
                return describe_triage(triage(self.client, mails))
            except AppleDataError as exc:
                raise ActionError(str(exc)) from exc
        if k == "desktop_task":
            from .desktop import DesktopError, run_desktop_task
            from .webagent import RISKY

            app, pid = self._front()
            if self.client is None or pid is None:
                raise ActionError("agent bureau indisponible")
            try:
                res = run_desktop_task(self.client, step.argv[0], app_name=app or "", pid=pid,
                                       terminal_apps=self._terminals(), risky=RISKY,
                                       on_progress=lambda m: self.progress(f"Agent bureau — {m}"))
            except DesktopError as exc:
                raise ActionError(str(exc)) from exc
            summary = f"Agent bureau : {res.summary} ({len(res.actions)} action(s), {res.elapsed_s:.0f} s)."
            if res.status != "done":
                raise ActionError(summary)
            return summary
        if k == "navigate":
            from .chrome import ChromeError, navigate

            try:
                navigate(step.argv[0])
            except ChromeError as exc:
                raise ActionError(str(exc)) from exc
            return None
        if k == "page_link" and step.argv:
            return self._page_link(step.argv[0])
        if k in ("memory_ask", "file_open", "page_link"):
            raise ActionError("étape non résolue (voir Launcher)")
        if k != "run" or not step.argv:
            return None
        argv = step.argv
        if argv[0] == "open" and argv[-1].startswith(("http://", "https://")) and len(argv) in (2, 4):
            from .chrome import open_in_browser  # nouvel onglet actif (pas « Little Arc »)

            app = argv[2] if len(argv) == 4 and argv[1] == "-a" else None
            if open_in_browser(argv[-1], app):
                return None
        self._subprocess(step)
        return None

    def _page_link(self, transcript: str) -> None:
        """Lien choisi une fois la page chargée (étape d'une demande composée)."""
        from .chrome import ChromeError, navigate, page_links, pick

        if self.client is None:
            raise ActionError("client Jev indisponible")
        deadline = time.monotonic() + 15.0
        time.sleep(1.0)  # laisser la nouvelle page démarrer son chargement
        try:
            while True:
                try:
                    title, url, links = page_links(timeout=4)  # essais courts : la page peut bloquer en chargeant
                except ChromeError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.7)  # page encore en chargement : le navigateur ne répond pas tout de suite
                    continue
                if any(link.result for link in links):
                    break
                if time.monotonic() > deadline:  # jamais de repli sur un lien quelconque de l'ancienne page
                    raise ChromeError("la page de résultats n'est pas apparue dans le navigateur")
                time.sleep(0.5)  # la page de résultats se charge encore
            link, _, _ = pick(self.client, transcript, title, url, links, getattr(self.settings, "pick_min_p", 0.5))
            self.progress(f"ouverture de « {link.text[:60]} » ({link.domain})")
            navigate(link.href)
        except ChromeError as exc:
            raise ActionError(str(exc)) from exc
        return None

    def _subprocess(self, step: Step) -> None:
        try:
            proc = subprocess.run(list(step.argv), capture_output=True, text=True,
                                  timeout=STEP_TIMEOUT, shell=False, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActionError(f"échec de « {step.label} » : {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise ActionError(f"échec de « {step.label} » (code {proc.returncode})"
                              + (f" : {detail[-1]}" if detail else ""))

    def _info(self, topic: str) -> str:
        import datetime as dt

        from .when import WEEKDAYS

        now = dt.datetime.now()
        if topic == "time":
            return f"Il est {now.hour} h {now.minute:02d}."
        if topic == "date":
            months = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
                      "octobre", "novembre", "décembre"]
            return f"Nous sommes le {list(WEEKDAYS)[now.weekday()]} {now.day} {months[now.month - 1]} {now.year}."
        if topic == "battery":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
            import re

            m = re.search(r"(\d+)%;\s*([^;]+);\s*([^\n]*)", out)
            if not m:
                return "Pas de batterie détectée."
            state = {"charging": "en charge", "discharging": "sur batterie", "charged": "chargée",
                     "finishing charge": "fin de charge", "AC attached": "branchée"}.get(m.group(2).strip(), m.group(2).strip())
            left = re.search(r"(\d+:\d+) remaining", m.group(3))
            left = left if left and left.group(1) != "0:00" else None
            return f"Batterie à {m.group(1)} %, {state}" + (f", {left.group(1).replace(':', ' h ')} restantes" if left else "") + "."
        return self.timers.describe()

    def _mail(self, to: str, instruction: str) -> str:
        from . import llm

        subject, body = "", instruction
        if llm.available():
            try:
                subject, body = llm.draft_mail(instruction + (f" (destinataire : {to})" if to else ""))
            except llm.LLMError as exc:
                self.progress(f"rédaction LLM indisponible ({exc}) : brouillon brut")
        address = _contact_email(to) if to else ""
        script = ["on run argv", 'tell application "Mail"',
                  "set m to make new outgoing message with properties {subject:(item 1 of argv), content:(item 2 of argv), visible:true}",
                  'if (item 3 of argv) is not "" then',
                  "tell m to make new to recipient at end of to recipients with properties {address:(item 3 of argv)}",
                  "end if", "activate", "end tell", "end run"]
        self._subprocess(Step("run", ("osascript", *[x for ln in script for x in ("-e", ln)], subject, body, address),
                              label="brouillon Mail"))
        return ("Brouillon prêt dans Mail" + (f" pour {address}" if address else (f" (adresse de {to} introuvable)" if to else ""))
                + ". Relisez-le et envoyez-le vous-même.")


def _contact_email(name: str) -> str:
    """Adresse du premier contact dont le nom contient `name` (app Contacts), ou chaîne vide."""
    script = ["on run argv", 'tell application "Contacts"',
              "set ps to (people whose name contains (item 1 of argv))",
              'if (count of ps) is 0 then return ""',
              "set p to item 1 of ps",
              'if (count of emails of p) is 0 then return ""',
              "return value of item 1 of emails of p", "end tell", "end run"]
    try:
        proc = subprocess.run(["osascript", *[x for ln in script for x in ("-e", ln)], name],
                              capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


class Sounds:
    """Sons système joués en arrière-plan (afplay, non bloquant)."""

    def __init__(self, sounds: dict[str, str], enabled: bool = True):
        self.sounds = {k: v for k, v in sounds.items() if Path(v).exists()}
        self.enabled = enabled

    def play(self, name: str) -> None:
        path = self.sounds.get(name)
        if self.enabled and path:
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def for_status(self, status: str) -> None:
        self.play({"executed": "success", "error": "failure", "cancelled": "failure",
                   "ignored": "ignored"}.get(status, ""))


_DIALOG = [
    "on run argv",
    'set r to display dialog (item 1 of argv) with title "voxjev" buttons {"Annuler", "Exécuter"} '
    'default button "Exécuter" cancel button "Annuler" giving up after ((item 2 of argv) as integer) with icon caution',
    'if gave up of r then return "timeout"',
    "return button returned of r",
    "end run",
]


def _ask_dialog(lines: list[str], timeout_s: int) -> bool:
    argv = ["osascript"] + [x for line in _DIALOG for x in ("-e", line)] + ["\n".join(lines), str(timeout_s)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s + 5)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "Exécuter"


def dialog_plan_confirmer(timeout_s: int = 10):
    """Confirmation d'une demande composée (une seule boîte pour tout le plan)."""

    def confirm(plan) -> bool:
        lines = [f"Exécuter ces {len(plan.runnable)} étape(s) ?", ""]
        lines += [f"{i}. {o.decision.command.short(o.args.shown if o.args else None)}"
                  for i, o in enumerate(plan.runnable, 1)]
        lines += [f"✗ ignoré : « {o.transcript} »" for o in plan.dropped]
        return _ask_dialog(lines, timeout_s)

    return confirm


def dialog_confirmer(timeout_s: int = 10):
    """Confirmation par boîte de dialogue macOS. Le texte est passé en argv (jamais interpolé)."""

    def confirm(decision, args, steps) -> bool:
        cmd = decision.command
        lines = [f"{cmd.description if cmd else '?'} ?", "", f"Raison : {decision.reason}"]
        lines += [f"• {s.label}" for s in steps if s.label]
        argv = ["osascript"] + [x for line in _DIALOG for x in ("-e", line)] + ["\n".join(lines), str(timeout_s)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s + 5)
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "Exécuter"

    return confirm
