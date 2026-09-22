#!/bin/bash
# Verr. Maj -> F18 : une touche dédiée au push-to-talk (idée reprise de kevinbadi/jev-voice, MIT).
#   ./scripts/capslock.sh install     remappe maintenant + à chaque ouverture de session (LaunchAgent)
#   ./scripts/capslock.sh uninstall   rend Verr. Maj normale
# Puis dans config/commands.yaml : settings.hotkey: f18
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/local.voxjev.capslock.plist"
MAP='{"UserKeyMapping":[{"HIDKeyboardModifierMappingSrc":0x700000039,"HIDKeyboardModifierMappingDst":0x70000006D}]}'

case "${1:-}" in
  install)
    /usr/bin/hidutil property --set "$MAP" >/dev/null
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.voxjev.capslock</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/hidutil</string><string>property</string><string>--set</string><string>$MAP</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PL
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Verr. Maj -> F18 activé (persistant). Mettez « hotkey: f18 » dans config/commands.yaml."
    ;;
  uninstall)
    /usr/bin/hidutil property --set '{"UserKeyMapping":[]}' >/dev/null
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Verr. Maj rétablie."
    ;;
  *) echo "usage : $0 install|uninstall"; exit 1 ;;
esac
