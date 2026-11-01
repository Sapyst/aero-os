#!/bin/sh
# Installs the storage rules. /tmp and the disk-backed temp link apply after a reboot.
set -eu
here=$(dirname "$(readlink -f "$0")")
sudo install -Dm644 "$here/tmp.mount.conf" /etc/systemd/system/tmp.mount.d/aero-storage.conf
sudo systemctl daemon-reload
install -Dm644 "$here/aero-temp.conf" "$HOME/.config/user-tmpfiles.d/aero-temp.conf"
install -Dm644 "$here/aero-temp.env" "$HOME/.config/environment.d/aero-temp.conf"
install -Dm644 "$here/background.slice.conf" "$HOME/.config/systemd/user/background.slice.d/aero.conf"
install -Dm755 "$here/aero-bg" "$HOME/.local/bin/aero-bg"
install -Dm755 "$here/aero-build-clean" "$HOME/.local/bin/aero-build-clean"
install -Dm644 "$here/aero-build-clean.service" "$HOME/.config/systemd/user/aero-build-clean.service"
install -Dm644 "$here/aero-build-clean.timer" "$HOME/.config/systemd/user/aero-build-clean.timer"
mkdir -p "$HOME/.cache/aero-temp/claude"
systemctl --user daemon-reload
systemctl --user enable --now aero-build-clean.timer
systemctl --user enable systemd-tmpfiles-setup.service systemd-tmpfiles-clean.timer
systemctl --user start systemd-tmpfiles-clean.timer
