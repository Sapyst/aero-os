# Known Errors and Fixes

General reference of errors observed on this machine, their causes, and proven fixes.

## Claude Code refuses symlink at /tmp/claude-<uid>
- Problem: Claude Code fails to start, displaying an error refusing `/tmp/claude-<uid>` or exiting immediately.
- Cause: Claude Code checks that its temporary directory is a real directory and refuses to follow a symlink at `/tmp/claude-<uid>` for security and sandbox isolation.
- Fix: Set `CLAUDE_CODE_TMPDIR=${HOME}/.cache/aero-temp/claude` in `~/.config/environment.d/aero-temp.conf` so scratch files are stored on disk without creating a symlink in `/tmp`. Remove any symlink at `/tmp/claude-<uid>` and do not recreate it.
- Source: commit 095a1a8 ("storage: use CLAUDE_CODE_TMPDIR instead of a /tmp symlink"), `system/storage/README.md`.

## System tray icon right-click menu empty (aero-trayd)
- Problem: Right-clicking system tray icons in the bar shows an empty menu or does nothing.
- Cause: The D-Bus `dbusmenu` layout interface returns a plain structure rather than a variant container. Parsing the reply as a variant caused the layout parser to fail and return an empty menu.
- Fix: Parse the `dbusmenu` layout return value as a plain structure so layout items are loaded.
- Source: commit 23e2bac ("aero-trayd: read dbusmenu layouts correctly").

## Tray daemon stops permanently after compositor reload (aero-trayd)
- Problem: The system tray disappears after Hyprland compositor reload or Wayland restart and does not return.
- Cause: On losing the Wayland display socket connection, `aero-trayd` exited with status code 0. `aero-supervise` treated exit code 0 as a clean stop and did not restart the service.
- Fix: Exit with a non-zero failure code when the Wayland connection drops so the supervisor automatically restarts the daemon.
- Source: commit 661d980 ("aero-trayd: exit with failure on a lost Wayland connection").

## Clipboard history window hangs on image thumbnails
- Problem: The clipboard history manager window freezes or becomes unresponsive when displaying copied images or screenshots.
- Cause: Requesting thumbnails through `gvfs` could block the UI thread indefinitely waiting on background gvfs services.
- Fix: Read and decode image thumbnails directly from the local filesystem without using `gvfs`.
- Source: commit bc84b59 ("clipboard: read thumbnails without gvfs so the window cannot hang").

## /tmp runs out of space during large builds
- Problem: Compilations, test runs, or downloads fail with out-of-space or out-of-memory errors under `/tmp`.
- Cause: `/tmp` is mounted on tmpfs (RAM) with a 2 GB limit to conserve memory for system applications.
- Fix: Place large temporary directories and build caches on the SSD under `~/.cache/aero-temp/` using `~/.config/user-tmpfiles.d/aero-temp.conf`, and execute intensive tasks with `aero-bg`.
- Source: commit eae6507 ("storage: RAM limit for /tmp, temp cleanup, background priority and build cleanup"), `system/storage/README.md`.
