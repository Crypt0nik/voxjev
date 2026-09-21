# voxjev — lanceur vocal macOS en français, décidé par Jev

Maintenez une touche, parlez, relâchez : la phrase est transcrite **localement** par Whisper,
**un seul appel** à [Jev](https://docs.typesafe.ai) (TypeSafe) décide quelle commande de votre
configuration elle vise, et du code déterministe exécute, demande confirmation ou ignore.

```
Option droite maintenue ─► micro 16 kHz ─► mlx-whisper (local, ~1,2 s)
        ─► Jev : 1 requête, 3 questions (~0,3–0,8 s)
             • Choice   : quelle commande de la config ? (+ « none »)
             • Noul     : l'énoncé m'est-il adressé ?
             • Noul     : l'action est-elle destructrice ?
        ─► décision en code : exécuter / confirmer / ignorer
        ─► arguments extraits en code (regex + fuzzy matching) ─► liste blanche d'actions
        ─► son de validation / d'échec + log des probabilités et latences
```

## Installation

Prérequis : Mac Apple Silicon, [uv](https://docs.astral.sh/uv/), `ffmpeg` (uniquement pour `--audio`).

```bash
uv sync
echo 'TYPESAFE_API_KEY=votre_clé' > .env     # jamais commité (.gitignore)
./voxjev --text "ouvre Spotify" --dry-run      # premier test, sans micro
```

Le premier lancement en mode micro télécharge le modèle Whisper (~1,5 Go, une seule fois).

**Autorisations macOS** (pour l'app de terminal qui lance voxjev : Terminal, Ghostty, VS Code…) :
1. Réglages Système › Confidentialité et sécurité › **Accessibilité** : écoute de la touche globale et raccourcis clavier ;
2. **Surveillance de l'entrée** : écoute de la touche globale ;
3. **Micro** : demandé automatiquement au premier appui.

voxjev vérifie l'accessibilité au démarrage et affiche la marche à suivre si elle manque.

> **Projet dans `~/Documents` synchronisé par iCloud** : iCloud marque les fichiers de `.venv`
> comme « cachés » (Python ignore alors les `.pth`) et ralentit énormément les imports.
> L'environnement vit donc dans `.venv.nosync` (ignoré par iCloud), et `.venv` n'est qu'un lien
> symbolique vers ce dossier. Pour le recréer : `rm -rf .venv .venv.nosync && uv venv .venv.nosync && ln -s .venv.nosync .venv && uv sync`.
> Le lanceur `./voxjev` ajoute `src/` au `PYTHONPATH` pour ne pas dépendre du `.pth`, et
> place les caches `.pyc` dans `~/Library/Caches/voxjev` : iCloud verrouille les fichiers
> qu'il synchronise, ce qui peut bloquer un import pendant des dizaines de secondes.
> **Le mieux reste de placer le projet hors de `~/Documents`** (par ex. `~/dev/jev_AI`).

## Utilisation

| Commande | Effet |
|---|---|
| `./voxjev` | mode micro : maintenir **Option droite** (configurable), parler, relâcher |
| `./voxjev --dry-run` | mode micro, mais affiche le plan sans rien exécuter |
| `./voxjev --text "ouvre chrome et cherche la météo à Lyon" --dry-run` | tester une phrase sans micro |
| `./voxjev --text "vide la corbeille"` | exécuter réellement (confirmation `o/N` dans le terminal) |
| `./voxjev --audio phrase.wav --dry-run` | Whisper puis Jev sur un fichier audio |
| `./voxjev --mode ctf ...` | forcer le mode (`defaut`, `ctf`, `travail`) |
| `./voxjev --eval [cases.tsv]` | évaluation : précision, faux déclenchements, latence |
| `./voxjev --fake ...` | faux client Jev, hors ligne |
| `uv run pytest` | tests unitaires (100 % hors ligne, faux client) |

Exemple de log :

```
« Ouvre Chrome et cherche la météo à Lyon. »  [mode defaut]
  Jev 312 ms (jev-1.13.0, 2384 tok) → web_search p=1.00 conf=1.00 | open_app 0.00 · none 0.00 | adressé 0.93 | destructif 0.02
  Décision : EXECUTE (p=1.00 >= 0.70)
  Arguments : query='la météo à Lyon' browser='Google Chrome'
  $ open -a 'Google Chrome' https://www.google.com/search?q=la+m%C3%A9t%C3%A9o+%C3%A0+Lyon
  → executed  (audio_s=2.4 stt_ms=1190 context_ms=1 jev_ms=312 action_ms=85 total_ms=1590)
```

Sons : *Tink* à l'appui, *Glass* = exécuté, *Basso* = échec ou annulé, *Pop* = ignoré (`--no-sound` pour couper).
Le mode actif et la dernière commande sont conservés dans `~/Library/Application Support/voxjev/state.json`.

## Interface graphique (`./voxjev --gui`)

Une petite interface native macOS, pensée pour un lanceur vocal :

- **Icône dans la barre des menus** (pas d'icône dans le Dock). Elle change selon l'état :
  écoute, transcription, Jev, confirmation, erreur. Son menu donne accès au mode (défaut,
  CTF, travail), à **Tester une phrase…** (⌘T), au dernier résultat, à l'historique, au
  dry-run, aux sons, à la configuration (ouvrir, recharger) et à Quitter.
- **HUD flottant en haut de l'écran**, façon Spotlight :
  - vumètre du micro pendant l'appui ;
  - transcript ;
  - décision (EXÉCUTÉ / CONFIRMER ? / IGNORÉ / ERREUR / DRY-RUN), commande et arguments ;
  - barres de probabilité : les 3 meilleures options du Choice, « adressé », « destructif » ;
  - latences (STT, Jev, action, total).

  Il se masque tout seul après quelques secondes.
- **Le HUD ne prend jamais le focus** : c'est un panneau non-activant. L'« app au premier
  plan » envoyée à Jev reste la vôtre, et les raccourcis clavier (nouvel onglet, fermer la
  fenêtre…) partent dans la bonne app.
- **Confirmation sans voler le focus** : cliquez sur *Exécuter* / *Annuler* dans le HUD, ou
  maintenez la touche et dites « oui » / « non ». La réponse vocale est analysée par du code
  déterministe, sans appel à Jev. Sans réponse au bout de 10 s, l'action est annulée.
- Tout le travail lourd (Whisper, Jev, actions) tourne dans un seul thread dédié.
  L'interface reste fluide.

```bash
./voxjev --gui                  # interface + push-to-talk
./voxjev --gui --dry-run        # idem, sans rien exécuter (bascule aussi dans le menu)
./voxjev --gui --text "ouvre notion"   # démarre en traitant une phrase de démo
```

Débogage : `VOXJEV_SNAPSHOT=/tmp/hud ./voxjev --gui ...` enregistre un PNG du HUD à chaque affichage.

## Règles de décision (code, `src/voxjev/decide.py`)

| Condition (dans l'ordre) | Décision |
|---|---|
| Choice = `none` | ignorer |
| adressé < `addressed_floor` (0,35) | ignorer |
| p(commande) < `confirm_floor` (0,40) | ignorer |
| commande `destructive: true` **ou** Noul destructif ≥ `destructive_threshold` (0,50) | **confirmer, toujours** |
| adressé < `addressed_threshold` (0,70) | confirmer |
| p(commande) < `threshold` (0,70) | confirmer |
| sinon | exécuter |

Tous les seuils sont dans `config/commands.yaml` (`settings`). La confirmation passe par une boîte
de dialogue macOS en mode micro (10 s, sinon annulé), et par `o/N` en mode `--text`.

*Pourquoi une zone « adressé incertain » plutôt qu'un seuil unique ?* Sur `cases.tsv`, le Noul
« adressé » sépare mal les commandes télégraphiques (« exploit db ProFTPD 1.3.5 » → 0,41) des
phrases ambiguës (« ferme la fenêtre il fait froid » → 0,53). Un seuil strict à 0,70 ratait 4
commandes. Avec la zone de confirmation, on obtient 100 % de rappel sans aucune exécution directe
non voulue (voir *Évaluation*).

## Configuration : `config/commands.yaml`

Tout ce qui peut être exécuté est déclaré ici, sous forme de données, et validé au chargement :

```yaml
- id: web_search
  description: "Faire une recherche sur le web (Google), éventuellement dans un navigateur précis"
  examples: ["cherche la météo à Lyon", "ouvre Chrome et cherche des recettes de crêpes"]
  destructive: false
  args:
    query:   {type: text, patterns: ["(?:re)?cherch\\w*\\s+(?P<query>.+?)..."]}
    browser: {type: app, optional: true, choices: [Google Chrome, Safari, Arc, Brave Browser], patterns: [...]}
  action: {type: open_url, url: "https://www.google.com/search?q={query}", app: "{browser}"}
```

- `description` et `examples` alimentent la question Choice envoyée à Jev (options structurées `{what, examples}`).
- **Modes** : `common` liste les commandes présentes partout ; chaque mode ajoute les siennes et peut
  déclarer un `on_enter` (ex. `ctf` ouvre Ghostty/Terminal, Burp Suite et Chrome). Seules les
  commandes du mode actif sont proposées à Jev.
- **Types d'argument** : `app` (fuzzy matching sur les apps installées + `app_aliases`), `text`
  (texte libre, avec des `rewrite` regex optionnels, ex. `CVE 2014 0160` → `CVE-2014-0160`),
  `enum` (valeur tirée de la config), `mode`.
- **Types d'action (liste blanche)** : `open_app`, `quit_app`, `open_url`, `applescript`,
  `keystroke`, `shortcut`, `exec`, `set_mode`, `sequence`.

Commandes fournies (26) : ouvrir/quitter une app, recherche web, YouTube, sites connus, volume
+/−, muet, lecture/pause et morceau suivant (Spotify), capture d'écran, verrouillage, veille de
l'écran, vider la corbeille, fermer la fenêtre, nouvel onglet, changer de mode ; en mode **ctf** :
Exploit-DB, CVE, GTFOBins, revshells, CyberChef, nouveau terminal ; en mode **travail** : nouvel
e-mail, recherche GitHub, nouvelle note.

## Sécurité

- **Jev ne fait que choisir** un identifiant parmi les commandes de la config. Jamais de chaîne
  issue de Jev ou du transcript n'est exécutée.
- **Aucun shell** : tout passe par `subprocess.run([...])` avec une liste d'arguments.
- Les **arguments** sont extraits par du code déterministe :
  - un nom d'app doit correspondre à une app **installée** ;
  - un texte libre n'entre **que dans une URL**, encodé (`quote_plus`), et l'URL finale doit commencer par `https://`, `http://` ou `mailto:` ;
  - un `enum` ou un `mode` renvoie une valeur écrite dans la config.
- Les scripts AppleScript sont **figés** dans la config, et les noms d'app leur sont passés en `argv`.
- `exec` n'accepte que des `argv` figés dont le binaire figure dans `settings.exec_allowlist`.
- La validation de la config refuse les types d'action inconnus, les placeholders non déclarés ou
  placés dans un champ interdit, les schémas d'URL non autorisés, etc. (voir `tests/test_core.py`).

## Données envoyées à l'API TypeSafe

Seule la requête Jev quitte la machine (`POST https://api.typesafe.ai/v1/systemone`). Son `state` contient **exactement** :

| Champ | Contenu |
|---|---|
| `transcript` | le texte transcrit de l'énoncé |
| `frontmost_app` | le nom de l'app au premier plan |
| `installed_apps` | la liste des noms d'apps installées (`/Applications`, `/System/Applications`, `~/Applications`) |
| `assistant_mode` | l'identifiant du mode actif (`defaut`, `ctf`, `travail`) |
| `last_command` | l'identifiant de la dernière commande exécutée (ex. `open_app`), ou `none` |

Les questions envoyées avec la requête ne contiennent que des éléments de la config (descriptions
et exemples des commandes). Le mode et la dernière commande ne sont que des identifiants définis
dans votre config : ni contenu utilisateur, ni argument.

**Rien d'autre ne part** : ni l'audio (Whisper tourne en local), ni les arguments extraits, ni les
fichiers, ni l'historique. La clé est lue dans `TYPESAFE_API_KEY` (fichier `.env` non commité) et
n'est jamais écrite en dur ni loggée. D'après TypeSafe, Jev n'est pas entraîné sur les requêtes des
clients.

## Choix du moteur de transcription

Mesuré sur ce Mac M2, avec des énoncés de ~3 s et le modèle déjà chargé :

| Moteur | Modèle | Latence |
|---|---|---|
| **mlx-whisper** (retenu) | large-v3-turbo fp16 | **~1,18 s** |
| mlx-whisper | large-v3-turbo q4 | ~1,27 s |
| whisper.cpp (Metal, `whisper-server`) | large-v3-turbo q5_0 | ~1,67 s |

mlx-whisper tourne dans le même processus (modèle gardé en mémoire), sans compilation. Un
`stt_prompt` souffle à Whisper le vocabulaire difficile (Burp, GTFOBins, CyberChef…).

## Évaluation (`./voxjev --eval`)

`cases.tsv` : 82 phrases, au moins 2 qui déclenchent et 1 phrase proche qui ne doit **pas**
déclencher par commande, plus du bruit conversationnel. Colonnes : phrase, id attendu ou `none`,
mode (optionnel).

Résultat avec `jev-1.13.0` (contexte figé, dry-run, 6 appels en parallèle) :

```
exactitude globale     : 81/82 = 98.8%
précision (déclenchés) : 52/53 = 98.1%
rappel (commandes)     : 100.0%  (0 ratés, 0 mauvaise commande, 0 arguments non extraits)
faux déclenchements    : 1/30 phrases « none » = 3.3%  (dont 0 exécutés directement, 1 avec confirmation demandée)
latence Jev            : p50 ~270 ms · p95 ~770 ms
tokens d'entrée        : ~2 500 par appel (≈ 0,0001 $ par énoncé)
```

Le seul faux déclenchement, « ferme la fenêtre il fait froid », passe par une confirmation. La
majorité des tokens vient de la liste des apps installées.

## Structure

```
config/commands.yaml   réglages, modes, commandes (données)
cases.tsv              jeu d'évaluation
voxjev                 lanceur (uv run + PYTHONPATH=src)
src/voxjev/
  cli.py        arguments, mode --text / --audio, affichage
  jev_client.py JevClient (1 appel, 3 questions) + FakeJevClient (tests hors ligne)
  decide.py     exécuter / confirmer / ignorer
  args.py       extraction déterministe des arguments
  actions.py    config -> étapes argv (liste blanche)
  executor.py   exécution subprocess, sons, dialogue de confirmation
  context.py    app au premier plan, apps installées, état persistant
  stt.py        mlx-whisper
  audio.py      micro + push-to-talk (pynput)
  listen.py     boucle du mode micro (terminal)
  gui.py        interface : barre des menus + HUD non-activant + thread moteur
  evaluate.py   runner --eval
tests/test_core.py
```

## Limites connues

- Les raccourcis clavier (capture, verrouillage, nouvel onglet) et le push-to-talk exigent les
  autorisations Accessibilité / Surveillance de l'entrée.
- `play_pause` et `next_track` visent Spotify (à changer dans la config pour Music).
- La liste des apps est lue une fois au démarrage du mode micro : redémarrez après une installation.
- Jev lit les phrases très littéralement. Pour ajouter une commande, donnez-lui une description
  distinctive et des exemples, puis ajoutez 3 lignes à `cases.tsv` et relancez `--eval`.
