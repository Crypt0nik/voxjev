"""Boucle principale en mode micro : push-to-talk -> Whisper -> Jev -> action."""

from __future__ import annotations

import queue
import subprocess
import sys
import time

from .cli import format_any
from .config import Config
from .context import Session, frontmost_app, installed_apps
from .executor import Sounds, SubprocessExecutor, dialog_confirmer
from .pipeline import Launcher


def run_listener(config: Config, client, session: Session, *, dry_run: bool = False, sound: bool = True) -> int:
    from .audio import PERMISSION_HELP, PushToTalk, accessibility_trusted
    from .stt import Transcriber

    s = config.settings
    if not accessibility_trusted():
        print(PERMISSION_HELP, flush=True)
        if sys.stdin.isatty():  # ouvre directement le bon panneau des Réglages
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                           check=False)
        return 3
    sounds = Sounds(s.sounds, enabled=sound)
    print(f"voxjev — mode {session.mode}, modèle Jev {s.model}, STT {s.stt_model}")
    print("Chargement de Whisper (téléchargement au premier lancement)…", flush=True)
    transcriber = Transcriber(s.stt_model, s.language, prompt=s.stt_prompt or None)
    print(f"  Whisper prêt ({transcriber.warmup():.0f} ms)")
    apps = installed_apps(s.app_dirs)
    frontmost_app()  # pré-charge AppKit
    print(f"  {len(apps)} apps installées indexées")

    launcher = Launcher(config, client, session,
                        executor=None if dry_run else SubprocessExecutor(settings=s),
                        confirmer=dialog_confirmer(s.confirm_timeout_seconds), dry_run=dry_run)
    from .executor import dialog_plan_confirmer
    from .multi import MultiRunner, build_splitter

    runner = MultiRunner(launcher, build_splitter(s), confirm_plan=dialog_plan_confirmer(s.confirm_timeout_seconds))
    ptt = PushToTalk(s.hotkey, on_start=lambda: (sounds.play("listening"), launcher.prefetch()))
    ptt.start()
    print(f"Maintenez « {s.hotkey} » pour parler (Ctrl+C pour quitter)."
          + ("  [dry-run : rien n'est exécuté]" if dry_run else ""))

    try:
        while True:
            try:
                audio, held = ptt.clips.get(timeout=0.5)
            except queue.Empty:
                continue
            if held < s.min_record_seconds:
                continue
            released = time.perf_counter()
            text, stt_ms = transcriber.transcribe(audio)
            if not text:
                print(f"  (rien compris — {held:.1f} s d'audio)")
                sounds.play("ignored")
                continue
            out = runner.handle(text)
            out.timings = {"audio_s": held, "stt_ms": stt_ms, **out.timings,
                           "total_ms": (time.perf_counter() - released) * 1000}
            print(format_any(out, session.mode), flush=True)
            if not dry_run:
                sounds.for_status(out.status)
    except KeyboardInterrupt:
        print("\nAu revoir.")
    finally:
        ptt.stop()
    return 0
