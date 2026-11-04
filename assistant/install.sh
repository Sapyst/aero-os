#!/bin/sh
# Builds and installs Aero Assistant for this user.
set -eu
here=$(dirname "$(readlink -f "$0")")
cd "$here"
cargo build --release
install -Dm755 target/release/aero-assistant "$HOME/.local/bin/aero-assistant"
ln -sf aero-assistant "$HOME/.local/bin/aero"
ln -sf aero-assistant "$HOME/.local/bin/-aero"
install -Dm644 aero-assistant-server.service "$HOME/.config/systemd/user/aero-assistant-server.service"
mkdir -p "$HOME/.config/aero-assistant/knowledge"
install -m644 triggers.conf "$HOME/.config/aero-assistant/triggers.conf"
install -m644 knowledge/*.md "$HOME/.config/aero-assistant/knowledge/"
[ -f "$HOME/.config/aero-assistant/system.md" ] || install -m644 system.md "$HOME/.config/aero-assistant/system.md"
mkdir -p "$HOME/.config/systemd/user/aero-assistant-server.service.d"
cat > "$HOME/.config/systemd/user/aero-assistant-server.service.d/idle.conf" <<EOF
[Service]
ExecStartPost=-/usr/bin/systemctl --user stop aero-assistant-idle.timer aero-assistant-idle.service
ExecStartPost=-/usr/bin/systemctl --user reset-failed aero-assistant-idle.timer aero-assistant-idle.service
ExecStartPost=-/usr/bin/systemd-run --user --quiet --collect --unit=aero-assistant-idle --on-active=5m $HOME/.local/bin/aero-assistant idle-stop
ExecStopPost=-/usr/bin/systemctl --user stop aero-assistant-idle.timer
EOF
systemctl --user daemon-reload
install -Dm644 models.json "$HOME/.local/share/aero-assistant/models.json"
install -Dm755 gui/app.py "$HOME/.local/share/aero-assistant/gui/app.py"
install -Dm755 gui/sphere.py "$HOME/.local/share/aero-assistant/gui/sphere.py"
install -Dm755 bin/aero-voice "$HOME/.local/bin/aero-voice"
install -Dm644 com.aero.Assistant.desktop "$HOME/.local/share/applications/com.aero.Assistant.desktop"
install -Dm644 com.aero.Assistant.svg "$HOME/.local/share/icons/hicolor/scalable/apps/com.aero.Assistant.svg"
install -Dm644 com.aero.Voice.desktop "$HOME/.local/share/applications/com.aero.Voice.desktop"
install -Dm644 com.aero.Voice.svg "$HOME/.local/share/icons/hicolor/scalable/apps/com.aero.Voice.svg"
ln -sf "$HOME/.local/share/aero-assistant/gui/app.py" "$HOME/.local/bin/aero-assistant-app"
"$HOME/.local/bin/aero-assistant" index
