#!/bin/bash
# Fenêtre « tests » de la démo : les VRAIS tests de voxjev, puis la vraie évaluation Jev (175 phrases).
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
printf '\033[2J\033[H\033[38;5;114m╭──────────────────────────────────────────────────────────╮\n'
printf '│\033[0m \033[1m✅ voxjev · tests et évaluation (réels)\033[0m                   \033[38;5;114m│\n'
printf '╰──────────────────────────────────────────────────────────╯\033[0m\n\n'
PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider --color=yes 2>&1 | tail -3
echo
if grep -q "^TYPESAFE_API_KEY=." .env 2>/dev/null; then
  set -a; . ./.env; set +a
  PYTHONPATH=src .venv/bin/python -m voxjev --eval --workers 8 2>&1 | tail -12
else
  echo "(évaluation Jev ignorée : pas de clé TYPESAFE_API_KEY)"
fi
echo
printf '\033[38;5;245m— fenêtre de démo, fermez-la ou dites « ferme les projets » —\033[0m\n'
exec sleep 100000
