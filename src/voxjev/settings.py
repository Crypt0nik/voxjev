"""Fenêtre Réglages de voxjev (style Réglages macOS 26 : onglets dans la barre d'outils, sections groupées).

Les modifications sont écrites dans les réglages personnels (``config.USER_CONFIG``), validées
comme le reste de la config (une valeur invalide est refusée avec un message), puis appliquées
immédiatement au moteur. ``config/commands.yaml`` n'est jamais modifié.

Les clés API ne sont jamais affichées : on voit seulement si elles sont présentes, et on peut les
remplacer (champ masqué) ; elles sont écrites dans ``.env`` (ignoré par git).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import objc
import yaml
from AppKit import (
    NSAlert,
    NSApplication,
    NSBox,
    NSButton,
    NSButtonCell,
    NSColor,
    NSFont,
    NSImage,
    NSMakeRect,
    NSMakeSize,
    NSPopUpButton,
    NSScrollView,
    NSSearchField,
    NSSecureTextField,
    NSSlider,
    NSStackView,
    NSStepper,
    NSSwitch,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSTextView,
    NSView,
    NSViewController,
    NSWindow,
)
from Foundation import NSObject

from .config import ConfigError, USER_CONFIG, load_config, read_user_config

PROJECT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT / ".env"
PAGE_W, PAGE_H = 670.0, 660.0
TAB_TOOLBAR = 2  # NSTabViewControllerTabStyleToolbar
HOTKEYS = [("⌥ Option droite", "alt_r"), ("⌘ Commande droite", "cmd_r"), ("⌃ Contrôle droite", "ctrl_r"),
           ("⇧ Maj droite", "shift_r"), ("Verr. Maj (après remappage F18)", "f18"), ("F13", "f13"), ("F14", "f14"),
           ("F15", "f15"), ("F16", "f16"), ("F17", "f17"), ("F19", "f19")]
KEYS = [("TYPESAFE_API_KEY", "TypeSafe (Jev)", "La couche de décision. Obligatoire.", "brain"),
        ("OPENROUTER_API_KEY", "OpenRouter", "Questions générales, brouillons de mail, découpage des demandes "
                                             "composées, saisie de l'agent web.", "text.bubble"),
        ("SPOTIFY_CLIENT_ID", "Spotify (Client ID)", "Lancer un morceau précis et liker.", "music.note")]


# ----------------------------------------------------------------------------- réglages personnels
def save_user_config(user: dict) -> None:
    """Valide puis écrit. Lève ConfigError (rien n'est écrit) si la config résultante est invalide."""
    load_config(user=user)
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    header = "# Réglages personnels de voxjev (fenêtre Réglages). Fusionnés par-dessus config/commands.yaml.\n"
    USER_CONFIG.write_text(header + yaml.safe_dump(user, allow_unicode=True, sort_keys=False), encoding="utf-8")


def env_has(key: str) -> bool:
    if os.environ.get(key):
        return True
    try:
        return any(re.match(rf"^\s*{key}\s*=\s*\S", line) for line in ENV_FILE.read_text().splitlines())
    except OSError:
        return False


def env_set(key: str, value: str) -> None:
    """Remplace ou ajoute KEY=valeur dans .env (jamais affiché ni loggé)."""
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-:/+=]+", value):
        raise ValueError("valeur invalide (espaces ou caractères inattendus)")
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not re.match(rf"^\s*{key}\s*=", ln)] + [f"{key}={value}"]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)
    os.environ[key] = value


def french_voices() -> list[str]:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    voices = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s{2,}(fr_\w+)\s", line)
        if m:
            voices.append(m.group(1).strip())
    return sorted(set(voices))


def login_item_enabled() -> bool:
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to exists login item "voxjev"'],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() == "true"


def quote(value: str) -> str:
    """Valeur lisible pour une ligne d'étape : entre guillemets doubles seulement si nécessaire."""
    if re.fullmatch(r"[\w\-.:/@]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----------------------------------------------------------------------------- petits outils AppKit
class Action(NSObject):
    """Cible d'action qui appelle une fonction Python (gardée en vie par la fenêtre)."""

    def initWithCallback_(self, cb):
        self = objc.super(Action, self).init()
        if self is None:
            return None
        self.cb = cb
        return self

    def fire_(self, sender):
        self.cb(sender)


def text(value: str, size: float = 13, weight: float = 0.0, color=None, wrap: bool = False):
    tf = NSTextField.wrappingLabelWithString_(value) if wrap else NSTextField.labelWithString_(value)
    tf.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    tf.setTextColor_(color or NSColor.labelColor())
    tf.setSelectable_(False)
    return tf


# Couleurs des tuiles d'icônes (comme les Réglages d'iOS : symbole blanc sur carré arrondi coloré).
TILE_COLORS = {
    "keyboard": (142, 142, 147), "square.stack.3d.up": (88, 86, 214), "bolt": (255, 159, 10), "ear": (255, 55, 95),
    "quote.bubble": (52, 199, 89), "timer": (255, 149, 0), "speaker.wave.2": (255, 45, 85),
    "text.bubble": (0, 122, 255), "person.wave.2": (175, 82, 222), "power": (52, 199, 89),
    "checkmark.shield": (52, 199, 89), "eye": (90, 200, 250), "gauge.high": (0, 122, 255),
    "gauge.low": (142, 142, 147), "exclamationmark.triangle": (255, 59, 48), "brain": (88, 86, 214),
    "music.note": (255, 45, 85), "globe": (0, 122, 255), "accessibility": (0, 122, 255), "mic": (255, 59, 48),
    "calendar": (255, 59, 48), "capslock": (142, 142, 147), "doc.text": (0, 122, 255),
    "person.crop.circle": (142, 142, 147), "list.bullet.rectangle": (255, 149, 0), "arrow.clockwise": (52, 199, 89),
    "arrow.counterclockwise": (255, 59, 48), "gearshape": (142, 142, 147), "command": (88, 86, 214),
    "list.bullet.rectangle.portrait": (255, 149, 0), "key": (255, 204, 0), "slider.horizontal.3": (100, 100, 110),
}


def ns_rgb(c, a: float = 1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(c[0] / 255, c[1] / 255, c[2] / 255, a)


def tile(name: str, size: float = 26):
    """Icône façon Réglages iOS : symbole SF blanc sur carré arrondi coloré, léger dégradé."""
    import Quartz
    from AppKit import NSImageSymbolConfiguration, NSImageView

    color = TILE_COLORS.get(name, (0, 122, 255))
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
    view.setWantsLayer_(True)
    grad = Quartz.CAGradientLayer.layer()
    grad.setFrame_(((0, 0), (size, size)))
    grad.setCornerRadius_(size * 0.27)
    top = tuple(min(255, int(c * 1.12 + 18)) for c in color)
    grad.setColors_([Quartz.CGColorCreateSRGB(*(v / 255 for v in color), 1.0),
                     Quartz.CGColorCreateSRGB(*(v / 255 for v in top), 1.0)])
    view.layer().addSublayer_(grad)
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is not None:
        img = img.imageWithSymbolConfiguration_(NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            size * 0.5, 0.3))
        iv = NSImageView.imageViewWithImage_(img)
        iv.setContentTintColor_(NSColor.whiteColor())
        iv.setFrame_(NSMakeRect(0, 0, size, size))
        iv.setImageScaling_(0)
        view.addSubview_(iv)
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.widthAnchor().constraintEqualToConstant_(size).setActive_(True)
    view.heightAnchor().constraintEqualToConstant_(size).setActive_(True)
    return view


def vstack(views, spacing: float = 8, alignment: int = 1):  # 1 = leading
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(1)
    s.setSpacing_(spacing)
    s.setAlignment_(alignment)
    return s


def hstack(views, spacing: float = 10):
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(0)
    s.setSpacing_(spacing)
    s.setAlignment_(10)  # centre vertical
    return s


def pin(view, parent, top=0.0, left=0.0, right=0.0, bottom=None):
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.topAnchor().constraintEqualToAnchor_constant_(parent.topAnchor(), top).setActive_(True)
    view.leadingAnchor().constraintEqualToAnchor_constant_(parent.leadingAnchor(), left).setActive_(True)
    view.trailingAnchor().constraintEqualToAnchor_constant_(parent.trailingAnchor(), -right).setActive_(True)
    if bottom is not None:
        view.bottomAnchor().constraintEqualToAnchor_constant_(parent.bottomAnchor(), -bottom).setActive_(True)


LARGE = 3  # NSControlSizeLarge : contrôles plus grands et plus arrondis (style iOS 26)


class CapsuleFieldCell(objc.lookUpClass("NSTextFieldCell")):
    """Cellule de champ avec marges intérieures (le texte ne touche pas les bords de la capsule)."""

    INSET = 12.0

    @objc.python_method
    def _inset(self, rect):
        """Marges horizontales + texte centré verticalement dans la capsule."""
        line = self.font().ascender() - self.font().descender() + 2 if self.font() else 18
        y = rect.origin.y + max(0.0, (rect.size.height - line) / 2)
        return NSMakeRect(rect.origin.x + self.INSET, y, max(0, rect.size.width - 2 * self.INSET), line)

    def drawingRectForBounds_(self, rect):
        r = objc.super(CapsuleFieldCell, self).drawingRectForBounds_(rect)
        return self._inset(r)

    def editWithFrame_inView_editor_delegate_event_(self, rect, view, editor, delegate, event):
        objc.super(CapsuleFieldCell, self).editWithFrame_inView_editor_delegate_event_(
            self._inset(rect), view, editor, delegate, event)

    def selectWithFrame_inView_editor_delegate_start_length_(self, rect, view, editor, delegate, start, length):
        objc.super(CapsuleFieldCell, self).selectWithFrame_inView_editor_delegate_start_length_(
            self._inset(rect), view, editor, delegate, start, length)


def rounded_field(value: str = "", placeholder: str = ""):
    """Champ de saisie en capsule façon iOS 26 : fond translucide, coins entièrement arrondis."""
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 32))
    cell = CapsuleFieldCell.alloc().initTextCell_(value)
    cell.setEditable_(True)
    cell.setSelectable_(True)
    cell.setScrollable_(True)
    cell.setUsesSingleLineMode_(True)
    f.setCell_(cell)
    f.setBordered_(False)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setFocusRingType_(1)  # pas d'anneau rectangulaire : la capsule change de teinte
    f.setFont_(NSFont.systemFontOfSize_(13.5))
    f.setPlaceholderString_(placeholder)
    f.setWantsLayer_(True)
    f.layer().setCornerRadius_(16)
    f.layer().setBackgroundColor_(NSColor.labelColor().colorWithAlphaComponent_(0.07).CGColor())
    f.layer().setBorderWidth_(0.5)
    f.layer().setBorderColor_(NSColor.labelColor().colorWithAlphaComponent_(0.10).CGColor())
    f.setTranslatesAutoresizingMaskIntoConstraints_(False)
    f.heightAnchor().constraintEqualToConstant_(32).setActive_(True)
    return f


class Flipped(NSView):
    def isFlipped(self):
        return True


def glass_card(radius: float = 18.0):
    """Carte en Liquid Glass (macOS 26) ; repli : carte translucide."""
    try:
        from AppKit import NSGlassEffectView

        card = NSGlassEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 40))
        card.setCornerRadius_(radius)
        content = Flipped.alloc().initWithFrame_(card.bounds())
        card.setContentView_(content)
        return card, content
    except ImportError:
        box = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 40))
        box.setBoxType_(4)
        box.setTitlePosition_(0)
        box.setBorderWidth_(0)
        box.setCornerRadius_(radius)
        box.setFillColor_(NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.6))
        box.setContentViewMargins_(NSMakeSize(0, 0))
        return box, box.contentView()


class Page:
    """Page façon iOS 26 : grand titre, cartes groupées en Liquid Glass, défilement fluide."""

    def __init__(self, title: str, subtitle: str):
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PAGE_W, PAGE_H))
        self.scroll = NSScrollView.alloc().initWithFrame_(self.view.bounds())
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setScrollerStyle_(1)  # surimpression : barre fine, visible seulement au défilement
        self.scroll.setDrawsBackground_(False)
        self.scroll.setAutoresizingMask_(18)
        self.scroll.contentView().setDrawsBackground_(False)
        self.doc = Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, PAGE_W, PAGE_H))
        self.scroll.setDocumentView_(self.doc)
        self.view.addSubview_(self.scroll)
        self.stack = vstack([], spacing=10)
        self.doc.addSubview_(self.stack)
        pin(self.stack, self.doc, top=54, left=34, right=34, bottom=34)
        self.doc.setTranslatesAutoresizingMaskIntoConstraints_(False)
        clip = self.scroll.contentView()
        self.doc.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()).setActive_(True)
        self.doc.trailingAnchor().constraintEqualToAnchor_(clip.trailingAnchor()).setActive_(True)
        self.doc.topAnchor().constraintEqualToAnchor_(clip.topAnchor()).setActive_(True)
        big = text(title, 30, 0.56)
        big.setFont_(_rounded(30, 0.56))
        self.add(big, after=4)
        self.add(text(subtitle, 13, 0.0, NSColor.secondaryLabelColor(), wrap=True), after=22)

    def add(self, view, after: float = 10):
        self.stack.addArrangedSubview_(view)
        self.stack.setCustomSpacing_afterView_(after, view)
        if not view.isKindOfClass_(NSTextField) or view.cell().wraps():
            view.widthAnchor().constraintEqualToAnchor_constant_(self.stack.widthAnchor(), 0).setActive_(True)
        return view

    def section(self, title: str | None, rows: list, footer: str | None = None):
        if title:
            head = text(title, 13, 0.4, NSColor.secondaryLabelColor())
            self.add(head, after=8)
        card, content = glass_card()
        inner = vstack([], spacing=0, alignment=1)
        for i, r in enumerate(rows):
            if i:
                sep = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 1))
                sep.setWantsLayer_(True)
                sep.layer().setBackgroundColor_(NSColor.labelColor().colorWithAlphaComponent_(0.10).CGColor())
                sep.setTranslatesAutoresizingMaskIntoConstraints_(False)
                sep.heightAnchor().constraintEqualToConstant_(1).setActive_(True)
                inner.addArrangedSubview_(sep)
                # séparateur aligné sur le texte (après la tuile), comme sur iOS
                sep.leadingAnchor().constraintEqualToAnchor_constant_(inner.leadingAnchor(), 56).setActive_(True)
                sep.trailingAnchor().constraintEqualToAnchor_(inner.trailingAnchor()).setActive_(True)
            inner.addArrangedSubview_(r)
            r.widthAnchor().constraintEqualToAnchor_(inner.widthAnchor()).setActive_(True)
        content.addSubview_(inner)
        inner.setTranslatesAutoresizingMaskIntoConstraints_(False)
        inner.topAnchor().constraintEqualToAnchor_(card.topAnchor()).setActive_(True)
        inner.leadingAnchor().constraintEqualToAnchor_(card.leadingAnchor()).setActive_(True)
        inner.trailingAnchor().constraintEqualToAnchor_(card.trailingAnchor()).setActive_(True)
        card.bottomAnchor().constraintEqualToAnchor_(inner.bottomAnchor()).setActive_(True)
        self.add(card, after=8 if footer else 26)
        if footer:
            self.add(text(footer, 11.5, 0.0, NSColor.secondaryLabelColor(), wrap=True), after=26)
        return card


def _rounded(size: float, weight: float):
    from AppKit import NSFontDescriptorSystemDesignRounded

    f = NSFont.systemFontOfSize_weight_(size, weight)
    desc = f.fontDescriptor().fontDescriptorWithDesign_(NSFontDescriptorSystemDesignRounded)
    return NSFont.fontWithDescriptor_size_(desc, size) if desc is not None else f


def row(title: str, subtitle: str | None, control, icon: str | None = None):
    labels = [text(title, 13.5, 0.0)]
    if subtitle:
        sub = text(subtitle, 11.5, 0.0, NSColor.secondaryLabelColor(), wrap=True)
        sub.setPreferredMaxLayoutWidth_(330)
        labels.append(sub)
    left = vstack(labels, spacing=2)
    left.setHuggingPriority_forOrientation_(1, 0)
    parts = ([tile(icon)] if icon else []) + [left]
    h = hstack(parts, spacing=14)
    if control is not None:  # contrôle aligné à droite
        if control.isKindOfClass_(NSStackView):
            control.setHuggingPriority_forOrientation_(750, 0)
        else:
            control.setContentHuggingPriority_forOrientation_(750, 0)
        h.addView_inGravity_(control, 3)
    h.setEdgeInsets_((12, 16, 12, 16))
    h.setTranslatesAutoresizingMaskIntoConstraints_(False)
    h.heightAnchor().constraintGreaterThanOrEqualToConstant_(50).setActive_(True)
    return h


# ----------------------------------------------------------------------------- tableau des commandes
class CommandsSource(NSObject):
    def initWithWindow_(self, win):
        self = objc.super(CommandsSource, self).init()
        if self is None:
            return None
        self.win = win
        self.rows = []
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, i):
        cmd = self.rows[i]
        ident = column.identifier()
        if ident == "on":
            return 0 if cmd.id in self.win.disabled else 1
        if ident == "safe":
            return 1 if cmd.id in self.win.safe else 0
        if ident == "name":
            return cmd.short({n: f"‹{n}›" for n in cmd.args}) if cmd.label else cmd.description
        return (cmd.examples[0] if cmd.examples else "") + ("   ⚠︎ destructive" if cmd.destructive else "")

    def tableView_setObjectValue_forTableColumn_row_(self, table, value, column, i):
        cmd = self.rows[i]
        on = bool(value)
        if column.identifier() == "on":
            (self.win.disabled.discard if on else self.win.disabled.add)(cmd.id)
        elif column.identifier() == "safe" and not (cmd.destructive or cmd.always_confirm):
            (self.win.safe.add if on else self.win.safe.discard)(cmd.id)
        self.win.save_commands()
        table.reloadData()

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, i):
        if column.identifier() == "safe":
            cmd = self.rows[i]
            cell.setEnabled_(not (cmd.destructive or cmd.always_confirm) and cmd.id not in self.win.disabled)


# ----------------------------------------------------------------------------- fenêtre
class SettingsWindow:
    def __init__(self, app):
        self.app = app  # GuiApp
        self.keep = []  # cibles d'action gardées en vie
        self.window = None

    # -------------------------------------------------------------- données
    @property
    def engine(self):
        return self.app.engine

    def user(self) -> dict:
        try:
            return read_user_config()
        except ConfigError:
            return {}

    def set_setting(self, key: str, value) -> bool:
        user = self.user()
        user.setdefault("settings", {})[key] = value
        return self.save(user)

    def save(self, user: dict) -> bool:
        try:
            save_user_config(user)
        except ConfigError as exc:
            self.alert("Réglage refusé", str(exc))
            return False
        self.engine.jobs.put(("reload",))
        return True

    def alert(self, title: str, message: str) -> None:
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(message)
        a.beginSheetModalForWindow_completionHandler_(self.window, None) if self.window else a.runModal()

    def act(self, fn):
        target = Action.alloc().initWithCallback_(fn)
        self.keep.append(target)
        return target

    def switch(self, on: bool, fn):
        sw = NSSwitch.alloc().init()
        sw.setControlSize_(LARGE)
        sw.setState_(1 if on else 0)
        sw.setTarget_(self.act(lambda s: fn(bool(s.state()))))
        sw.setAction_("fire:")
        return sw

    def button(self, title: str, fn, prominent: bool = False):
        b = NSButton.buttonWithTitle_target_action_(title, self.act(lambda s: fn()), "fire:")
        b.setControlSize_(LARGE)
        if prominent:
            b.setKeyEquivalent_("\r")
        return b

    def popup(self, items: list[tuple[str, str]], current: str, fn):
        p = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 220, 26), False)
        p.setControlSize_(LARGE)
        for title, value in items:
            p.addItemWithTitle_(title)
            p.lastItem().setRepresentedObject_(value)
        idx = next((i for i, (_, v) in enumerate(items) if v == current), 0)
        p.selectItemAtIndex_(idx)
        p.setTarget_(self.act(lambda s: fn(s.selectedItem().representedObject())))
        p.setAction_("fire:")
        return p

    def slider(self, value: float, lo: float, hi: float, fn, fmt=lambda v: f"{v * 100:.0f} %"):
        label = text(fmt(value), 12, 0.2)
        label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0.2))
        label.setAlignment_(2)  # à droite
        label.widthAnchor().constraintEqualToConstant_(40).setActive_(True)
        s = NSSlider.sliderWithValue_minValue_maxValue_target_action_(value, lo, hi, None, None)
        s.setContinuous_(True)
        s.widthAnchor().constraintEqualToConstant_(170).setActive_(True)

        def changed(sender):
            v = round(float(sender.doubleValue()), 2)
            label.setStringValue_(fmt(v))
            if NSApplication.sharedApplication().currentEvent().type() == 2:  # souris relâchée
                fn(v)

        s.setTarget_(self.act(changed))
        s.setAction_("fire:")
        return hstack([s, label], spacing=8)

    def field(self, value: str, fn, width: float = 220, placeholder: str = ""):
        f = rounded_field(value, placeholder)
        f.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        f.cell().setSendsActionOnEndEditing_(True)
        f.setTarget_(self.act(lambda s: fn(str(s.stringValue()))))
        f.setAction_("fire:")
        return f

    # -------------------------------------------------------------- pages
    def page_general(self) -> Page:
        s = self.engine.config.settings
        page = Page("Général", "Comment vous parlez à voxjev, et comment il vous répond.")
        modes = [(m.description, name) for name, m in self.engine.config.modes.items()]
        page.section("Parole", [
            row("Touche de parole", "Maintenez-la, parlez, relâchez.",
                self.popup(HOTKEYS, s.hotkey, lambda v: self.set_setting("hotkey", v)), "keyboard"),
            row("Mode actif", "Les commandes proposées à Jev dépendent du mode.",
                self.popup(modes, self.engine.session.mode, lambda v: self.engine.jobs.put(("mode", v))),
                "square.stack.3d.up"),
            row("Réponse anticipée", "Transcrit et interroge Jev pendant que vous parlez : réponse quasi immédiate.",
                self.switch(s.speculate, lambda on: self.set_setting("speculate", on)), "bolt"),
        ])
        stepper_label = text(f"{s.followup_seconds:.0f} s", 12, 0.2)
        stepper = NSStepper.alloc().init()
        stepper.setMinValue_(3)
        stepper.setMaxValue_(30)
        stepper.setIntegerValue_(int(s.followup_seconds))

        def step(sender):
            stepper_label.setStringValue_(f"{sender.integerValue()} s")
            self.set_setting("followup_seconds", int(sender.integerValue()))

        stepper.setTarget_(self.act(step))
        stepper.setAction_("fire:")
        page.section("Mains libres", [
            row("Mains libres au démarrage", "Micro ouvert en continu ; seules les phrases qui commencent par le mot "
                "d'éveil partent vers Jev.", self.switch(s.hands_free, self._hands_free), "ear"),
            row("Mots d'éveil", "Séparés par des virgules.",
                self.field(", ".join(s.wake_words), self._wake_words, 200, "jarvis, vox"), "quote.bubble"),
            row("Fenêtre de suite", "Après une commande, on peut enchaîner sans le mot d'éveil.",
                hstack([stepper_label, stepper], 6), "timer"),
        ])
        voices = [("Voix du système", "")] + [(v, v) for v in french_voices()]
        page.section("Retours", [
            row("Sons", "Tink à l'appui, Glass si exécuté, Basso si échec.",
                self.switch(s.sounds_enabled, lambda on: self.set_setting("sounds_enabled", on)), "speaker.wave.2"),
            row("Lire les réponses à voix haute", "Heure, agenda, questions, minuteurs…",
                self.switch(s.speak_answers, lambda on: self.set_setting("speak_answers", on)), "text.bubble"),
            row("Voix", None, hstack([self.popup(voices, s.voice, lambda v: self.set_setting("voice", v)),
                                      self.button("Écouter", self._try_voice)], 8), "person.wave.2"),
        ])
        page.section("Démarrage", [
            row("Ouvrir voxjev à l'ouverture de session", None,
                self.switch(login_item_enabled(), self._login_item), "power"),
        ])
        return page

    def page_trust(self) -> Page:
        s = self.engine.config.settings
        page = Page("Confiance", "Quand voxjev agit seul, quand il vous demande, et quand il ignore.")
        page.section("Confirmations", [
            row("Sans confirmation pour les actions sans risque",
                "Ouvrir une app ou une page, rechercher, volume, musique, agenda… (liste dans l'onglet Commandes).",
                self.switch(s.quiet_mode, self._quiet), "checkmark.shield"),
            row("Simulation (dry-run)", "Montre ce que voxjev ferait, sans rien exécuter.",
                self.switch(self.engine.dry_run, lambda on: self.engine.jobs.put(("dry_run", on))), "eye"),
        ], footer="Les actions destructives (corbeille, quitter une app…) sont toujours confirmées.")
        page.section("Seuils de Jev", [
            row("Exécuter sans demander à partir de", "Au-dessous : confirmation (sauf actions sans risque).",
                self.slider(s.threshold, 0.5, 0.99, lambda v: self.set_setting("threshold", v)), "gauge.high"),
            row("Ignorer au-dessous de", "Commande trop incertaine : rien ne se passe.",
                self.slider(s.confirm_floor, 0.1, 0.7, lambda v: self.set_setting("confirm_floor", v)), "gauge.low"),
            row("« Ça m'est adressé » : confiance minimale", "Évite de réagir aux conversations autour de vous.",
                self.slider(s.addressed_floor, 0.1, 0.7, lambda v: self.set_setting("addressed_floor", v)),
                "person.wave.2"),
            row("Considérer comme destructif à partir de", "Au-dessus : confirmation obligatoire.",
                self.slider(s.destructive_threshold, 0.2, 0.9, lambda v: self.set_setting("destructive_threshold", v)),
                "exclamationmark.triangle"),
        ])
        return page

    def page_commands(self) -> Page:
        page = Page("Commandes", "Activez ou désactivez des commandes, et choisissez celles qui s'exécutent sans "
                                 "confirmation. Une commande désactivée n'est plus proposée à Jev.")
        user = self.user()
        self.disabled = set(user.get("disabled_commands") or [])
        self.safe = set(self.engine.config.settings.safe_commands)
        search = NSSearchField.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 26))
        search.setControlSize_(LARGE)
        search.setPlaceholderString_("Rechercher une commande")
        page.add(search, after=10)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 380))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        table.setUsesAlternatingRowBackgroundColors_(False)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setRowHeight_(26)
        table.setStyle_(4)  # simple
        table.setHeaderView_(None)  # en-tête remplacé par des libellés posés sur le verre
        table.setIntercellSpacing_(NSMakeSize(10, 4))
        table.setGridStyleMask_(0)
        for ident, title, width, check in (("on", "Activée", 60, True), ("safe", "Sans confirm.", 90, True),
                                           ("name", "Commande", 230, False), ("ex", "Exemple", 260, False)):
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.setTitle_(title)
            col.setWidth_(width)
            if check:
                cell = NSButtonCell.alloc().init()
                cell.setButtonType_(3)  # case à cocher
                cell.setTitle_("")
                col.setDataCell_(cell)
            else:
                col.setEditable_(False)
            table.addTableColumn_(col)
        self.source = CommandsSource.alloc().initWithWindow_(self)
        all_cmds = list(load_config(user={**user, "disabled_commands": []}).commands.values())

        def refresh(query: str = "") -> None:
            q = query.lower().strip()
            self.source.rows = [c for c in all_cmds if not q or q in (c.id + c.description + " ".join(c.examples)).lower()]
            table.reloadData()

        search.setTarget_(self.act(lambda s: refresh(str(s.stringValue()))))
        search.setAction_("fire:")
        refresh()
        table.setDataSource_(self.source)
        table.setDelegate_(self.source)
        scroll.setDocumentView_(table)
        scroll.setScrollerStyle_(1)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.contentView().setDrawsBackground_(False)
        card, content = glass_card()
        content.addSubview_(scroll)
        scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
        header = hstack([], spacing=0)
        for title, width in (("Activée", 70), ("Sans confirm.", 100), ("Commande", 240), ("Exemple", 200)):
            lab = text(title.upper(), 10.5, 0.4, NSColor.secondaryLabelColor())
            lab.setTranslatesAutoresizingMaskIntoConstraints_(False)
            lab.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
            header.addArrangedSubview_(lab)
        content.addSubview_(header)
        header.setTranslatesAutoresizingMaskIntoConstraints_(False)
        header.topAnchor().constraintEqualToAnchor_constant_(card.topAnchor(), 14).setActive_(True)
        header.leadingAnchor().constraintEqualToAnchor_constant_(card.leadingAnchor(), 18).setActive_(True)
        scroll.topAnchor().constraintEqualToAnchor_constant_(header.bottomAnchor(), 8).setActive_(True)
        scroll.leadingAnchor().constraintEqualToAnchor_constant_(card.leadingAnchor(), 8).setActive_(True)
        scroll.trailingAnchor().constraintEqualToAnchor_constant_(card.trailingAnchor(), -8).setActive_(True)
        scroll.bottomAnchor().constraintEqualToAnchor_constant_(card.bottomAnchor(), -6).setActive_(True)
        card.heightAnchor().constraintEqualToConstant_(400).setActive_(True)
        page.add(card)
        return page

    def save_commands(self) -> None:
        user = self.user()
        user["disabled_commands"] = sorted(self.disabled)
        user.setdefault("settings", {})["safe_commands"] = sorted(self.safe - self.disabled)
        self.save(user)

    # routines ---------------------------------------------------------------
    def page_routines(self) -> Page:
        page = Page("Routines", "Une phrase déclenche plusieurs commandes, avec des valeurs écrites ici (aucune "
                                "valeur ne vient de la voix). Une étape par ligne : « commande argument=valeur », "
                                "ou « wait 2 » pour une pause.")
        self.routine_list = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 260, 26), False)
        self.routine_list.setControlSize_(LARGE)
        self.routine_list.setTarget_(self.act(lambda s: self._load_routine()))
        self.routine_list.setAction_("fire:")
        page.add(hstack([self.routine_list, self.button("Nouvelle", self._new_routine),
                         self.button("Supprimer", self._delete_routine)], 8), after=14)
        self.r_name = rounded_field("", "Nom affiché (ex. Routine du soir)")
        self.r_phrases = rounded_field("", "lance ma routine du soir, mode soirée")
        for f in (self.r_name, self.r_phrases):
            f.widthAnchor().constraintEqualToConstant_(360).setActive_(True)
        steps_scroll = NSTextView.scrollableTextView()
        self.r_steps = steps_scroll.documentView()
        self.r_steps.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.5, 0.0))
        self.r_steps.setAutomaticQuoteSubstitutionEnabled_(False)
        self.r_steps.setAutomaticDashSubstitutionEnabled_(False)
        self.r_steps.setRichText_(False)
        self.r_steps.setDrawsBackground_(False)
        self.r_steps.setTextContainerInset_(NSMakeSize(8, 8))
        steps_scroll.setDrawsBackground_(True)
        steps_scroll.setBackgroundColor_(NSColor.labelColor().colorWithAlphaComponent_(0.06))
        steps_scroll.setScrollerStyle_(1)
        steps_scroll.setAutohidesScrollers_(True)
        steps_scroll.setWantsLayer_(True)
        steps_scroll.layer().setCornerRadius_(14)
        steps_scroll.layer().setMasksToBounds_(True)
        steps_scroll.layer().setBorderWidth_(0.5)
        steps_scroll.layer().setBorderColor_(NSColor.labelColor().colorWithAlphaComponent_(0.10).CGColor())
        steps_scroll.heightAnchor().constraintEqualToConstant_(150).setActive_(True)
        steps_scroll.widthAnchor().constraintEqualToConstant_(360).setActive_(True)
        cmds = sorted(self.engine.config.commands.values(), key=lambda c: c.id)
        adder = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 200, 26), True)
        adder.setControlSize_(LARGE)
        adder.addItemWithTitle_("Ajouter une étape…")
        adder.addItemWithTitle_("Pause (wait 2)")
        adder.lastItem().setRepresentedObject_("wait 2")
        for c in cmds:
            if c.action.get("type") in ("routine", "undo"):
                continue
            args = " ".join(f"{n}=" for n, a in c.args.items() if not a.optional)
            adder.addItemWithTitle_(f"{c.short({}) if c.label else c.description[:40]}  ({c.id})")
            adder.lastItem().setRepresentedObject_(f"{c.id} {args}".strip())
        adder.setTarget_(self.act(lambda s: self._insert_step(s.selectedItem().representedObject())))
        adder.setAction_("fire:")
        self.r_status = text("", 11.5, 0.0, NSColor.secondaryLabelColor(), wrap=True)
        self.r_status.setPreferredMaxLayoutWidth_(560)
        page.section(None, [
            row("Nom", None, self.r_name),
            row("Phrases", "Séparées par des virgules.", self.r_phrases),
            row("Étapes", "Ex. : open_app app=Mail · set_timer duration=\"25 minutes\"", steps_scroll),
            row("", None, hstack([adder, self.button("Enregistrer", self._save_routine, prominent=True)], 8)),
        ])
        page.add(self.r_status)
        self._fill_routines()
        return page

    def _routines(self) -> dict:
        return {c.id: c for c in self.engine.config.commands.values() if c.action.get("type") == "routine"}

    def _fill_routines(self, select: str | None = None) -> None:
        self.routine_list.removeAllItems()
        for rid, c in self._routines().items():
            self.routine_list.addItemWithTitle_(c.label or c.description)
            self.routine_list.lastItem().setRepresentedObject_(rid)
        if select:
            idx = self.routine_list.indexOfItemWithRepresentedObject_(select)
            if idx >= 0:
                self.routine_list.selectItemAtIndex_(idx)
        self._load_routine()

    def _load_routine(self) -> None:
        item = self.routine_list.selectedItem()
        c = self._routines().get(item.representedObject()) if item else None
        if c is None:
            return self._new_routine()
        self.editing = c.id
        self.r_name.setStringValue_(c.label or c.description)
        self.r_phrases.setStringValue_(", ".join(c.examples))
        lines = []
        for st in c.action["steps"]:
            if "wait" in st:
                lines.append(f"wait {st['wait']}")
            else:
                args = " ".join(f"{k}={quote(str(v))}" for k, v in (st.get("with") or {}).items())
                lines.append(f"{st['run']} {args}".strip())
        self.r_steps.setString_("\n".join(lines))
        self.r_status.setStringValue_("")

    def _new_routine(self) -> None:
        self.editing = None
        self.r_name.setStringValue_("")
        self.r_phrases.setStringValue_("")
        self.r_steps.setString_("")
        self.r_status.setStringValue_("Nouvelle routine : donnez un nom, des phrases et des étapes, puis Enregistrer.")

    def _insert_step(self, line: str | None) -> None:
        if not line:
            return
        current = str(self.r_steps.string()).rstrip("\n")
        self.r_steps.setString_((current + "\n" if current else "") + line)

    def _parse_steps(self) -> list[dict]:
        steps = []
        for n, raw in enumerate(str(self.r_steps.string()).splitlines(), 1):
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                raise ConfigError(f"ligne {n} : {exc}") from exc
            if parts[0] == "wait":
                try:
                    steps.append({"wait": float(parts[1]) if len(parts) > 1 else 1})
                except ValueError as exc:
                    raise ConfigError(f"ligne {n} : durée de pause invalide") from exc
                continue
            given = {}
            for p in parts[1:]:
                if "=" not in p:
                    raise ConfigError(f"ligne {n} : « {p} » doit être de la forme argument=valeur")
                k, v = p.split("=", 1)
                given[k] = v
            steps.append({"run": parts[0], "with": given} if given else {"run": parts[0]})
        return steps

    def _save_routine(self) -> None:
        name = str(self.r_name.stringValue()).strip()
        phrases = [p.strip() for p in str(self.r_phrases.stringValue()).split(",") if p.strip()]
        try:
            steps = self._parse_steps()
        except ConfigError as exc:
            return self._routine_error(str(exc))
        if not name or not phrases or not steps:
            return self._routine_error("Il faut un nom, au moins une phrase et au moins une étape.")
        import unicodedata

        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
        slug = re.sub(r"^routine_?", "", slug)[:30] or "perso"
        rid = self.editing or f"routine_{slug}"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", rid):
            rid = "routine_perso"
        routine = {"id": rid, "label": name, "description": f"Lancer la routine « {name} »",
                   "examples": phrases, "steps": steps}
        user = self.user()
        user["routines"] = [r for r in user.get("routines") or [] if r.get("id") != rid] + [routine]
        user["removed_routines"] = [r for r in user.get("removed_routines") or [] if r != rid]
        settings = user.setdefault("settings", {})
        base_safe = list(settings.get("safe_commands") or self.engine.config.settings.safe_commands)
        settings["safe_commands"] = sorted(set(base_safe) | {rid})  # sans confirmation si rien de destructif
        try:
            save_user_config(user)
        except ConfigError:
            settings["safe_commands"] = sorted(set(base_safe) - {rid})
            try:
                save_user_config(user)
            except ConfigError as exc:
                return self._routine_error(str(exc))
        self.engine.jobs.put(("reload",))
        self.r_status.setTextColor_(NSColor.systemGreenColor())
        self.r_status.setStringValue_(f"✓ Routine enregistrée. Dites : « {phrases[0]} »")
        self.editing = rid
        from PyObjCTools import AppHelper

        AppHelper.callLater(0.6, self._fill_routines, rid)

    def _routine_error(self, message: str) -> None:
        self.r_status.setTextColor_(NSColor.systemRedColor())
        self.r_status.setStringValue_(f"✗ {message}")

    def _delete_routine(self) -> None:
        if not self.editing:
            return
        user = self.user()
        user["routines"] = [r for r in user.get("routines") or [] if r.get("id") != self.editing]
        user["removed_routines"] = sorted(set(user.get("removed_routines") or []) | {self.editing})
        if self.save(user):
            from PyObjCTools import AppHelper

            AppHelper.callLater(0.6, self._fill_routines)

    # connexions ---------------------------------------------------------------
    def page_accounts(self) -> Page:
        page = Page("Connexions", "Clés API, services et autorisations macOS. Les clés ne sont jamais affichées ; "
                                  "elles sont enregistrées dans le fichier .env du projet (jamais dans git).")
        rows = []
        for key, title, subtitle, icon in KEYS:
            present = env_has(key)
            state = text("✓ Configurée" if present else "Absente", 12, 0.3,
                         NSColor.systemGreenColor() if present else NSColor.secondaryLabelColor())
            rows.append(row(title, subtitle, hstack([state, self.button("Remplacer…" if present else "Ajouter…",
                                                                         lambda k=key, t=title: self._ask_key(k, t))], 10),
                            icon))
        page.section("Clés API", rows, footer="Le changement de clé TypeSafe est pris en compte au prochain lancement.")
        page.section("Services", [
            row("Spotify", "Connectez votre compte pour lancer un morceau précis et liker.",
                self.button("Se connecter…", self._spotify_login), "music.note"),
            row("Pilotage de Chrome (agent web)", "Cochez « Allow remote debugging », puis « Allow » à la 1re utilisation.",
                self.button("Ouvrir le réglage", lambda: subprocess.Popen(
                    ["open", "-a", "Google Chrome", "chrome://inspect/#remote-debugging"])), "globe"),
        ])
        from .audio import accessibility_trusted

        trusted = accessibility_trusted()
        panes = [("Accessibilité", "Privacy_Accessibility", "Touche de parole, menus, raccourcis clavier.", "accessibility",
                  trusted),
                 ("Surveillance de l'entrée", "Privacy_ListenEvent", "Touche de parole.", "keyboard", None),
                 ("Micro", "Privacy_Microphone", "Écoute (demandé au premier appui).", "mic", None),
                 ("Calendriers et rappels", "Privacy_Calendars", "Agenda (lecture locale), rappels.", "calendar", None)]
        prows = []
        for title, pane, subtitle, icon, ok in panes:
            badge = text("✓ Accordée" if ok else ("À accorder" if ok is False else ""), 12, 0.3,
                         NSColor.systemGreenColor() if ok else NSColor.systemOrangeColor())
            prows.append(row(title, subtitle, hstack([badge, self.button(
                "Ouvrir…", lambda p=pane: subprocess.Popen(
                    ["open", f"x-apple.systempreferences:com.apple.preference.security?{p}"]))], 10), icon))
        page.section("Autorisations macOS", prows,
                     footer="Accordez-les à « voxjev » (ou à votre terminal si vous lancez ./voxjev), puis relancez.")
        return page

    def _ask_key(self, key: str, title: str) -> None:
        a = NSAlert.alloc().init()
        a.setMessageText_(f"Clé {title}")
        a.setInformativeText_("Collez la clé. Elle sera enregistrée dans .env et ne sera plus jamais affichée.")
        field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
        a.setAccessoryView_(field)
        a.addButtonWithTitle_("Enregistrer")
        a.addButtonWithTitle_("Annuler")
        a.window().setInitialFirstResponder_(field)

        def done(code):
            if code != 1000:
                return
            try:
                env_set(key, str(field.stringValue()))
            except ValueError as exc:
                return self.alert("Clé refusée", str(exc))
            if key == "OPENROUTER_API_KEY":
                self.engine.jobs.put(("reload",))
            self.rebuild(select=4)

        a.beginSheetModalForWindow_completionHandler_(self.window, done)

    def _spotify_login(self) -> None:
        if not env_has("SPOTIFY_CLIENT_ID"):
            return self.alert("Client ID Spotify manquant",
                              "Créez une app sur developer.spotify.com (Redirect URI http://127.0.0.1:8888/callback), "
                              "puis ajoutez son Client ID ci-dessus.")
        env = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
        subprocess.Popen([sys.executable, "-m", "voxjev", "--spotify-login"], cwd=str(PROJECT), env=env)

    # avancé ---------------------------------------------------------------------
    def page_advanced(self) -> Page:
        page = Page("Avancé", "Fichiers, journal et outils.")
        caps = Path("~/Library/LaunchAgents/local.voxjev.capslock.plist").expanduser().exists()
        page.section("Clavier", [
            row("Verr. Maj comme touche de parole", "Remappe Verr. Maj en F18 (et règle la touche de parole sur F18).",
                self.switch(caps, self._capslock), "capslock"),
        ])
        page.section("Fichiers", [
            row("Configuration complète (commands.yaml)", "Commandes, modes, arguments. Pour les usages avancés.",
                self.button("Ouvrir", lambda: subprocess.Popen(["open", "-t", str(self.engine.config.path)])),
                "doc.text"),
            row("Vos réglages personnels", str(USER_CONFIG).replace(str(Path.home()), "~"),
                self.button("Afficher", lambda: subprocess.Popen(["open", "-R", str(USER_CONFIG)])
                            if USER_CONFIG.exists() else None), "person.crop.circle"),
            row("Journal des actions", "Tout ce que voxjev a exécuté (local, jamais envoyé).",
                self.button("Ouvrir", self._open_journal), "list.bullet.rectangle"),
            row("Recharger la configuration", "Après une modification manuelle des fichiers.",
                self.button("Recharger", lambda: self.engine.jobs.put(("reload",))), "arrow.clockwise"),
        ])
        page.section("Réinitialiser", [
            row("Revenir aux réglages d'origine", "Supprime vos réglages personnels (routines perso comprises).",
                self.button("Réinitialiser…", self._reset), "arrow.counterclockwise"),
        ])
        return page

    def _open_journal(self) -> None:
        from .context import JOURNAL

        if JOURNAL.exists():
            subprocess.Popen(["open", "-t", str(JOURNAL)])

    def _capslock(self, on: bool) -> None:
        script = PROJECT / "scripts" / "capslock.sh"
        subprocess.run([str(script), "install" if on else "uninstall"], capture_output=True, timeout=20)
        self.set_setting("hotkey", "f18" if on else "alt_r")

    def _reset(self) -> None:
        a = NSAlert.alloc().init()
        a.setMessageText_("Réinitialiser les réglages ?")
        a.setInformativeText_("Vos réglages personnels et vos routines perso seront supprimés. "
                              "config/commands.yaml n'est pas modifié.")
        a.addButtonWithTitle_("Réinitialiser")
        a.addButtonWithTitle_("Annuler")

        def done(code):
            if code == 1000:
                USER_CONFIG.unlink(missing_ok=True)
                self.engine.jobs.put(("reload",))
                self.rebuild(select=5)

        a.beginSheetModalForWindow_completionHandler_(self.window, done)

    # actions générales -----------------------------------------------------------
    def _hands_free(self, on: bool) -> None:
        if self.set_setting("hands_free", on):
            self.app.set_hands_free(on)

    def _wake_words(self, value: str) -> None:
        words = [w.strip().lower() for w in value.split(",") if w.strip()]
        if not words:
            return self.alert("Mot d'éveil manquant", "Indiquez au moins un mot, par exemple « jarvis ».")
        self.set_setting("wake_words", words)

    def _quiet(self, on: bool) -> None:
        self.engine.session.quiet = None  # le réglage de la fenêtre prime sur l'option du menu
        self.engine.session.save()
        self.set_setting("quiet_mode", on)

    def _try_voice(self) -> None:
        from .executor import speak

        speak("Bonjour, je suis voxjev. Que puis-je faire pour vous ?", self.engine.config.settings.voice)

    def _login_item(self, on: bool) -> None:
        app = Path("~/Applications/voxjev.app").expanduser()
        if on:
            script = (f'tell application "System Events" to make login item at end with properties '
                      f'{{path:"{app}", hidden:true, name:"voxjev"}}')
        else:
            script = 'tell application "System Events" to if exists login item "voxjev" then delete login item "voxjev"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)

    # -------------------------------------------------------------- fenêtre
    PAGES = (("Général", "gearshape", "page_general"), ("Confiance", "checkmark.shield", "page_trust"),
             ("Commandes", "command", "page_commands"), ("Routines", "list.bullet.rectangle.portrait", "page_routines"),
             ("Connexions", "key", "page_accounts"), ("Avancé", "slider.horizontal.3", "page_advanced"))

    def _page(self, index: int) -> Page:
        page = self.pages.get(index)
        if page is None:
            page = getattr(self, self.PAGES[index][2])()
            self.pages[index] = page
        return page

    def select(self, index: int, animated: bool = True) -> None:
        """Change de page : fondu + léger glissement vers le haut (ressort doux)."""
        from AppKit import NSAnimationContext
        import Quartz

        if index < 0 or index >= len(self.PAGES):
            return
        new = self._page(index).view
        old = self.current_view
        if new is old:
            return
        self.current = index
        self.current_view = new
        bounds = self.stage.bounds()
        new.setAutoresizingMask_(18)
        motion = animated and not _reduce_motion()
        new.setFrame_(NSMakeRect(0, -14 if motion else 0, bounds.size.width, bounds.size.height))
        new.setAlphaValue_(0.0 if motion else 1.0)
        self.stage.addSubview_(new)
        if self.sidebar_table.selectedRow() != index:
            self.sidebar_table.selectRowIndexes_byExtendingSelection_(
                __import__("Foundation").NSIndexSet.indexSetWithIndex_(index), False)
        if not motion:
            if old is not None:
                old.removeFromSuperview()
            return

        def changes(ctx):
            ctx.setDuration_(0.34)
            ctx.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.2, 0.9, 0.25, 1.0))
            new.animator().setAlphaValue_(1.0)
            new.animator().setFrame_(NSMakeRect(0, 0, bounds.size.width, bounds.size.height))
            if old is not None:
                old.animator().setAlphaValue_(0.0)

        def done():
            if old is not None and old is not self.current_view:
                old.removeFromSuperview()
                old.setAlphaValue_(1.0)

        NSAnimationContext.runAnimationGroup_completionHandler_(changes, done)

    def rebuild(self, select: int | None = None) -> None:
        """Reconstruit une page (après une modification qui change son contenu)."""
        index = self.current if select is None else select
        old = self.pages.pop(index, None)
        if old is not None and old.view is self.current_view:
            self.current_view = None
            old.view.removeFromSuperview()
        self.select(index, animated=False)

    def _build_window(self) -> None:
        from AppKit import (NSSplitViewController, NSSplitViewItem)

        self.keep = []
        self.pages = {}
        self.current, self.current_view = 0, None
        # barre latérale (Liquid Glass natif de macOS 26 via NSSplitViewItem.sidebar)
        side = NSViewController.alloc().init()
        side_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 230, 640))
        side.setView_(side_view)
        header = self._sidebar_header()
        side_view.addSubview_(header)
        pin(header, side_view, top=52, left=18, right=14)
        table_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 230, 400))
        table_scroll.setDrawsBackground_(False)
        table_scroll.contentView().setDrawsBackground_(False)
        table = NSTableView.alloc().initWithFrame_(table_scroll.bounds())
        table.setStyle_(3)  # source list
        table.setHeaderView_(None)
        table.setRowHeight_(34)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setIntercellSpacing_(NSMakeSize(0, 2))
        col = NSTableColumn.alloc().initWithIdentifier_("page")
        col.setWidth_(200)
        table.addTableColumn_(col)
        self.sidebar_source = SidebarSource.alloc().initWithWindow_(self)
        table.setDataSource_(self.sidebar_source)
        table.setDelegate_(self.sidebar_source)
        table_scroll.setDocumentView_(table)
        side_view.addSubview_(table_scroll)
        table_scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
        table_scroll.topAnchor().constraintEqualToAnchor_constant_(header.bottomAnchor(), 18).setActive_(True)
        table_scroll.leadingAnchor().constraintEqualToAnchor_(side_view.leadingAnchor()).setActive_(True)
        table_scroll.trailingAnchor().constraintEqualToAnchor_(side_view.trailingAnchor()).setActive_(True)
        table_scroll.bottomAnchor().constraintEqualToAnchor_constant_(side_view.bottomAnchor(), -12).setActive_(True)
        self.sidebar_table = table
        # contenu : fond « aurore » aux couleurs du logo + page courante
        content = NSViewController.alloc().init()
        content_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PAGE_W, PAGE_H))
        content.setView_(content_view)
        aurora = Aurora.alloc().initWithFrame_(content_view.bounds())
        aurora.setAutoresizingMask_(18)
        content_view.addSubview_(aurora)
        self.stage = NSView.alloc().initWithFrame_(content_view.bounds())
        self.stage.setAutoresizingMask_(18)
        content_view.addSubview_(self.stage)
        split = NSSplitViewController.alloc().init()
        sidebar_item = NSSplitViewItem.sidebarWithViewController_(side)
        sidebar_item.setMinimumThickness_(210)
        sidebar_item.setMaximumThickness_(260)
        split.addSplitViewItem_(sidebar_item)
        split.addSplitViewItem_(NSSplitViewItem.splitViewItemWithViewController_(content))
        style = 1 | 2 | 4 | 8 | (1 << 15)  # titrée, fermable, réductible, redimensionnable, contenu plein cadre
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 900, 660), style, 2, False)
        win.setContentViewController_(split)
        from AppKit import NSToolbar

        toolbar = NSToolbar.alloc().initWithIdentifier_("voxjev.settings")
        toolbar.setShowsBaselineSeparator_(False)
        win.setToolbar_(toolbar)
        win.setToolbarStyle_(3)  # unifiée : barre latérale flottante en Liquid Glass (macOS 26)
        win.setTitle_("Réglages de voxjev")
        win.setTitleVisibility_(1)  # masqué
        win.setTitlebarAppearsTransparent_(True)
        win.setMovableByWindowBackground_(True)
        win.setMinSize_(NSMakeSize(780, 520))
        win.setReleasedWhenClosed_(False)
        win.setContentSize_(NSMakeSize(900, 660))
        win.center()
        self.window = win
        table.reloadData()

    def _sidebar_header(self):
        from AppKit import NSImageView, NSWorkspace

        bundle = str(Path("~/Applications/voxjev.app").expanduser())
        icon = NSImageView.imageViewWithImage_(NSWorkspace.sharedWorkspace().iconForFile_(bundle))
        icon.setTranslatesAutoresizingMaskIntoConstraints_(False)
        icon.widthAnchor().constraintEqualToConstant_(46).setActive_(True)
        icon.heightAnchor().constraintEqualToConstant_(46).setActive_(True)
        name = text("voxjev", 17, 0.5)
        name.setFont_(_rounded(17, 0.5))
        sub = text("Assistant vocal · Jev", 11.5, 0.0, NSColor.secondaryLabelColor())
        return hstack([icon, vstack([name, sub], spacing=1)], spacing=10)

    def show(self, page: int = 0) -> None:
        nsapp = NSApplication.sharedApplication()
        if self.window is None:
            self._build_window()
            self.select(page, animated=False)
            self.window.setAlphaValue_(0.0)
            self.window.makeKeyAndOrderFront_(None)
            from AppKit import NSAnimationContext

            NSAnimationContext.runAnimationGroup_completionHandler_(
                lambda ctx: (ctx.setDuration_(0.25), self.window.animator().setAlphaValue_(1.0)), None)
        else:
            self.pages = {}  # contenu à jour (config rechargée)
            self.current_view.removeFromSuperview() if self.current_view is not None else None
            self.current_view = None
            self.select(self.current, animated=False)
        nsapp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)


def _reduce_motion() -> bool:
    try:
        from AppKit import NSWorkspace

        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


class SidebarSource(NSObject):
    """Barre latérale : tuiles colorées + libellés ; la sélection change de page."""

    def initWithWindow_(self, win):
        self = objc.super(SidebarSource, self).init()
        if self is None:
            return None
        self.win = win
        return self

    def numberOfRowsInTableView_(self, table):
        return len(SettingsWindow.PAGES)

    def tableView_viewForTableColumn_row_(self, table, column, i):
        title, icon, _ = SettingsWindow.PAGES[i]
        label = text(title, 13.5, 0.0)
        cell = hstack([tile(icon, 24), label], spacing=10)
        cell.setEdgeInsets_((0, 8, 0, 8))
        return cell

    def tableViewSelectionDidChange_(self, notification):
        self.win.select(int(notification.object().selectedRow()))


class Aurora(NSView):
    """Fond vivant aux couleurs du logo : grands halos flous qui dérivent lentement, pour que le
    Liquid Glass des cartes ait quelque chose à réfracter. Suit le thème clair/sombre."""

    BLOBS = (((10, 132, 255), (0.15, 0.85), 0.9), ((90, 200, 250), (0.85, 0.7), 0.7),
             ((255, 159, 10), (0.25, 0.15), 0.55), ((191, 90, 242), (0.9, 0.2), 0.45))

    def initWithFrame_(self, frame):
        import Quartz

        self = objc.super(Aurora, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        self.blobs = []
        for color, (fx, fy), alpha in self.BLOBS:
            g = Quartz.CAGradientLayer.layer()
            g.setType_("radial")
            g.setStartPoint_((0.5, 0.5))
            g.setEndPoint_((1.0, 1.0))
            g.setColors_([Quartz.CGColorCreateSRGB(color[0] / 255, color[1] / 255, color[2] / 255, 0.30 * alpha),
                          Quartz.CGColorCreateSRGB(color[0] / 255, color[1] / 255, color[2] / 255, 0.0)])
            self.layer().addSublayer_(g)
            self.blobs.append((g, fx, fy))
        self._layout_blobs()
        if not _reduce_motion():
            for i, (g, _, _) in enumerate(self.blobs):
                drift = Quartz.CABasicAnimation.animationWithKeyPath_("transform")
                drift.setFromValue_(Quartz.NSValue.valueWithCATransform3D_(Quartz.CATransform3DIdentity))
                t = Quartz.CATransform3DMakeTranslation((-1) ** i * 60, (-1) ** (i // 2) * 40, 0)
                drift.setToValue_(Quartz.NSValue.valueWithCATransform3D_(Quartz.CATransform3DScale(t, 1.15, 1.15, 1)))
                drift.setDuration_(11 + 3 * i)
                drift.setAutoreverses_(True)
                drift.setRepeatCount_(1e9)
                drift.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_("easeInEaseOut"))
                g.addAnimation_forKey_(drift, "drift")
        return self

    @objc.python_method
    def _layout_blobs(self) -> None:
        b = self.bounds()
        size = max(b.size.width, b.size.height) * 0.95
        for g, fx, fy in self.blobs:
            g.setFrame_(((b.size.width * fx - size / 2, b.size.height * fy - size / 2), (size, size)))

    def setFrameSize_(self, size):
        objc.super(Aurora, self).setFrameSize_(size)
        self._layout_blobs()
