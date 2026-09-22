# Démo vidéo de voxjev

Un scénario de ~45 secondes pour montrer voxjev en action : voix → action en moins d'une seconde,
fenêtres qui se rangent toutes seules, vrais tests qui défilent.

> Les projets « API », « entraînement » et « déploiement » sont **fictifs** (animations locales,
> aucun réseau). La fenêtre « tests » lance les **vrais** tests de voxjev et la vraie évaluation Jev.

## Préparer le tournage (2 minutes)

1. Fermez ou masquez les fenêtres personnelles (mails, messageries…) et sortez votre terminal du
   plein écran : le rangement ne touche jamais une fenêtre en plein écran sans qu'on le demande.
2. Bureau vide, fond d'écran sobre, barre des menus visible (*Réglages › Barre des menus*).
3. Réglages de voxjev › Général : **Sons** activés (le « tink » à chaque ordre rend bien en vidéo),
   **Lire les réponses à voix haute** activé.
4. Enregistrement : `⇧⌘5` › *Enregistrer tout l'écran*, micro activé (pour entendre votre voix).
5. Une première fois hors caméra : dites « lance mes projets » (Ghostty peut demander une
   autorisation au tout premier lancement), puis « ferme les projets ».

## Scénario

| Temps | Vous dites (en maintenant ⌥ droite) | À l'écran |
|---|---|---|
| 0:00 | « passe en mode démo » | la capsule Liquid Glass apparaît, l'orbe réagit à la voix |
| 0:04 | « ouvre la calculatrice et lance mes projets » | la Calculatrice s'ouvre, puis **4 terminaux** : API, entraînement, déploiement, tests. Ils glissent en **grille** |
| 0:12 | *(laisser tourner 5 s)* | barres de progression, requêtes en direct, **110 tests réussis en 0,5 s**, évaluation Jev qui défile |
| 0:18 | « mets la fenêtre des tests en grand » *(cliquez d'abord dessus)* ou « mets cette fenêtre à gauche » | la fenêtre prend la moitié de l'écran |
| 0:22 | « remets les fenêtres comme avant » | tout reprend sa place, animé |
| 0:26 | « cherche la météo à Paris et ouvre le premier lien » | le navigateur s'ouvre **à côté**, la recherche puis le 1er résultat |
| 0:33 | « quelle heure est-il » | réponse lue à voix haute, carte de résultat |
| 0:37 | « range les fenêtres » | tout se range en grille |
| 0:41 | « ferme les projets » | les terminaux disparaissent |

## Idées de montage

- Accélérer ×1,5 les pauses, jamais les actions (la vitesse réelle est l'argument).
- Sous-titrer chaque phrase dite ; afficher en incrustation les chiffres du HUD
  (« Jev 300 ms », « total 1,1 s »).
- Plan final : le logo et « Whisper local · Jev · 110 tests · open source ».

## Démo jouée automatiquement

```bash
./voxjev --gui --script demo/scenario.txt   # les phrases du scénario, comme si elles étaient dites
```

## Commandes utiles

```bash
./demo/launch.sh     # ouvre les 4 terminaux sans passer par la voix
./demo/stop.sh       # ferme uniquement les fenêtres de démo
```

Le mode démo n'est proposé à Jev qu'après « passe en mode démo » : ses commandes n'encombrent
pas l'usage normal.
