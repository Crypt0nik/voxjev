"""Traitement d'un énoncé : contexte -> Jev (1 appel) -> décision -> arguments -> plan -> exécution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .actions import ActionError, Step, plan_command
from .args import ArgResult, extract_args
from .config import Config
from .candidates import Provider
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
    messages: list[str] = field(default_factory=list)  # réponses à afficher / lire (agenda, question…)
    candidates: dict = field(default_factory=dict, repr=False)  # candidats montrés à Jev (menus…)

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
                 apps: tuple[str, ...] | None = None, provider: Provider | None = None):
        self.config = config
        self.client = client
        self.session = session
        self.executor = executor
        self.confirmer = confirmer
        self.dry_run = dry_run or executor is None
        self._frontmost = frontmost
        self._apps = apps
        self.current: Outcome | None = None  # énoncé en cours (lu par les confirmateurs graphiques)
        self.provider = provider or Provider()
        self._warm_at = 0.0
        self._spec: dict = {}  # clé de requête -> (instant, future) : réponses anticipées
        self._pool = None
        if executor is not None:
            if getattr(executor, "client", "absent") is None:
                executor.client = client
            if getattr(executor, "settings", "absent") is None:
                executor.settings = config.settings

    @property
    def settings(self):
        """Réglages de la config, avec le mode sans confirmation choisi dans le menu."""
        from dataclasses import replace

        s = self.config.settings
        return s if self.session.quiet is None else replace(s, quiet_mode=self.session.quiet)

    def _request(self, transcript: str, mode: str):
        commands = self.config.commands_for_mode(mode)
        state = build_state(transcript, self._frontmost(), self.apps, mode, self.session.last_command)
        cands = self.provider.gather(self.sources_for(commands), transcript)
        hints = {}
        if cands.get("shortcut"):
            names = ", ".join(c.label for c in cands["shortcut"][:40])
            hints = {c.id: f"raccourcis de l'utilisateur : {names}" for c in commands
                     if any(a.type == "pick" and a.source == "shortcut" for a in c.args.values())}
        return state, commands, cands, hints

    @staticmethod
    def _key(state, commands, cands, hints) -> str:
        import json

        return json.dumps([state, [c.id for c in commands], {k: [c.label for c in v] for k, v in cands.items()},
                           hints], sort_keys=True, ensure_ascii=False)

    def speculate(self, transcript: str) -> None:
        """Transcription partielle pendant l'appui : lance l'appel Jev en avance (sans rien exécuter).

        Si la phrase finale est identique, ``plan`` réutilise la réponse : la latence Jev disparaît.
        """
        from concurrent.futures import ThreadPoolExecutor

        transcript = transcript.strip()
        if not transcript:
            return
        state, commands, cands, hints = self._request(transcript, self.session.mode)
        key = self._key(state, commands, cands, hints)
        if key in self._spec:
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voxjev-spec")
        now = time.monotonic()
        self._spec = {k: v for k, v in self._spec.items() if now - v[0] < 20}
        self._spec[key] = (now, self._pool.submit(self.client.evaluate, state, commands,
                                                   self.config.settings.none_option, cands or None, hints or None))

    def sources_for(self, commands) -> set[str]:
        """Sources de candidats utiles pour ces commandes (menus, Raccourcis, segments)."""
        out = set()
        for c in commands:
            for spec in c.args.values():
                if spec.type == "pick":
                    out.add(spec.source)
                elif spec.type == "text" and spec.span and spec.patterns:
                    out.add("span")
        return out

    def prefetch(self) -> None:
        """À l'appui sur la touche : lit les menus / Raccourcis et chauffe la connexion Jev pendant qu'on parle."""
        self.provider.prefetch(self.sources_for(self.config.commands_for_mode(self.session.mode)))
        if time.monotonic() - self._warm_at > 45 and hasattr(self.client, "warm"):
            self._warm_at = time.monotonic()
            import threading

            threading.Thread(target=self.client.warm, daemon=True, name="voxjev-warm").start()

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
        s = self.settings
        mode = mode or self.session.mode
        t0 = time.perf_counter()
        state, commands, cands, hints = self._request(transcript, mode)
        out.candidates = cands
        out.timings["context_ms"] = (time.perf_counter() - t0) * 1000

        try:
            key = self._key(state, commands, cands, hints)
            spec = self._spec.get(key)
            if spec is not None and time.monotonic() - spec[0] < 20:
                out.result = spec[1].result(timeout=10)  # réponse anticipée pendant qu'on parlait
                out.timings["speculated"] = 1
            else:
                out.result = self.client.evaluate(state, commands, s.none_option, picks=cands or None,
                                                  hints=hints or None)
        except JevError as exc:
            out.status, out.error = "error", str(exc)
            return out
        out.timings["jev_ms"] = out.result.latency_ms

        out.decision = decide(out.result, {c.id: c for c in commands}, s)
        if out.decision.verdict == Verdict.IGNORE:
            return out

        cmd = out.decision.command
        assert cmd is not None
        picks = out.result.picks
        out.args = extract_args(cmd, transcript, self.config, self.apps, picks, cands)
        if not out.args.ok:
            # Repli : la 2e option de Jev, si elle est proche et que SES arguments sont valides
            # (« ouvre YouTube » : open_app 0,52 sans app installée -> open_website 0,47).
            by_id = {c.id: c for c in commands}
            for alt_id, alt_p in out.result.runners_up(1):
                alt = by_id.get(alt_id)
                if alt is None or alt_p < s.fallback_min_p:
                    continue
                alt_args = extract_args(alt, transcript, self.config, self.apps, picks, cands)
                if alt_args.ok:
                    reason = f"repli sur la 2e option ({alt_id} p={alt_p:.2f}) : {cmd.id} sans argument valide"
                    quiet = s.quiet_mode and alt.id in s.safe_commands and not alt.destructive
                    out.decision = Decision(Verdict.EXECUTE if quiet else Verdict.CONFIRM, alt, reason,
                                            destructive=alt.destructive, code="safe" if quiet else "fallback")
                    cmd, out.args = alt, alt_args
                    break
        if not out.args.ok:
            out.status, out.error = "error", f"argument(s) manquant(s) pour {cmd.id} : {', '.join(out.args.missing)}"
            return out
        if out.args.destructive and out.decision.verdict == Verdict.EXECUTE:
            out.decision = Decision(Verdict.CONFIRM, cmd, "élément au nom destructeur", destructive=True,
                                    code="destructive")
        try:
            if cmd.action.get("type") == "undo":
                out.steps = self._plan_undo(out)
            else:
                out.steps = plan_command(cmd, out.args.values, self.config, self.apps, data=out.args.data)
            out.steps = self._resolve(out, out.steps)
        except ActionError as exc:
            out.status, out.error = "error", str(exc)
            return out
        out.status = "planned"
        return out

    def _resolve(self, out: Outcome, steps: list[Step]) -> list[Step]:
        """Étapes qui demandent un 2e choix de Jev (fichier, souvenir), faites au moment du plan
        pour que la confirmation montre le fichier / le souvenir retenu."""
        s = self.config.settings
        resolved = []
        for step in steps:
            if step.kind == "file_open":
                from .files import resolve

                hit, p, hits = resolve(self.client, out.transcript, step.argv[0], s.pick_min_p)
                if hit is None:
                    raise ActionError(f"aucun fichier trouvé pour « {step.argv[0]} »" if not hits
                                      else f"{len(hits)} fichiers possibles, aucun ne correspond clairement")
                reveal = len(step.argv) > 1
                resolved.append(Step("run", ("open", "-R", hit.path) if reveal else ("open", hit.path),
                                     label=f"{'Montrer' if reveal else 'Ouvrir'} {hit.name} ({hit.parent})"))
                out.args.display["fichier"] = hit.name
                if p < s.threshold and out.decision.verdict == Verdict.EXECUTE:
                    out.decision = Decision(Verdict.CONFIRM, out.decision.command,
                                            f"fichier incertain (p={p:.2f})", code="medium_p")
            elif step.kind in ("memory_ask", "memory_forget"):
                from .memory import Memory

                fact, p = Memory().find(self.client, step.argv[0], s.pick_min_p)
                if fact is None:
                    raise ActionError("je n'ai rien retenu à ce sujet")
                if step.kind == "memory_ask":
                    resolved.append(Step("say", (f"Vous m'avez dit : {fact}",), label="Souvenir"))
                else:
                    resolved.append(Step("memory_forget", (fact,), label=f"Oublier « {fact[:60]} »"))
                    out.args.display["souvenir"] = fact
            else:
                resolved.append(step)
        return resolved

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
            out.messages = list(self.executor.run(out.steps) or [])
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
