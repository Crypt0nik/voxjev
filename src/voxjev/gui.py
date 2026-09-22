"""Interface graphique native macOS : icône de barre des menus + HUD flottant.

Choix de conception :
- le HUD est un NSPanel **non-activant** : il ne prend jamais le focus, donc l'« app au
  premier plan » envoyée à Jev reste la vôtre et les raccourcis clavier (nouvel onglet,
  fermer la fenêtre…) partent dans la bonne app ;
- l'app n'a pas d'icône dans le Dock (politique « accessory ») ;
- la confirmation se fait sans voler le focus : clic sur le HUD ou réponse vocale
  (« oui » / « non ») en maintenant la touche ;
- tout le travail lourd (Whisper, Jev, actions) tourne dans UN thread dédié qui possède le
  modèle MLX ; l'interface n'est mise à jour que depuis le thread principal (callAfter).
"""

from __future__ import annotations

import collections
import os
import queue
import subprocess
import threading
import time
from datetime import datetime

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSTextField,
    NSVariableStatusItemLength,
    NSWorkspace,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from .audio import (PERMISSION_HELP, HandsFree, PushToTalk, accessibility_trusted, hotkey_label, request_permissions,
                    strip_wake_word)
from .stt import MIN_RMS, SAMPLE_RATE
from .cli import format_any
from .config import Config, load_config
from .context import Session
from .decide import parse_yes_no
from .executor import Sounds, SubprocessExecutor
from .multi import MultiRunner, PlanOutcome, build_splitter
from .hud import HUD, PHASES
from .pipeline import Launcher, Outcome

# ----------------------------------------------------------------------------- constantes AppKit
ACCESSORY_POLICY = 1
ALERT_FIRST_BUTTON = 1000
ACTIVATE_IGNORING_OTHERS = 1 << 1
PERMISSION_TEXT = (
    ("Autorisez « voxjev »" if os.environ.get("VOXJEV_APP") else "Autorisez votre terminal") + " dans Réglages › Confidentialité et sécurité › Accessibilité "
    "et › Surveillance de l'entrée, puis relancez. En attendant : menu › Tester une phrase…"
)


# ----------------------------------------------------------------------------- moteur
class Engine(threading.Thread):
    """Thread unique qui possède Whisper (MLX), le client Jev et le pipeline."""

    def __init__(self, app: GuiApp, config: Config, client, session: Session, dry_run: bool, sound: bool):
        super().__init__(daemon=True, name="voxjev-engine")
        self.app, self.config, self.client, self.session = app, config, client, session
        self.jobs: queue.Queue = queue.Queue()
        self.answers: queue.Queue = queue.Queue()
        self.deferred: list = []
        self.dry_run = dry_run
        self.sound_allowed = sound  # --no-sound
        self.sounds = Sounds(config.settings.sounds, enabled=sound and config.settings.sounds_enabled)
        self.transcriber = None
        self.launcher: Launcher | None = None
        self.speak = config.settings.speak_answers
        self.partial = ("", 0)  # (texte, nb d'échantillons couverts) : transcription anticipée
        self.followup_until = 0.0  # fenêtre de suite du mode mains libres

    def ui(self, fn, *args) -> None:
        AppHelper.callAfter(fn, *args)

    def run(self) -> None:
        from .stt import Transcriber

        s = self.config.settings
        hud = self.app.hud
        self.ui(hud.phase, "loading", "Chargement de Whisper…")
        self.ui(self.app.set_icon, "loading")
        try:
            self.transcriber = Transcriber(s.stt_model, s.language, prompt=s.stt_prompt or None)
            warm = self.transcriber.warmup()
        except Exception as exc:  # modèle introuvable, pas de réseau au premier lancement…
            self.ui(hud.show_message, "Whisper indisponible", str(exc), "error")
            warm = None
        executor = SubprocessExecutor(progress=lambda m: (print(f"  {m}", flush=True),
                                                         self.ui(hud.phase, "thinking", m, 0, True)))
        executor.settings = self.config.settings
        executor.on_timer = lambda text: (self.sounds.play("success"),
                                          self.ui(hud.show_message, "Minuteur", text, "done", 8.0))
        self.launcher = Launcher(self.config, self.client, self.session, executor=executor,
                                 confirmer=self._confirm, dry_run=self.dry_run)
        self.runner = MultiRunner(self.launcher, build_splitter(s), confirm_plan=self._confirm_plan)
        print(f"Demandes composées : découpage par {self.runner.splitter.kind}", flush=True)
        self.ui(self.app.set_icon, "idle")
        if warm is not None:
            print(f"Whisper prêt ({warm:.0f} ms)")
            if self.app.trusted:
                self.ui(hud.phase, "idle", f"Prêt — maintenez {hotkey_label(s.hotkey)} pour parler", 3.0)
            else:
                self.ui(hud.show_message, "Push-to-talk désactivé", PERMISSION_TEXT, "error", 8.0)
        while True:
            if self.deferred:
                job = self.deferred.pop(0)
            else:
                try:
                    job = self.jobs.get(timeout=0.35)
                except queue.Empty:
                    self._partial_tick()
                    continue
            if job is None:
                return
            try:
                self._handle(job)
            except Exception as exc:  # ne jamais tuer le thread moteur
                print(f"erreur interne : {exc!r}")
                self.ui(hud.show_message, "Erreur interne", repr(exc), "error", 6.0)
                self.ui(self.app.set_icon, "error")

    # ---------------------------------------------------------------- anticipation
    def _partial_tick(self) -> None:
        """Pendant l'appui : transcrit l'audio déjà capté et lance Jev en avance."""
        ptt = self.app.ptt
        s = self.config.settings
        if not (s.speculate and ptt and ptt.recorder.recording and self.transcriber and self.launcher):
            return
        audio = ptt.recorder.snapshot()
        if audio.size < int(1.0 * SAMPLE_RATE) or audio.size - self.partial[1] < int(0.7 * SAMPLE_RATE):
            return
        text, _ = self.transcriber.transcribe(audio)
        self.partial = (text, audio.size)
        if text:
            self.ui(self.app.hud.show_transcript, text)
            self.launcher.speculate(text)

    def _final_text(self, audio) -> tuple[str, float, bool]:
        """Réutilise la transcription anticipée si la fin de l'audio n'est que du silence."""
        text, covered = self.partial
        self.partial = ("", 0)
        tail = audio[covered:]
        if text and covered and (tail.size == 0 or float((tail**2).mean() ** 0.5) < MIN_RMS * 1.5):
            return text, 0.0, True
        t, ms = self.transcriber.transcribe(audio)
        return t, ms, False

    # ---------------------------------------------------------------- jobs
    def _handle(self, job) -> None:
        kind = job[0]
        hud, s = self.app.hud, self.config.settings
        if kind == "press":
            self.partial = ("", 0)
            return
        if kind == "hands_free":
            job = self._hands_free(job)
            if job is None:
                return
            kind = job[0]
        if kind == "mode":
            self.session.mode = job[1]
            self.session.save()
            self.ui(self.app.refresh_mode)
            self.ui(hud.phase, "idle", f"Mode {job[1]}", 1.5)
            return
        if kind == "dry_run":
            self.dry_run = job[1]
            self.launcher.dry_run = job[1]
            self.ui(self.app.refresh_mode)
            return
        if kind == "reload":
            try:
                self.config = load_config(self.config.path)
            except Exception as exc:
                self.ui(hud.show_message, "Config invalide", str(exc), "error", 8.0)
                return
            old_hotkey = self.launcher.config.settings.hotkey
            self.launcher.config = self.config
            s = self.config.settings
            if self.session.mode not in self.config.modes:
                self.session.mode = self.config.default_mode
            # réglages appliqués à chaud (fenêtre Réglages)
            self.sounds.enabled = s.sounds_enabled and self.sound_allowed
            self.speak = s.speak_answers
            executor = self.launcher.executor
            if executor is not None:
                executor.settings = s
                executor.speak_answers = s.speak_answers
            self.runner.splitter = build_splitter(s)
            if s.hotkey != old_hotkey:
                self.ui(self.app.restart_ptt, s.hotkey)
            self.ui(self.app.rebuild_menu)
            self.ui(hud.phase, "idle", "Réglages appliqués", 1.2)
            return

        started = time.perf_counter()
        timings = {}
        if kind == "audio":
            audio, held = job[1], job[2]
            if held < s.min_record_seconds or self.transcriber is None:
                self.ui(hud.hide)
                self.ui(self.app.set_icon, "idle")
                return
            self.ui(hud.phase, "transcribing", "Transcription…", 0, True)
            self.ui(self.app.set_icon, "transcribing")
            text, stt_ms, reused = self._final_text(audio)
            timings = {"audio_s": held, "stt_ms": stt_ms} | ({"stt_anticipé": 1} if reused else {})
            if not text:
                print(f"(rien compris — {held:.1f} s d'audio)", flush=True)
                self.sounds.play("ignored")
                self.ui(hud.phase, "idle", "Rien compris", 1.5)
                self.ui(self.app.set_icon, "idle")
                return
        else:
            text = job[1]
            timings = dict(job[2]) if len(job) > 2 else {}
        self.ui(hud.show_transcript, text)
        self.ui(hud.phase, "thinking", "Jev réfléchit…", 0, True)
        self.ui(self.app.set_icon, "thinking")
        out = self.runner.handle(text)
        out.timings = {**timings, **out.timings, "total_ms": (time.perf_counter() - started) * 1000}
        print(format_any(out, self.session.mode), flush=True)
        if out.status != "dry_run":
            self.sounds.for_status(out.status)
        if isinstance(out, PlanOutcome):
            self.ui(hud.show_plan, out, self.config.settings)
        else:
            self.ui(hud.show_outcome, out, self.config.settings)
        self.ui(self.app.set_icon, "error" if out.status == "error" else "idle")
        self.ui(self.app.add_history, out)
        self.ui(self.app.refresh_mode)
        if out.status in ("executed", "dry_run"):
            self.followup_until = time.monotonic() + s.followup_seconds

    def _hands_free(self, job):
        """Phrase captée micro ouvert : transcrite en local ; ne part vers Jev qu'avec le mot d'éveil
        (ou pendant la fenêtre de suite après une commande)."""
        s, hud = self.config.settings, self.app.hud
        audio, dur = job[1], job[2]
        if self.transcriber is None:
            return None
        text, stt_ms = self.transcriber.transcribe(audio)
        if not text:
            return None
        woke, rest = strip_wake_word(text, s.wake_words)
        in_followup = time.monotonic() < self.followup_until
        if not woke and not in_followup:
            print(f"  (mains libres, ignoré localement : « {text} »)", flush=True)
            return None
        if woke and not rest:  # « Jarvis. » seul : on ouvre la fenêtre d'écoute
            self.followup_until = time.monotonic() + s.followup_seconds
            self.sounds.play("listening")
            self.ui(hud.phase, "listening", "Je vous écoute…", s.followup_seconds)
            return None
        return ("text", rest if woke else text, {"audio_s": dur, "stt_ms": stt_ms})

    # ---------------------------------------------------------------- confirmation
    def _confirm(self, decision, args, steps) -> bool:
        s = self.config.settings
        preview = self.launcher.current or Outcome(transcript="", decision=decision, args=args, steps=steps)
        print(f"  confirmation demandée ({decision.reason}) — {s.confirm_timeout_seconds} s", flush=True)
        return self._await_answer(self.app.hud.ask_confirm, preview)

    def _confirm_plan(self, plan: PlanOutcome) -> bool:
        s = self.config.settings
        print(f"  confirmation demandée pour un plan de {len(plan.runnable)} étape(s) — "
              f"{s.confirm_timeout_seconds} s", flush=True)
        return self._await_answer(self.app.hud.ask_confirm_plan, plan)

    def _await_answer(self, show, subject) -> bool:
        """Bloque le thread moteur jusqu'à un clic, une réponse vocale ou l'expiration."""
        s = self.config.settings
        hud = self.app.hud
        while not self.answers.empty():
            self.answers.get_nowait()
        self.ui(show, subject, s, s.confirm_timeout_seconds, hotkey_label(s.hotkey))
        self.ui(self.app.set_icon, "confirm")
        self.sounds.play("listening")
        deadline = time.monotonic() + s.confirm_timeout_seconds
        shown = s.confirm_timeout_seconds
        while (left := deadline - time.monotonic()) > 0:
            if int(left) + 1 != shown:
                shown = int(left) + 1
                self.ui(hud.confirm_countdown, shown)
            try:
                return self.answers.get(timeout=min(0.1, left))
            except queue.Empty:
                pass
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                continue
            if job and job[0] in ("audio", "hands_free") and self.transcriber is not None:
                text, _ = self.transcriber.transcribe(job[1])
                verdict = parse_yes_no(text) if text else None
                print(f"  réponse vocale : {text!r} -> {verdict}")
                if verdict is not None:
                    return verdict
                self.ui(hud.confirm_countdown, shown)
            elif job is not None:
                self.deferred.append(job)  # traité après la confirmation
            else:
                self.deferred.append(None)  # demande d'arrêt : on annule et on sortira ensuite
                return False
        return False


# ----------------------------------------------------------------------------- menu + app
class MenuTarget(NSObject):
    def initWithApp_(self, app):
        self = objc.super(MenuTarget, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def setMode_(self, sender):
        self.app.engine.jobs.put(("mode", sender.representedObject()))

    def toggleDryRun_(self, sender):
        self.app.engine.jobs.put(("dry_run", not self.app.engine.dry_run))

    def toggleSound_(self, sender):
        self.app.engine.sounds.enabled = not self.app.engine.sounds.enabled
        self.app.rebuild_menu()

    def testPhrase_(self, sender):
        self.app.test_phrase()

    def showLast_(self, sender):
        self.app.show_last()

    def openSettings_(self, sender):
        self.app.open_settings()

    def openConfig_(self, sender):
        subprocess.Popen(["open", "-t", str(self.app.engine.config.path)])

    def reloadConfig_(self, sender):
        self.app.engine.jobs.put(("reload",))

    def toggleHandsFree_(self, sender):
        self.app.set_hands_free(not self.app.hands_free_on)

    def toggleQuiet_(self, sender):
        e = self.app.engine
        e.session.quiet = not e.launcher.settings.quiet_mode if e.launcher else not e.config.settings.quiet_mode
        e.session.save()
        self.app.rebuild_menu()

    def toggleSpeak_(self, sender):
        e = self.app.engine
        e.speak = not e.speak
        if e.launcher and e.launcher.executor:
            e.launcher.executor.speak_answers = e.speak
        self.app.rebuild_menu()

    def openJournal_(self, sender):
        from .context import JOURNAL

        if JOURNAL.exists():
            subprocess.Popen(["open", "-t", str(JOURNAL)])

    def openPrivacy_(self, sender):
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])

    def quit_(self, sender):
        self.app.quit()


class AppDelegate(NSObject):
    """Rouvrir voxjev (Spotlight, Finder, Dock) affiche son menu sous la souris.

    Utile quand macOS masque l'icône (barre des menus pleine, encoche, Réglages › Barre des menus).
    """

    def initWithApp_(self, app):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def applicationShouldHandleReopen_hasVisibleWindows_(self, nsapp, flag):
        self.app.popup_menu()
        return False


class GuiApp:
    def __init__(self, config: Config, client, session: Session, dry_run: bool, sound: bool):
        self.nsapp = NSApplication.sharedApplication()
        self.nsapp.setActivationPolicy_(ACCESSORY_POLICY)
        self.delegate = AppDelegate.alloc().initWithApp_(self)
        self.nsapp.setDelegate_(self.delegate)
        self.hud = HUD(on_confirm=lambda ok: self.engine.answers.put(ok))
        self.engine = Engine(self, config, client, session, dry_run, sound)
        self.history: collections.deque = collections.deque(maxlen=15)
        self.last: Outcome | None = None
        self.trusted = accessibility_trusted()
        self.target = MenuTarget.alloc().initWithApp_(self)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        # Position mémorisée par macOS (après un ⌘-glisser) ; toujours visible.
        self.item.setAutosaveName_("local.voxjev.statusitem")
        self.item.setVisible_(True)
        bundle = os.path.expanduser("~/Applications/voxjev.app")
        if os.path.exists(bundle):  # icône des fenêtres Réglages et des alertes (découpée par macOS)
            self.nsapp.setApplicationIconImage_(NSWorkspace.sharedWorkspace().iconForFile_(bundle))
        self.ptt: PushToTalk | None = None
        self.hands_free: HandsFree | None = None
        self.settings_window = None
        self.set_icon("loading")
        self.refresh_mode()

    # ---------------------------------------------------------------- barre des menus
    def set_icon(self, phase: str) -> None:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(PHASES[phase][1], "voxjev")
        if image is not None:
            image.setTemplate_(True)
            self.item.button().setImage_(image)
        else:
            self.item.button().setTitle_("🎙")

    def _add(self, menu, title: str, action: str | None, key: str = "", state: bool | None = None, obj=None):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            item.setTarget_(self.target)
        else:
            item.setEnabled_(False)
        if state is not None:
            item.setState_(1 if state else 0)
        if obj is not None:
            item.setRepresentedObject_(obj)
        menu.addItem_(item)
        return item

    def rebuild_menu(self) -> None:
        e = self.engine
        s = e.config.settings
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self._add(menu, f"voxjev — {s.model}", None)
        self._add(menu, f"Maintenir {hotkey_label(s.hotkey)} pour parler" if self.trusted
                  else "⚠️ Push-to-talk : autorisations manquantes", None)
        if not self.trusted:
            self._add(menu, "Ouvrir Réglages › Accessibilité…", "openPrivacy:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Mode", None)
        for name, mode in e.config.modes.items():
            self._add(menu, f"   {mode.description}", "setMode:", state=(name == e.session.mode), obj=name)
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Tester une phrase…", "testPhrase:", "t")
        self._add(menu, "Afficher le dernier résultat", "showLast:", "l")
        hist = NSMenu.alloc().init()
        hist.setAutoenablesItems_(False)
        if not self.history:
            self._add(hist, "(vide)", None)
        for when, out in reversed(self.history):
            mark = {"executed": "✓", "dry_run": "◌", "ignored": "·", "cancelled": "↩", "error": "✗"}.get(out.status, "·")
            cmd = out.command_id or "—"
            self._add(hist, f"{mark} {when}  « {out.transcript[:38]} » → {cmd}", None)
        sub = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Historique", None, "")
        sub.setSubmenu_(hist)
        menu.addItem_(sub)
        menu.addItem_(NSMenuItem.separatorItem())
        wake = ", ".join(w.capitalize() for w in s.wake_words)
        self._add(menu, f"Mains libres (dire « {wake}, … »)", "toggleHandsFree:", state=self.hands_free_on)
        quiet = e.launcher.settings.quiet_mode if e.launcher else s.quiet_mode
        self._add(menu, "Sans confirmation pour les actions sans risque", "toggleQuiet:", state=quiet)
        self._add(menu, "Dry-run (ne rien exécuter)", "toggleDryRun:", state=e.dry_run)
        self._add(menu, "Sons", "toggleSound:", state=e.sounds.enabled)
        self._add(menu, "Lire les réponses à voix haute", "toggleSpeak:", state=e.speak)
        self._add(menu, "Ouvrir le journal des actions", "openJournal:")
        self._add(menu, "Réglages…", "openSettings:", ",")
        self._add(menu, "Ouvrir la configuration (YAML)", "openConfig:")
        self._add(menu, "Recharger la configuration", "reloadConfig:", "r")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quitter voxjev", "quit:", "q")
        self.item.setMenu_(menu)

    @property
    def hands_free_on(self) -> bool:
        return bool(self.hands_free and self.hands_free.running)

    def set_hands_free(self, on: bool) -> None:
        if on and not self.hands_free_on:
            try:
                self.hands_free = HandsFree(
                    on_clip=lambda audio, dur: self.engine.jobs.put(("hands_free", audio, dur)),
                    on_level=lambda rms: AppHelper.callAfter(self.hud.level_push, rms),
                )
                self.hands_free.start()
            except Exception as exc:
                self.hud.show_message("Mains libres indisponible", str(exc), "error", 6.0)
                self.hands_free = None
            else:
                wake = self.engine.config.settings.wake_words[0].capitalize()
                self.hud.phase("idle", f"Mains libres : dites « {wake}, … »", 3.0)
        elif not on and self.hands_free_on:
            self.hands_free.stop()
            self.hud.phase("idle", "Mains libres désactivé", 1.5)
        self.rebuild_menu()

    def open_settings(self) -> None:
        from .settings import SettingsWindow

        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        self.settings_window.show()

    def restart_ptt(self, hotkey: str) -> None:
        """Nouvelle touche de parole, sans relancer l'app."""
        if self.ptt is None:
            return
        self.ptt.stop()
        try:
            self.ptt = PushToTalk(hotkey, on_start=self._on_press, on_clip=self._on_clip,
                                  on_level=lambda rms: AppHelper.callAfter(self.hud.level_push, rms))
            self.ptt.start()
            self.hud.phase("idle", f"Touche de parole : {hotkey_label(hotkey)}", 2.0)
        except ValueError as exc:
            self.hud.show_message("Touche invalide", str(exc), "error", 5.0)

    def popup_menu(self) -> None:
        """Affiche le menu de voxjev à l'emplacement de la souris."""
        from AppKit import NSEvent

        print("menu affiché sous la souris (app rouverte)", flush=True)
        self.rebuild_menu()
        self.nsapp.activateIgnoringOtherApps_(True)
        self.item.menu().popUpMenuPositioningItem_atLocation_inView_(None, NSEvent.mouseLocation(), None)

    def refresh_mode(self) -> None:
        self.hud.set_mode(self.engine.session.mode, self.engine.dry_run)
        self.rebuild_menu()

    def add_history(self, out: Outcome) -> None:
        self.last = out
        self.history.append((datetime.now().strftime("%H:%M"), out))
        self.rebuild_menu()

    def show_last(self) -> None:
        if isinstance(self.last, PlanOutcome):
            self.hud.show_plan(self.last, self.engine.config.settings)
        elif self.last:
            self.hud.show_outcome(self.last, self.engine.config.settings)

    def test_phrase(self) -> None:
        """Saisie texte. L'alerte prend le focus ; on le rend à l'app précédente avant l'appel Jev."""
        previous = NSWorkspace.sharedWorkspace().frontmostApplication()
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Tester une phrase")
        alert.setInformativeText_(f"Jev décidera comme si vous l'aviez dite (mode {self.engine.session.mode}).")
        alert.addButtonWithTitle_("Envoyer")
        alert.addButtonWithTitle_("Annuler")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
        field.setPlaceholderString_("ouvre chrome et cherche la météo à Lyon")
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)
        self.nsapp.activateIgnoringOtherApps_(True)
        response = alert.runModal()
        text = field.stringValue().strip()
        if previous is not None:
            previous.activateWithOptions_(ACTIVATE_IGNORING_OTHERS)
        if response == ALERT_FIRST_BUTTON and text:
            AppHelper.callLater(0.35, self.engine.jobs.put, ("text", text))

    # ---------------------------------------------------------------- cycle de vie
    def start(self, initial_text: str | None = None) -> None:
        s = self.engine.config.settings
        self.engine.start()
        if self.trusted:
            self.ptt = PushToTalk(
                s.hotkey,
                on_start=self._on_press,
                on_clip=self._on_clip,
                on_level=lambda rms: AppHelper.callAfter(self.hud.level_push, rms),
            )
            self.ptt.start()
        else:
            print(PERMISSION_HELP)
            if os.environ.get("VOXJEV_APP"):  # voxjev.app : fenêtres système de demande
                request_permissions()
        if s.hands_free:
            self.set_hands_free(True)
        if initial_text:
            self.engine.jobs.put(("text", initial_text))

    def _on_clip(self, audio, held) -> None:
        if self.hands_free is not None:
            self.hands_free.paused = False
        self.engine.jobs.put(("audio", audio, held))

    def _on_press(self) -> None:
        """Appelé depuis le thread clavier : tout passe par callAfter."""
        self.engine.sounds.play("listening")
        if self.hands_free is not None:
            self.hands_free.paused = True
        self.engine.jobs.put(("press",))
        if self.engine.launcher is not None:  # menus, Raccourcis, connexion Jev : prêts avant la fin de la phrase
            self.engine.launcher.prefetch()
        AppHelper.callAfter(self.set_icon, "listening")
        AppHelper.callAfter(self._listening_ui)

    def _listening_ui(self) -> None:
        if self.hud.hint.isHidden():
            self.hud.phase("listening", "Écoute…")
        else:  # pendant une confirmation, on garde le panneau et on écoute la réponse
            self.hud.confirm_listening()

    def quit(self) -> None:
        if self.ptt:
            self.ptt.stop()
        if self.hands_free_on:
            self.hands_free.stop()
        self.engine.jobs.put(None)
        AppHelper.stopEventLoop()


def run_gui(config: Config, client, session: Session, *, dry_run: bool = False, sound: bool = True,
            initial_text: str | None = None) -> int:
    app = GuiApp(config, client, session, dry_run, sound)
    app.start(initial_text)
    print(f"voxjev (interface) — mode {session.mode}. Icône dans la barre des menus ; Ctrl+C pour quitter.")
    AppHelper.runEventLoop(installInterrupt=True)
    return 0
