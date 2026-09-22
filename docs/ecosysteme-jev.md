# Écosystème Jev : ce qu'on peut reprendre dans voxjev

Recherche GitHub du 2026-09-22 (listes `yibie/awesome-jev` (1 125 ★), `AnotiaWang/awesome-jev`,
`cobanov/awesome-jev`…, plus une recherche directe). Environ 75 projets pertinents ; ceux-ci ont un
lien direct avec voxjev. **Réutiliser du code n'est possible que sous licence permissive, en
conservant la mention de copyright** ; sans licence, on ne reprend que les idées.

## À reprendre en priorité

| Projet | Licence | Ce qu'il fait | Ce qu'on en tire |
|---|---|---|---|
| [kevinbadi/jev-voice](https://github.com/kevinbadi/jev-voice) | MIT | Le « jumeau » de voxjev : whisper.cpp + 1 appel Jev + automatisation macOS, ~15 questions spéculatives par énoncé | **Mot d'éveil local** (« Alfred, … », seule la phrase adressée part vers Jev) + **fenêtre de suite de 8 s** ; **Verr. Maj → F18** comme touche (hidutil + LaunchAgent) ; **« sélectionner plutôt que générer »** : le code découpe des candidats dans la phrase, Jev choisit (saisie de texte sans LLM) ; ~45 raccourcis clavier, défilement, saisie ; retour vocal `say` ; **agent bureau** (jev-ultrafast porté sur l'arbre d'accessibilité macOS) ; mode « recommend » |
| [rohit9mehta/dwim](https://github.com/rohit9mehta/dwim) | MIT | Palette « fais ce que je veux dire » : lit **tout le menu** de l'app au premier plan via l'accessibilité, un Noul par élément de menu | **Commande universelle pour n'importe quelle app** : « exporte en PDF », « nouvelle fenêtre privée »… sans rien configurer. Garde-fou : les menus au nom destructeur ne s'exécutent jamais seuls. Seuils `confident` + `lead` (écart avec le 2e) |
| [moritzkremb/jev-voice-browser](https://github.com/moritzkremb/jev-voice-browser) | MIT | Décision Jev sur **chaque transcription partielle** (~300 ms), agit avant la fin de la phrase | Questions « la commande est-elle complète ? » : **réduire la latence** en décidant pendant qu'on parle |
| [gaborishka/jev-canvas](https://github.com/gaborishka/jev-canvas) | MIT | Voix + doigt pointé ; 8 questions par transcription partielle (commande ?, phrase complète ?…) | Même idée de streaming ; « annuler / rétablir » vocal ; Jev accessible aussi via l'API « Decisions » d'OpenRouter |
| [vynnlee/jev-mail](https://github.com/vynnlee/jev-mail) | MIT | Tri Gmail continu : `requires_action`, `is_important`, `bucket` | **« Quels mails demandent une action ? »** : triage de la boîte (Mail.app ou Gmail), incertain → à revoir |
| [kitfunso/hippo-memory](https://github.com/kitfunso/hippo-memory) | à vérifier | Mémoire d'agent + reranker Jev (R@1 0,41 → 0,62) | **Mémoire personnelle** (« retiens que mon dentiste c'est le Dr X ») avec rappel classé par Jev |
| [hotchpotch/jev-reranker](https://github.com/hotchpotch/jev-reranker), [shinpr/jev-reranker](https://github.com/shinpr/jev-reranker) | à vérifier | Reclassement de candidats par Noul | **Recherche de fichiers** : Spotlight → candidats → Jev classe |
| [mrnugget/jev-shell-history](https://github.com/mrnugget/jev-shell-history) | à vérifier | Historique zsh classé par Jev | Idée : « relance la commande de build d'hier » |

## Idées seulement (pas de licence : code non réutilisable)

- [dabit3/jev-experiments](https://github.com/dabit3/jev-experiments) : 22 démos axées latence (garde shell, veille de logs, recherche instantanée, reranking, **tours de parole vocaux**).

## Écartés

- Générateurs de comptes / « clés illimitées » (contournement des conditions de TypeSafe) : exclus.
- Proxys « imitant » Jev, routeurs de modèles pour agents de code : hors sujet pour voxjev.

## Plan d'intégration retenu

1. **Toujours là** : lancement à l'ouverture de session (LaunchAgent) + `voxjev.app` ; option
   Verr. Maj → F18 (jev-voice) ; « annule ça » + journal.
2. **Routines et raccourcis** : routines perso dans le YAML ; import automatique des Raccourcis
   macOS ; **menus de n'importe quelle app (DWIM)**.
3. **Productivité** : rappels/minuteurs, agenda, notes dictées (dates lues par Jev, calcul en code).
4. **« Sélectionner plutôt que générer »** (jev-voice) : arguments texte choisis par Jev parmi des
   segments de la phrase quand les regex échouent ; saisie de texte de l'agent web sans LLM.
5. **Fichiers** : Spotlight + reranking Jev.
6. Plus tard : mot d'éveil + fenêtre de suite, décisions en streaming, agent bureau (accessibilité),
   triage des mails, mémoire personnelle.
