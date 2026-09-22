"""Capture micro (16 kHz mono) et push-to-talk global via pynput.

Le flux micro n'est ouvert que pendant l'appui : l'indicateur micro de macOS ne
s'allume que lorsque la touche est maintenue.
"""

from __future__ import annotations

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
                 "ctrl_r": "⌃ droite", "ctrl": "⌃", "shift_r": "⇧ droite", "caps_lock": "⇪"}


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
    """Touche maintenue -> enregistrement ; relâchée -> clip audio poussé dans `clips`."""

    def __init__(self, hotkey: str, on_start=None, on_clip=None, on_level=None):
        from pynput import keyboard

        self._keyboard = keyboard
        self.hotkey = parse_hotkey(hotkey)
        self.recorder = Recorder(on_level=on_level)
        self._on_clip = on_clip  # (audio, durée) -> None ; sinon file `clips`
        self.clips: queue.Queue[tuple[np.ndarray, float]] = queue.Queue()
        self._pressed_at: float | None = None
        self._on_start = on_start
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def _matches(self, key) -> bool:
        return key == self.hotkey or getattr(key, "char", None) == getattr(self.hotkey, "char", object())

    def _press(self, key) -> None:
        if not self._matches(key) or self._pressed_at is not None:
            return  # l'auto-répétition du clavier renvoie des press en boucle
        self._pressed_at = time.perf_counter()
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
        held = time.perf_counter() - self._pressed_at
        self._pressed_at = None
        clip = (self.recorder.stop(), held)
        if self._on_clip:
            self._on_clip(*clip)
        else:
            self.clips.put(clip)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
