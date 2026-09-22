#!/usr/bin/env bash
# Install Kai as a desktop app (Linux).
# Adds it to the application menu and optionally to autostart.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # repo root — this script lives in scripts/
DESKTOP_SRC="$SCRIPT_DIR/kai.desktop"
DESKTOP_DEST="$HOME/.local/share/applications/kai.desktop"
ICON="$ROOT_DIR/kai/static/icon-192.png"
PYTHON="$ROOT_DIR/.venv/bin/python"

# Resolve the real python if venv not set up
if [ ! -f "$PYTHON" ]; then
    PYTHON="$(which python3)"
fi

# Write a resolved .desktop file (absolute paths baked in)
mkdir -p "$HOME/.local/share/applications"
cat > "$DESKTOP_DEST" <<EOF
[Desktop Entry]
Type=Application
Name=Kai
GenericName=Local AI Agent
Comment=Kai — your personal local AI assistant
Exec=bash -c 'cd $ROOT_DIR && $PYTHON app.py'
Icon=$ICON
Terminal=false
Categories=Utility;Office;
Keywords=AI;assistant;chat;
StartupNotify=true
EOF

# Refresh the menu database
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

echo "Kai added to your application menu."
echo "You can now launch it from your desktop environment's app launcher,"
echo "or run:  $PYTHON $ROOT_DIR/app.py"

# Offer autostart
read -p "Start Kai automatically on login? [y/N] " yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
    mkdir -p "$HOME/.config/autostart"
    cp "$DESKTOP_DEST" "$HOME/.config/autostart/kai.desktop"
    echo "Autostart enabled."
fi
