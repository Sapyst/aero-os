# EZ File Manager (Aero fork)

A modified copy of [EZ File Manager](https://github.com/TechyTechster/ez-fm) by TechyTechster (MIT, see `LICENSE`).

What changed here (comments tagged `[Aero/doc NN]` in the code):

- terminal-glass look that follows the system theme color and light/dark mode live
- Ctrl + scroll zoom: list → grid → big thumbnails (up to 400 px), remembered per folder
- resizable preview pane (drag its edge, up to 75% of the window), Space toggles it in the picker
- window buttons wired to the desktop's own minimize/maximize commands
- **system file picker**: `portal-service.js` answers `org.freedesktop.impl.portal.FileChooser`
  (Open, Save, Save Files, start folder, safe `file://` URIs)
- hardening: no in-app sudo, picker refuses file names with line breaks

Runs on the system Electron (`/usr/bin/electron`), not the version pinned in `package.json`.
Install notes for the portal are in `../install.md`.
