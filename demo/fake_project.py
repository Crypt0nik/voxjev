"""Projets FICTIFS animés pour la démo vidéo de voxjev (aucun vrai serveur, aucun réseau).

    python demo/fake_project.py api|train|deploy

Chaque « projet » affiche un en-tête, une montée en charge rapide et colorée, puis reste vivant
(nouvelles lignes régulières) jusqu'à la fermeture de la fenêtre.
"""

from __future__ import annotations

import math
import random
import sys
import time

R, B, DIM = "\033[0m", "\033[1m", "\033[2m"
C = {"blue": "\033[38;5;75m", "cyan": "\033[38;5;87m", "green": "\033[38;5;114m", "yellow": "\033[38;5;221m",
     "orange": "\033[38;5;215m", "pink": "\033[38;5;211m", "purple": "\033[38;5;141m", "gray": "\033[38;5;245m"}
SPARK = "▁▂▃▄▅▆▇█"
rnd = random.Random(7)


def out(s: str = "", end: str = "\n") -> None:
    sys.stdout.write(s + end)
    sys.stdout.flush()


def type_line(s: str, speed: float = 0.006) -> None:
    for ch in s:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(speed)
    out()


def header(title: str, subtitle: str, color: str) -> None:
    out("\033[2J\033[H", end="")
    width = 58
    out(f"{color}╭{'─' * width}╮{R}")
    out(f"{color}│{R} {B}{title:<{width - 2}}{R} {color}│{R}")
    out(f"{color}│{R} {DIM}{subtitle:<{width - 2}}{R} {color}│{R}")
    out(f"{color}╰{'─' * width}╯{R}")
    out()


def bar(label: str, color: str, total: float = 1.2, width: int = 32) -> None:
    steps = 40
    for i in range(steps + 1):
        frac = i / steps
        full = int(frac * width)
        sys.stdout.write(f"\r  {label:<18} {color}{'█' * full}{DIM}{'░' * (width - full)}{R} {frac * 100:5.1f}%")
        sys.stdout.flush()
        time.sleep(total / steps)
    out(f"  {C['green']}✓{R}")


def spinner(label: str, seconds: float, color: str) -> None:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        sys.stdout.write(f"\r  {color}{frames[i % len(frames)]}{R} {label}")
        sys.stdout.flush()
        time.sleep(0.06)
        i += 1
    out(f"\r  {C['green']}✓{R} {label}   ")


# ------------------------------------------------------------------ projets
def api() -> None:
    header("⚡ voxjev-cloud · API", "FastAPI · Postgres · Redis — environnement de démo", C["blue"])
    for step, t in (("Chargement de la configuration", 0.3), ("Connexion à Postgres", 0.5),
                    ("Connexion à Redis", 0.3), ("Migrations (12)", 0.6), ("Préchauffage du cache", 0.4)):
        spinner(step, t, C["blue"])
    out(f"\n  {B}{C['green']}● Prêt{R} en {B}412 ms{R} sur {C['cyan']}http://localhost:8000{R}\n")
    routes = ["GET  /v1/commands", "POST /v1/decide", "GET  /v1/health", "POST /v1/transcribe", "GET  /v1/metrics"]
    lat: list[float] = []
    while True:
        route = rnd.choice(routes)
        ms = max(3.0, rnd.gauss(28, 12))
        lat = (lat + [ms])[-12:]
        code = 200 if rnd.random() > 0.03 else 429
        color = C["green"] if code == 200 else C["orange"]
        spark = "".join(SPARK[min(7, int(v / 60 * 7))] for v in lat)
        out(f"  {DIM}{time.strftime('%H:%M:%S')}{R}  {color}{code}{R}  {route:<22} {ms:6.1f} ms  {C['cyan']}{spark}{R}")
        time.sleep(rnd.uniform(0.05, 0.35))


def train() -> None:
    header("🧠 whisper-fr · entraînement", "Apple Silicon · MLX · 8 époques — environnement de démo", C["purple"])
    spinner("Chargement du jeu de données (48 000 phrases)", 0.8, C["purple"])
    spinner("Compilation du graphe MLX", 0.5, C["purple"])
    out()
    loss, wer = 2.9, 31.0
    for epoch in range(1, 9):
        for i in range(0, 101, 4):
            loss *= 0.992
            wer = max(3.9, wer * 0.9915)
            w = 26
            full = int(i / 100 * w)
            gpu = 92 + 6 * math.sin(i / 7)
            sys.stdout.write(f"\r  époque {epoch}/8 {C['purple']}{'█' * full}{DIM}{'░' * (w - full)}{R} "
                             f"perte {C['yellow']}{loss:.3f}{R}  WER {C['green']}{wer:4.1f}%{R}  GPU {gpu:3.0f}%")
            sys.stdout.flush()
            time.sleep(0.035)
        out()
    out(f"\n  {B}{C['green']}✓ Modèle exporté{R} · WER final {B}{wer:.1f}%{R} · {C['cyan']}whisper-fr-v2.safetensors{R}\n")
    while True:
        out(f"  {DIM}{time.strftime('%H:%M:%S')}{R}  évaluation continue · WER {C['green']}{wer + rnd.uniform(-0.2, 0.2):.2f}%{R}")
        time.sleep(1.2)


def deploy() -> None:
    header("🚀 landing · build & déploiement", "Next.js · Edge — environnement de démo", C["orange"])
    bar("Installation", C["orange"], 0.8)
    bar("Compilation TS", C["orange"], 0.9)
    bar("Optimisation images", C["orange"], 0.7)
    bar("Bundling", C["orange"], 0.8)
    out()
    for name, size, color in (("app/page.js", "84.2 kB", "green"), ("app/layout.js", "12.9 kB", "green"),
                              ("chunks/main.js", "142 kB", "yellow"), ("styles.css", "9.4 kB", "green")):
        type_line(f"  {C[color]}○{R} {name:<22} {size:>9}", 0.004)
    out()
    for region in ("Paris (cdg1)", "Francfort (fra1)", "Londres (lhr1)", "Virginie (iad1)"):
        spinner(f"Déploiement {region}", 0.35, C["orange"])
    out(f"\n  {B}{C['green']}✓ En ligne{R} en {B}6,8 s{R} → {C['cyan']}https://voxjev-demo.app{R}\n")
    while True:
        visitors = rnd.randint(40, 180)
        out(f"  {DIM}{time.strftime('%H:%M:%S')}{R}  {C['green']}▲{R} {visitors} visiteurs · "
            f"TTFB {rnd.uniform(18, 42):.0f} ms · {C['purple']}{SPARK[rnd.randint(2, 7)] * 3}{R}")
        time.sleep(1.0)


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "api"
    try:
        {"api": api, "train": train, "deploy": deploy}[kind]()
    except KeyboardInterrupt:
        pass
