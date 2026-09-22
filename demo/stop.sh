#!/bin/bash
# Ferme uniquement les fenêtres de démo (instances Ghostty lancées avec --title=voxjev-demo …).
pkill -f "ghostty .*--title=voxjev-demo" 2>/dev/null
exit 0
