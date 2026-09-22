"""Agir sur la page déjà ouverte dans le navigateur : « clique sur le 2ᵉ lien », « ouvre le meilleur résultat ».

Navigateur visé : celui au premier plan, sinon le navigateur par défaut.
- Arc, Brave, Edge, Vivaldi, Safari : AppleScript (lecture de la page + changement d'adresse) ;
- Chrome : protocole de débogage (CDP, chrome://inspect/#remote-debugging), repli AppleScript.
Dans tous les cas :
- le code lit les liens VISIBLES de l'onglet actif (script de lecture figé, aucun code venu de la voix) ;
  sur une page de résultats, seuls les vrais résultats sont retenus ;
- un rang (« le 2ᵉ », « le dernier ») est compté par le code ; sinon Jev choisit parmi les liens réels ;
- seul un lien http(s) présent sur la page peut être ouvert, dans le même onglet.

Envoyé à l'API TypeSafe pour le choix (pas pour un rang) : le texte et le domaine des liens, le titre
et l'adresse de la page.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT_FILES = [Path("~/Library/Application Support/Google/Chrome/DevToolsActivePort").expanduser()]
MAX_LINKS = 60

# Script de lecture FIGÉ (exécuté dans la page, en lecture seule).
LINKS_JS = r"""
(() => {
  const seen = new Set(), out = [];
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = a => (a.querySelector('h3')?.innerText || a.innerText || a.getAttribute('aria-label')
                      || a.title || '').replace(/\s+/g, ' ').trim();
  const add = (a, result) => {
    const href = a.href || ''; if (!/^https?:/i.test(href) || seen.has(href)) return;
    const text = label(a); if (!text || !visible(a)) return;
    seen.add(href); const r = a.getBoundingClientRect();
    out.push({href, text: text.slice(0, 140), result, y: r.top + scrollY});
  };
  const results = [...document.querySelectorAll('#search a:has(h3), #rso a:has(h3), main a:has(h2), main a:has(h3), article a:has(h2), article a:has(h3)')];
  results.forEach(a => add(a, true));
  document.querySelectorAll('a[href]').forEach(a => add(a, false));
  out.sort((a, b) => (b.result - a.result) || (a.y - b.y));
  return JSON.stringify({title: document.title, url: location.href, links: out.slice(0, 80)});
})()
"""

ORDINALS = {
    "premier": 1, "première": 1, "1er": 1, "1ère": 1, "1re": 1, "deuxième": 2, "second": 2, "seconde": 2,
    "2e": 2, "2ème": 2, "troisième": 3, "3e": 3, "3ème": 3, "quatrième": 4, "4e": 4, "4ème": 4,
    "cinquième": 5, "5e": 5, "5ème": 5, "sixième": 6, "6e": 6, "septième": 7, "7e": 7, "huitième": 8, "8e": 8,
    "neuvième": 9, "9e": 9, "dixième": 10, "10e": 10,
}
QUESTION = (
    "The user is looking at the web page `page` in Chrome and says `transcript`. Which link should be opened: "
    "the one the user describes, or, if they ask for the best result, the one that best answers the page's search "
    "query? Pick `none` if no link fits."
)


# Liens jamais transmis ni proposés : comptes, connexion, e-mails (données personnelles).
PRIVATE = re.compile(r"accounts\.google|myaccount|/signin|/login|/logout|signout|@[\w-]+\.[a-z]{2,}", re.I)
BEST = re.compile(r"\b(?:meilleur|meilleure|plus pertinent|top)\b", re.I)


class ChromeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Link:
    href: str
    text: str
    result: bool

    @property
    def domain(self) -> str:
        return urlparse(self.href).netloc.removeprefix("www.")

    @property
    def label(self) -> str:
        return f"{self.text} — {self.domain}"


def ordinal(transcript: str) -> int | None:
    """« le 2ᵉ lien » -> 2, « le dernier » -> -1, sinon None."""
    t = transcript.lower().replace("ᵉ", "e").replace("’", "'")
    if re.search(r"\bdernier|dernière\b", t):
        return -1
    for word, n in ORDINALS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", t):
            return n
    m = re.search(r"\b(?:lien|résultat|resultat)\s+(?:n°|numéro|numero)?\s*(\d{1,2})\b", t)
    return int(m.group(1)) if m else None


def _endpoint() -> str:
    for f in PORT_FILES:
        try:
            port, path = f.read_text().split("\n")[:2]
            return f"ws://127.0.0.1:{port.strip()}{path.strip()}"
        except (OSError, ValueError):
            continue
    raise ChromeError("Chrome n'autorise pas le pilotage : activez chrome://inspect/#remote-debugging "
                      "(« Allow remote debugging »), puis cliquez « Allow » dans Chrome.")


def active_tab_url() -> str:
    try:
        r = subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to get URL of active tab of front window'],
                           capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise ChromeError("Chrome ne répond pas") from exc
    url = r.stdout.strip()
    if r.returncode != 0 or not url:
        raise ChromeError("aucun onglet Chrome ouvert")
    return url


class _Session:
    """Connexion CDP minimale (niveau navigateur, sessions « flatten »)."""

    def __init__(self):
        from websockets.sync.client import connect

        try:
            self.ws = connect(_endpoint(), open_timeout=5, max_size=8 * 2**20)
        except ChromeError:
            raise
        except Exception as exc:
            raise ChromeError(f"connexion à Chrome impossible ({exc}) : cliquez « Allow » dans Chrome") from exc
        self.ids = itertools.count(1)

    def call(self, method: str, session: str | None = None, **params):
        msg_id = next(self.ids)
        msg = {"id": msg_id, "method": method, "params": params} | ({"sessionId": session} if session else {})
        self.ws.send(json.dumps(msg))
        while True:
            data = json.loads(self.ws.recv(timeout=8))
            if data.get("id") == msg_id:
                if "error" in data:
                    raise ChromeError(f"Chrome : {data['error'].get('message')}")
                return data.get("result", {})

    def attach_active(self) -> tuple[str, str]:
        url = active_tab_url()
        pages = [t for t in self.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        target = next((t for t in pages if t["url"] == url), None) or next(
            (t for t in pages if t["url"].split("#")[0] == url.split("#")[0]), None)
        if target is None:
            raise ChromeError("onglet actif introuvable via le débogage de Chrome")
        session = self.call("Target.attachToTarget", targetId=target["targetId"], flatten=True)["sessionId"]
        return target["targetId"], session

    def close(self):
        pass  # connexion gardée : Chrome ne redemande « Allow » qu'une fois par session


_shared: _Session | None = None


def _session() -> _Session:
    """Connexion CDP réutilisée (reconnexion automatique si Chrome l'a fermée)."""
    global _shared
    if _shared is not None:
        try:
            _shared.call("Browser.getVersion")
            return _shared
        except Exception:
            _shared = None
    _shared = _Session()
    return _shared


# ------------------------------------------------------------------ navigateurs pilotés par AppleScript
# Scripts FIGÉS par navigateur (le dictionnaire AppleScript exige le nom de l'app en dur) ;
# le script JavaScript de lecture et l'adresse passent en argv.
_CHROMIUM_AS = {
    "Arc": ('tell application "Arc" to tell front window to tell active tab to execute javascript (item 1 of argv)',
            'tell application "Arc" to tell front window to set URL of active tab to (item 1 of argv)'),
    "Brave Browser": ('tell application "Brave Browser" to execute front window\'s active tab javascript (item 1 of argv)',
                      'tell application "Brave Browser" to set URL of active tab of front window to (item 1 of argv)'),
    "Microsoft Edge": ('tell application "Microsoft Edge" to execute front window\'s active tab javascript (item 1 of argv)',
                       'tell application "Microsoft Edge" to set URL of active tab of front window to (item 1 of argv)'),
    "Vivaldi": ('tell application "Vivaldi" to execute front window\'s active tab javascript (item 1 of argv)',
                'tell application "Vivaldi" to set URL of active tab of front window to (item 1 of argv)'),
    "Google Chrome": ('tell application "Google Chrome" to execute front window\'s active tab javascript (item 1 of argv)',
                      'tell application "Google Chrome" to set URL of active tab of front window to (item 1 of argv)'),
    "Safari": ('tell application "Safari" to do JavaScript (item 1 of argv) in current tab of front window',
               'tell application "Safari" to set URL of current tab of front window to (item 1 of argv)'),
}
BROWSERS = tuple(_CHROMIUM_AS)
# Ouvrir une adresse dans un NOUVEL ONGLET ACTIF de la fenêtre principale (pas « Little Arc ») :
# l'étape suivante (« ouvre le premier lien ») retrouve ainsi la bonne page.
_NEW_TAB = {
    name: ["on run argv", f'tell application "{name}"', "if (count of windows) is 0 then make new window",
           "tell front window to make new tab with properties {URL:(item 1 of argv)}", "activate", "end tell",
           "end run"]
    for name in ("Arc", "Google Chrome", "Brave Browser", "Microsoft Edge", "Vivaldi")
}
_NEW_TAB["Safari"] = ["on run argv", 'tell application "Safari"', "if (count of windows) is 0 then make new document",
                      "tell front window to set current tab to (make new tab with properties {URL:(item 1 of argv)})",
                      "activate", "end tell", "end run"]


def open_in_browser(url: str, browser: str | None = None) -> bool:
    """Ouvre `url` (http/https) dans un nouvel onglet actif. False si le navigateur n'est pas pilotable."""
    if not re.match(r"^https?://", url, re.I):
        return False
    browser = browser or target_browser()
    lines = _NEW_TAB.get(browser)
    if not lines:
        return False
    try:
        r = subprocess.run(["osascript", *[x for ln in lines for x in ("-e", ln)], url],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def target_browser() -> str:
    """Navigateur au premier plan s'il est pris en charge, sinon le navigateur par défaut."""
    try:
        from AppKit import NSURL, NSWorkspace

        ws = NSWorkspace.sharedWorkspace()
        front = ws.frontmostApplication()
        name = str(front.localizedName()) if front else ""
        if name in BROWSERS:
            return name
        url = ws.URLForApplicationToOpenURL_(NSURL.URLWithString_("https://example.com"))
        if url is not None:
            default = Path(str(url.path())).stem
            if default in BROWSERS:
                return default
    except Exception:
        pass
    return "Google Chrome"


def _osascript(line: str, arg: str, timeout: float = 10) -> str:
    script = ["on run argv", line, "end run"]
    try:
        r = subprocess.run(["osascript", *[x for ln in script for x in ("-e", ln)], arg],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ChromeError("le navigateur ne répond pas") from exc
    if r.returncode != 0:
        err = r.stderr.strip()
        if "JavaScript" in err or "-1743" in err or "javascript" in err.lower():
            raise ChromeError("le navigateur refuse la lecture de la page : autorisez voxjev dans Réglages › "
                              "Confidentialité › Automatisation (Safari : Développement › Autoriser le JavaScript "
                              "depuis les événements Apple)")
        raise ChromeError(f"navigateur : {err[-160:] or 'aucune fenêtre ouverte'}")
    return r.stdout.strip()


def _decode(raw: str) -> dict:
    """JSON renvoyé par la page ; certains navigateurs (Arc) le renvoient encodé deux fois."""
    data = json.loads(raw or "{}")
    if isinstance(data, str):
        data = json.loads(data or "{}")
    return data if isinstance(data, dict) else {}


def _parse(data: dict) -> tuple[str, str, list[Link]]:
    links = [Link(x["href"], x["text"], bool(x["result"])) for x in data.get("links", [])
             if not PRIVATE.search(x["href"]) and not PRIVATE.search(x["text"])]
    return data.get("title", ""), data.get("url", ""), links[:MAX_LINKS]


def page_links(timeout: float = 20) -> tuple[str, str, list[Link]]:
    """(titre, url, liens) de l'onglet actif du navigateur ; les résultats de recherche en premier."""
    browser = target_browser()
    if browser == "Google Chrome":
        try:
            return _cdp_page_links()
        except ChromeError as cdp_error:
            try:
                return _parse(_decode(_osascript(_CHROMIUM_AS[browser][0], LINKS_JS)))
            except ChromeError:
                raise cdp_error from None
    return _parse(_decode(_osascript(_CHROMIUM_AS[browser][0], LINKS_JS, timeout=timeout)))


def _cdp_page_links() -> tuple[str, str, list[Link]]:
    s = _session()
    try:
        _, session = s.attach_active()
        r = s.call("Runtime.evaluate", session, expression=LINKS_JS, returnByValue=True)
        data = json.loads(r.get("result", {}).get("value") or "{}")
    finally:
        s.close()
    return _parse(data)


def navigate(url: str) -> None:
    """Ouvre `url` dans l'onglet actif. Seules des adresses http(s) lues sur la page arrivent ici."""
    if not re.match(r"^https?://", url, re.I):
        raise ChromeError(f"adresse refusée : {url!r}")
    browser = target_browser()
    if browser != "Google Chrome":
        _osascript(_CHROMIUM_AS[browser][1], url)
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        return
    try:
        _cdp_navigate(url)
    except ChromeError:
        _osascript(_CHROMIUM_AS[browser][1], url)


def _cdp_navigate(url: str) -> None:
    s = _session()
    try:
        _, session = s.attach_active()
        s.call("Page.navigate", session, url=url)
    finally:
        s.close()
    subprocess.run(["open", "-a", "Google Chrome"], check=False, timeout=5)


def search_query(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    return (q.get("q") or q.get("query") or q.get("search_query") or [""])[0]


def pick(client, transcript: str, title: str, url: str, links: list[Link], min_p: float = 0.5):
    """(lien, p, comment) : rang compté par le code, sinon choix de Jev parmi les liens réels."""
    if not links:
        raise ChromeError("aucun lien visible sur la page")
    n = ordinal(transcript)
    results = [link for link in links if link.result] or links
    if n is not None:
        pool = results
        if n == -1:
            return pool[-1], 1.0, "rang"
        if n > len(pool):
            raise ChromeError(f"la page n'a que {len(pool)} résultat(s)")
        return pool[n - 1], 1.0, "rang"
    if BEST.search(transcript) and any(link.result for link in links):
        return results[0], 1.0, "rang"  # « le meilleur » : le 1er résultat du moteur de recherche
    state = {"transcript": transcript, "page": {"title": title, "url": url, "search_query": search_query(url)}}
    ordered = results + [link for link in links if link not in results]
    idx, p, _ = client.choose(state, QUESTION, [link.label for link in ordered], none="No link fits")
    if idx is None or p < min_p:
        raise ChromeError("je ne sais pas quel lien ouvrir : précisez (« le 2ᵉ », « celui de Wikipédia »)")
    return ordered[idx], p, "jev"
