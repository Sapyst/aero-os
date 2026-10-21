#!/usr/bin/env bash
# The X9 app for the current user (no root): ~/.local/share/x9, ~/.local/bin/x9 and the menu entry.
set -euo pipefail
aqui="$(cd "$(dirname "$0")" && pwd)"
install -d "$HOME/.local/share/x9" "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m644 "$aqui/x9.py" "$HOME/.local/share/x9/x9.py"
install -m755 "$aqui/../bin/x9" "$HOME/.local/bin/x9"
sed "s|@HOME@|$HOME|g" "$aqui/x9.desktop" > "$HOME/.local/share/applications/x9.desktop"
echo "X9 app installed (menu: X9)."
