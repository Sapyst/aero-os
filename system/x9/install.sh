#!/usr/bin/env bash
# X9 (admin activity log) + the polkit rules of the Settings app. Root part.
#   sudo bash system/x9/install.sh            -> shows what it would do (dry run)
#   sudo bash system/x9/install.sh --apply    -> installs and starts x9d
#   sudo bash system/x9/install.sh --uninstall --apply
#   ... --reimport  -> reads the whole journal again (fills gaps; entries you deleted in X9 come back)
# The archive (/var/log/x9) is kept on uninstall; delete it yourself if you want it gone.
# The app (no root) is installed by x9/install-app.sh.
set -euo pipefail
aqui="$(cd "$(dirname "$0")" && pwd)"
raiz="$(cd "$aqui/../.." && pwd)"
aplicar=0; remover=0; reimportar=0
for a in "$@"; do
  case "$a" in
    --apply) aplicar=1 ;;
    --uninstall) remover=1 ;;
    --reimport) reimportar=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
faz() { if [ $aplicar -eq 1 ]; then "$@"; else echo "would run: $*"; fi; }

if [ $remover -eq 1 ]; then
  faz systemctl disable --now x9d.service || true
  faz rm -f /etc/systemd/system/x9d.service /usr/share/polkit-1/actions/com.aero.x9.policy
  faz rm -rf /usr/local/lib/x9
  faz systemctl daemon-reload
  echo "Removed X9 (archive kept in /var/log/x9). Settings polkit rules kept."
  exit 0
fi

faz install -d -m755 -o root -g root /usr/local/lib/x9 /etc/x9 /var/lib/x9 /var/lib/x9/locks
faz install -d -m750 -o root -g wheel /var/log/x9
faz install -m644 -o root -g root "$aqui/x9core.py" /usr/local/lib/x9/x9core.py
for f in x9d x9-admin x9-setlock; do
  faz install -m755 -o root -g root "$aqui/$f" "/usr/local/lib/x9/$f"
done
if [ ! -e /etc/x9/retention.json ]; then
  if [ $aplicar -eq 1 ]; then
    printf '{"keep_days": 90, "rules": []}\n' > /etc/x9/retention.json; chmod 644 /etc/x9/retention.json
  else echo "would write: /etc/x9/retention.json (keep 90 days)"; fi
fi
faz install -m644 -o root -g root "$aqui/com.aero.x9.policy" /usr/share/polkit-1/actions/com.aero.x9.policy
faz install -m644 -o root -g root "$raiz/system/polkit/com.aero.settings.policy" \
  /usr/share/polkit-1/actions/com.aero.settings.policy
faz install -m644 -o root -g root "$aqui/x9d.service" /etc/systemd/system/x9d.service
faz systemctl daemon-reload
[ $reimportar -eq 1 ] && faz rm -f /var/lib/x9/history-imported
faz systemctl enable x9d.service
faz systemctl restart x9d.service   # picks up a new version when reinstalling

# A local polkit rule that says YES to admins would skip every password prompt: warn about it
if grep -lsE 'Result\.YES' /etc/polkit-1/rules.d/*.rules 2>/dev/null; then
  echo "WARNING: the rules above (in /etc/polkit-1/rules.d) can allow admin actions without a password." >&2
fi
[ $aplicar -eq 1 ] && echo "X9 installed. Service: systemctl status x9d" || echo "(dry run: add --apply)"
