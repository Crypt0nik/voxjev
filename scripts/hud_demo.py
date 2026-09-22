"""Démo du HUD : fait défiler les états et capture l'écran à chaque étape.

    PYTHONPATH=src .venv/bin/python scripts/hud_demo.py [dossier_de_sortie]
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

from AppKit import NSApplication, NSScreen
from PyObjCTools import AppHelper

from voxjev.args import ArgResult
from voxjev.config import load_config
from voxjev.decide import Decision, Verdict
from voxjev.hud import HUD
from voxjev.jev_client import NONE, JevResult
from voxjev.pipeline import Outcome

OUT = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), "/tmp/voxjev-hud"))
OUT.mkdir(parents=True, exist_ok=True)
config = load_config()
s = config.settings
app = NSApplication.sharedApplication()
app.setActivationPolicy_(1)
hud = HUD(on_confirm=lambda ok: print("réponse", ok))
if "--dark" in sys.argv:
    from AppKit import NSAppearance

    hud.panel.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
hud.set_mode("defaut", False)

if "--backdrop" in sys.argv:  # fond neutre derrière le HUD (captures publiables, sans le bureau)
    from AppKit import NSBackingStoreBuffered, NSColor, NSMakeRect, NSWindow

    from voxjev.settings import Aurora

    frame = NSScreen.mainScreen().frame()
    back = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(frame.origin.x + frame.size.width / 2 - 360, frame.origin.y + frame.size.height - 560, 720, 560),
        0, NSBackingStoreBuffered, False)
    back.setLevel_(0)
    back.setBackgroundColor_(NSColor.windowBackgroundColor())
    view = Aurora.alloc().initWithFrame_(back.contentView().bounds())
    view.setAutoresizingMask_(18)
    back.contentView().addSubview_(view)
    back.orderFrontRegardless()


def shot(name: str):
    def go():
        f = hud.panel.frame()
        screen = NSScreen.screens()[0].frame()
        x, y = f.origin.x - 30, screen.size.height - (f.origin.y + f.size.height) - 30
        subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{f.size.width + 60},{f.size.height + 60}",
                        str(OUT / f"{name}.png")])
        print("capture", name)
    return go


def outcome(status: str, cmd_id: str, text: str, p=0.97, **args) -> Outcome:
    cmd = config.commands[cmd_id]
    r = JevResult(cmd_id, {cmd_id: p, "open_website": 0.02, NONE: 0.01}, p, 0.94, 0.03, latency_ms=284)
    verdict = Verdict.CONFIRM if status == "confirm" else Verdict.EXECUTE
    d = Decision(verdict, cmd, "", code="ok" if verdict == Verdict.EXECUTE else "destructive",
                 destructive=cmd.destructive)
    out = Outcome(transcript=text, status=status, result=r, decision=d, args=ArgResult(values=args))
    out.timings = {"stt_ms": 1140, "total_ms": 1720, "action_ms": 64}
    return out


t = 0.5
steps = []


def at(delay, fn, *a):
    global t
    t += delay
    AppHelper.callLater(t, fn, *a)


at(0, hud.phase, "listening", "Écoute…")
for i in range(40):
    at(0.035, hud.level_push, 0.02 + 0.09 * abs(math.sin(i / 3.0)))
at(0.2, shot("1-ecoute"))
at(0.4, hud.show_transcript, "Ouvre Spotify et mets du jazz")
at(0.1, hud.phase, "thinking", "Jev réfléchit…", 0, True)
at(0.9, shot("2-reflexion"))
at(0.3, hud.show_outcome, outcome("executed", "open_app", "Ouvre Spotify", app="Spotify"), s)
at(1.2, shot("3-fait"))
out = outcome("confirm", "empty_trash", "Vide la corbeille", p=0.99)
at(0.5, hud.ask_confirm, out, s, 10, "⌥ droite")
at(1.6, shot("4-confirmation"))
at(0.4, hud.show_message, "Minuteur", "Minuteur lancé : 10 min (pâtes).", "done", 3)
at(1.0, shot("5-message"))
at(1.0, AppHelper.stopEventLoop)
AppHelper.runEventLoop()
