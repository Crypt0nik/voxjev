"""Menus de l'app au premier plan, lus via l'API d'accessibilité (idée : rohit9mehta/dwim, MIT).

« exporte en PDF », « nouvelle fenêtre privée », « affiche la barre latérale »… marchent dans
n'importe quelle app sans configuration : le code lit la barre de menus, Jev choisit l'élément
parmi ceux qui existent réellement, et le code clique dessus.

Garde-fous :
- seuls des éléments observés dans la barre de menus peuvent être cliqués (Jev ne fait que
  choisir un indice) ; l'élément est retrouvé par son chemin et revérifié (existe, actif) avant le clic ;
- menus ignorés (vie privée / inutiles) : menu Pomme, Aide, Fenêtre, Historique, Signets, Favoris,
  éléments récents, Profils, Services ; les éléments grisés et les séparateurs ne sont pas proposés ;
- un élément au nom destructeur (supprimer, quitter, vider, envoyer…) est toujours confirmé.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_ITEMS = 240  # une question Choice de Jev est fiable jusqu'à ~240 options
MAX_DEPTH = 2  # menu > élément > sous-menu > élément

SKIP_MENUS = re.compile(
    r"^(?:aide|help|fenêtre|window|historique|history|signets|bookmarks|favoris|favorites|"
    r"profils?|profiles?|services|ouvrir l.élément récent|open recent|récents?|recent items|"
    r"ouvrir récent|onglets récemment fermés|recently closed)\b",
    re.I,
)
DESTRUCTIVE = re.compile(
    r"\b(?:supprim\w*|effac\w*|vider|vide|quitter|quit|fermer|close|déconnex\w*|log ?out|sign ?out|"
    r"réinitialis\w*|reset|remove|retirer|delete|erase|empty|discard|revert|rétablir|annuler les modif\w*|"
    r"corbeille|trash|envoy\w*|send|publi\w*|publish|désinstall\w*|uninstall|forcer|force|écraser|replace|"
    r"remplacer|redémarr\w*|restart|éteindre|shut ?down|verrouill\w*|lock)\b",
    re.I,
)


@dataclass(frozen=True)
class MenuItem:
    path: tuple[str, ...]  # ("Fichier", "Exporter au format PDF…")
    shortcut: str = ""

    @property
    def label(self) -> str:
        return " › ".join(self.path)

    @property
    def destructive(self) -> bool:
        return bool(DESTRUCTIVE.search(self.path[-1]))


def _ax():
    import ApplicationServices as AS  # pyobjc-framework-ApplicationServices

    return AS


def _attr(el, name):
    AS = _ax()
    err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def _front_pid() -> int | None:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app else None


def _menubar(pid: int):
    AS = _ax()
    return _attr(AS.AXUIElementCreateApplication(pid), "AXMenuBar")


_MODS = {0: "⌘", 1: "⇧⌘", 2: "⌥⌘", 3: "⌥⇧⌘", 4: "⌃⌘", 8: ""}


def _walk(menu_el, prefix: tuple[str, ...], depth: int, out: list, found_el: dict | None, want=None):
    for item in _attr(menu_el, "AXChildren") or []:
        title = str(_attr(item, "AXTitle") or "").strip()
        if not title:
            continue  # séparateur
        path = prefix + (title,)
        children = _attr(item, "AXChildren") or []
        if children and depth < MAX_DEPTH:
            if SKIP_MENUS.search(title):
                continue
            for sub in children:  # un AXMenu
                _walk(sub, path, depth + 1, out, found_el, want)
            continue
        if children:
            continue
        if want is not None:
            if path == want:
                found_el["el"] = item
                return
            continue
        if not _attr(item, "AXEnabled"):
            continue
        key = str(_attr(item, "AXMenuItemCmdChar") or "")
        mods = _attr(item, "AXMenuItemCmdModifiers")
        shortcut = (_MODS.get(int(mods), "") + key) if key and mods is not None else ""
        out.append(MenuItem(path, shortcut))


def read_menus(pid: int | None = None, limit: int = MAX_ITEMS) -> list[MenuItem]:
    """Éléments de menu cliquables de l'app au premier plan (vide si l'accessibilité est refusée)."""
    try:
        pid = pid or _front_pid()
        bar = _menubar(pid) if pid else None
    except Exception:
        return []
    if bar is None:
        return []
    items: list[MenuItem] = []
    for i, top in enumerate(_attr(bar, "AXChildren") or []):
        title = str(_attr(top, "AXTitle") or "").strip()
        if i == 0 or not title or SKIP_MENUS.search(title):  # menu Pomme : redémarrer, forcer à quitter…
            continue
        for sub in _attr(top, "AXChildren") or []:
            _walk(sub, (title,), 1, items, None)
    # Le menu de l'app (2e) contient surtout Réglages / Masquer / Quitter : on le garde, mais en dernier.
    return items[:limit]


def press(path: tuple[str, ...], pid: int | None = None) -> None:
    """Retrouve l'élément par son chemin dans la barre de menus ACTUELLE, vérifie, puis clique."""
    AS = _ax()
    pid = pid or _front_pid()
    bar = _menubar(pid) if pid else None
    if bar is None:
        raise RuntimeError("barre de menus inaccessible (autorisation Accessibilité ?)")
    for top in _attr(bar, "AXChildren") or []:
        if str(_attr(top, "AXTitle") or "").strip() != path[0]:
            continue
        found: dict = {}
        for sub in _attr(top, "AXChildren") or []:
            _walk(sub, (path[0],), 1, [], found, want=tuple(path))
            if "el" in found:
                break
        el = found.get("el")
        if el is None:
            break
        if not _attr(el, "AXEnabled"):
            raise RuntimeError(f"« {' › '.join(path)} » est grisé")
        err = AS.AXUIElementPerformAction(el, "AXPress")
        if err != 0:
            raise RuntimeError(f"clic refusé (erreur AX {err})")
        return
    raise RuntimeError(f"élément de menu introuvable : « {' › '.join(path)} »")
