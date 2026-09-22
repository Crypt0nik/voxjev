"""Traitement d'un énoncé : contexte -> Jev (1 appel) -> décision -> arguments -> plan -> exécution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .actions import ActionError, Step, plan_command
from .args import ArgResult, extract_args
from .config import Config
from .context import Session, frontmost_app, installed_apps, journal
from .decide import Decision, Verdict, decide
from .jev_client import DecisionClient, JevError, JevResult, build_state


class Executor(Protocol):
    def run(self, steps: list[Step]) -> None: ...


# (décision, arguments, étapes) -> True si l'utilisateur confirme
Confirmer = Callable[[Decision, ArgResult, list[Step]], bool]


@dataclass
class Outcome:
    transcript: str
    status: str = "ignored"  # planned | executed | dry_run | ignored | cancelled | error
    result: JevResult | None = None
    decision: Decision | None = None
    args: ArgResult | None = None
    steps: list[Step] = field(default_factory=list)
    error: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def command_id(self) -> str | None:
        return self.decision.command.id if self.decision and self.decision.command else None

    @property
    def triggered(self) -> bool:
        """La commande aurait été exécutée (directement ou après confirmation)."""
        return bool(self.decision and self.decision.verdict != Verdict.IGNORE)


class Launcher:
    def __init__(self, config: Config, client: DecisionClient, session: Session, *,
                 executor: Executor | None = None, confirmer: Confirmer | None = None,
                 dry_run: bool = False, frontmost: Callable[[], str | None] = frontmost_app,
                 apps: tuple[str, ...] | None = None):
        self.config = config
        self.client = client
        self.session = session
        self.executor = executor
        self.confirmer = confirmer
        self.dry_run = dry_run or executor is None
        self._frontmost = frontmost
        self._apps = apps
        self.current: Outcome | None = None  # énoncé en cours (lu par les confirmateurs graphiques)

    @property
    def apps(self) -> tuple[str, ...]:
        return self._apps if self._apps is not None else installed_apps(self.config.settings.app_dirs)

    def handle(self, transcript: str) -> Outcome:
        """Un énoncé simple : planifier, puis confirmer/exécuter."""
        return self.finish(self.plan(transcript))

    def plan(self, transcript: str, mode: str | None = None) -> Outcome:
        """Contexte -> Jev -> décision -> arguments -> étapes. N'exécute rien.

        Statut en sortie : ``planned`` (prêt), ``ignored`` ou ``error``. ``mode`` permet de
        planifier une sous-étape d'une demande composée dans le mode qu'elle aura à l'exécution.
        """
        out = Outcome(transcript=transcript)
        self.current = out
        transcript = transcript.strip()
        if not transcript:
            out.error = "transcript vide"
            return out
        s = self.config.settings
        mode = mode or self.session.mode
        t0 = time.perf_counter()
        commands = self.config.commands_for_mode(mode)
        state = build_state(transcript, self._frontmost(), self.apps, mode, self.session.last_command)
        out.timings["context_ms"] = (time.perf_counter() - t0) * 1000

        try:
            out.result = self.client.evaluate(state, commands, s.none_option)
        except JevError as exc:
            out.status, out.error = "error", str(exc)
            return out
        out.timings["jev_ms"] = out.result.latency_ms

        out.decision = decide(out.result, {c.id: c for c in commands}, s)
        if out.decision.verdict == Verdict.IGNORE:
            return out

        cmd = out.decision.command
        assert cmd is not None
        out.args = extract_args(cmd, transcript, self.config, self.apps)
        if not out.args.ok:
            # Repli : la 2e option de Jev, si elle est proche et que SES arguments sont valides
            # (« ouvre YouTube » : open_app 0,52 sans app installée -> open_website 0,47).
            by_id = {c.id: c for c in commands}
            for alt_id, alt_p in out.result.runners_up(1):
                alt = by_id.get(alt_id)
                if alt is None or alt_p < s.fallback_min_p:
                    continue
                alt_args = extract_args(alt, transcript, self.config, self.apps)
                if alt_args.ok:
                    reason = f"repli sur la 2e option ({alt_id} p={alt_p:.2f}) : {cmd.id} sans argument valide"
                    out.decision = Decision(Verdict.CONFIRM, alt, reason, destructive=alt.destructive,
                                            code="fallback")
                    cmd, out.args = alt, alt_args
                    break
        if not out.args.ok:
            out.status, out.error = "error", f"argument(s) manquant(s) pour {cmd.id} : {', '.join(out.args.missing)}"
            return out
        try:
            if cmd.action.get("type") == "undo":
                out.steps = self._plan_undo(out)
            else:
                out.steps = plan_command(cmd, out.args.values, self.config, self.apps)
        except ActionError as exc:
            out.status, out.error = "error", str(exc)
            return out
        out.status = "planned"
        return out

    def _plan_undo(self, out: Outcome):
        """« annule ça » : rejoue l'action inverse déclarée (`undo:`) de la dernière commande."""
        from .actions import plan_action

        last = self.config.commands.get(self.session.last_command or "")
        if last is None:
            raise ActionError("rien à annuler")
        if not last.undo:
            raise ActionError(f"« {last.short(self.session.last_args)} » ne peut pas être annulé")
        values = dict(self.session.last_args) | {"previous_mode": self.session.previous_mode or self.config.default_mode}
        steps = plan_action(last.undo, last, values, self.config, self.apps)
        out.args.values["annule"] = last.short(self.session.last_args)
        if last.destructive or last.undo.get("type") == "quit_app":  # fermer une app : toujours confirmer
            out.decision = Decision(Verdict.CONFIRM, out.decision.command, "annulation = quitter une app",
                                    destructive=True, code="destructive")
        return steps

    def finish(self, out: Outcome) -> Outcome:
        """Termine un énoncé planifié : dry-run, confirmation si nécessaire, exécution."""
        self.current = out
        if out.status != "planned":
            return out
        if self.dry_run:
            out.status = "dry_run"
            return out
        if out.decision.verdict == Verdict.CONFIRM:
            if not (self.confirmer and self.confirmer(out.decision, out.args, out.steps)):
                out.status = "cancelled"
                return out
        return self.execute(out)

    def execute(self, out: Outcome) -> Outcome:
        """Exécute un énoncé planifié, sans confirmation (déjà obtenue par l'appelant)."""
        cmd = out.decision.command
        t1 = time.perf_counter()
        try:
            assert self.executor is not None
            self.executor.run(out.steps)
        except ActionError as exc:
            out.status, out.error = "error", str(exc)
            return out
        finally:
            out.timings["action_ms"] = (time.perf_counter() - t1) * 1000
        for step in out.steps:
            if step.kind == "set_mode":
                self.session.previous_mode, self.session.mode = self.session.mode, step.mode
        if cmd.action.get("type") != "undo":
            self.session.last_command = cmd.id
            self.session.last_args = dict(out.args.values) if out.args else {}
        else:
            self.session.last_command = None  # une annulation ne s'annule pas
        self.session.save()
        out.status = "executed"
        journal({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "transcript": out.transcript, "command": cmd.id,
                 "args": out.args.values if out.args else {}, "mode": self.session.mode})
        return out
