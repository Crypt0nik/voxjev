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
# On construit dans un dossier temporaire et on ne remplace l'app QUE si elle change : une nouvelle
# signature (ad hoc) fait oublier à macOS les autorisations déjà données (Micro, Accessibilité…).
# Le code Python n'est pas dans l'app (elle lance le projet) : une mise à jour du code ne la touche pas.
BUILD="$(mktemp -d)/voxjev.app"
trap 'rm -rf "$(dirname "$BUILD")"' EXIT
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
clang -O2 -Wall -fobjc-arc -framework Cocoa -DPROJECT_DIR="\"$PROJECT\"" -o "$BUILD/Contents/MacOS/voxjev" \
  "$PROJECT/scripts/launcher.m"
cat > "$BUILD/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>voxjev</string>
  <key>CFBundleDisplayName</key><string>voxjev</string>
  <key>CFBundleIdentifier</key><string>local.voxjev.app</string>
  <key>CFBundleExecutable</key><string>voxjev</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>0.2</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSMicrophoneUsageDescription</key><string>voxjev écoute votre voix pendant l'appui sur la touche (ou en continu en mode mains libres) et transcrit en local.</string>
  <key>NSAppleEventsUsageDescription</key><string>voxjev pilote les apps que vous lui demandez (Spotify, Finder, Notes…).</string>
  <key>NSCalendarsFullAccessUsageDescription</key><string>voxjev lit votre agenda quand vous demandez « qu'est-ce que j'ai demain ? » (lecture locale).</string>
  <key>NSCalendarsUsageDescription</key><string>voxjev lit votre agenda quand vous le demandez (lecture locale).</string>
  <key>NSRemindersFullAccessUsageDescription</key><string>voxjev crée les rappels que vous dictez.</string>
  <key>NSContactsUsageDescription</key><string>voxjev retrouve l'adresse d'un contact pour préparer un brouillon d'e-mail.</string>
</dict></plist>
PLIST
cp "$PROJECT/assets/AppIcon.icns" "$BUILD/Contents/Resources/AppIcon.icns"

# Signature avec un certificat local STABLE (créé une fois dans le trousseau de session) : macOS
# identifie l'app par « identifiant + certificat », donc les autorisations survivent aux mises à jour.
# (Une signature ad hoc change à chaque construction et fait tout oublier.)
IDENTITY="voxjev local signing"
if ! security find-certificate -c "$IDENTITY" >/dev/null 2>&1; then
  TMPC="$(mktemp -d)"; PASS="$(openssl rand -hex 12)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout "$TMPC/k" -out "$TMPC/c" -subj "/CN=$IDENTITY" \
    -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning" \
    -addext "basicConstraints=critical,CA:false" 2>/dev/null
  openssl pkcs12 -export -legacy -inkey "$TMPC/k" -in "$TMPC/c" -name "$IDENTITY" -out "$TMPC/p" -passout "pass:$PASS"
  security import "$TMPC/p" -k "$HOME/Library/Keychains/login.keychain-db" -P "$PASS" -T /usr/bin/codesign >/dev/null
  rm -rf "$TMPC"
  echo "Certificat local « $IDENTITY » créé dans le trousseau de session."
fi
SIGN="$IDENTITY"
security find-certificate -c "$IDENTITY" >/dev/null 2>&1 || SIGN="-"

STAMP="$(cat "$BUILD/Contents/MacOS/voxjev" "$BUILD/Contents/Info.plist" "$BUILD/Contents/Resources/AppIcon.icns" \
         <(echo "$SIGN") | shasum -a 256 | cut -d' ' -f1)"
if [[ -f "$APP/Contents/Resources/build.sha256" && "$(cat "$APP/Contents/Resources/build.sha256")" == "$STAMP" ]]; then
  echo "App déjà à jour : $APP (autorisations conservées)"
else
  echo "$STAMP" > "$BUILD/Contents/Resources/build.sha256"
  rm -rf "$APP" && mkdir -p "$(dirname "$APP")" && cp -R "$BUILD" "$APP"
  codesign --force --deep --sign "$SIGN" "$APP" >/dev/null 2>&1 || codesign --force --deep --sign - "$APP" >/dev/null 2>&1
  touch "$APP"  # le Finder et le Dock rafraîchissent l'icône
  echo "App construite : $APP"
  echo "Signée avec : $SIGN"
  echo "⚠️  Si l'identité de signature a changé, réactivez voxjev une fois dans Réglages › Confidentialité"
  echo "   (Accessibilité, Surveillance de l'entrée ; retirez l'ancienne entrée avec « − » si besoin)."
fi

if [[ "${1:-}" == "--login" ]]; then
  remove_login_item || true
  osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP\", hidden:true, name:\"voxjev\"}" >/dev/null
  echo "voxjev s'ouvrira à chaque connexion."
fi
echo "Au premier lancement, autorisez voxjev dans Réglages › Confidentialité et sécurité :"
echo "  Micro, Accessibilité, Surveillance de l'entrée (puis relancez l'app)."
