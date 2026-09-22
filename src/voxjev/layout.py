"""Rangement des fenêtres : côte à côte, grille, moitiés d'écran — sans superposition.

« range les fenêtres », « mets cette fenêtre à gauche », « mets Chrome à gauche et Spotify à droite »,
et, en automatique, chaque nouvelle page web s'ouvre à côté de ce que vous regardiez.

Tout passe par l'API d'accessibilité (déjà autorisée pour la touche de parole) : le code lit les
fenêtres visibles (Quartz), calcule les emplacements, puis déplace et redimensionne (AXPosition /
AXSize), avec une courte animation. Aucune fenêtre n'est fermée ni réduite. Le dernier rangement
peut être annulé (« remets les fenêtres comme avant »).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

GAP = 8.0  # marge entre fenêtres et bords, comme le rangement natif de macOS
MAX_WINDOWS = 9
IGNORED_OWNERS = {"Window Server", "Dock", "Centre de contrôle", "Control Center", "SystemUIServer",
                  "Centre de notifications", "Notification Center", "Spotlight", "voxjev", "Python",
                  "voxjev-gui", "Wallpaper", "Fond d'écran", "loginwindow", "Hidden Bar"}


class LayoutError(RuntimeError):
    pass


@dataclass
class Win:
    pid: int
    owner: str
    title: str
    x: float
    y: float
    w: float
    h: float
    ref: object = None  # élément AX

    @property
    def frame(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.w, self.h


# ------------------------------------------------------------------ géométrie (testable sans écran)
def slots(n: int, area: tuple[float, float, float, float], gap: float = GAP) -> list[tuple[float, float, float, float]]:
    """Emplacements pour n fenêtres dans `area` (x, y, w, h ; origine en haut à gauche).

    1 : plein · 2 : gauche | droite · 3 : gauche | (haut / bas) · 4 : quadrants · plus : grille.
    La 1re fenêtre (la plus récente) reçoit toujours la plus grande place, à gauche.
    """
    x, y, w, h = area
    x, y, w, h = x + gap, y + gap, w - 2 * gap, h - 2 * gap
    half_w, half_h = (w - gap) / 2, (h - gap) / 2
    if n <= 0:
        return []
    if n == 1:
        return [(x, y, w, h)]
    if n == 2:
        return [(x, y, half_w, h), (x + half_w + gap, y, half_w, h)]
    if n == 3:
        return [(x, y, half_w, h), (x + half_w + gap, y, half_w, half_h),
                (x + half_w + gap, y + half_h + gap, half_w, half_h)]
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cw, rh = (w - (cols - 1) * gap) / cols, (h - (rows - 1) * gap) / rows
    return [(x + (i % cols) * (cw + gap), y + (i // cols) * (rh + gap), cw, rh) for i in range(n)]


POSITIONS = {  # emplacement nommé -> fraction de l'écran (x, y, w, h)
    "left": (0, 0, 0.5, 1), "right": (0.5, 0, 0.5, 1), "top": (0, 0, 1, 0.5), "bottom": (0, 0.5, 1, 0.5),
    "full": (0, 0, 1, 1), "center": (0.15, 0.1, 0.7, 0.8),
    "top_left": (0, 0, 0.5, 0.5), "top_right": (0.5, 0, 0.5, 0.5),
    "bottom_left": (0, 0.5, 0.5, 0.5), "bottom_right": (0.5, 0.5, 0.5, 0.5),
}


def position_frame(position: str, area, gap: float = GAP):
    fx, fy, fw, fh = POSITIONS[position]
    x, y, w, h = area
    return (x + fx * w + gap, y + fy * h + gap, fw * w - 2 * gap, fh * h - 2 * gap)


# ------------------------------------------------------------------ écran et fenêtres réelles
def _ax():
    import ApplicationServices as AS

    return AS


def _attr(el, name):
    err, value = _ax().AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def screen_area(point: tuple[float, float] | None = None) -> tuple[float, float, float, float]:
    """Zone utile (sans barre des menus ni Dock) de l'écran qui contient `point`, en coordonnées
    « haut-gauche » (celles de l'accessibilité et de Quartz)."""
    from AppKit import NSScreen

    screens = NSScreen.screens()
    main_h = screens[0].frame().size.height
    chosen = NSScreen.mainScreen()
    if point is not None:
        px, py = point
        for s in screens:
            f = s.frame()
            top = main_h - (f.origin.y + f.size.height)
            if f.origin.x <= px < f.origin.x + f.size.width and top <= py < top + f.size.height:
                chosen = s
                break
    vf = chosen.visibleFrame()
    return (vf.origin.x, main_h - (vf.origin.y + vf.size.height), vf.size.width, vf.size.height)


def visible_windows() -> list[Win]:
    """Fenêtres normales visibles, de la plus récente à la plus ancienne (ordre d'empilement)."""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    wins: list[Win] = []
    for info in infos or []:
        if info.get("kCGWindowLayer", 1) != 0 or info.get("kCGWindowAlpha", 1) == 0:
            continue
        owner = str(info.get("kCGWindowOwnerName", ""))
        b = info.get("kCGWindowBounds") or {}
        w, h = float(b.get("Width", 0)), float(b.get("Height", 0))
        if owner in IGNORED_OWNERS or w < 220 or h < 160:
            continue
        wins.append(Win(int(info["kCGWindowOwnerPID"]), owner, str(info.get("kCGWindowName", "") or ""),
                        float(b.get("X", 0)), float(b.get("Y", 0)), w, h))
    return wins[:MAX_WINDOWS]


def _match_ax(win: Win):
    """Élément AX de la fenêtre Quartz (même app, position et taille les plus proches)."""
    AS = _ax()
    app = AS.AXUIElementCreateApplication(win.pid)
    best, score = None, float("inf")
    for el in _attr(app, "AXWindows") or []:
        if _attr(el, "AXMinimized"):
            continue
        pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
        if pos is None or size is None:
            continue
        p = _point(pos)
        s = _size(size)
        d = abs(p[0] - win.x) + abs(p[1] - win.y) + abs(s[0] - win.w) + abs(s[1] - win.h)
        if d < score:
            best, score = el, d
    return best


def _point(value):
    AS = _ax()
    ok, pt = AS.AXValueGetValue(value, AS.kAXValueCGPointType, None)
    return (pt.x, pt.y) if ok else (0.0, 0.0)


def _size(value):
    AS = _ax()
    ok, sz = AS.AXValueGetValue(value, AS.kAXValueCGSizeType, None)
    return (sz.width, sz.height) if ok else (0.0, 0.0)


def _set_frame(el, frame) -> None:
    AS = _ax()
    x, y, w, h = frame
    pos = AS.AXValueCreate(AS.kAXValueCGPointType, (x, y))
    size = AS.AXValueCreate(AS.kAXValueCGSizeType, (w, h))
    AS.AXUIElementSetAttributeValue(el, "AXPosition", pos)
    AS.AXUIElementSetAttributeValue(el, "AXSize", size)
    AS.AXUIElementSetAttributeValue(el, "AXPosition", pos)  # certaines apps recalent après le redimensionnement


def is_fullscreen(el) -> bool:
    return bool(_attr(el, "AXFullScreen"))


def exit_fullscreen(el) -> None:
    """Sort une fenêtre du plein écran macOS (sinon macOS refuse de la déplacer)."""
    _ax().AXUIElementSetAttributeValue(el, "AXFullScreen", False)
    deadline = time.monotonic() + 2.0
    while is_fullscreen(el) and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.6)  # fin de l'animation de sortie du plein écran


def _frame_of(el) -> tuple[float, float, float, float] | None:
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    return (*_point(pos), *_size(size))


def _move_all(moves: list[tuple[object, tuple, tuple]], duration: float = 0.18, steps: int = 7) -> None:
    """Déplace plusieurs fenêtres ensemble, avec une courte animation (ease-out)."""
    try:
        from AppKit import NSWorkspace

        if NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion():
            steps = 1
    except Exception:
        pass
    for i in range(1, steps + 1):
        t = 1 - (1 - i / steps) ** 3
        for el, start, end in moves:
            _set_frame(el, tuple(a + (b - a) * t for a, b in zip(start, end)))
        if i < steps:
            time.sleep(duration / steps)


class Layouts:
    """Rangements, avec mémoire du dernier état pour « remets les fenêtres comme avant »."""

    def __init__(self):
        self.previous: list[tuple[object, tuple]] = []

    def _apply(self, pairs: list[tuple[Win, tuple]], exit_full: bool = False) -> int:
        """exit_full : demande explicite, on sort du plein écran ; sinon ces fenêtres sont laissées."""
        moves, saved = [], []
        for win, frame in pairs:
            el = win.ref or _match_ax(win)
            if el is None:
                continue
            if is_fullscreen(el):
                if not exit_full:
                    continue
                exit_fullscreen(el)
            current = _frame_of(el) or win.frame
            moves.append((el, current, frame))
            saved.append((el, current))
        if not moves:
            raise LayoutError("aucune fenêtre à ranger (autorisation Accessibilité ?)")
        self.previous = saved
        _move_all(moves)
        return len(moves)

    def tile(self, wins: list[Win] | None = None) -> int:
        """Toutes les fenêtres visibles de l'écran principal de travail, sans superposition."""
        wins = wins if wins is not None else visible_windows()
        if not wins:
            raise LayoutError("aucune fenêtre visible à ranger")
        area = screen_area((wins[0].x + wins[0].w / 2, wins[0].y + wins[0].h / 2))
        wins = [w for w in wins if _intersects(w, area)]
        return self._apply(list(zip(wins, slots(len(wins), area))), exit_full=True)

    def place_front(self, position: str) -> str:
        wins = visible_windows()
        if not wins:
            raise LayoutError("aucune fenêtre au premier plan")
        win = wins[0]
        area = screen_area((win.x + win.w / 2, win.y + win.h / 2))
        self._apply([(win, position_frame(position, area))], exit_full=True)
        return win.owner

    def pair(self, left_app: str, right_app: str) -> None:
        wins = visible_windows()
        left = next((w for w in wins if w.owner == left_app), None)
        right = next((w for w in wins if w.owner == right_app), None)
        missing = [a for a, w in ((left_app, left), (right_app, right)) if w is None]
        if missing:
            raise LayoutError(f"aucune fenêtre visible pour {', '.join(missing)} (ouvrez-la d'abord)")
        area = screen_area((left.x + left.w / 2, left.y + left.h / 2))
        self._apply([(left, position_frame("left", area)), (right, position_frame("right", area))], exit_full=True)

    def beside(self, new: Win, candidates: list[Win]) -> None:
        """Nouvelle fenêtre à droite ; à gauche, la fenêtre normale la plus récente du même écran.
        Les fenêtres en plein écran ne sont jamais touchées (espace à part)."""
        area = screen_area((new.x + new.w / 2, new.y + new.h / 2))
        context = None
        for w in candidates:
            if (w.pid, w.title) == (new.pid, new.title) or not _intersects(w, area):
                continue
            el = _match_ax(w)
            if el is not None and not is_fullscreen(el):
                w.ref, context = el, w
                break
        if context is None:
            self._apply([(new, position_frame("full", area))])
            return
        self._apply([(context, position_frame("left", area)), (new, position_frame("right", area))])

    def restore(self) -> int:
        if not self.previous:
            raise LayoutError("aucun rangement à annuler")
        moves = []
        for el, frame in self.previous:
            pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
            if pos is None or size is None:
                continue  # fenêtre fermée depuis
            moves.append((el, (*_point(pos), *_size(size)), frame))
        self.previous = []
        _move_all(moves)
        return len(moves)


def _intersects(win: Win, area) -> bool:
    x, y, w, h = area
    cx, cy = win.x + win.w / 2, win.y + win.h / 2
    return x <= cx <= x + w and y <= cy <= y + h


def wait_new_window(owner: str, before: set[tuple], timeout: float = 4.0) -> Win | None:
    """Attend qu'une nouvelle fenêtre de `owner` apparaisse (par rapport à `before`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in visible_windows():
            if w.owner == owner and (w.pid, w.title, round(w.x), round(w.y)) not in before:
                return w
        time.sleep(0.15)
    return None


def fingerprint(wins: list[Win]) -> set[tuple]:
    return {(w.pid, w.title, round(w.x), round(w.y)) for w in wins}
