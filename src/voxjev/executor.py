"""Exécution des étapes planifiées + retours (sons, dialogue de confirmation)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .actions import ActionError, Step

STEP_TIMEOUT = 15


class SubprocessExecutor:
    """Lance chaque étape via ``subprocess.run(argv)`` — jamais de shell."""

    def run(self, steps: list[Step]) -> None:
        for step in steps:
            if step.kind != "run" or not step.argv:
                continue
            try:
                proc = subprocess.run(list(step.argv), capture_output=True, text=True,
                                      timeout=STEP_TIMEOUT, shell=False, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ActionError(f"échec de « {step.label} » : {exc}") from exc
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                raise ActionError(f"échec de « {step.label} » (code {proc.returncode})"
                                  + (f" : {detail[-1]}" if detail else ""))


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
