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
    NSAppearance,
    NSApplication,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBox,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSMakePoint,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSPanel,
    NSScreen,
    NSStatusBar,
    NSTextField,
    NSVariableStatusItemLength,
    NSView,
    NSVisualEffectView,
    NSWorkspace,
)
from Foundation import NSObject, NSString
from PyObjCTools import AppHelper

from .audio import PERMISSION_HELP, PushToTalk, accessibility_trusted, hotkey_label
from .cli import format_any
from .config import Config, load_config
from .context import Session
from .decide import explain, parse_yes_no
from .executor import Sounds, SubprocessExecutor
from .multi import MultiRunner, PlanOutcome, build_splitter
from .pipeline import Launcher, Outcome

# ----------------------------------------------------------------------------- constantes AppKit
BORDERLESS, NONACTIVATING = 0, 1 << 7
STATUS_WINDOW_LEVEL = 25
JOIN_ALL_SPACES, STATIONARY, FULLSCREEN_AUX = 1 << 0, 1 << 4, 1 << 8
ACCESSORY_POLICY = 1
HUD_MATERIAL, BEHIND_WINDOW, EFFECT_ACTIVE = 13, 0, 1
ALERT_FIRST_BUTTON = 1000
ACTIVATE_IGNORING_OTHERS = 1 << 1
W, PAD = 460.0, 16.0

SEMIBOLD, MEDIUM, REGULAR = 0.3, 0.23, 0.0


def rgb(r: int, g: int, b: int, a: float = 1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)


def white(alpha: float):
    return NSColor.whiteColor().colorWithAlphaComponent_(alpha)


GREEN, ORANGE, RED, BLUE = rgb(48, 209, 88), rgb(255, 159, 10), rgb(255, 69, 58), rgb(10, 132, 255)
PURPLE, TEAL, GRAY = rgb(191, 90, 242), rgb(100, 210, 255), rgb(142, 142, 147)

STATUS_STYLE = {  # statut -> (libellé de la pastille, couleur, durée d'affichage)
    "executed": ("EXÉCUTÉ", GREEN, 3.5),
    "dry_run": ("SIMULATION", TEAL, 6.0),
    "ignored": ("IGNORÉ", GRAY, 2.5),
    "cancelled": ("ANNULÉ", ORANGE, 3.0),
    "error": ("ERREUR", RED, 6.0),
    "confirm": ("CONFIRMER ?", ORANGE, 0),
}
PHASES = {  # phase -> (couleur du point, symbole SF de la barre des menus)
    "loading": (GRAY, "hourglass"),
    "idle": (GRAY, "waveform"),
    "listening": (RED, "mic.fill"),
    "transcribing": (BLUE, "text.bubble"),
    "thinking": (PURPLE, "sparkles"),
    "confirm": (ORANGE, "questionmark.circle"),
    "done": (GREEN, "waveform"),
    "error": (RED, "exclamationmark.triangle"),
}
PERMISSION_TEXT = (
    "Autorisez votre terminal dans Réglages › Confidentialité et sécurité › Accessibilité "
    "et › Surveillance de l'entrée, puis relancez. En attendant : menu › Tester une phrase…"
)


# ----------------------------------------------------------------------------- vues
class FlippedView(NSView):
    def isFlipped(self):
        return True


class LevelView(NSView):
    """Vumètre du micro : barres verticales centrées, historique glissant."""

    def initWithFrame_(self, frame):
        self = objc.super(LevelView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.levels = collections.deque([0.0] * 56, maxlen=56)
        return self

    def isFlipped(self):
        return True

    @objc.python_method
    def push(self, rms: float) -> None:
        self.levels.append(min(1.0, (rms / 0.12) ** 0.6))
        self.setNeedsDisplay_(True)

    @objc.python_method
    def reset(self) -> None:
        self.levels.extend([0.0] * self.levels.maxlen)
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        n = len(self.levels)
        step = b.size.width / n
        mid = b.size.height / 2
        RED.colorWithAlphaComponent_(0.85).setFill()
        for i, lv in enumerate(self.levels):
            h = max(2.0, lv * b.size.height)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(i * step + 1, mid - h / 2, max(1.5, step - 2), h), 1, 1).fill()


class BarsView(NSView):
    """Barres de probabilité : [(libellé, valeur 0..1, couleur)]."""

    LABEL_W, VALUE_W, ROW_H = 118.0, 40.0, 17.0

    def initWithFrame_(self, frame):
        self = objc.super(BarsView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.rows = []
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        width = self.bounds().size.width
        font = NSFont.systemFontOfSize_weight_(11, REGULAR)
        mono = NSFont.monospacedDigitSystemFontOfSize_weight_(11, MEDIUM)
        track_w = width - self.LABEL_W - self.VALUE_W - 8
        for i, (label, value, color) in enumerate(self.rows):
            y = i * self.ROW_H
            NSString.stringWithString_(label).drawAtPoint_withAttributes_(
                NSMakePoint(0, y), {NSFontAttributeName: font, NSForegroundColorAttributeName: white(0.78)})
            white(0.10).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(self.LABEL_W, y + 5, track_w, 6), 3, 3).fill()
            color.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(self.LABEL_W, y + 5, max(3.0, track_w * value), 6), 3, 3).fill()
            NSString.stringWithString_(f"{value:.2f}").drawAtPoint_withAttributes_(
                NSMakePoint(width - self.VALUE_W + 8, y),
                {NSFontAttributeName: mono, NSForegroundColorAttributeName: NSColor.labelColor()})


def make_label(size: float, weight: float = REGULAR, color=None, mono: bool = False, wrap: bool = False):
    tf = NSTextField.wrappingLabelWithString_("") if wrap else NSTextField.labelWithString_("")
    tf.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if mono
                else NSFont.systemFontOfSize_weight_(size, weight))
    tf.setTextColor_(color or NSColor.labelColor())
    tf.setSelectable_(False)
    if not wrap:
        tf.cell().setLineBreakMode_(4)  # troncature en fin de ligne
    return tf


def make_pill(size: float = 10):
    tf = make_label(size, SEMIBOLD, NSColor.whiteColor())
    tf.setAlignment_(1)  # centré
    tf.setWantsLayer_(True)
    tf.layer().setCornerRadius_(5)
    tf.layer().setMasksToBounds_(True)
    tf.setDrawsBackground_(True)
    return tf


class PillButton:
    """Bouton coloré dessiné à la main : les NSButton standards sont grisés dans un panneau
    sans focus (et notre panneau n'a jamais le focus, par conception)."""

    def __init__(self, title: str, color, target, action: str):
        self.box = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, 96, 30))
        self.box.setBoxType_(4)  # NSBoxCustom
        self.box.setTitlePosition_(0)  # pas de titre
        self.box.setBorderWidth_(0)
        self.box.setCornerRadius_(8)
        self.box.setFillColor_(color)
        self.button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 96, 30))
        self.button.setBordered_(False)
        self.button.setTarget_(target)
        self.button.setAction_(action)
        self.set_title(title)

    def set_title(self, title: str) -> None:
        self.button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            title, {NSFontAttributeName: NSFont.systemFontOfSize_weight_(13, SEMIBOLD),
                    NSForegroundColorAttributeName: NSColor.whiteColor()}))

    def set_color(self, color) -> None:
        self.box.setFillColor_(color)

    def set_armed(self, armed: bool) -> None:
        """Désarmé : semi-transparent et ignoré au clic (cf. HUD._click)."""
        for v in (self.box, self.button):
            v.setAlphaValue_(1.0 if armed else 0.35)

    def add_to(self, view) -> None:
        view.addSubview_(self.box)
        view.addSubview_(self.button)

    def setFrame_(self, frame) -> None:
        self.box.setFrame_(frame)
        self.button.setFrame_(frame)

    def setHidden_(self, hidden: bool) -> None:
        self.box.setHidden_(hidden)
        self.button.setHidden_(hidden)

    def isHidden(self) -> bool:
        return self.box.isHidden()


def text_height(tf, width: float) -> float:
    return float(tf.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 10_000)).height)


# ----------------------------------------------------------------------------- HUD
class ButtonTarget(NSObject):
    def initWithCallback_(self, cb):
        self = objc.super(ButtonTarget, self).init()
        if self is None:
            return None
        self.cb = cb
        return self

    def fire_(self, sender):
        self.cb()


class HUD:
    """Panneau flottant non-activant en haut de l'écran. Toutes les méthodes : thread principal."""

    def __init__(self, on_confirm):
        self._generation = 0
        self._shots = 0
        self._on_confirm = on_confirm
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, 120), BORDERLESS | NONACTIVATING, NSBackingStoreBuffered, False)
        p = self.panel
        p.setLevel_(STATUS_WINDOW_LEVEL)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setHidesOnDeactivate_(False)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setFloatingPanel_(True)
        p.setMovableByWindowBackground_(True)
        p.setCollectionBehavior_(JOIN_ALL_SPACES | STATIONARY | FULLSCREEN_AUX)
        p.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark"))

        effect = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 120))
        effect.setMaterial_(HUD_MATERIAL)
        effect.setBlendingMode_(BEHIND_WINDOW)
        effect.setState_(EFFECT_ACTIVE)
        effect.setWantsLayer_(True)
        effect.layer().setCornerRadius_(16)
        effect.layer().setMasksToBounds_(True)
        p.setContentView_(effect)
        self.root = FlippedView.alloc().initWithFrame_(effect.bounds())
        self.root.setAutoresizingMask_(18)  # largeur + hauteur
        effect.addSubview_(self.root)

        self.dot = make_label(11, REGULAR)
        self.dot.setStringValue_("●")
        self.status = make_label(12, SEMIBOLD, NSColor.secondaryLabelColor())
        self.mode_pill = make_pill()
        self.dry_pill = make_pill()
        self.dry_pill.setStringValue_(" DRY-RUN ")
        self.dry_pill.setBackgroundColor_(TEAL.colorWithAlphaComponent_(0.8))
        self.level = LevelView.alloc().initWithFrame_(NSMakeRect(PAD, 0, W - 2 * PAD, 26))
        self.transcript = make_label(16, SEMIBOLD, wrap=True)
        self.transcript.setMaximumNumberOfLines_(3)
        self.chip = make_pill(10.5)
        self.command = make_label(12.5, MEDIUM)
        self.detail = make_label(11.5, REGULAR, NSColor.secondaryLabelColor(), wrap=True)
        self.bars_title = make_label(10, SEMIBOLD, white(0.5))
        self.bars_title.setStringValue_("CE QUE JEV A COMPRIS")
        self.bars = BarsView.alloc().initWithFrame_(NSMakeRect(PAD, 0, W - 2 * PAD, 0))
        self.footer = make_label(10.5, REGULAR, white(0.55), mono=True)
        self.hint = make_label(11.5, MEDIUM, white(0.85))
        # Anti-clic accidentel : le HUD surgit sous le curseur, par-dessus l'app en cours.
        # Boutons inactifs pendant ARM_DELAY, et double clic pour une action destructrice.
        self._armed_at = 0.0
        self._destructive = False
        self._yes_primed = False
        self._yes_target = ButtonTarget.alloc().initWithCallback_(lambda: self._click(True))
        self._no_target = ButtonTarget.alloc().initWithCallback_(lambda: self._click(False))
        self.yes = PillButton("Exécuter", GREEN.colorWithAlphaComponent_(0.92), self._yes_target, "fire:")
        self.no = PillButton("Annuler", white(0.18), self._no_target, "fire:")
        for v in (self.dot, self.status, self.mode_pill, self.dry_pill, self.level, self.transcript, self.chip,
                  self.command, self.detail, self.bars_title, self.bars, self.footer, self.hint):
            self.root.addSubview_(v)
        self.no.add_to(self.root)
        self.yes.add_to(self.root)
        self._reset_content()

    ARM_DELAY = 1.2

    def _click(self, yes: bool) -> None:
        if time.monotonic() < self._armed_at:
            return  # trop tôt : probablement un clic destiné à l'app en dessous
        if yes and self._destructive and not self._yes_primed:
            self._yes_primed = True
            self.yes.set_title("Cliquer encore pour confirmer")
            self.yes.set_color(RED.colorWithAlphaComponent_(0.92))
            gen = self._generation
            AppHelper.callLater(4.0, self._unprime, gen)
            return
        self._on_confirm(yes)

    def _unprime(self, gen: int) -> None:
        if gen == self._generation and self._yes_primed:
            self._yes_primed = False
            self.yes.set_title("Exécuter…" if self._destructive else "Exécuter")
            self.yes.set_color(GREEN.colorWithAlphaComponent_(0.92))

    def _arm(self, gen: int) -> None:
        if gen == self._generation:
            self.yes.set_armed(True)
            self.no.set_armed(True)

    # ------------------------------------------------------------------ mise en page
    def _reset_content(self) -> None:
        for v in (self.level, self.transcript, self.chip, self.command, self.detail, self.bars_title,
                  self.bars, self.footer, self.hint, self.yes, self.no):
            v.setHidden_(True)
        self.bars.rows = []
        self.detail.setMaximumNumberOfLines_(3)

    def _layout(self) -> None:
        inner = W - 2 * PAD
        y = 14.0
        self.dot.setFrame_(NSMakeRect(PAD - 1, y + 1, 14, 16))
        right = W - PAD
        for pill in (self.dry_pill, self.mode_pill):
            if not pill.isHidden():
                w = pill.fittingSize().width + 4
                right -= w
                pill.setFrame_(NSMakeRect(right, y + 1, w, 16))
                right -= 6
        self.status.setFrame_(NSMakeRect(PAD + 16, y, right - PAD - 20, 18))
        y += 28
        if not self.level.isHidden():
            self.level.setFrame_(NSMakeRect(PAD, y, inner, 26))
            y += 34
        if not self.transcript.isHidden():
            h = text_height(self.transcript, inner)
            self.transcript.setFrame_(NSMakeRect(PAD, y, inner, h))
            y += h + 8
        if not self.chip.isHidden():
            w = self.chip.fittingSize().width + 6
            self.chip.setFrame_(NSMakeRect(PAD, y + 1, w, 17))
            self.command.setFrame_(NSMakeRect(PAD + w + 8, y, inner - w - 8, 19))
            y += 25
        if not self.detail.isHidden():
            h = text_height(self.detail, inner)
            self.detail.setFrame_(NSMakeRect(PAD, y, inner, h))
            y += h + 8
        if not self.bars.isHidden():
            self.bars_title.setHidden_(False)
            self.bars_title.setFrame_(NSMakeRect(PAD, y + 2, inner, 14))
            y += 18
            h = len(self.bars.rows) * BarsView.ROW_H
            self.bars.setFrame_(NSMakeRect(PAD, y + 2, inner, h))
            self.bars.setNeedsDisplay_(True)
            y += h + 10
        if not self.footer.isHidden():
            self.footer.setFrame_(NSMakeRect(PAD, y, inner, 14))
            y += 20
        if not self.hint.isHidden():
            self.hint.setFrame_(NSMakeRect(PAD, y, inner, 18))
            y += 26
            half = (inner - 10) / 2
            self.no.setFrame_(NSMakeRect(PAD, y, half, 32))
            self.yes.setFrame_(NSMakeRect(PAD + half + 10, y, half, 32))
            y += 42
        height = y + 6
        vis = NSScreen.mainScreen().visibleFrame()
        top = vis.origin.y + vis.size.height - 10
        self.panel.setFrame_display_(NSMakeRect(vis.origin.x + (vis.size.width - W) / 2, top - height, W, height), True)

    def _show(self, hide_after: float = 0) -> None:
        self._layout()
        self._generation += 1
        self.panel.setAlphaValue_(1.0)
        self.panel.orderFrontRegardless()
        if hide_after:
            gen = self._generation
            AppHelper.callLater(hide_after, self._hide_if, gen)
        if os.environ.get("VOXJEV_SNAPSHOT"):  # débogage : rendu du HUD en PNG
            AppHelper.callLater(0.2, self._snapshot)

    def _snapshot(self) -> None:
        view = self.panel.contentView()
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        self._shots += 1
        path = f"{os.environ['VOXJEV_SNAPSHOT']}/hud-{self._shots:02d}.png"
        rep.representationUsingType_properties_(4, None).writeToFile_atomically_(path, True)

    def _hide_if(self, gen: int) -> None:
        if gen == self._generation:
            self.panel.orderOut_(None)

    def hide(self) -> None:
        self._generation += 1
        self.panel.orderOut_(None)

    # ------------------------------------------------------------------ API
    def set_mode(self, mode: str, dry_run: bool) -> None:
        self.mode_pill.setStringValue_(f" {mode.upper()} ")
        self.mode_pill.setBackgroundColor_({"ctf": RED, "travail": BLUE}.get(mode, GRAY).colorWithAlphaComponent_(0.8))
        self.dry_pill.setHidden_(not dry_run)
        if self.panel.isVisible():
            self._layout()

    def phase(self, phase: str, text: str, hide_after: float = 0, keep_content: bool = False) -> None:
        self.dot.setTextColor_(PHASES[phase][0])
        self.status.setStringValue_(text)
        if not keep_content:
            self._reset_content()
        if phase == "listening":
            self.level.reset()
            self.level.setHidden_(False)
        else:
            self.level.setHidden_(True)
        self._show(hide_after)

    def level_push(self, rms: float) -> None:
        if not self.level.isHidden():
            self.level.push(rms)

    def show_transcript(self, text: str) -> None:
        self._reset_content()
        self.transcript.setStringValue_(f"« {text} »")
        self.transcript.setHidden_(False)
        self._show()

    def show_message(self, title: str, body: str, phase: str = "idle", hide_after: float = 0) -> None:
        self.phase(phase, title)
        self.detail.setStringValue_(body)
        self.detail.setHidden_(False)
        self._show(hide_after)

    def _fill_result(self, out: Outcome, s, dry_run: bool = False) -> None:
        r = out.result
        if out.transcript:
            self.transcript.setStringValue_(f"« {out.transcript} »")
            self.transcript.setHidden_(False)
        cmd = out.decision.command if out.decision else None
        p = r.p_command if r else 0.0
        if cmd:
            self.command.setStringValue_(cmd.short(out.args.values if out.args else None))
        elif out.decision:
            self.command.setStringValue_(explain(out.decision, p, dry_run))
        else:
            self.command.setStringValue_(out.error or "")
        self.command.setHidden_(False)
        details = []
        if out.args and out.args.values:
            details.append("   ·   ".join(f"{k} : {v}" for k, v in out.args.values.items()))
        if out.status == "cancelled":
            details.append("Annulé — rien n'a été exécuté.")
        elif out.decision and cmd:
            details.append(explain(out.decision, p, dry_run))
        if out.error and cmd:
            details.append(f"Erreur : {out.error}")
        if details:
            self.detail.setStringValue_("\n".join(details))
            self.detail.setHidden_(False)
        if r:
            ranked = sorted(r.probabilities.items(), key=lambda kv: -kv[1])[:3]
            rows = [("aucune commande" if cid == "none" else cid, prob, BLUE if cid == r.command else GRAY)
                    for cid, prob in ranked]
            adr_color = GREEN if r.addressed >= s.addressed_threshold else (
                ORANGE if r.addressed >= s.addressed_floor else RED)
            rows.append(("m'est adressé", r.addressed, adr_color))
            rows.append(("destructif", r.destructive, RED if r.destructive >= s.destructive_threshold else GRAY))
            self.bars.rows = rows
            self.bars.setHidden_(False)
        t = out.timings
        parts = []
        if "stt_ms" in t:
            parts.append(f"STT {t['stt_ms'] / 1000:.2f} s")
        if r:
            parts.append(f"Jev {r.latency_ms:.0f} ms")
        if "action_ms" in t:
            parts.append(f"action {t['action_ms']:.0f} ms")
        if "total_ms" in t:
            parts.append(f"total {t['total_ms'] / 1000:.2f} s")
        if parts:
            self.footer.setStringValue_("  ·  ".join(parts))
            self.footer.setHidden_(False)

    def _set_chip(self, key: str) -> None:
        label, color, _ = STATUS_STYLE[key]
        self.chip.setStringValue_(f" {label} ")
        self.chip.setBackgroundColor_(color.colorWithAlphaComponent_(0.9))
        self.chip.setHidden_(False)

    def show_outcome(self, out: Outcome, s) -> None:
        self._reset_content()
        key = out.status if out.status in STATUS_STYLE else "ignored"
        self._fill_result(out, s, dry_run=out.status == "dry_run")
        self._set_chip(key)
        phase = {"executed": "done", "error": "error"}.get(out.status, "idle")
        titles = {"executed": "Fait", "dry_run": "Simulation (rien n'a été exécuté)", "ignored": "Ignoré",
                  "cancelled": "Annulé", "error": "Échec"}
        self.dot.setTextColor_(PHASES[phase][0])
        self.status.setStringValue_(titles.get(out.status, out.status))
        self._show(STATUS_STYLE[key][2])

    def ask_confirm(self, out: Outcome, s, timeout: int, hotkey: str) -> None:
        self._reset_content()
        self._fill_result(out, s)
        self._ask(bool(out.decision and out.decision.destructive), timeout, hotkey)

    def ask_confirm_plan(self, plan, s, timeout: int, hotkey: str) -> None:
        self._reset_content()
        self._fill_plan(plan)
        self._ask(plan.destructive, timeout, hotkey)

    def _ask(self, destructive: bool, timeout: int, hotkey: str) -> None:
        self._set_chip("confirm")
        self.dot.setTextColor_(ORANGE)
        self.status.setStringValue_(f"Confirmation requise — {timeout} s")
        self._destructive = destructive
        self._yes_primed = False
        self.yes.set_title("Exécuter…" if self._destructive else "Exécuter")
        self.yes.set_color(GREEN.colorWithAlphaComponent_(0.92))
        self.hint.setStringValue_(f"Maintenez {hotkey} et dites « oui » ou « non », ou cliquez"
                                  + (" deux fois" if self._destructive else ""))
        for v in (self.hint, self.yes, self.no):
            v.setHidden_(False)
        self.yes.set_armed(False)
        self.no.set_armed(False)
        self._armed_at = time.monotonic() + self.ARM_DELAY
        self._show()
        AppHelper.callLater(self.ARM_DELAY, self._arm, self._generation)

    PLAN_MARKS = {"executed": "✓", "dry_run": "◌", "planned": "•", "ignored": "✗", "skipped": "–",
                  "error": "✗", "cancelled": "–"}

    def _fill_plan(self, plan) -> None:
        self.transcript.setStringValue_(f"« {plan.transcript} »")
        self.transcript.setHidden_(False)
        n = len(plan.runnable)
        self.command.setStringValue_(f"Plan en {n} étape{'s' if n > 1 else ''}")
        self.command.setHidden_(False)
        lines = []
        for i, o in enumerate(plan.items, 1):
            mark = self.PLAN_MARKS.get(o.status, "•")
            cmd = o.decision.command if o.decision else None
            if cmd and o.status != "ignored":
                lines.append(f"{mark} {i}. {cmd.short(o.args.values if o.args else None)}")
            else:
                lines.append(f"{mark} {i}. « {o.transcript} » : pas une commande, ignoré")
            if o.error:
                lines.append(f"      Erreur : {o.error}")
        if plan.error and not any(o.error for o in plan.items):
            lines.append(f"Erreur : {plan.error}")
        self.detail.setMaximumNumberOfLines_(10)
        self.detail.setStringValue_("\n".join(lines))
        self.detail.setHidden_(False)
        t = plan.timings
        parts = [f"Jev {t.get('jev_detect_ms', 0):.0f} ms", f"découpage ({plan.splitter}) {t.get('split_ms', 0):.0f} ms",
                 f"plan {t.get('plan_ms', 0):.0f} ms"]
        if "total_ms" in t:
            parts.append(f"total {t['total_ms'] / 1000:.2f} s")
        self.footer.setStringValue_("  ·  ".join(parts))
        self.footer.setHidden_(False)

    def show_plan(self, plan, s) -> None:
        self._reset_content()
        self._fill_plan(plan)
        key = plan.status if plan.status in STATUS_STYLE else "ignored"
        self._set_chip(key)
        titles = {"executed": "Plan exécuté", "dry_run": "Simulation du plan (rien n'a été exécuté)",
                  "ignored": "Ignoré", "cancelled": "Plan annulé", "error": "Plan non exécuté"}
        self.dot.setTextColor_(PHASES[{"executed": "done", "error": "error"}.get(plan.status, "idle")][0])
        self.status.setStringValue_(titles.get(plan.status, plan.status))
        self._show(STATUS_STYLE[key][2] + 2)

    def confirm_countdown(self, left: int) -> None:
        if not self.hint.isHidden():
            self.level.setHidden_(True)
            self.status.setStringValue_(f"Confirmation requise — {left} s")
            self._layout()

    def confirm_listening(self) -> None:
        self.status.setStringValue_("Écoute de votre réponse…")
        self.level.reset()
        self.level.setHidden_(False)
        self._layout()


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
        self.sounds = Sounds(config.settings.sounds, enabled=sound)
        self.transcriber = None
        self.launcher: Launcher | None = None

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
        self.launcher = Launcher(self.config, self.client, self.session, executor=SubprocessExecutor(),
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
            job = self.deferred.pop(0) if self.deferred else self.jobs.get()
            if job is None:
                return
            try:
                self._handle(job)
            except Exception as exc:  # ne jamais tuer le thread moteur
                print(f"erreur interne : {exc!r}")
                self.ui(hud.show_message, "Erreur interne", repr(exc), "error", 6.0)
                self.ui(self.app.set_icon, "error")

    # ---------------------------------------------------------------- jobs
    def _handle(self, job) -> None:
        kind = job[0]
        hud, s = self.app.hud, self.config.settings
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
            self.launcher.config = self.config
            if self.session.mode not in self.config.modes:
                self.session.mode = self.config.default_mode
            self.ui(self.app.rebuild_menu)
            self.ui(hud.phase, "idle", "Configuration rechargée", 1.5)
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
            text, stt_ms = self.transcriber.transcribe(audio)
            timings = {"audio_s": held, "stt_ms": stt_ms}
            if not text:
                print(f"(rien compris — {held:.1f} s d'audio)", flush=True)
                self.sounds.play("ignored")
                self.ui(hud.phase, "idle", "Rien compris", 1.5)
                self.ui(self.app.set_icon, "idle")
                return
        else:
            text = job[1]
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
            if job and job[0] == "audio" and self.transcriber is not None:
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

    def openConfig_(self, sender):
        subprocess.Popen(["open", "-t", str(self.app.engine.config.path)])

    def reloadConfig_(self, sender):
        self.app.engine.jobs.put(("reload",))

    def openPrivacy_(self, sender):
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])

    def quit_(self, sender):
        self.app.quit()


class GuiApp:
    def __init__(self, config: Config, client, session: Session, dry_run: bool, sound: bool):
        self.nsapp = NSApplication.sharedApplication()
        self.nsapp.setActivationPolicy_(ACCESSORY_POLICY)
        self.hud = HUD(on_confirm=lambda ok: self.engine.answers.put(ok))
        self.engine = Engine(self, config, client, session, dry_run, sound)
        self.history: collections.deque = collections.deque(maxlen=15)
        self.last: Outcome | None = None
        self.trusted = accessibility_trusted()
        self.target = MenuTarget.alloc().initWithApp_(self)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.ptt: PushToTalk | None = None
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
        self._add(menu, "Dry-run (ne rien exécuter)", "toggleDryRun:", state=e.dry_run)
        self._add(menu, "Sons", "toggleSound:", state=e.sounds.enabled)
        self._add(menu, "Ouvrir la configuration", "openConfig:", ",")
        self._add(menu, "Recharger la configuration", "reloadConfig:", "r")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quitter voxjev", "quit:", "q")
        self.item.setMenu_(menu)

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
                on_clip=lambda audio, held: self.engine.jobs.put(("audio", audio, held)),
                on_level=lambda rms: AppHelper.callAfter(self.hud.level_push, rms),
            )
            self.ptt.start()
        else:
            print(PERMISSION_HELP)
        if initial_text:
            self.engine.jobs.put(("text", initial_text))

    def _on_press(self) -> None:
        """Appelé depuis le thread clavier : tout passe par callAfter."""
        self.engine.sounds.play("listening")
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
        self.engine.jobs.put(None)
        AppHelper.stopEventLoop()


def run_gui(config: Config, client, session: Session, *, dry_run: bool = False, sound: bool = True,
            initial_text: str | None = None) -> int:
    app = GuiApp(config, client, session, dry_run, sound)
    app.start(initial_text)
    print(f"voxjev (interface) — mode {session.mode}. Icône dans la barre des menus ; Ctrl+C pour quitter.")
    AppHelper.runEventLoop(installInterrupt=True)
    return 0
