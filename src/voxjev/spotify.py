"""Intégration Spotify : lancer un morceau/artiste/playlist, liker le morceau en cours, chercher.

- Lecture : app de bureau via AppleScript (``play track "spotify:…"``), aucun compte API requis
  pour jouer, mais l'identifiant exact vient de la recherche de l'API Web.
- Recherche et like : API Web Spotify avec connexion OAuth « PKCE » (identifiant d'app seul, pas
  de secret). Connexion une fois : ``./voxjev --spotify-login``. Le jeton est stocké localement
  (permissions 600) et rafraîchi automatiquement.
- Sans connexion : « mets X » ouvre la recherche dans l'app (repli), le like explique quoi faire.

Sécurité : seuls des identifiants ``spotify:(track|artist|album|playlist):<22 caractères>``
validés par regex atteignent AppleScript, toujours en argv ; la requête de recherche n'est
envoyée qu'à l'API (encodée) ou dans une URL ``spotify:search:`` encodée.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from rapidfuzz import fuzz

from .context import STATE_FILE

TOKEN_FILE = STATE_FILE.parent / "spotify_token.json"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-library-modify user-library-read user-read-playback-state"
API = "https://api.spotify.com/v1"
URI_RE = re.compile(r"^spotify:(track|artist|album|playlist):[A-Za-z0-9]{22}$")


class SpotifyError(RuntimeError):
    pass


def _osascript(lines: list[str], *argv: str, timeout: float = 8) -> str:
    cmd = ["osascript"] + [x for line in lines for x in ("-e", line)] + list(argv)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SpotifyError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "AppleScript a échoué")
    return proc.stdout.strip()


def current_track_uri() -> str | None:
    try:
        out = _osascript(['tell application "Spotify" to get id of current track'])
    except (SpotifyError, subprocess.TimeoutExpired):
        return None
    return out if URI_RE.match(out) else None


# ----------------------------------------------------------------------------- OAuth PKCE
@dataclass
class Token:
    access: str
    refresh: str
    expires_at: float

    def save(self) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(self.__dict__))
        os.chmod(TOKEN_FILE, 0o600)

    @classmethod
    def load(cls) -> Token | None:
        try:
            return cls(**json.loads(TOKEN_FILE.read_text()))
        except (OSError, ValueError, TypeError):
            return None


def _post_token(data: dict) -> dict:
    req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                 data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SpotifyError(f"jeton refusé par Spotify ({exc.code}) : {exc.read().decode()[:200]}") from exc


def login(client_id: str) -> Token:
    """Ouvre le navigateur sur la page de consentement Spotify et récupère le jeton (PKCE)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "voxjev est connecté à Spotify. Vous pouvez fermer cet onglet." if ok else "Connexion refusée."
            self.wfile.write(f"<p style='font:16px system-ui'>{msg}</p>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 8888), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
    })
    subprocess.run(["open", url], check=False)
    print("Navigateur ouvert sur Spotify : acceptez l'accès (2 minutes max)…")
    thread.join(timeout=120)
    server.server_close()
    if "code" not in result:
        raise SpotifyError(f"pas de code reçu ({result.get('error', 'délai dépassé')})")
    data = _post_token({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT_URI,
                        "client_id": client_id, "code_verifier": verifier})
    token = Token(data["access_token"], data["refresh_token"], time.time() + data["expires_in"] - 60)
    token.save()
    return token


# ----------------------------------------------------------------------------- client
class Spotify:
    def __init__(self, client_id: str | None = None):
        self.client_id = client_id or os.environ.get("SPOTIFY_CLIENT_ID")
        self._token = Token.load()

    @property
    def connected(self) -> bool:
        return bool(self.client_id and self._token)

    def _access(self) -> str:
        if not self.connected:
            raise SpotifyError("Spotify non connecté : ajoutez SPOTIFY_CLIENT_ID au .env puis "
                               "lancez ./voxjev --spotify-login")
        if time.time() >= self._token.expires_at:
            data = _post_token({"grant_type": "refresh_token", "refresh_token": self._token.refresh,
                                "client_id": self.client_id})
            self._token = Token(data["access_token"], data.get("refresh_token", self._token.refresh),
                                time.time() + data["expires_in"] - 60)
            self._token.save()
        return self._token.access

    def _api(self, method: str, path: str, params: dict | None = None) -> dict:
        url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {self._access()}"},
                                     data=b"" if method in ("PUT", "POST") else None)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise SpotifyError(f"API Spotify {exc.code} sur {path} : {exc.read().decode()[:160]}") from exc
        except urllib.error.URLError as exc:
            raise SpotifyError(f"API Spotify injoignable : {exc.reason}") from exc

    # ---------------------------------------------------------------- opérations
    def resolve(self, query: str) -> tuple[str, str]:
        """Requête parlée -> (uri, libellé). Artiste si la requête EST un nom d'artiste, sinon titre."""
        data = self._api("GET", "/search", {"q": query, "type": "track,artist,playlist", "limit": 5, "market": "from_token"})
        artists = [a for a in data.get("artists", {}).get("items", []) if a]
        tracks = [t for t in data.get("tracks", {}).get("items", []) if t]
        playlists = [p for p in data.get("playlists", {}).get("items", []) if p]
        if "playlist" in query.lower() and playlists:
            p = playlists[0]
            return p["uri"], f"playlist « {p['name']} »"
        if artists and fuzz.ratio(artists[0]["name"].lower(), query.lower()) >= 88:
            return artists[0]["uri"], f"artiste {artists[0]['name']}"
        if tracks:
            t = tracks[0]
            return t["uri"], f"« {t['name']} » — {', '.join(a['name'] for a in t['artists'])}"
        raise SpotifyError(f"aucun résultat Spotify pour « {query} »")

    def play(self, query: str) -> str:
        uri, label = self.resolve(query)
        if not URI_RE.match(uri):
            raise SpotifyError(f"identifiant Spotify inattendu : {uri!r}")
        _osascript(["on run argv", 'tell application "Spotify"', "activate",
                    "play track (item 1 of argv)", "end tell", "end run"], uri)
        if uri.startswith("spotify:track:"):  # attendre le changement réel (pour un « like » juste après)
            for _ in range(15):
                if current_track_uri() == uri:
                    break
                time.sleep(0.2)
        return f"lecture : {label}"

    def like_current(self) -> str:
        uri = current_track_uri()
        if not uri or not uri.startswith("spotify:track:"):
            raise SpotifyError("aucun morceau en cours dans Spotify")
        track_id = uri.rsplit(":", 1)[1]
        self._api("PUT", "/me/tracks", {"ids": track_id})
        name = _osascript(['tell application "Spotify" to get (name of current track) & " — " & (artist of current track)'])
        return f"liké : {name}"

    @staticmethod
    def search_ui(query: str) -> str:
        subprocess.run(["open", "spotify:search:" + urllib.parse.quote(query, safe="")], check=True, timeout=8)
        return f"recherche Spotify : « {query} »"


def run_op(op: str, query: str = "") -> str:
    """Point d'entrée de l'exécuteur. Repli sans connexion : la lecture ouvre la recherche."""
    sp = Spotify()
    if op == "play":
        if not sp.connected:
            return Spotify.search_ui(query) + " (connectez Spotify pour lancer directement : ./voxjev --spotify-login)"
        return sp.play(query)
    if op == "like":
        return sp.like_current()
    if op == "search":
        return Spotify.search_ui(query)
    raise SpotifyError(f"opération Spotify inconnue : {op!r}")


def login_cli() -> int:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        print("SPOTIFY_CLIENT_ID absent du .env. Créez une app sur https://developer.spotify.com/dashboard :\n"
              f"  - Redirect URI : {REDIRECT_URI}\n  - API : Web API\n"
              "puis ajoutez SPOTIFY_CLIENT_ID=<Client ID> au fichier .env et relancez.")
        return 2
    try:
        login(client_id)
        me = Spotify(client_id)._api("GET", "/me")
    except SpotifyError as exc:
        print(f"Échec : {exc}")
        return 1
    print(f"Connecté à Spotify en tant que {me.get('display_name') or me.get('id')} "
          f"(compte {me.get('product', '?')}). Jeton : {TOKEN_FILE}")
    return 0
