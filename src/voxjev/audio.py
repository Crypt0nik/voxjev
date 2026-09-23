"""Capture micro (16 kHz mono) et push-to-talk global via pynput.

Le flux micro n'est ouvert que pendant l'appui : l'indicateur micro de macOS ne
s'allume que lorsque la touche est maintenue.
"""

from __future__ import annotations

import collections
import os
import queue
import threading
import time

import numpy as np

from .stt import SAMPLE_RATE


class Recorder:
    def __init__(self, on_level=None):
        import sounddevice as sd

        self._sd = sd
        self._on_level = on_level  # appelé avec le RMS de chaque bloc (thread audio)
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                                callback=self._callback)
            self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        chunk = indata[:, 0].copy()
        self._chunks.append(chunk)
        if self._on_level:
            self._on_level(float(np.sqrt(np.mean(chunk**2))))

    def snapshot(self) -> np.ndarray:
        """Audio capté jusqu'ici, sans arrêter l'enregistrement (transcription anticipée)."""
        chunks = list(self._chunks)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def stop(self) -> np.ndarray:
        with self._lock:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
        fake = os.environ.get("VOXJEV_FAKE_MIC")
        if fake:  # débogage : la vraie touche, mais l'audio vient d'un fichier
            from .stt import load_audio_file

            return load_audio_file(fake)
        return audio


def accessibility_trusted() -> bool:
    """Vrai si le processus (en pratique : l'app de terminal) peut écouter le clavier global."""
    import ctypes

    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except OSError:
        return True  # impossible de vérifier : on laisse pynput essayer


def input_monitoring_granted() -> bool | None:
    """« Surveillance de l'entrée » accordée ? (nécessaire pour entendre la touche de parole).
    None si impossible à vérifier."""
    import ctypes

    try:
        iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        return iokit.IOHIDCheckAccess(1) == 0  # kIOHIDRequestTypeListenEvent ; 0 = accordé
    except (OSError, AttributeError):
        return None


def request_permissions() -> None:
    """Déclenche les fenêtres système « Accessibilité » et « Surveillance de l'entrée »
    (utilisé par voxjev.app au premier lancement ; sans effet si déjà accordées)."""
    import ctypes

    try:
        import objc
        from Foundation import NSBundle

        bundle = NSBundle.bundleWithPath_("/System/Library/Frameworks/ApplicationServices.framework")
        fns: dict = {}
        objc.loadBundleFunctions(bundle, fns, [("AXIsProcessTrustedWithOptions", b"Z@")])
        fns["AXIsProcessTrustedWithOptions"]({"AXTrustedCheckOptionPrompt": True})
    except Exception as exc:
        print(f"demande d'accessibilité impossible : {exc}")
    try:
        iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDRequestAccess.restype = ctypes.c_bool
        iokit.IOHIDRequestAccess(1)  # kIOHIDRequestTypeListenEvent : surveillance de l'entrée
    except OSError as exc:
        print(f"demande de surveillance de l'entrée impossible : {exc}")


def _grantee() -> str:
    """Qui doit recevoir les autorisations : voxjev.app, ou l'app de terminal qui lance voxjev."""
    return "voxjev" if os.environ.get("VOXJEV_APP") else "le terminal"


PERMISSION_HELP = f"""\
⚠️  Le push-to-talk nécessite deux autorisations macOS pour {"voxjev.app" if _grantee() == "voxjev"
    else "votre app de terminal (Terminal, Ghostty, iTerm, VS Code…)"} :
   1. Réglages Système > Confidentialité et sécurité > Accessibilité      -> activer {_grantee()}
   2. Réglages Système > Confidentialité et sécurité > Surveillance de l'entrée -> activer {_grantee()}
   Le micro sera demandé automatiquement au premier appui. Relancez ensuite voxjev.
   (En attendant : `./voxjev --text "..."` ou `./voxjev --audio fichier.wav` fonctionnent sans ces droits.)"""


HOTKEY_LABELS = {"alt_r": "⌥ droite", "alt_l": "⌥ gauche", "alt": "⌥", "cmd_r": "⌘ droite", "cmd": "⌘",
                 "ctrl_r": "⌃ droite", "ctrl": "⌃", "shift_r": "⇧ droite", "caps_lock": "⇪",
                 "f18": "Verr. Maj (F18)"}


def hotkey_label(name: str) -> str:
    return HOTKEY_LABELS.get(name, name.upper() if name.startswith("f") else name)


def parse_hotkey(name: str):
    """'alt_r', 'cmd_r', 'f13'... -> pynput Key ; un seul caractère -> KeyCode."""
    from pynput import keyboard

    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    try:
        return getattr(keyboard.Key, name)
    except AttributeError as exc:
        raise ValueError(f"touche inconnue : {name!r} (voir pynput.keyboard.Key)") from exc


class PushToTalk:
    """Touche maintenue -> enregistrement ; relâchée -> clip audio poussé dans `clips`.

    Double-clic (deux appuis brefs rapprochés) -> ``on_double_tap`` (écoute continue).
    """

    TAP_MAX_S = 0.3  # un appui plus court est un « clic »
    DOUBLE_TAP_WINDOW_S = 0.4  # délai maximal entre le 1er relâchement et le 2e appui

    def __init__(self, hotkey: str, on_start=None, on_clip=None, on_level=None, on_double_tap=None):
        from pynput import keyboard

        self._keyboard = keyboard
        self.hotkey = parse_hotkey(hotkey)
        self.recorder = Recorder(on_level=on_level)
        self._on_clip = on_clip  # (audio, durée) -> None ; sinon file `clips`
        self.clips: queue.Queue[tuple[np.ndarray, float]] = queue.Queue()
        self._pressed_at: float | None = None
        self._on_start = on_start
        self._on_double_tap = on_double_tap
        self._last_tap_release: float | None = None
        self._swallow_release = False
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def _matches(self, key) -> bool:
        return key == self.hotkey or getattr(key, "char", None) == getattr(self.hotkey, "char", object())

    def _press(self, key) -> None:
        if not self._matches(key) or self._pressed_at is not None:
            return  # l'auto-répétition du clavier renvoie des press en boucle
        now = time.perf_counter()
        self._pressed_at = now
        if (self._on_double_tap and self._last_tap_release is not None
                and now - self._last_tap_release < self.DOUBLE_TAP_WINDOW_S):
            self._last_tap_release = None
            self._swallow_release = True  # ce 2e appui ne déclenche pas d'enregistrement
            self._on_double_tap()
            return
        try:
            self.recorder.start()
        except Exception as exc:  # micro indisponible / permission refusée
            print(f"  micro indisponible : {exc}")
            self._pressed_at = None
            return
        if self._on_start:
            self._on_start()

    def _release(self, key) -> None:
        if not self._matches(key) or self._pressed_at is None:
            return
        now = time.perf_counter()
        held = now - self._pressed_at
        self._pressed_at = None
        if self._swallow_release:
            self._swallow_release = False
            return
        self._last_tap_release = now if held < self.TAP_MAX_S else None
        clip = (self.recorder.stop(), held)
        if self._on_clip:
            self._on_clip(*clip)
        else:
            self.clips.put(clip)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()


# ----------------------------------------------------------------------------- mains libres
SPEAKING_UNTIL = [0.0]  # mis à jour par executor.speak : on n'écoute pas sa propre voix


class HandsFree:
    """Micro ouvert en continu, découpage en phrases par l'énergie (VAD simple, local).

    Chaque phrase détectée est passée à ``on_clip(audio, durée)``. C'est l'appelant qui
    transcrit localement et ne garde que les phrases adressées (mot d'éveil ou fenêtre de suite) :
    rien ne part vers l'API sans le mot d'éveil.
    """

    BLOCK = 512  # 32 ms à 16 kHz
    PRE_ROLL_S = 0.5  # garde le début de la phrase (« ouvre… » plutôt que « …vre »)
    END_SILENCE_S = 0.75
    MAX_S = 15.0
    MIN_VOICED_S = 0.3  # durée de voix minimale (un clic ou une toux ne suffit pas)
    HISTORY_S = 3.0  # fenêtre sur laquelle on mesure le bruit de fond
    NOISE_PERCENTILE = 50  # le fond = le niveau médian des 3 dernières secondes (suit le bruit ambiant)
    ON_FACTOR, OFF_FACTOR = 2.5, 1.7  # voix = nettement au-dessus du fond ; fin = retour près du fond
    MIN_ON, MIN_OFF = 0.012, 0.008

    def __init__(self, on_clip, on_level=None, on_speech=None, end_silence_s: float | None = None,
                 max_s: float | None = None):
        import sounddevice as sd

        self._sd = sd
        self._on_clip = on_clip
        self._on_level = on_level
        self._on_speech = on_speech  # début de phrase détecté (pour l'icône)
        self._stream = None
        self.paused = False
        self.end_silence_s = end_silence_s or self.END_SILENCE_S
        self.max_s = max_s or self.MAX_S
        self._history: collections.deque[float] = collections.deque(
            maxlen=int(self.HISTORY_S * SAMPLE_RATE / self.BLOCK))
        self._floor = 0.004
        self._reset()

    def _reset(self) -> None:
        self._pre: list[np.ndarray] = []
        self._speech: list[np.ndarray] = []
        self._in_speech = False
        self._silent_blocks = 0
        self._loud_blocks = 0
        self._voiced = 0

    def start(self) -> None:
        self._stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                            blocksize=self.BLOCK, callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._reset()

    @property
    def running(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status) -> None:
        chunk = indata[:, 0].copy()
        if self.paused or time.time() < SPEAKING_UNTIL[0]:
            self._reset()
            return
        rms = float(np.sqrt(np.mean(chunk**2)))
        # Bruit de fond mesuré en permanence (même pendant une phrase) : avec de la musique ou une
        # conversation autour, le seuil monte au lieu de tout prendre pour de la voix.
        self._history.append(rms)
        if len(self._history) >= 10:
            self._floor = float(np.percentile(self._history, self.NOISE_PERCENTILE))
        on = max(self.MIN_ON, self._floor * self.ON_FACTOR)
        off = max(self.MIN_OFF, self._floor * self.OFF_FACTOR)
        loud = rms > (off if self._in_speech else on)
        if not self._in_speech:
            self._pre.append(chunk)
            max_pre = int(self.PRE_ROLL_S * SAMPLE_RATE / self.BLOCK)
            self._pre = self._pre[-max_pre:]
            self._loud_blocks = self._loud_blocks + 1 if loud else 0
            if self._loud_blocks >= 3:  # ~100 ms de voix : début de phrase
                self._in_speech = True
                self._speech = list(self._pre)
                self._silent_blocks = 0
                self._voiced = self._loud_blocks
                if self._on_speech:
                    self._on_speech()
            return
        self._speech.append(chunk)
        if self._on_level:
            self._on_level(rms)
        self._silent_blocks = 0 if loud else self._silent_blocks + 1
        self._voiced += 1 if loud else 0
        length = len(self._speech) * self.BLOCK / SAMPLE_RATE
        if self._silent_blocks * self.BLOCK / SAMPLE_RATE >= self.end_silence_s or length >= self.max_s:
            audio = np.concatenate(self._speech)
            voiced = self._voiced * self.BLOCK / SAMPLE_RATE
            self._reset()
            if voiced >= self.MIN_VOICED_S:
                self._on_clip(audio, length)


def strip_wake_word(text: str, wake_words: tuple[str, ...]) -> tuple[bool, str]:
    """(mot d'éveil présent en tête de phrase ?, reste de la phrase)."""
    import re

    low = text.lower().replace("’", "'")
    for w in wake_words:
        m = re.match(rf"^\W*(?:(?:ok|hey|hé|eh|dis|salut|bonjour)\W+)?{re.escape(w.lower())}\b\W*", low)
        if m:
            return True, text[m.end():].strip()
    return False, text
