"""Transcription locale en français avec mlx-whisper (Apple Silicon, GPU via MLX).

Choix mesuré sur Mac M2 (énoncés de ~3 s, modèle chaud) :
  mlx-whisper   large-v3-turbo fp16 : ~1,18 s
  mlx-whisper   large-v3-turbo q4   : ~1,27 s
  whisper.cpp   large-v3-turbo q5_0 : ~1,67 s (whisper-server, Metal, modèle chaud)
Le modèle reste chargé en mémoire dans le processus ; rien ne sort de la machine.
"""

from __future__ import annotations

import os
import re
import time

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np

SAMPLE_RATE = 16_000
MIN_RMS = 0.004  # en dessous : silence, on ne transcrit pas

# Hallucinations classiques de Whisper sur du silence / bruit en français.
_HALLUCINATIONS = re.compile(
    r"sous-titr|amara\.org|merci d'avoir regardé|abonnez-vous|^\W*merci\W*$|^\W*$",
    re.IGNORECASE,
)


def is_repetition(text: str) -> bool:
    """Hallucination de Whisper sur du bruit ou de la musique : « oh, oh, oh, oh… »."""
    words = [w.strip(",.!?…").lower() for w in text.split()]
    words = [w for w in words if w]
    return len(words) >= 6 and len(set(words)) <= 2


def load_audio_file(path: str) -> np.ndarray:
    """Décode un fichier audio en PCM 16 kHz mono via ffmpeg (argv, sans shell)."""
    import subprocess

    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le",
                          "-ac", "1", "-ar", str(SAMPLE_RATE), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


class Transcriber:
    def __init__(self, model: str, language: str = "fr", prompt: str | None = None):
        import mlx_whisper  # import lourd : uniquement en mode micro

        self._mlx_whisper = mlx_whisper
        self.model = model
        self.language = language
        self.prompt = prompt

    def warmup(self) -> float:
        """Charge le modèle (et le télécharge au premier lancement)."""
        t = time.perf_counter()
        self._mlx_whisper.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32),
                                     path_or_hf_repo=self.model, language=self.language)
        return (time.perf_counter() - t) * 1000

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        """Renvoie (texte, latence ms). Texte vide si silence ou hallucination probable."""
        t = time.perf_counter()
        if audio.size == 0 or float(np.sqrt(np.mean(audio**2))) < MIN_RMS:
            return "", 0.0
        result = self._mlx_whisper.transcribe(
            audio.astype(np.float32),
            path_or_hf_repo=self.model,
            language=self.language,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=self.prompt,
        )
        text = " ".join(str(result.get("text", "")).split())
        if _HALLUCINATIONS.search(text) or is_repetition(text):
            text = ""
        return text, (time.perf_counter() - t) * 1000
