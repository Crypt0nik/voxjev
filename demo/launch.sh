#!/bin/bash
# Démo vidéo : ouvre 4 fenêtres Ghostty (3 projets fictifs animés + les VRAIS tests de voxjev).
# Lancé par la commande vocale « lance mes projets » (mode démo). Fermeture : demo/stop.sh.
# La commande est écrite dans le shell de la fenêtre (--input) : pas de demande « Allow » de Ghostty.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d /Applications/Ghostty.app ] || { echo "Ghostty introuvable" >&2; exit 1; }

open_window() {  # titre, commande (script fixe du dépôt)
  open -na /Applications/Ghostty.app --args \
    --window-save-state=never --confirm-close-surface=false --quit-after-last-window-closed=true \
    --background=#0b1020 --foreground=#e6edf3 --background-opacity=0.88 --background-blur=true \
    --font-size=13 --window-padding-x=14 --window-padding-y=10 \
    --title="voxjev-demo · $1" --working-directory="$DIR" "--input=raw:clear; $2\\n"
}

open_window "API" ".venv/bin/python demo/fake_project.py api"
sleep 0.2
open_window "entraînement" ".venv/bin/python demo/fake_project.py train"
sleep 0.2
open_window "déploiement" ".venv/bin/python demo/fake_project.py deploy"
sleep 0.2
open_window "tests" "bash demo/real_tests.sh"
exit 0
