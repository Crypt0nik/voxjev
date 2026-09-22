"""HUD de voxjev en Liquid Glass (macOS 26) : capsule compacte pendant l'écoute, carte pour les résultats.

- Verre : ``NSGlassEffectView`` (repli : ``NSVisualEffectView`` avant macOS 26) ; suit le thème clair/sombre.
- Capsule → carte : la fenêtre se transforme avec une animation (ressort doux) ; apparition en
  glissant depuis le haut, disparition en fondu.
- Orbe animée façon Siri (dégradé conique en rotation, pulsation sur la voix) pendant l'écoute,
  la transcription et la réflexion ; symboles SF animés ensuite (coche dessinée, alerte, question).
- Anneau de compte à rebours pour les confirmations ; boutons en capsule.
- « Réduire les animations » (Accessibilité) est respecté.

Le panneau reste NON-ACTIVANT : il ne prend jamais le focus (l'app au premier plan reste la vôtre).
Toutes les méthodes publiques : thread principal uniquement.
"""

from __future__ import annotations

import collections
import math
import os
import time

import objc
import Quartz
from AppKit import (
    NSAnimationContext,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontDescriptorSystemDesignRounded,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageView,
    NSKernAttributeName,
    NSMakePoint,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSString,
    NSTextField,
    NSView,
    NSVisualEffectView,
    NSWorkspace,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

try:  # macOS 26
    from AppKit import NSGlassEffectView
except ImportError:  # pragma: no cover
    NSGlassEffectView = None

from .decide import explain

# ----------------------------------------------------------------------------- constantes
BORDERLESS, NONACTIVATING = 0, 1 << 7
STATUS_WINDOW_LEVEL = 25
JOIN_ALL_SPACES, STATIONARY, FULLSCREEN_AUX = 1 << 0, 1 << 4, 1 << 8
COMPACT_W, COMPACT_H, CARD_W = 340.0, 52.0, 460.0
PAD = 18.0
TOP_GAP = 8.0
SEMIBOLD, MEDIUM, REGULAR, BOLD = 0.3, 0.23, 0.0, 0.4


def rgb(r: int, g: int, b: int, a: float = 1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)


def cg(r: int, g: int, b: int, a: float = 1.0):
    return Quartz.CGColorCreateSRGB(r / 255, g / 255, b / 255, a)


GREEN, ORANGE, RED, BLUE = rgb(48, 209, 88), rgb(255, 159, 10), rgb(255, 69, 58), rgb(10, 132, 255)
PURPLE, TEAL, GRAY, PINK, INDIGO = rgb(191, 90, 242), rgb(100, 210, 255), rgb(142, 142, 147), rgb(255, 55, 95), rgb(94, 92, 230)

STATUS_STYLE = {  # statut -> (libellé, couleur, durée d'affichage, symbole SF)
    "executed": ("Exécuté", GREEN, 3.5, "checkmark.circle.fill"),
    "dry_run": ("Simulation", TEAL, 6.0, "eye.circle.fill"),
    "ignored": ("Ignoré", GRAY, 2.5, "minus.circle.fill"),
    "cancelled": ("Annulé", ORANGE, 3.0, "arrow.uturn.backward.circle.fill"),
    "error": ("Erreur", RED, 6.0, "exclamationmark.triangle.fill"),
    "confirm": ("Confirmer ?", ORANGE, 0, "questionmark.circle.fill"),
}
PHASES = {  # phase -> (couleur, symbole SF de la barre des menus)
    "loading": (GRAY, "hourglass"),
    "idle": (GRAY, "waveform"),
    "listening": (RED, "mic.fill"),
    "transcribing": (BLUE, "text.bubble"),
    "thinking": (PURPLE, "sparkles"),
    "confirm": (ORANGE, "questionmark.circle"),
    "done": (GREEN, "waveform"),
    "error": (RED, "exclamationmark.triangle"),
}
ORB_PALETTES = {  # dégradés de l'orbe
    "listening": [(255, 55, 95), (255, 149, 0), (255, 45, 85), (191, 90, 242), (255, 55, 95)],
    "transcribing": [(10, 132, 255), (100, 210, 255), (94, 92, 230), (10, 132, 255)],
    "thinking": [(10, 132, 255), (191, 90, 242), (255, 55, 95), (255, 159, 10), (10, 132, 255)],
    "loading": [(142, 142, 147), (200, 200, 205), (142, 142, 147)],
}
PHASE_SYMBOL = {"idle": ("waveform", GRAY), "done": ("checkmark.circle.fill", GREEN),
                "error": ("exclamationmark.triangle.fill", RED), "confirm": ("questionmark.circle.fill", ORANGE)}


def reduce_motion() -> bool:
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


def font(size: float, weight: float = REGULAR, rounded: bool = False, mono: bool = False):
    if mono:
        return NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    f = NSFont.systemFontOfSize_weight_(size, weight)
    if rounded:
        desc = f.fontDescriptor().fontDescriptorWithDesign_(NSFontDescriptorSystemDesignRounded)
        if desc is not None:
            f = NSFont.fontWithDescriptor_size_(desc, size) or f
    return f


def label(size: float, weight: float = REGULAR, color=None, *, wrap: bool = False, rounded: bool = False,
          mono: bool = False, lines: int = 0):
    tf = NSTextField.wrappingLabelWithString_("") if wrap else NSTextField.labelWithString_("")
    tf.setFont_(font(size, weight, rounded, mono))
    tf.setTextColor_(color or NSColor.labelColor())
    tf.setSelectable_(False)
    if wrap:
        tf.setMaximumNumberOfLines_(lines)
    else:
        tf.cell().setLineBreakMode_(4)
    return tf


def text_height(tf, width: float) -> float:
    return float(tf.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 10_000)).height)


def symbol_image(name: str, size: float, weight: float = SEMIBOLD, color=None):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
    if color is not None:
        config = config.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color))
    return img.imageWithSymbolConfiguration_(config)


def animate(duration: float, changes, completion=None, curve=(0.2, 0.9, 0.25, 1.0)) -> None:
    """Groupe d'animation AppKit (ressort doux par défaut)."""
    if reduce_motion():
        duration = min(duration, 0.12)

    def group(ctx):
        ctx.setDuration_(duration)
        ctx.setAllowsImplicitAnimation_(True)
        ctx.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(*curve))
        changes()

    NSAnimationContext.runAnimationGroup_completionHandler_(group, completion)


# ----------------------------------------------------------------------------- vues
class FlippedView(NSView):
    def isFlipped(self):
        return True


class Orb(NSView):
    """Orbe façon Siri : dégradé conique en rotation, pulsation sur le niveau de la voix.
    Dans les autres états, un symbole SF animé prend sa place."""

    def initWithFrame_(self, frame):
        self = objc.super(Orb, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        size = frame.size.width
        root = self.layer()
        root.setMasksToBounds_(False)
        self.scaler = Quartz.CALayer.layer()
        self.scaler.setFrame_(((0, 0), (size, size)))
        self.gradient = Quartz.CAGradientLayer.layer()
        self.gradient.setType_("conic")
        self.gradient.setStartPoint_((0.5, 0.5))
        self.gradient.setEndPoint_((0.5, 0.0))
        self.gradient.setFrame_(((0, 0), (size, size)))
        self.gradient.setCornerRadius_(size / 2)
        self.gradient.setMasksToBounds_(True)
        self.scaler.addSublayer_(self.gradient)
        self.scaler.setShadowOpacity_(0.85)
        self.scaler.setShadowRadius_(7)
        self.scaler.setShadowOffset_((0, 0))
        self.scaler.setShadowPath_(Quartz.CGPathCreateWithEllipseInRect(((0, 0), (size, size)), None))
        root.addSublayer_(self.scaler)
        self.symbol = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
        self.symbol.setImageScaling_(3)  # proportionnel, centré
        self.addSubview_(self.symbol)
        self.state = None
        return self

    @objc.python_method
    def set_state(self, phase: str) -> None:
        if phase == self.state:
            return
        self.state = phase
        palette = ORB_PALETTES.get(phase)
        if palette:
            self.symbol.setHidden_(True)
            self.scaler.setHidden_(False)
            self.gradient.setColors_([cg(*c) for c in palette])
            self.scaler.setShadowColor_(cg(*palette[1], 0.9))
            speed = {"listening": 3.2, "transcribing": 1.6, "thinking": 1.1, "loading": 2.5}[phase]
            self.gradient.removeAllAnimations()
            if not reduce_motion():
                spin = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
                spin.setFromValue_(0.0)
                spin.setToValue_(-2 * math.pi)
                spin.setDuration_(speed)
                spin.setRepeatCount_(1e9)
                self.gradient.addAnimation_forKey_(spin, "spin")
                if phase in ("thinking", "transcribing"):
                    breathe = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
                    breathe.setFromValue_(0.86)
                    breathe.setToValue_(1.04)
                    breathe.setDuration_(0.9)
                    breathe.setAutoreverses_(True)
                    breathe.setRepeatCount_(1e9)
                    breathe.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_("easeInEaseOut"))
                    self.scaler.addAnimation_forKey_(breathe, "breathe")
                else:
                    self.scaler.removeAnimationForKey_("breathe")
            return
        self.gradient.removeAllAnimations()
        self.scaler.removeAllAnimations()
        self.scaler.setHidden_(True)
        name, color = PHASE_SYMBOL.get(phase, PHASE_SYMBOL["idle"])
        self.show_symbol(name, color, effect={"done": "draw", "error": "wiggle", "confirm": "pulse"}.get(phase))

    @objc.python_method
    def show_symbol(self, name: str, color, effect: str | None = None) -> None:
        self.scaler.setHidden_(True)
        self.symbol.setHidden_(False)
        self.symbol.removeAllSymbolEffects()
        self.symbol.setImage_(symbol_image(name, 20, SEMIBOLD, color))
        self.symbol.setContentTintColor_(color)
        if not effect or reduce_motion():
            return
        try:
            import AppKit

            options = AppKit.NSSymbolEffectOptions.options()
            if effect == "draw" and hasattr(AppKit, "NSSymbolDrawOnEffect"):
                self.symbol.addSymbolEffect_options_animated_(AppKit.NSSymbolDrawOnEffect.effect(), options, True)
            elif effect == "wiggle":
                self.symbol.addSymbolEffect_options_animated_(AppKit.NSSymbolWiggleEffect.effect(), options, True)
            elif effect == "pulse":
                self.symbol.addSymbolEffect_options_animated_(
                    AppKit.NSSymbolPulseEffect.effect(), AppKit.NSSymbolEffectOptions.optionsWithRepeating(), True)
            else:
                self.symbol.addSymbolEffect_options_animated_(AppKit.NSSymbolBounceEffect.effect(), options, True)
        except Exception:
            pass

    @objc.python_method
    def level(self, value: float) -> None:
        """Pulsation de l'orbe sur la voix (0..1)."""
        if self.state != "listening":
            return
        s = 0.82 + 0.45 * value
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.08)
        size = self.bounds().size.width
        t = Quartz.CATransform3DTranslate(Quartz.CATransform3DIdentity, size / 2, size / 2, 0)
        t = Quartz.CATransform3DScale(t, s, s, 1)
        t = Quartz.CATransform3DTranslate(t, -size / 2, -size / 2, 0)
        self.scaler.setTransform_(t)
        Quartz.CATransaction.commit()


class Wave(NSView):
    """Onde sonore : barres symétriques arrondies, lissées, qui s'éteignent doucement."""

    N = 22

    def initWithFrame_(self, frame):
        self = objc.super(Wave, self).initWithFrame_(frame)
        if self is None:
            return None
        self.levels = collections.deque([0.0] * self.N, maxlen=self.N)
        self.color = RED
        return self

    def isFlipped(self):
        return True

    @objc.python_method
    def push(self, rms: float) -> float:
        v = min(1.0, (rms / 0.10) ** 0.6)
        prev = self.levels[-1]
        v = prev * 0.35 + v * 0.65
        self.levels.append(v)
        self.setNeedsDisplay_(True)
        return v

    @objc.python_method
    def reset(self) -> None:
        self.levels.extend([0.0] * self.N)
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        step = b.size.width / self.N
        bar = max(2.0, step * 0.55)
        mid = b.size.height / 2
        for i, lv in enumerate(self.levels):
            # enveloppe : plus haut au centre, comme les ondes de Siri
            env = 0.55 + 0.45 * math.sin(math.pi * (i + 0.5) / self.N)
            h = max(bar, lv * env * b.size.height)
            alpha = 0.35 + 0.65 * min(1.0, lv * 1.6)
            self.color.colorWithAlphaComponent_(alpha).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(i * step + (step - bar) / 2, mid - h / 2, bar, h), bar / 2, bar / 2).fill()


class Ring(NSView):
    """Anneau de compte à rebours (confirmation) avec les secondes au centre."""

    def initWithFrame_(self, frame):
        self = objc.super(Ring, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        size = frame.size.width
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathAddArc(path, None, size / 2, size / 2, size / 2 - 2, math.pi / 2, math.pi / 2 - 2 * math.pi, True)
        self.track = Quartz.CAShapeLayer.layer()
        self.track.setPath_(path)
        self.track.setFillColor_(None)
        self.track.setStrokeColor_(cg(142, 142, 147, 0.25))
        self.track.setLineWidth_(2.5)
        self.arc = Quartz.CAShapeLayer.layer()
        self.arc.setPath_(path)
        self.arc.setFillColor_(None)
        self.arc.setStrokeColor_(cg(255, 159, 10))
        self.arc.setLineWidth_(2.5)
        self.arc.setLineCap_("round")
        self.layer().addSublayer_(self.track)
        self.layer().addSublayer_(self.arc)
        self.text = label(10, SEMIBOLD, ORANGE, mono=True)
        self.text.setAlignment_(1)
        self.text.setFrame_(NSMakeRect(0, (size - 13) / 2, size, 13))
        self.addSubview_(self.text)
        return self

    @objc.python_method
    def start(self, seconds: int, color=ORANGE) -> None:
        self.arc.setStrokeColor_(color.CGColor())
        self.text.setTextColor_(color)
        self.text.setStringValue_(str(seconds))
        self.arc.removeAllAnimations()
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("strokeEnd")
        anim.setFromValue_(1.0)
        anim.setToValue_(0.0)
        anim.setDuration_(float(seconds))
        anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_("linear"))
        self.arc.setStrokeEnd_(0.0)
        self.arc.addAnimation_forKey_(anim, "countdown")

    @objc.python_method
    def set_seconds(self, left: int) -> None:
        self.text.setStringValue_(str(left))


class Chip:
    """Pastille teintée (fond coloré léger, texte et symbole de la couleur)."""

    def __init__(self, size: float = 11.0):
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 60, 22))
        self.view.setWantsLayer_(True)
        self.view.layer().setCornerRadius_(11)
        self.icon = NSImageView.alloc().initWithFrame_(NSMakeRect(8, 4, 14, 14))
        self.text = label(size, SEMIBOLD, rounded=True)
        self.view.addSubview_(self.icon)
        self.view.addSubview_(self.text)
        self.size = size

    def set(self, text: str, color, symbol: str | None = None) -> None:
        self.text.setStringValue_(text)
        self.text.setTextColor_(color)
        self.view.layer().setBackgroundColor_(color.colorWithAlphaComponent_(0.18).CGColor())
        img = symbol_image(symbol, self.size, SEMIBOLD, color) if symbol else None
        self.icon.setImage_(img)
        self.icon.setHidden_(img is None)

    def width(self) -> float:
        w = self.text.fittingSize().width
        return w + (40 if not self.icon.isHidden() else 22)

    def place(self, x: float, y: float, h: float = 22) -> float:
        w = self.width()
        self.view.setFrame_(NSMakeRect(x, y, w, h))
        self.view.layer().setCornerRadius_(h / 2)
        icon = not self.icon.isHidden()
        self.icon.setFrame_(NSMakeRect(9, (h - 14) / 2, 14, 14))
        tw, th = self.text.fittingSize().width, self.text.fittingSize().height
        self.text.setFrame_(NSMakeRect(28 if icon else 11, (h - th) / 2, tw + 2, th))
        return w

    def setHidden_(self, hidden: bool) -> None:
        self.view.setHidden_(hidden)

    def isHidden(self) -> bool:
        return self.view.isHidden()


class ButtonTarget(NSObject):
    def initWithCallback_(self, cb):
        self = objc.super(ButtonTarget, self).init()
        if self is None:
            return None
        self.cb = cb
        return self

    def fire_(self, sender):
        self.cb()


class CapsuleButton:
    """Bouton en capsule dessiné à la main : les NSButton standards sont grisés dans un
    panneau qui n'a jamais le focus (c'est voulu : il ne vole pas le clavier)."""

    def __init__(self, title: str, target, action: str, prominent: bool):
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 120, 36))
        self.view.setWantsLayer_(True)
        self.button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 120, 36))
        self.button.setBordered_(False)
        self.button.setTarget_(target)
        self.button.setAction_(action)
        self.view.addSubview_(self.button)
        self.prominent = prominent
        self.set_style(title, GREEN if prominent else None)

    def set_style(self, title: str, color=None) -> None:
        fg = NSColor.whiteColor() if color is not None else NSColor.labelColor()
        bg = color.colorWithAlphaComponent_(0.92) if color is not None else NSColor.labelColor().colorWithAlphaComponent_(0.10)
        self.view.layer().setBackgroundColor_(bg.CGColor())
        self.button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            title, {NSFontAttributeName: font(13.5, SEMIBOLD, rounded=True), NSForegroundColorAttributeName: fg}))

    def set_armed(self, armed: bool) -> None:
        animate(0.25, lambda: self.view.animator().setAlphaValue_(1.0 if armed else 0.4))

    def setFrame_(self, frame) -> None:
        self.view.setFrame_(frame)
        self.view.layer().setCornerRadius_(frame.size.height / 2)
        self.button.setFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))

    def setHidden_(self, hidden: bool) -> None:
        self.view.setHidden_(hidden)

    def isHidden(self) -> bool:
        return self.view.isHidden()


class Insights(NSView):
    """« Ce que Jev a compris » : barres fines [(libellé, valeur, couleur)]."""

    ROW_H = 19.0
    LABEL_W, VALUE_W = 124.0, 36.0

    def initWithFrame_(self, frame):
        self = objc.super(Insights, self).initWithFrame_(frame)
        if self is None:
            return None
        self.rows = []
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        width = self.bounds().size.width
        f = font(11.5, REGULAR)
        mono = font(10.5, MEDIUM, mono=True)
        track_w = width - self.LABEL_W - self.VALUE_W - 6
        secondary = NSColor.secondaryLabelColor()
        for i, (text, value, color) in enumerate(self.rows):
            y = i * self.ROW_H
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                NSMakePoint(0, y + 1), {NSFontAttributeName: f, NSForegroundColorAttributeName: secondary})
            NSColor.labelColor().colorWithAlphaComponent_(0.08).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(self.LABEL_W, y + 7, track_w, 4), 2, 2).fill()
            color.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(self.LABEL_W, y + 7, max(4.0, track_w * value), 4), 2, 2).fill()
            NSString.stringWithString_(f"{value * 100:.0f} %").drawAtPoint_withAttributes_(
                NSMakePoint(width - self.VALUE_W + 4, y + 1),
                {NSFontAttributeName: mono, NSForegroundColorAttributeName: NSColor.tertiaryLabelColor()})


# ----------------------------------------------------------------------------- HUD
class HUD:
    """Capsule / carte en Liquid Glass en haut de l'écran. Thread principal uniquement."""

    ARM_DELAY = 1.2
    PLAN_MARKS = {"executed": "✓", "dry_run": "◌", "planned": "•", "ignored": "✗", "skipped": "–",
                  "error": "✗", "cancelled": "–"}

    def __init__(self, on_confirm):
        self._generation = 0
        self._shots = 0
        self._on_confirm = on_confirm
        self._compact = True
        self._mode = "defaut"
        self._dry = False
        p = self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, COMPACT_W, COMPACT_H), BORDERLESS | NONACTIVATING, NSBackingStoreBuffered, False)
        p.setLevel_(STATUS_WINDOW_LEVEL)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setHidesOnDeactivate_(False)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setFloatingPanel_(True)
        p.setMovableByWindowBackground_(True)
        p.setCollectionBehavior_(JOIN_ALL_SPACES | STATIONARY | FULLSCREEN_AUX)

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COMPACT_W, COMPACT_H))
        container.setWantsLayer_(True)
        p.setContentView_(container)
        self.root = FlippedView.alloc().initWithFrame_(container.bounds())
        self.root.setAutoresizingMask_(18)
        if NSGlassEffectView is not None:
            self.glass = NSGlassEffectView.alloc().initWithFrame_(container.bounds())
            self.glass.setCornerRadius_(COMPACT_H / 2)
            self.glass.setContentView_(self.root)
        else:  # avant macOS 26
            self.glass = NSVisualEffectView.alloc().initWithFrame_(container.bounds())
            self.glass.setMaterial_(13)
            self.glass.setBlendingMode_(0)
            self.glass.setState_(1)
            self.glass.setWantsLayer_(True)
            self.glass.layer().setCornerRadius_(COMPACT_H / 2)
            self.glass.layer().setMasksToBounds_(True)
            self.glass.addSubview_(self.root)
        self.glass.setAutoresizingMask_(18)
        container.addSubview_(self.glass)

        # en-tête
        self.orb = Orb.alloc().initWithFrame_(NSMakeRect(0, 0, 26, 26))
        self.status = label(14, SEMIBOLD, rounded=True)
        self.wave = Wave.alloc().initWithFrame_(NSMakeRect(0, 0, 92, 24))
        self.ring = Ring.alloc().initWithFrame_(NSMakeRect(0, 0, 26, 26))
        self.mode_chip = Chip(10)
        self.dry_chip = Chip(10)
        self.dry_chip.set("DRY-RUN", TEAL, "eye")
        # contenu de la carte
        self.transcript = label(17, SEMIBOLD, wrap=True, rounded=True, lines=3)
        self.chip = Chip(11.5)
        self.command = label(13, MEDIUM)
        self.detail = label(12.5, REGULAR, NSColor.secondaryLabelColor(), wrap=True, lines=4)
        self.insights_title = label(10, SEMIBOLD, NSColor.tertiaryLabelColor())
        self.insights_title.setAttributedStringValue_(NSAttributedString.alloc().initWithString_attributes_(
            "CE QUE JEV A COMPRIS", {NSFontAttributeName: font(10, SEMIBOLD), NSKernAttributeName: 0.8,
                                     NSForegroundColorAttributeName: NSColor.tertiaryLabelColor()}))
        self.bars = Insights.alloc().initWithFrame_(NSMakeRect(0, 0, CARD_W - 2 * PAD, 0))
        self.separator = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 1))
        self.separator.setWantsLayer_(True)
        self.footer = label(10.5, REGULAR, NSColor.tertiaryLabelColor(), mono=True)
        self.hint = label(12, MEDIUM, NSColor.secondaryLabelColor(), wrap=True, lines=2)
        # boutons (anti-clic accidentel : inactifs ARM_DELAY, double clic si destructeur)
        self._armed_at = 0.0
        self._destructive = False
        self._yes_primed = False
        self._yes_target = ButtonTarget.alloc().initWithCallback_(lambda: self._click(True))
        self._no_target = ButtonTarget.alloc().initWithCallback_(lambda: self._click(False))
        self.yes = CapsuleButton("Exécuter", self._yes_target, "fire:", prominent=True)
        self.no = CapsuleButton("Annuler", self._no_target, "fire:", prominent=False)

        for v in (self.orb, self.status, self.wave, self.ring, self.mode_chip.view, self.dry_chip.view,
                  self.transcript, self.chip.view, self.command, self.detail, self.separator,
                  self.insights_title, self.bars, self.footer, self.hint, self.no.view, self.yes.view):
            self.root.addSubview_(v)
        self._reset_content()
        self.ring.setHidden_(True)

    # ------------------------------------------------------------------ boutons
    def _click(self, yes: bool) -> None:
        if time.monotonic() < self._armed_at:
            return  # trop tôt : probablement un clic destiné à l'app en dessous
        if yes and self._destructive and not self._yes_primed:
            self._yes_primed = True
            self.yes.set_style("Cliquer encore pour confirmer", RED)
            AppHelper.callLater(4.0, self._unprime, self._generation)
            return
        self._on_confirm(yes)

    def _unprime(self, gen: int) -> None:
        if gen == self._generation and self._yes_primed:
            self._yes_primed = False
            self.yes.set_style("Exécuter…" if self._destructive else "Exécuter", GREEN)

    def _arm(self, gen: int) -> None:
        if gen == self._generation:
            self.yes.set_armed(True)
            self.no.set_armed(True)

    # ------------------------------------------------------------------ mise en page
    def _reset_content(self) -> None:
        for v in (self.wave, self.transcript, self.chip, self.command, self.detail, self.separator,
                  self.insights_title, self.bars, self.footer, self.hint, self.yes, self.no):
            v.setHidden_(True)
        self.ring.setHidden_(True)
        self.bars.rows = []
        self.detail.setMaximumNumberOfLines_(4)

    def _has_card_content(self) -> bool:
        return any(not v.isHidden() for v in (self.transcript, self.chip, self.detail, self.bars, self.hint))

    def _layout(self) -> tuple[float, float]:
        """Place les vues ; renvoie la taille voulue (largeur, hauteur)."""
        compact = not self._has_card_content()
        self._compact = compact
        width = COMPACT_W if compact else CARD_W
        inner = width - 2 * PAD
        head_y = (COMPACT_H - 26) / 2 if compact else 16.0
        self.orb.setFrame_(NSMakeRect(PAD - 2, head_y, 26, 26))
        right = width - PAD
        if not self.ring.isHidden():
            right -= 26
            self.ring.setFrame_(NSMakeRect(right, head_y, 26, 26))
            right -= 10
        if not self.wave.isHidden():
            right -= 92
            self.wave.setFrame_(NSMakeRect(right, head_y + 1, 92, 24))
            right -= 10
        show_mode = self._mode != "defaut"
        self.mode_chip.setHidden_(not show_mode)
        for chip in (self.dry_chip, self.mode_chip):
            if not chip.isHidden():
                w = chip.width()
                right -= w
                chip.place(right, head_y + 3, 20)
                right -= 6
        self.status.setFrame_(NSMakeRect(PAD + 34, head_y + 3, max(40.0, right - PAD - 38), 20))
        if compact:
            return width, COMPACT_H

        y = head_y + 26 + 12
        if not self.transcript.isHidden():
            h = text_height(self.transcript, inner)
            self.transcript.setFrame_(NSMakeRect(PAD, y, inner, h))
            y += h + 10
        if not self.chip.isHidden():
            w = self.chip.place(PAD, y, 24)
            self.command.setFrame_(NSMakeRect(PAD + w + 10, y + 3, inner - w - 10, 18))
            y += 32
        if not self.detail.isHidden():
            h = text_height(self.detail, inner)
            self.detail.setFrame_(NSMakeRect(PAD, y, inner, h))
            y += h + 10
        if not self.bars.isHidden():
            self.separator.setHidden_(False)
            self.separator.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
            self.separator.setFrame_(NSMakeRect(PAD, y + 2, inner, 1))
            y += 12
            self.insights_title.setHidden_(False)
            self.insights_title.setFrame_(NSMakeRect(PAD, y, inner, 14))
            y += 20
            h = len(self.bars.rows) * Insights.ROW_H
            self.bars.setFrame_(NSMakeRect(PAD, y, inner, h))
            self.bars.setNeedsDisplay_(True)
            y += h + 6
        if not self.footer.isHidden():
            self.footer.setFrame_(NSMakeRect(PAD, y, inner, 14))
            y += 20
        if not self.hint.isHidden():
            y += 2
            h = text_height(self.hint, inner)
            self.hint.setFrame_(NSMakeRect(PAD, y, inner, h))
            y += h + 10
            half = (inner - 10) / 2
            self.no.setFrame_(NSMakeRect(PAD, y, half, 36))
            self.yes.setFrame_(NSMakeRect(PAD + half + 10, y, half, 36))
            y += 46
        return width, y + 8

    def _target_frame(self, width: float, height: float):
        screen = NSScreen.mainScreen()
        full, vis = screen.frame(), screen.visibleFrame()
        top = min(vis.origin.y + vis.size.height, full.origin.y + full.size.height - 32) - TOP_GAP
        return NSMakeRect(full.origin.x + (full.size.width - width) / 2, top - height, width, height)

    def _set_radius(self, compact: bool, height: float) -> None:
        radius = height / 2 if compact else 26.0
        if NSGlassEffectView is not None:
            self.glass.setCornerRadius_(radius)
        else:
            self.glass.layer().setCornerRadius_(radius)

    def _show(self, hide_after: float = 0) -> None:
        width, height = self._layout()
        target = self._target_frame(width, height)
        self._generation += 1
        gen = self._generation
        if not self.panel.isVisible() or self.panel.alphaValue() < 0.05:
            start = NSMakeRect(target.origin.x + width * 0.04, target.origin.y + 14, width * 0.92, height)
            self._set_radius(self._compact, height)
            self.panel.setFrame_display_(start, False)
            self.panel.setAlphaValue_(0.0)
            self.panel.orderFrontRegardless()
            animate(0.42, lambda: (self.panel.animator().setFrame_display_(target, True),
                                   self.panel.animator().setAlphaValue_(1.0)))
        else:
            self._set_radius(self._compact, height)
            animate(0.38, lambda: (self.panel.animator().setFrame_display_(target, True),
                                   self.panel.animator().setAlphaValue_(1.0)))
        if hide_after:
            AppHelper.callLater(hide_after, self._hide_if, gen)
        if os.environ.get("VOXJEV_SNAPSHOT"):  # débogage : rendu du HUD en PNG
            AppHelper.callLater(0.6, self._snapshot)

    def _snapshot(self) -> None:
        view = self.panel.contentView()
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        self._shots += 1
        path = f"{os.environ['VOXJEV_SNAPSHOT']}/hud-{self._shots:02d}.png"
        rep.representationUsingType_properties_(4, None).writeToFile_atomically_(path, True)

    def _hide_if(self, gen: int) -> None:
        if gen == self._generation:
            self._fade_out(gen)

    def _fade_out(self, gen: int) -> None:
        f = self.panel.frame()
        up = NSMakeRect(f.origin.x + f.size.width * 0.03, f.origin.y + 10, f.size.width * 0.94, f.size.height)

        def done():
            if gen == self._generation:
                self.panel.orderOut_(None)

        animate(0.3, lambda: (self.panel.animator().setAlphaValue_(0.0),
                              self.panel.animator().setFrame_display_(up, True)), done, curve=(0.4, 0.0, 1.0, 1.0))

    def hide(self) -> None:
        self._generation += 1
        self._fade_out(self._generation)

    # ------------------------------------------------------------------ API (appelée par le moteur)
    def set_mode(self, mode: str, dry_run: bool) -> None:
        self._mode, self._dry = mode, dry_run
        self.mode_chip.set(mode.upper(), {"ctf": RED, "travail": BLUE}.get(mode, GRAY),
                           {"ctf": "terminal.fill", "travail": "briefcase.fill"}.get(mode, "circle.fill"))
        self.dry_chip.setHidden_(not dry_run)
        if self.panel.isVisible():
            self._show()

    def phase(self, phase: str, text: str, hide_after: float = 0, keep_content: bool = False) -> None:
        self.orb.set_state(phase)
        self.status.setStringValue_(text)
        if not keep_content:
            self._reset_content()
        if phase == "listening":
            self.wave.color = RED
            self.wave.reset()
            self.wave.setHidden_(False)
        else:
            self.wave.setHidden_(True)
        self._show(hide_after)

    def level_push(self, rms: float) -> None:
        if not self.wave.isHidden():
            self.orb.level(self.wave.push(rms))

    def show_transcript(self, text: str) -> None:
        keep_wave = not self.wave.isHidden()
        self._reset_content()
        self.wave.setHidden_(not keep_wave)
        self.transcript.setStringValue_(f"« {text} »")
        self.transcript.setHidden_(False)
        self._show()

    def show_message(self, title: str, body: str, phase: str = "idle", hide_after: float = 0) -> None:
        self.orb.set_state(phase)
        self.status.setStringValue_(title)
        self._reset_content()
        self.detail.setStringValue_(body)
        self.detail.setHidden_(False)
        self._show(hide_after)

    def _fill_result(self, out, s, dry_run: bool = False) -> None:
        r = out.result
        if out.transcript:
            self.transcript.setStringValue_(f"« {out.transcript} »")
            self.transcript.setHidden_(False)
        cmd = out.decision.command if out.decision else None
        p = r.p_command if r else 0.0
        if cmd:
            self.command.setStringValue_(cmd.short(out.args.shown if out.args else None))
        elif out.decision:
            self.command.setStringValue_(explain(out.decision, p, dry_run))
        else:
            self.command.setStringValue_(out.error or "")
        self.command.setHidden_(False)
        details = list(out.messages)
        if out.args and out.args.values and not out.messages:
            details.append("   ·   ".join(f"{k} : {v}" for k, v in out.args.shown.items()))
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
            parts.append("voix anticipée" if t.get("stt_anticipé") else f"voix {t['stt_ms'] / 1000:.2f} s")
        if r:
            parts.append("Jev anticipé" if t.get("speculated") else f"Jev {r.latency_ms:.0f} ms")
        if "action_ms" in t:
            parts.append(f"action {t['action_ms']:.0f} ms")
        if "total_ms" in t:
            parts.append(f"total {t['total_ms'] / 1000:.2f} s")
        if parts:
            self.footer.setStringValue_("  ·  ".join(parts))
            self.footer.setHidden_(False)

    def _set_chip(self, key: str) -> None:
        text, color, _, symbol = STATUS_STYLE[key]
        self.chip.set(text, color, symbol)
        self.chip.setHidden_(False)

    def show_outcome(self, out, s) -> None:
        self._reset_content()
        key = out.status if out.status in STATUS_STYLE else "ignored"
        self._fill_result(out, s, dry_run=out.status == "dry_run")
        self._set_chip(key)
        phase = {"executed": "done", "error": "error"}.get(out.status, "idle")
        titles = {"executed": "C'est fait", "dry_run": "Simulation — rien n'a été exécuté", "ignored": "Ignoré",
                  "cancelled": "Annulé", "error": "Échec"}
        self.orb.set_state(phase)
        if out.status in ("ignored", "cancelled", "dry_run"):
            self.orb.show_symbol(STATUS_STYLE[key][3], STATUS_STYLE[key][1], "bounce")
        self.status.setStringValue_(titles.get(out.status, out.status))
        hide = STATUS_STYLE[key][2]
        if out.messages:  # une réponse (agenda, question…) reste le temps d'être lue
            hide = max(hide, min(20.0, 4.0 + sum(len(m) for m in out.messages) / 18))
        self._show(hide)

    def ask_confirm(self, out, s, timeout: int, hotkey: str) -> None:
        self._reset_content()
        self._fill_result(out, s)
        self._ask(bool(out.decision and out.decision.destructive), timeout, hotkey)

    def ask_confirm_plan(self, plan, s, timeout: int, hotkey: str) -> None:
        self._reset_content()
        self._fill_plan(plan)
        self._ask(plan.destructive, timeout, hotkey)

    def _ask(self, destructive: bool, timeout: int, hotkey: str) -> None:
        self._set_chip("confirm")
        self.orb.set_state("confirm")
        self.status.setStringValue_("Confirmer ?" if not destructive else "Action destructrice — confirmer ?")
        self._destructive = destructive
        self._yes_primed = False
        self.yes.set_style("Exécuter…" if destructive else "Exécuter", RED if destructive else GREEN)
        self.no.set_style("Annuler")
        self.hint.setStringValue_(f"Maintenez {hotkey} et dites « oui » ou « non », ou cliquez"
                                  + (" deux fois sur Exécuter." if destructive else "."))
        for v in (self.hint, self.yes, self.no, self.ring):
            v.setHidden_(False)
        self.ring.start(timeout, RED if destructive else ORANGE)
        self.yes.view.setAlphaValue_(0.4)
        self.no.view.setAlphaValue_(0.4)
        self._armed_at = time.monotonic() + self.ARM_DELAY
        self._show()
        AppHelper.callLater(self.ARM_DELAY, self._arm, self._generation)

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
                lines.append(f"{mark}  {i}. {cmd.short(o.args.shown if o.args else None)}")
                lines += [f"       {m}" for m in o.messages]
            else:
                lines.append(f"{mark}  {i}. « {o.transcript} » : pas une commande, ignoré")
            if o.error:
                lines.append(f"       Erreur : {o.error}")
        if plan.error and not any(o.error for o in plan.items):
            lines.append(f"Erreur : {plan.error}")
        self.detail.setMaximumNumberOfLines_(12)
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
        titles = {"executed": "Plan exécuté", "dry_run": "Simulation du plan", "ignored": "Ignoré",
                  "cancelled": "Plan annulé", "error": "Plan non exécuté"}
        self.orb.set_state({"executed": "done", "error": "error"}.get(plan.status, "idle"))
        self.status.setStringValue_(titles.get(plan.status, plan.status))
        self._show(STATUS_STYLE[key][2] + 2)

    def confirm_countdown(self, left: int) -> None:
        if not self.hint.isHidden():
            self.ring.set_seconds(left)

    def confirm_listening(self) -> None:
        self.status.setStringValue_("J'écoute votre réponse…")
        self.orb.set_state("listening")
        self.wave.reset()
        self.wave.setHidden_(False)
        self._show()
