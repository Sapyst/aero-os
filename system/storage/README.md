# Storage rules

No daemon: only systemd rules, so this uses no memory while idle.

| Location | Contents | Cleanup |
|---|---|---|
| `/tmp` (RAM, 2 GB limit) | sockets and small files | on reboot |
| `~/.cache/aero-temp` (SSD) | logs, videos, test output, Claude scratch files | after 7 days |
| `~/.cache/pip`, `~/.cache/uv` | download caches | after 30 days |

Run `aero-bg <command>` to start work at background priority.

`/tmp/claude-<uid>` becomes a link to the SSD at login, once `/tmp` is empty after a reboot.

Install with `./install.sh`.

`aero-build-clean` removes stale Rust build artifacts daily, at background priority, while on AC power and with low system pressure.
