"""Traitement d'un énoncé : contexte -> Jev (1 appel) -> décision -> arguments -> plan -> exécution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .actions import ActionError, Step, plan_command
from .args import ArgResult, extract_args
from .config import Config
from .context import Session, frontmost_app, installed_apps
from .decide import Decision, Verdict, decide
from .jev_client import DecisionClient, JevError, JevResult, build_state


class Executor(Protocol):
    def run(self, steps: list[Step]) -> None: ...


# (décision, arguments, étapes) -> True si l'utilisateur confirme
Confirmer = Callable[[Decision, ArgResult, list[Step]], bool]


@dataclass
class Outcome:
    transcript: str
    status: str = "ignored"  # executed | dry_run | ignored | cancelled | error
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
        out = Outcome(transcript=transcript)
        self.current = out
        transcript = transcript.strip()
        if not transcript:
            out.error = "transcript vide"
            return out
        s = self.config.settings
        t0 = time.perf_counter()
        commands = self.config.commands_for_mode(self.session.mode)
        state = build_state(transcript, self._frontmost(), self.apps, self.session.mode, self.session.last_command)
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
            out.status, out.error = "error", f"argument(s) manquant(s) pour {cmd.id} : {', '.join(out.args.missing)}"
            return out
        try:
            out.steps = plan_command(cmd, out.args.values, self.config, self.apps)
        except ActionError as exc:
            out.status, out.error = "error", str(exc)
            return out

        if self.dry_run:
            out.status = "dry_run"
            return out
        if out.decision.verdict == Verdict.CONFIRM:
            if not (self.confirmer and self.confirmer(out.decision, out.args, out.steps)):
                out.status = "cancelled"
                return out

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
                self.session.mode = step.mode
        self.session.last_command = cmd.id
        self.session.save()
        out.status = "executed"
        return out
