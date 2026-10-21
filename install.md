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

- **Widgets** need `gtk4-layer-shell`. The goddesses of the System widget (`shell/deusas/`, `shell/deusas.json`)
  come with the shell; nothing to download.
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
- **Bluetooth**: a pairing agent (answers devices that ask for a code or a confirmation), Bluetooth on at login
  and retries when connecting. Settings → Bluetooth uses it; needs `bluez` and `bluez-utils`:
  ```bash
  sudo systemctl enable --now bluetooth
  cp config/systemd/user/aero-bluetooth.service ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now aero-bluetooth.service
  ```
- **Fixed device volume** (Settings → Sound): and **Switch to new devices** (sound moves to a headset or speaker when it connects, like a phone). Fixed volume is for USB headsets whose own volume misbehaves (one side
  louder below 100%, or silent after reconnecting). Needs `alsa-utils`; turn on the guard once:
  ```bash
  cp config/systemd/user/aero-audio-guard.service ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now aero-audio-guard.service
  ```
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

Settings → Power changes the lid and power-key rules through a small root helper that only accepts
fixed values. Install it once:
```bash
sudo install -m755 system/bin/painel-energia /usr/local/bin/painel-energia
```
Aero's default: with the lid closed and no monitor, a laptop on battery sleeps, but **plugged in it
keeps working** (screen off), also when you turn the external monitor off.

## 5. Admin password and X9 (`system/x9/`)

Settings never uses `sudo`: every root change (power, memory, system upgrade) goes through
`pkexec`, and polkit asks the admin password **every time** (`auth_admin`, nothing remembered).
An open sudo ticket does not count there.

X9 is the admin activity log: a small root service (`x9d`) archives every sudo, pkexec and su call
(one file per day in `/var/log/x9`, readable by the `wheel` group), and the X9 app shows them with
filters, deletion and auto-delete rules. Deleting or changing the rules asks the admin password.
```bash
sudo bash system/x9/install.sh            # dry run: shows what it would do
sudo bash system/x9/install.sh --apply    # X9 service + polkit rules (X9 and Settings)
bash x9/install-app.sh                    # the app (no root)
```
The X9 password locks the app on your screen; it is not encryption. What protects the logs is that
only root can change them. Lines that only claim to come from sudo (`logger -t sudo`) are ignored.

## Notes

- Comments mention `doc NN`: those are the author's personal notes, not included.
- The desktop keeps state in `~/.config/hypr/painel.conf` (key=value). It is **read** by
  `shell/painel.sh`, never executed.
