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
    NSTabViewController,
    NSTabViewItem,
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
PAGE_W, PAGE_H = 660.0, 560.0
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


def symbol(name: str, size: float = 15):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    from AppKit import NSImageSymbolConfiguration, NSImageView

    view = NSImageView.imageViewWithImage_(img.imageWithSymbolConfiguration_(
        NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, 0.2)))
    view.setContentTintColor_(NSColor.controlAccentColor())
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


class Flipped(NSView):
    def isFlipped(self):
        return True


class Page:
    """Une page de réglages : titre, sections groupées (façon Réglages Système), défilement."""

    def __init__(self, title: str, subtitle: str):
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PAGE_W, PAGE_H))
        self.scroll = NSScrollView.alloc().initWithFrame_(self.view.bounds())
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setAutoresizingMask_(18)
        self.doc = Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, PAGE_W, PAGE_H))
        self.scroll.setDocumentView_(self.doc)
        self.view.addSubview_(self.scroll)
        self.stack = vstack([], spacing=10)
        self.doc.addSubview_(self.stack)
        pin(self.stack, self.doc, top=22, left=28, right=28, bottom=24)
        self.doc.setTranslatesAutoresizingMaskIntoConstraints_(False)
        clip = self.scroll.contentView()
        self.doc.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()).setActive_(True)
        self.doc.trailingAnchor().constraintEqualToAnchor_(clip.trailingAnchor()).setActive_(True)
        self.doc.topAnchor().constraintEqualToAnchor_(clip.topAnchor()).setActive_(True)
        self.add(text(title, 22, 0.3))
        self.add(text(subtitle, 12.5, 0.0, NSColor.secondaryLabelColor(), wrap=True), after=14)

    def add(self, view, after: float = 10):
        self.stack.addArrangedSubview_(view)
        self.stack.setCustomSpacing_afterView_(after, view)
        if view.isKindOfClass_(NSBox) or view.isKindOfClass_(NSStackView) or view.isKindOfClass_(NSScrollView):
            view.widthAnchor().constraintEqualToAnchor_constant_(self.stack.widthAnchor(), 0).setActive_(True)
        return view

    def section(self, title: str | None, rows: list, footer: str | None = None):
        if title:
            self.add(text(title.upper(), 11, 0.3, NSColor.secondaryLabelColor()), after=6)
        box = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 40))
        box.setBoxType_(4)
        box.setTitlePosition_(0)
        box.setBorderWidth_(0)
        box.setCornerRadius_(12)
        box.setFillColor_(NSColor.labelColor().colorWithAlphaComponent_(0.045))
        box.setContentViewMargins_(NSMakeSize(0, 0))
        inner = vstack([], spacing=0, alignment=1)
        for i, row in enumerate(rows):
            if i:
                sep = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 1))
                sep.setBoxType_(2)  # séparateur
                inner.addArrangedSubview_(sep)
                sep.widthAnchor().constraintEqualToAnchor_constant_(inner.widthAnchor(), -24).setActive_(True)
            inner.addArrangedSubview_(row)
            row.widthAnchor().constraintEqualToAnchor_(inner.widthAnchor()).setActive_(True)
        inner.setAlignment_(9)  # centre horizontal (séparateurs centrés)
        box.contentView().addSubview_(inner)
        pin(inner, box.contentView(), bottom=0)
        self.add(box, after=6 if footer else 18)
        if footer:
            self.add(text(footer, 11, 0.0, NSColor.secondaryLabelColor(), wrap=True), after=18)
        return box


def row(title: str, subtitle: str | None, control, icon: str | None = None):
    labels = [text(title, 13, 0.0)]
    if subtitle:
        sub = text(subtitle, 11, 0.0, NSColor.secondaryLabelColor(), wrap=True)
        sub.setPreferredMaxLayoutWidth_(360)
        labels.append(sub)
    left = vstack(labels, spacing=2)
    left.setContentHuggingPriority_forOrientation_(1, 0)
    parts = ([symbol(icon)] if icon else []) + [left]
    h = hstack(parts, spacing=12)
    if control is not None:  # contrôle aligné à droite, comme dans Réglages Système
        if control.isKindOfClass_(NSStackView):
            control.setHuggingPriority_forOrientation_(750, 0)
        else:
            control.setContentHuggingPriority_forOrientation_(750, 0)
        h.addView_inGravity_(control, 3)  # NSStackViewGravityTrailing
    h.setEdgeInsets_((10, 14, 10, 14))
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
        sw.setState_(1 if on else 0)
        sw.setTarget_(self.act(lambda s: fn(bool(s.state()))))
        sw.setAction_("fire:")
        return sw

    def button(self, title: str, fn, prominent: bool = False):
        b = NSButton.buttonWithTitle_target_action_(title, self.act(lambda s: fn()), "fire:")
        if prominent:
            b.setKeyEquivalent_("\r")
        return b

    def popup(self, items: list[tuple[str, str]], current: str, fn):
        p = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 220, 26), False)
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
        f = NSTextField.textFieldWithString_(value)
        f.setPlaceholderString_(placeholder)
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
        search.setPlaceholderString_("Rechercher une commande")
        page.add(search, after=10)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 380))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setRowHeight_(24)
        table.setStyle_(1)  # plein
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
        scroll.heightAnchor().constraintEqualToConstant_(380).setActive_(True)
        page.add(scroll)
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
        self.routine_list.setTarget_(self.act(lambda s: self._load_routine()))
        self.routine_list.setAction_("fire:")
        page.add(hstack([self.routine_list, self.button("Nouvelle", self._new_routine),
                         self.button("Supprimer", self._delete_routine)], 8), after=14)
        self.r_name = NSTextField.textFieldWithString_("")
        self.r_name.setPlaceholderString_("Nom affiché (ex. Routine du soir)")
        self.r_phrases = NSTextField.textFieldWithString_("")
        self.r_phrases.setPlaceholderString_("lance ma routine du soir, mode soirée")
        for f in (self.r_name, self.r_phrases):
            f.widthAnchor().constraintEqualToConstant_(360).setActive_(True)
        steps_scroll = NSTextView.scrollableTextView()
        self.r_steps = steps_scroll.documentView()
        self.r_steps.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.5, 0.0))
        self.r_steps.setAutomaticQuoteSubstitutionEnabled_(False)
        self.r_steps.setAutomaticDashSubstitutionEnabled_(False)
        self.r_steps.setRichText_(False)
        steps_scroll.heightAnchor().constraintEqualToConstant_(150).setActive_(True)
        steps_scroll.widthAnchor().constraintEqualToConstant_(360).setActive_(True)
        cmds = sorted(self.engine.config.commands.values(), key=lambda c: c.id)
        adder = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 200, 26), True)
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

    def build(self, select: int = 0):
        self.keep = []
        tabs = NSTabViewController.alloc().init()
        tabs.setTabStyle_(TAB_TOOLBAR)
        self.pages = []
        for title, icon, method in self.PAGES:
            page = getattr(self, method)()
            self.pages.append(page)
            vc = NSViewController.alloc().init()
            vc.setView_(page.view)
            vc.setTitle_(title)
            vc.setPreferredContentSize_(NSMakeSize(PAGE_W, PAGE_H))
            item = NSTabViewItem.tabViewItemWithViewController_(vc)
            item.setLabel_(title)
            item.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(icon, title))
            tabs.addTabViewItem_(item)
        tabs.setSelectedTabViewItemIndex_(select)
        return tabs

    def rebuild(self, select: int = 0) -> None:
        if self.window is not None:
            frame = self.window.frame()
            self.window.setContentViewController_(self.build(select))
            self.window.setFrame_display_(frame, True)

    def show(self) -> None:
        nsapp = NSApplication.sharedApplication()
        if self.window is None:
            self.window = NSWindow.windowWithContentViewController_(self.build())
            self.window.setTitle_("Général")
            self.window.setStyleMask_(self.window.styleMask() & ~(1 << 3))  # pas de redimensionnement
            self.window.setToolbarStyle_(2)  # préférences
            self.window.setReleasedWhenClosed_(False)
            self.window.center()
        else:
            self.rebuild(0)
        nsapp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
