<div align="center">

```
   ▄▀█ █▀█ █▀▀ █ █   ▄▀█ █▀▀ █▀█ █▀█   █▀█ █▀
   █▀█ █▀▄ █▄▄ █▀█   █▀█ ██▄ █▀▄ █▄█   █▄█ ▄█
```

**A hand-shaped Arch Linux + Hyprland desktop.** Glass, terminal aesthetics, tiling by default.

<sub>built by someone who is not much of a coder, with prompt engineering and programming logic</sub>

</div>

![Super menu](screenshots/supermenu-focus.png)

## What is in here

| Folder | What it is | Installs to |
|---|---|---|
| `shell/` | the desktop shell (Python + GTK4 + layer-shell): dock, Super menu, Alt+Tab, tray, sound/quick center, clipboard history, emoji picker, power menu and the **widgets** | `~/.local/share/aero-shell` |
| `settings/` | the **Settings** app (libadwaita): theme, icons, dock, widgets, workspaces and transitions, display, sound, keyboard, mouse, screen recording, network, bluetooth, power, location, updates | `~/.local/share/aero-settings` |
| `bin/` | small commands the desktop calls (maximize/minimize to the dock, theme switcher, wallpaper, screenshots, screen recording...) | `~/.local/bin` |
| `config/` | Hyprland, Waybar, kitty, mako, wofi, GTK overrides | `~/.config` |
| `file-manager/` | a modified fork of [EZ File Manager](https://github.com/TechyTechster/ez-fm) (MIT), also used as the system **file picker** through xdg-desktop-portal | `~/.local/share/ez-fm` |
| `music/` | TerMusic / YTMusic: a YouTube Music engine (mpv + ytmusicapi) with a terminal UI and a GTK app | `~/.local/share/ytmotor` |
| `login/` | the greetd login screen (GTK, runs in its own Hyprland) | `/etc/greetd` |
| `boot/` | the start-up animation: the Venus de Milo in ASCII (boot splash, login, first second of the desktop) | `/usr/share/aero` |
| `x9/` | **X9**, the admin activity log app: every sudo, pkexec and su call, who asked and what for | `~/.local/share/x9` |
| `system/` | root parts: the X9 service, the memory guard, Btrfs snapshots, polkit rules, and hardware snippets used on the original laptop (hybrid AMD + NVIDIA) | `/etc/...`, `/usr/local` |

Code comments are in Portuguese; everything on screen is in English.

## Tour

**Workspaces with a personality.** Each of the 5 workspaces can have its own wallpaper and its own widgets
(**Settings → Workspaces**), and switching plays a transition: glitch, ASCII, pixelate or flash, with speed,
strength and tint (`shell/transitions/<effect>/`, run by `bin/hypr-transitions`; Hyprland screen shaders with no
`time` uniform, so damage tracking stays on).

**Widgets.** Clock, System (a live fetch with the **Greek goddess of the day**: an ASCII statue, her name and what
she rules), Resources (rings, gauges, network curves), Processes (a live top) and Weather (7 days). They sit inside
Hyprland's own gaps, share the windows' glass and cost almost no CPU when idle. Each has a *Private* switch for screenshots.
More widgets can be dropped in: any `widget_*.py` next to the others is loaded, and a `widget_*.json` beside it adds
it to **Settings → Widgets**.

**Super menu**: pinned, recent, categories, search, and the weather on top of the wallpaper.

**Settings → Theme**: the whole look at once. Two visuals, **AeroTerm** (terminal, OLED black, ASCII and glitch)
and **AerOS** (light pastel glass, soft corners); each remembers its own settings, so switching back restores everything.

![Settings: theme](screenshots/settings-theme.png)

**ASCII icons.** One switch turns every app icon (menu, dock, Alt+Tab) into a monochrome ASCII tile with a
phosphor glow. It runs once, remembers what it did, converts new apps at login and restores the originals when
turned off. **Settings → Icons** also picks the icon pack underneath, installs packs (folders or archives of any
size) and gives any single app its own image (`shell/icones.py`, command `aero-icones`).

![Settings: icons](screenshots/settings-icons.png)

**Settings → Widgets**: add, remove, arrange, scale, pick the glass surface.

**Screen recording**: Ctrl+Shift+Print pops a card: **1** for the whole screen or **2** to drag an area; the same
shortcut stops it. Ctrl+Print goes straight to the area. NVENC when available, computer sound and/or microphone,
24/30/60 fps; **Settings → Screen recording** has quality, format, countdown and the folder.

**Files** with big thumbnails (Ctrl + scroll), a resizable preview pane, and the same window as the system file picker.

![File manager](screenshots/file-manager.png)

**Also:** Alt+Tab like Windows with live thumbnails, a dock with intellihide, a wave effect and minimize-to-icon
animations, one theme color for the whole system with light/dark by time.

### Safety: X9, polkit and snapshots

**X9 is the admin activity log.** (In Brazilian slang an *X9* is a snitch: it tells on whoever used root.)
A small root service follows the system journal and archives **every sudo, pkexec and su call**, one file
per day. For each call it shows:

- **when**, and **which app asked** (a terminal, Settings, Claude, Steam...), read from the app's systemd scope;
- the **command**, the folder, and whether it came from a terminal or from an app with no terminal;
- a **category** (Packages, Services, Files & config, Disks, Network, Users & security, Kernel & boot, Power,
  Root shell, Scripts & code, Inspect, Aero system...) and **what it was for** in plain words, like
  *Install packages: htop* or *Restart service: bluetooth.service*. It is a best guess from the command line;
  X9 can't see inside a script;
- a **risk** level and the **result**: allowed, denied, wrong password or cancelled.

When an app tries to reuse a cached sudo password **without asking**, the entry turns red: a normal app never
needs that. Filters combine freely: days, time of day (22:00 to 06:00 crosses midnight), category, result, risk,
app and text. Entries can be deleted one by one, several at a time or all, and auto-delete rules keep the archive
small ("keep 90 days", "delete read-only entries after 24 hours").

How it is protected:

- The app is **not root**: it only reads the archive (`/var/log/x9`, group `wheel`).
- Deleting entries or changing auto-delete goes through a root helper, and polkit asks the **admin password
  every time**. A deletion leaves a note that entries were deleted.
- Lines that only *claim* to come from sudo (`logger -t sudo ...`) are ignored: X9 trusts the real program
  that wrote them, as recorded by journald.
- The X9 password locks the app on your screen (*Forgot password?* asks the admin password to set a new one).
  It is a screen lock, not encryption.
- The service is sandboxed (no network, read-only system, writes only its own folders) and stays around 20 MB.

**Settings never uses sudo.** Every root change (power, memory, system upgrade) goes through `pkexec`, and polkit
asks the admin password **every time**, so an open sudo ticket in some terminal does not count there.

**Snapshots.** snapper + snap-pac take a Btrfs snapshot of `/` before and after every pacman change, and
`aero-snap "what" <command>` wraps any installer in a before/after pair (`sudo aero-snap undo PRE POST` puts the
files back). Bounded (10 pairs, at most 20% of the disk) and with no resident daemon. `/home` and `/var/log`
are left out on purpose, so your files and X9's log survive a rollback.

<sub>Screenshots: wallpapers from [ascii-walls](https://github.com/Sapyst/ascii-walls). Process names,
places and personal items are blurred.</sub>

## Principles

- **Tiling first.** Windows tile; only Super+V (or a dialog) floats one. Maximize is Hyprland's own `fullscreen 1`.
- **Glass, not plastic.** Theme color at ~62% + Hyprland blur, 1px light outline, straight inner lines.
- **Terminal soul.** Mono type, TUI frames with the title in the border, pastel accents, no emoji in the UI.
- **Cheap when idle.** Pre-rendered glass, GLib timers instead of per-frame CSS, caches everywhere.

## Requirements (what the original machine runs)

Arch Linux, Hyprland 0.56, hyprpaper, hypridle/hyprlock, waybar, mako, wofi, kitty, greetd,
python-gobject, gtk4, libadwaita, gtk4-layer-shell, python-cairo, grim/slurp, wl-clipboard,
pipewire/wireplumber, brightnessctl, networkmanager, bluez, electron + nodejs (file manager),
mpv + yt-dlp + python-ytmusicapi (music), cava (visualizer).

There is no one-click installer on purpose: this is one person's desktop, tuned to one laptop.
Read [`install.md`](install.md), take what you like, and adapt paths and hardware bits (`system/`, `config/hypr/`).

## Credits

- [EZ File Manager](https://github.com/TechyTechster/ez-fm) — MIT, © TechyTechster (license kept in `file-manager/LICENSE`)
- Weather: [Open-Meteo](https://open-meteo.com)
- Venus de Milo (start-up animation): public-domain photo, Louvre, via Wikimedia Commons
- Goddesses of the System widget: ASCII from [ascii-walls](https://github.com/Sapyst/ascii-walls), made from photos
  that are public domain, CC0 or CC BY (no share-alike):
  - Athena: *Atena Mattei* (Paris), photo by Jastrow, Public domain · [source](https://commons.wikimedia.org/wiki/File:Mattei_Athena_Louvre_Ma530_n2.jpg)
  - Aphrodite: *Vênus de Milo* (Paris), photo by Shonagon, CC0 · [source](https://commons.wikimedia.org/wiki/File:V%C3%A9nus_de_Milo_-_Mus%C3%A9e_du_Louvre_AGER_LL_299_;_N_527_;_Ma_399.jpg)
  - Artemis: *Diana de Versalhes* (Paris), photo by Shonagon, CC0 · [source](https://commons.wikimedia.org/wiki/File:Diane_de_Versailles_-_Mus%C3%A9e_du_Louvre_AGER_Ma_589.jpg)
  - Hera: *Juno Ludovisi* (Roma), photo by , Public domain · [source](https://commons.wikimedia.org/wiki/File:Hera_Ludovisi_Altemps_Inv8631.jpg)
  - Demeter: *Deméter de Cnido* (Londres), photo by Raymond.ellis, CC0 · [source](https://commons.wikimedia.org/wiki/File:Marble_Statue_of_Demeter.jpg)
  - Hestia: *Héstia Giustiniani* (Roma), photo by Marie-Lan Taÿ Pamart, Public domain · [source](https://commons.wikimedia.org/wiki/File:Hestia_Giustiniani_Torlonia_Collection_MT490.jpg)
  - Persephone: *Grande Relevo de Elêusis (Deméter, Triptólemo e Perséfone)* (Nova York), photo by , CC BY 2.5 · [source](https://commons.wikimedia.org/wiki/File:Great_Eleusinian_relief_fragments_Met_14.130.9_n01.jpg)
  - Nike: *Vitória de Samotrácia* (Paris), photo by Shonagon, CC0 · [source](https://commons.wikimedia.org/wiki/File:Victoire_de_Samothrace_-_Musee_du_Louvre_-_20190812.jpg)
  - Tyche: *Fortuna ou Tutela (MAM 2735)* (Arles), photo by Rama, Public domain · [source](https://commons.wikimedia.org/wiki/File:Fortuna_or_Tutela-MAM_2735-IMG_9179-gradient.jpg)
  - Hygieia: *Higeia, deusa da saúde (Ra 42)* (Toulouse), photo by Didier Descouens, Public domain · [source](https://commons.wikimedia.org/wiki/File:(Toulouse)_Hygie,_d%C3%A9esse_de_la_Sant%C3%A9_-_Mus%C3%A9e_Saint-Raymond,_Ra_42.jpg)
  - Themis: *Justitia do Landgericht* (Berlim), photo by Genealogist, CC0 · [source](https://commons.wikimedia.org/wiki/File:Justitia_am_Landgericht_Berlin.JPG)
  - Hebe: *Hebe* (Copenhague), photo by Bertel Thorvaldsen, CC0 · [source](https://commons.wikimedia.org/wiki/File:Hebe_by_Bertel_Thorvaldsen_(A874).jpg)
  - Selene: *Selene deitada* (Montauban), photo by Didier Descouens, Public domain · [source](https://commons.wikimedia.org/wiki/File:Mus%C3%A9e_Ingres-Bourdelle_-_S%C3%A9l%C3%A9n%C3%A9_couch%C3%A9e,_1917_-_Bourdelle_-_Joconde06070001193.jpg)
  - Hecate: *Diana como Hécate* (Londres), photo by Unknown authorUnknown author, Public domain · [source](https://commons.wikimedia.org/wiki/File:Estatua_de_Diana_como_H%C3%A9cate,_British_Museum.jpg)
  - Nemesis: *Nêmesis alada do Egito* (Paris), photo by Gary Todd from Xinzheng, China, CC0 · [source](https://commons.wikimedia.org/wiki/File:Marble_statue_of_Nemesis,_from_Egypt,_2nd_c._AD_(28273794646).jpg)
  - Eris: *Eris com a maçã da discórdia* (Bydgoszcz), photo by Pit1233, CC0 · [source](https://commons.wikimedia.org/wiki/File:Bdg_Gdanska15_Eris_07-2013.jpg)
- [Hyprland](https://hyprland.org), [hyprbars](https://github.com/hyprwm/hyprland-plugins)

## License

MIT for everything written here (see `LICENSE`); `file-manager/` keeps its own MIT notice.
