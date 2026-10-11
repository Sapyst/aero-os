#!/usr/bin/env bash
# Instala a tela de login (doc 34). Rode com:  sudo bash ~/.local/share/aero-shell/login/instalar.sh
set -e
AQUI="$(cd "$(dirname "$0")" && pwd)"
pacman -S --needed --noconfirm greetd                      # o serviço de login (cria o usuário "greeter")
install -d -m755 /etc/greetd/aero-login
install -m644 "$AQUI/aero-login.py" "$AQUI/hyprland.conf" /etc/greetd/aero-login/
# fundo.jpg (opcional): ponha uma imagem sua ao lado deste script; sem ela, o fundo é a cor do tema
[ -f "$AQUI/fundo.jpg" ] && install -m644 "$AQUI/fundo.jpg" /etc/greetd/aero-login/
# pasta "visual": é SUA (sem senha, o Settings troca o fundo e o encaixe por ela); a tela de login só lê
DONO="${SUDO_USER:-$(logname)}"
install -d -m755 -o "$DONO" -g "$DONO" /etc/greetd/aero-login/visual
# o que vai dentro da pasta do usuário é gravado COMO o usuário (root não segue links que ele tenha posto lá)
if [ ! -e /etc/greetd/aero-login/visual/fundo ] && [ -f "$AQUI/fundo.jpg" ]; then
    sudo -u "$DONO" cp "$AQUI/fundo.jpg" /etc/greetd/aero-login/visual/fundo
fi
[ -e /etc/greetd/aero-login/visual/login.conf ] || sudo -u "$DONO" sh -c 'echo ENCAIXE=crop > /etc/greetd/aero-login/visual/login.conf'
[ -f /etc/greetd/config.toml.original ] || [ ! -f /etc/greetd/config.toml ] || cp /etc/greetd/config.toml /etc/greetd/config.toml.original
install -m644 "$AQUI/config.toml" /etc/greetd/config.toml
systemctl enable greetd.service                             # vale a partir do próximo boot
echo
echo "Pronto. No próximo boot aparece a tela de login."
echo "Emergência: Ctrl+Alt+F2 abre um login de texto; 'sudo systemctl disable greetd' desfaz."
