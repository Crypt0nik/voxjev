"""Point d'entrée : `voxjev --text "..." [--dry-run]`, `voxjev --eval cases.tsv`, `voxjev` (micro)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import DEFAULT_CONFIG, ConfigError, load_config
from .context import Session
from .decide import Verdict
from .pipeline import Launcher, Outcome

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_COLORS = {"executed": "32", "dry_run": "36", "ignored": "90", "cancelled": "33", "error": "31"}


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def format_outcome(out: Outcome, mode: str) -> str:
    lines = [f"« {out.transcript} »  " + _c(f"[mode {mode}]", "90")]
    r = out.result
    if r:
        others = " · ".join(f"{k} {v:.2f}" for k, v in r.runners_up(2))
        tok = f", {r.input_tokens} tok" if r.input_tokens else ""
        lines.append(
            f"  Jev {r.latency_ms:.0f} ms ({r.model or 'fake'}{tok}) → {_c(r.command, '1')} "
            f"p={r.p_command:.2f} conf={r.confidence:.2f} | {others} | "
            f"adressé {r.addressed:.2f} | destructif {r.destructive:.2f}"
        )
    d = out.decision
    if d:
        color = {"execute": "32", "confirm": "33", "ignore": "90"}[d.verdict.value]
        lines.append(f"  Décision : {_c(d.verdict.value.upper(), color)} ({d.reason})")
    if out.args and out.args.values:
        lines.append("  Arguments : " + " ".join(f"{k}={v!r}" for k, v in out.args.values.items()))
    for step in out.steps:
        lines.append(f"  $ {step}")
    status = out.status + (f" : {out.error}" if out.error else "")
    timing = " ".join(f"{k}={v:.0f}" for k, v in out.timings.items())
    lines.append(f"  → {_c(status, _COLORS.get(out.status, '0'))}" + (f"  ({timing})" if timing else ""))
    return "\n".join(lines)


def terminal_confirmer(decision, args, steps) -> bool:
    what = decision.command.description if decision.command else "?"
    try:
        answer = input(f"  Confirmer « {what} » ({decision.reason}) ? [o/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"o", "oui", "y", "yes"}


def build_client(args, config):
    if args.fake:
        from .jev_client import FakeJevClient

        return FakeJevClient()
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY absente : ajoutez-la au fichier .env (TYPESAFE_API_KEY=...) ou à l'environnement.")
    from .jev_client import JevClient

    return JevClient(model=config.settings.model)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="voxjev", description="Lanceur vocal macOS avec Jev comme couche de décision.")
    p.add_argument("--text", help="traiter une phrase au lieu du micro")
    p.add_argument("--audio", help="traiter un fichier audio (wav/aiff/mp3…) : Whisper puis Jev, sans micro")
    p.add_argument("--gui", action="store_true",
                   help="interface graphique : icône de barre des menus + HUD flottant (avec --text : phrase de démo)")
    p.add_argument("--dry-run", action="store_true", help="afficher le plan sans rien exécuter")
    p.add_argument("--eval", nargs="?", const=str(PROJECT_ROOT / "cases.tsv"), metavar="CASES.TSV",
                   help="évaluer un fichier de cas (défaut : cases.tsv)")
    p.add_argument("--mode", help="forcer le mode actif (défaut, ctf, travail)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="fichier YAML de config")
    p.add_argument("--fake", action="store_true", help="utiliser le faux client Jev (hors ligne)")
    p.add_argument("--yes", action="store_true", help="confirmer automatiquement (mode --text)")
    p.add_argument("--no-sound", action="store_true", help="désactiver les sons")
    p.add_argument("--workers", type=int, default=6, help="appels Jev parallèles pour --eval")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # logs lisibles en direct, même redirigés
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()  # .env du dossier courant, s'il existe (sans écraser)
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config invalide : {exc}", file=sys.stderr)
        return 2
    if args.mode and args.mode not in config.modes:
        print(f"Mode inconnu {args.mode!r} (disponibles : {', '.join(config.modes)})", file=sys.stderr)
        return 2

    if args.eval:
        from .evaluate import run_eval

        return run_eval(Path(args.eval), config, build_client(args, config), workers=args.workers)

    session = Session.load(config.default_mode, set(config.modes))
    if args.mode:
        session.mode = args.mode
    client = build_client(args, config)

    if args.audio:
        from .stt import Transcriber, load_audio_file

        s = config.settings
        transcriber = Transcriber(s.stt_model, s.language, prompt=s.stt_prompt or None)
        transcriber.warmup()
        args.text, stt_ms = transcriber.transcribe(load_audio_file(args.audio))
        print(f"Whisper {stt_ms:.0f} ms : {args.text!r}")
        if not args.text:
            return 1

    if args.gui:
        from .gui import run_gui

        return run_gui(config, client, session, dry_run=args.dry_run, sound=not args.no_sound,
                       initial_text=args.text)

    if args.text is not None:
        executor = None
        if not args.dry_run:
            from .executor import SubprocessExecutor

            executor = SubprocessExecutor()
        confirmer = (lambda *a: True) if args.yes else terminal_confirmer
        launcher = Launcher(config, client, session, executor=executor, confirmer=confirmer, dry_run=args.dry_run)
        out = launcher.handle(args.text)
        print(format_outcome(out, session.mode))
        if not args.dry_run:
            from .executor import Sounds

            Sounds(config.settings.sounds, enabled=not args.no_sound).for_status(out.status)
        return 0 if out.status in {"executed", "dry_run", "ignored", "cancelled"} else 1

    from .listen import run_listener

    return run_listener(config, client, session, dry_run=args.dry_run, sound=not args.no_sound)


if __name__ == "__main__":
    raise SystemExit(main())
