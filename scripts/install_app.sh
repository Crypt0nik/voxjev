#!/usr/bin/env bash
# Construit ~/Applications/voxjev.app et, avec --login, l'ouvre à chaque connexion.
#   ./scripts/install_app.sh [--login]      installer (ou reconstruire)
#   ./scripts/install_app.sh --uninstall    retirer l'app et l'ouverture à la connexion
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$HOME/Applications/voxjev.app"

remove_login_item() {
  osascript -e 'tell application "System Events" to if exists login item "voxjev" then delete login item "voxjev"' >/dev/null
}

if [[ "${1:-}" == "--uninstall" ]]; then
  remove_login_item || true
  pkill -f "python -m voxjev --gui" || true
  rm -rf "$APP"
  echo "voxjev.app retirée."
  exit 0
fi

[[ -x "$PROJECT/.venv/bin/python" ]] || { echo "Lancez d'abord : uv sync"; exit 1; }
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
clang -O2 -Wall -DPROJECT_DIR="\"$PROJECT\"" -o "$APP/Contents/MacOS/voxjev" "$PROJECT/scripts/launcher.c"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>voxjev</string>
  <key>CFBundleDisplayName</key><string>voxjev</string>
  <key>CFBundleIdentifier</key><string>local.voxjev.app</string>
  <key>CFBundleExecutable</key><string>voxjev</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSMicrophoneUsageDescription</key><string>voxjev écoute votre voix uniquement pendant l'appui sur la touche, et transcrit en local.</string>
  <key>NSAppleEventsUsageDescription</key><string>voxjev pilote les apps que vous lui demandez (Spotify, Finder, Notes…).</string>
</dict></plist>
PLIST
codesign --force --deep --sign - "$APP" >/dev/null 2>&1
echo "App construite : $APP"

if [[ "${1:-}" == "--login" ]]; then
  remove_login_item || true
  osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP\", hidden:true, name:\"voxjev\"}" >/dev/null
  echo "voxjev s'ouvrira à chaque connexion."
fi
echo "Au premier lancement, autorisez voxjev dans Réglages › Confidentialité et sécurité :"
echo "  Micro, Accessibilité, Surveillance de l'entrée (puis relancez l'app)."
