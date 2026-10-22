#!/usr/bin/env bash
# Aero snapshots: a Btrfs "photo" of the system before every pacman change and before Aero
# installers, so a bad change can be undone. Uses snapper + snap-pac. No daemon stays running:
# snap-pac is a pacman hook, snapperd starts on demand and exits, cleanup is a daily timer.
#   bash system/snapshots/install.sh                 -> dry run (no root needed)
#   sudo bash system/snapshots/install.sh --apply    -> installs and takes the first snapshot
#   sudo bash system/snapshots/install.sh --uninstall --apply   -> removes config AND snapshots
# What it covers: / (subvolume @). NOT covered on purpose: /home, /var/log (X9 logs survive a
# rollback), the pacman cache and /swap. /boot is FAT (no snapshots): a rollback after a kernel
# update needs the matching kernel reinstalled.
set -euo pipefail
aqui="$(cd "$(dirname "$0")" && pwd)"
aplicar=0; remover=0
for a in "$@"; do
  case "$a" in
    --apply) aplicar=1 ;;
    --uninstall) remover=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
if [ $aplicar -eq 1 ] && [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo) with --apply" >&2; exit 1; fi
faz() { if [ $aplicar -eq 1 ]; then "$@"; else echo "would run: $*"; fi; }

[ "$(findmnt -no FSTYPE /)" = btrfs ] || { echo "/ is not btrfs: snapshots not possible" >&2; exit 3; }

if [ $remover -eq 1 ]; then
  faz systemctl disable --now snapper-cleanup.timer || true
  faz snapper -c root delete-config
  faz rm -f /usr/local/bin/aero-snap
  echo "Snapshots removed. Packages kept (sudo pacman -Rns snap-pac snapper to remove them)."
  exit 0
fi

# 1. packages (pacman shows what it downloads and asks before installing)
if ! pacman -Q snapper snap-pac >/dev/null 2>&1; then
  faz pacman -S --needed snapper snap-pac
fi

# 2. config for / (creates the /.snapshots subvolume inside @)
if [ -e /etc/snapper/configs/root ]; then
  echo "snapper config 'root' already exists: kept as is"
else
  if [ -e /.snapshots ]; then echo "/.snapshots exists but there is no snapper config: check it by hand" >&2; exit 4; fi
  faz snapper -c root create-config /
  # Small and bounded: last 10 pacman pre/post pairs, 5 "important", never more than 20% of the disk,
  # always leave 20% free. No hourly timeline (saves disk and wakeups).
  faz snapper -c root set-config TIMELINE_CREATE=no NUMBER_CLEANUP=yes NUMBER_LIMIT=10 \
      NUMBER_LIMIT_IMPORTANT=5 SPACE_LIMIT=0.2 FREE_LIMIT=0.2 ALLOW_USERS= ALLOW_GROUPS= SYNC_ACL=no
fi
faz systemctl disable --now snapper-timeline.timer 2>/dev/null || true
faz systemctl enable --now snapper-cleanup.timer

# 3. aero-snap: wrap any root installer in a before/after pair
faz install -m755 -o root -g root "$aqui/aero-snap" /usr/local/bin/aero-snap

# 4. first snapshot
faz snapper -c root create -c number --userdata important=yes -d "Aero: first snapshot (snapshots installed)"
[ $aplicar -eq 1 ] && snapper -c root list | tail -3 || echo "(dry run: add --apply)"
