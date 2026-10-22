#!/usr/bin/env bash
# Automatic Wi-Fi country (aero-wifi-country) + its NetworkManager trigger. Root part.
#   bash system/geo/install.sh                -> dry run
#   sudo bash system/geo/install.sh --apply
#   sudo bash system/geo/install.sh --uninstall --apply
# Needs iw (reads the routers' country from the scan the card already has, and sets the country).
set -euo pipefail
aqui="$(cd "$(dirname "$0")" && pwd)"
aplicar=0; remover=0
for a in "$@"; do
  case "$a" in --apply) aplicar=1 ;; --uninstall) remover=1 ;; *) echo "unknown option: $a" >&2; exit 2 ;; esac
done
if [ $aplicar -eq 1 ] && [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo) with --apply" >&2; exit 1; fi
faz() { if [ $aplicar -eq 1 ]; then "$@"; else echo "would run: $*"; fi; }
gancho=/etc/NetworkManager/dispatcher.d/90-aero-wifi-country

if [ $remover -eq 1 ]; then
  faz rm -f "$gancho" /etc/modprobe.d/aero-wifi-country.conf
  faz rm -rf /usr/local/lib/aero-geo /var/lib/aero-geo
  echo "Removed (config /etc/aero/wifi-country.conf kept)."; exit 0
fi
pacman -Q iw >/dev/null 2>&1 || faz pacman -S --needed iw
faz install -d -m755 -o root -g root /usr/local/lib/aero-geo /var/lib/aero-geo /etc/aero
faz install -m755 -o root -g root "$aqui/aero-wifi-country" /usr/local/lib/aero-geo/aero-wifi-country
# the dispatcher only runs root-owned scripts that nobody else can write
faz install -m755 -o root -g root "$aqui/90-aero-wifi-country" "$gancho"
# the old fixed country line (US) is replaced by the automatic one
if [ -f /etc/modprobe.d/aero-wifi.conf ] && grep -q '^options cfg80211' /etc/modprobe.d/aero-wifi.conf; then
  faz sed -i '/^# Country for the radio rules/,/^options cfg80211/d' /etc/modprobe.d/aero-wifi.conf
fi
faz install -m644 -o root -g root "$aqui/../polkit/com.aero.settings.policy" /usr/share/polkit-1/actions/com.aero.settings.policy
faz /usr/local/lib/aero-geo/aero-wifi-country run
[ $aplicar -eq 1 ] && /usr/local/lib/aero-geo/aero-wifi-country status || echo "(dry run: add --apply)"
