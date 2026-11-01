# AeroOS and Arch Linux Command Catalog

## Brightness
- `brilho +5` / `brilho -5`: raise or lower screen brightness by 5% with OSD; bound to `XF86MonBrightnessUp` and `XF86MonBrightnessDown` (`bin/brilho`, `~/.local/bin/brilho`, `shell/servicos/brilho.py`, `config/hypr/hyprland.conf`).
- `brilho N`: set brightness to an exact percentage `1..100`, e.g. `brilho 40` (`~/.local/bin/brilho`, `shell/servicos/brilho.py`).
- `brilho --ler`: print current screen brightness percentage `0..100` (`~/.local/bin/brilho`, `shell/servicos/brilho.py`).
- Laptop backlight uses `/sys/class/backlight/amdgpu_bl1` via `systemd-logind` (`SetBrightness`); external monitors use `ddcutil`; state is cached in `~/.cache/aero-shell/brilho.json` (`bin/brilho`, `shell/servicos/brilho.py`).

## Volume and Audio
- Raise / lower / mute default output with OSD (`config/hypr/hyprland.conf`, `bin/som`):
  - `wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 5%+ && ~/.local/bin/som osd volume` (`XF86AudioRaiseVolume`)
  - `wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%- && ~/.local/bin/som osd volume` (`XF86AudioLowerVolume`)
  - `wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle && ~/.local/bin/som osd volume` (`XF86AudioMute`)
  - Exact volume: `wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 50% && ~/.local/bin/som osd volume` or `pactl set-sink-volume @DEFAULT_SINK@ 50%` (`config/hypr/hyprland.conf`, `settings/sec_som.py`, `src/main.rs`).
- Inspect audio: `wpctl get-volume @DEFAULT_AUDIO_SINK@`, `wpctl get-volume @DEFAULT_AUDIO_SOURCE@`, `wpctl status`, `pactl list sinks`, `pactl list sources`, `pactl get-default-sink`, `pactl get-default-source` (`src/main.rs`, `settings/sec_som.py`).
- Switch default sink or source: `pactl set-default-sink NAME`, `pactl set-default-source NAME` (`settings/sec_som.py`).
- Sound popup, center and OSD (`bin/som`): `som popup simples`, `som popup completo`, `som osd volume`, `som osd mic`, `som osd brilho`, `som central abrir`, `som --daemon`.
- Fixed volume and auto-switch (`bin/aero-audio`, `settings/sec_som.py`, `install.md`): `aero-audio list`, `aero-audio set CARD on|off`, `aero-audio apply`, `aero-audio switch on|off`.
- Restart audio stack: `systemctl --user restart pipewire pipewire-pulse wireplumber` (`triggers.conf`).
- TerMusic / YTMusic app volume: `termusic-vol +5`, `termusic-vol -5` on `Super+=` / `Super+-` (`config/hypr/hyprland.conf`, `bin/termusic-vol`).

## Screenshots
- `print-area`: freeze screen, select area with `slurp`, save PNG to `~/Pictures/Prints/screenshot-YYYY-MM-DD_HH-MM-SS.png`, optimize with `pngquant`/`oxipng`, and copy to clipboard with `wl-copy`; bound to `Print` and `Super+Shift+S` (`bin/print-area`, `config/hypr/hyprland.conf`).
- `print-area tela`: full-screen screenshot; bound to `Shift+Print` (`bin/print-area`, `config/hypr/hyprland.conf`).
- `print-area janela`: active-window screenshot; bound to `Alt+Print` (`bin/print-area`, `config/hypr/hyprland.conf`).
- `print-area "X,Y WxH"`: capture explicit geometry (`bin/print-area`).

## Screen Recording
- `screen-record`: select region with `slurp` and record with `wf-recorder` (or stop active recording if already running); bound to `Ctrl+Print` (`bin/screen-record`, `config/hypr/hyprland.conf`).
- `screen-record pick`: open screen/area recording picker; bound to `Ctrl+Shift+Print` (`bin/screen-record`, `config/hypr/hyprland.conf`).
- `screen-record screen`: record focused monitor directly (`bin/screen-record`).
- `screen-record stop`: stop active recording and save to `~/Videos/Recordings` (or `REC_DIR` in `~/.config/hypr/painel.conf`) (`bin/screen-record`, `settings/sec_recording.py`).

## Wallpaper
- `papel-de-parede FILE`: copy image to `~/Pictures/Wallpapers` and set wallpaper via `hyprpaper` (`bin/papel-de-parede`).
- `papel-de-parede --clipboard`: set clipboard image/file/URL as wallpaper; bound to `Super+Shift+W` (`bin/papel-de-parede`, `config/hypr/hyprland.conf`).
- `papel-de-parede --url URL [PAGE]`: download image URL and set as wallpaper (`bin/papel-de-parede`).
- `papel-de-parede --cores`: recalculate theme colors from current wallpaper (`bin/papel-de-parede`).
- Target flags: `papel-de-parede --area N ...` (workspace `N` only) or `papel-de-parede --all ...` (all workspaces) (`bin/papel-de-parede`, `settings/sec_aparencia.py`).
- `ajustar-wallpaper`: apply `WALL_BRILHO`, `WALL_CONTRASTE`, `WALL_SATURACAO` from `~/.config/hypr/painel.conf` (`bin/ajustar-wallpaper`).
- `cores-do-wallpaper --ligar` / `cores-do-wallpaper --desligar` / `cores-do-wallpaper --aplicar FILE`: toggle or apply automatic wallpaper palette (`bin/cores-do-wallpaper`).

## Theme
- `hypr-tema`: apply changed theme settings from `~/.config/hypr/painel.conf`; `hypr-tema --forcar` forces full re-apply; `hypr-tema --loop` runs schedule daemon (`bin/hypr-tema`, `config/hypr/hyprland.conf`).
- Theme keys in `~/.config/hypr/painel.conf`: `TEMA_MODO=auto|claro|escuro`, `FILTRO_MODO=auto|ligado|desligado`, `FILTRO_TEMP=4500`, `COR_DESTAQUE`, `VIDRO=desligado|leve|media|forte`, `RAIO_CANTOS` (`config/hypr/painel.conf`, `settings/sec_aparencia.py`).
- `aero-visual`: print active visual profile; `aero-visual Solid`, `aero-visual AeroTerm`, `aero-visual AerOS`, `aero-visual DotStyle` switch desktop visual profile (`~/.local/bin/aero-visual`, `shell/visuais/visual.py`, `settings/sec_theme.py`).
- `aero-tema-onda [--cascata] COMMAND [ARGS...]`: run a theme change with wave transition (`bin/aero-tema-onda`).

## Clipboard
- `clipboard`: toggle clipboard history window (`~/.local/share/aero-shell/clipboard.py`); bound to `Ctrl+Shift+V` (`bin/clipboard`, `config/hypr/hyprland.conf`).
- `clipboard --daemon`: run supervised clipboard daemon (`aero-clipd` under `aero-supervise`) (`bin/clipboard`, `config/hypr/hyprland.conf`).

## Bluetooth
- `aero-bluetooth pair MAC` / `aero-bluetooth connect MAC`: power on adapter, trust, pair, and connect with retries (`bin/aero-bluetooth`, `settings/sec_bluetooth.py`).
- `aero-bluetooth startup on|off`: set `startup=on|off` in `~/.config/aero/bluetooth.conf` (`bin/aero-bluetooth`, `install.md`).
- `bluetoothctl`: `bluetoothctl show`, `bluetoothctl devices`, `bluetoothctl info MAC`, `bluetoothctl power on`, `bluetoothctl power off`, `bluetoothctl disconnect MAC`, `bluetoothctl remove MAC`, `bluetoothctl --timeout 10 scan on` (`settings/sec_bluetooth.py`, `src/guard.rs`).
- `sudo systemctl restart bluetooth`: restart Bluetooth service (`triggers.conf`, `install.md`).

## Wi-Fi and Network
- `nmcli`: `nmcli radio wifi on`, `nmcli radio wifi off`, `nmcli device wifi list`, `nmcli device wifi connect SSID password PASS`, `nmcli connection up id NAME`, `nmcli connection down id NAME`, `nmcli device status`, `nmcli -t -f NAME,TYPE,DEVICE connection show --active` (`settings/sec_rede.py`, `src/main.rs`, `src/guard.rs`).
- Arch network tools: `ip -br addr`, `ip addr`, `ip route show default`, `ss -tulnp`, `resolvectl status`, `ping -c 3 HOST`, `sudo systemctl restart NetworkManager` (`src/main.rs`, `src/guard.rs`, `triggers.conf`).

## Power Profile and Battery
- `aero-energia --status` / `aero-energia --waybar`: read battery charge, charger, charge limiter (`smart`, `fixo`, `bypass`, `desligado`), health, cycles, and power profile (`bin/aero-energia`, `shell/bateria.py`).
- `aero-energia --alternar`: toggle 80% smart charge limiter; `aero-energia --cheia`: charge to 100% once (`bin/aero-energia`, `shell/bateria.py`).
- `powerprofilesctl get`, `powerprofilesctl list`, `powerprofilesctl set power-saver`, `powerprofilesctl set balanced`, `powerprofilesctl set performance` (`settings/sec_energia.py`, `src/guard.rs`).
- `energia logout`: session logout/reboot/shutdown dialog (`Super+Shift+E`); `travar`: lock screen with `hyprlock` (`Super+L`) (`bin/energia`, `bin/travar`, `config/hypr/hyprland.conf`).

## Dock, Bar, Display and Notifications
- Dock ("Atlas"): `dock` starts supervisor; `setsid -f dock --reiniciar >/dev/null 2>&1; sleep 3; pgrep -f aero-shell/dock.py >/dev/null` restarts it (`bin/dock`, `triggers.conf`, `settings/sec_dock.py`).
- Bar: `aero-barra` starts `aerobar` (`DotStyle`/`Solid`) or `waybar`; restart `aerobar` via `kill $(cat $XDG_RUNTIME_DIR/aerobar.pid); for i in 1 2 3 4 5 6 7 8 9 10; do flock -n $XDG_RUNTIME_DIR/aerobar.trava true && break; sleep 0.5; done; setsid -f aerobar >/dev/null 2>&1; sleep 2; pgrep -f aero-shell/aerobar.py >/dev/null` (`bin/aero-barra`, `bin/aerobar`, `triggers.conf`).
- Display and refresh rate: `hyprctl monitors` lists monitors and modes; `aero-hz --status` reads adaptive refresh rate state; monitor modes live in `~/.config/hypr/monitores.conf` (`bin/aero-hz`, `settings/sec_tela.py`, `src/main.rs`).
- Notifications ("Hermes"): `hermesctl mode`, `hermesctl mode -a dnd`, `hermesctl mode -r dnd`, `hermesctl history`, `hermesctl dismiss`, `hermesctl reload`, `notificacoes-estado` (`bin/hermesctl`, `bin/notificacoes-estado`).
- Launchers and supervisor: `configuracoes` (`Super+I`), `supermenu` (`Super`), `ez-fm` (`Super+E`), `aero-supervise --reload [clipboard|tray|hermes|session]` (`config/hypr/hyprland.conf`, `bin/aero-supervise`).

## Packages, Services, Logs, Disk and Processes
- Packages (`src/main.rs`, `src/guard.rs`, `triggers.conf`): `sudo pacman -Syu` (update repo packages; never `pacman -Sy` alone, never `--noconfirm`), `checkupdates` (pending updates), `sudo pacman -S NAME` (install repo package), `aero-assistant install NAME` (install AUR package), `sudo pacman -Rns NAME` (remove package), `sudo pacman -Rns $(pacman -Qdtq)` (remove orphans), `paccache -d` / `sudo paccache -rkN` (clean cache), `pacman -Qq`, `pacman -Qqe`, `pacman -Qqm`, `pacman -Qqdt`, `pacman -Qi NAME`, `pacman -Si NAME`, `pacman -Ss QUERY`.
- Services and logs (`src/main.rs`, `src/guard.rs`): `systemctl status UNIT`, `systemctl --user status UNIT`, `systemctl --failed --no-legend --plain`, `systemctl --user --failed --no-legend --plain`, `systemctl --user list-timers`, `sudo systemctl restart|start|stop|enable|disable UNIT`, `systemctl --user restart|start|stop UNIT`, `journalctl -b -p 3 --no-pager -o cat --lines=400`, `journalctl -u UNIT --no-pager --lines=200`, `coredumpctl list`, `dmesg`.
- Disk, Btrfs, memory and swap (`system.md`, `src/main.rs`, `src/guard.rs`): `df -h -x tmpfs -x devtmpfs -x efivarfs -x squashfs`, `du -x -sh PATH`, `lsblk`, `findmnt`, `btrfs filesystem df /`, `btrfs filesystem usage /`, `btrfs subvolume list /`, `snapper list`, `free -h`, `swapon --show`, `zramctl`.
- Processes (`src/main.rs`, `src/guard.rs`, `system.md`): `ps -eo rss,comm --sort=-rss --no-headers`, `ps -eo pid,pcpu,pmem,comm --sort=-pcpu --no-headers`, `ps aux`, `pgrep -a PATTERN`, `aero-bg COMMAND`, `aero-build-clean`.
