# Installing (by hand)

This is one person's desktop on one laptop (AMD iGPU + NVIDIA dGPU, 2560×1440 external screen).
Copy what you want, read the code first, and adapt. Nothing here asks for your password except the
login screen and `system/` files, which you install yourself with `sudo`.

## 1. User files

```bash
mkdir -p ~/.local/share ~/.local/bin ~/.config
cp -r shell    ~/.local/share/aero-shell
cp -r settings ~/.local/share/aero-settings
cp -r music    ~/.local/share/ytmotor
cp -r file-manager ~/.local/share/ez-fm
cp bin/*       ~/.local/bin/ && chmod +x ~/.local/bin/*
cp -r config/* ~/.config/          # back up your own ~/.config first!
```

`config/hypr/hyprland.conf` starts everything (`exec-once`): bar, dock, Super menu, Alt+Tab, widgets,
clipboard history, sound/quick center. Review it before logging in with it.

## 2. Pieces that need setup

- **Widgets** need `gtk4-layer-shell`. Pokémon data: run `pokefetch-catalogar` once (downloads from PokéAPI),
  then `pokefetch-golpes` for the battle moves.
- **Weather**: Settings → Location. *Automatic* asks ipinfo.io for your city by IP; pick a city to avoid that.
- **A second disk** in the About page and the Resources widget: `export AERO_DADOS=/your/mount`.
- **File picker** (xdg-desktop-portal):
  ```bash
  cd ~/.local/share/ez-fm && npm install --omit=dev dbus-next
  cp file-manager/myfm-portal.service ~/.config/systemd/user/
  sudo install -m644 file-manager/myfm.portal /usr/share/xdg-desktop-portal/portals/
  printf '[D-BUS Service]\nName=org.freedesktop.impl.portal.myfm\nExec=/usr/bin/false\nSystemdService=myfm-portal.service\n' \
    > ~/.local/share/dbus-1/services/org.freedesktop.impl.portal.myfm.service
  printf '[preferred]\ndefault=hyprland;gtk\norg.freedesktop.impl.portal.FileChooser=myfm;gtk\n' \
    > ~/.config/xdg-desktop-portal/hyprland-portals.conf
  systemctl --user daemon-reload && systemctl --user restart xdg-desktop-portal
  ```
  Firefox: `widget.use-xdg-desktop-portal.file-picker = 1`.
- **Music** (TerMusic / YTMusic) reads your YouTube login from Firefox's cookies at runtime (kept in memory only).
  Without a login it works anonymously.

## 3. Login screen (greetd)

```bash
sudo bash login/instalar.sh      # optional: put a fundo.jpg next to it for the background
```
Emergency: Ctrl+Alt+F2 gives a text login; `sudo systemctl disable greetd` undoes it.

## 4. Hardware snippets (`system/`)

Only for the same kind of laptop: GPU device symlinks (udev, check the PCI addresses with `lspci`),
NVIDIA modprobe options, and lid/power-key behaviour (logind). Read each file before copying it to `/etc`.

## Notes

- Comments mention `doc NN`: those are the author's personal notes, not included.
- The desktop keeps state in `~/.config/hypr/painel.conf` (key=value). It is **read** by
  `shell/painel.sh`, never executed.
