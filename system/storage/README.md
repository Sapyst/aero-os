# Storage rules

No daemon: only systemd rules, so this uses no memory while idle.

| Location | Contents | Cleanup |
|---|---|---|
| `/tmp` (RAM, 2 GB limit) | sockets and small files | on reboot |
| `~/.cache/aero-temp` (SSD) | logs, videos, test output, Claude scratch files | after 7 days |
| `~/.cache/pip`, `~/.cache/uv` | download caches | after 30 days |

Run `aero-bg <command>` to start work at background priority.

Claude Code keeps its scratch files on the SSD through `CLAUDE_CODE_TMPDIR`, set at login. Do not link `/tmp/claude-<uid>`: Claude Code refuses a symlink there and will not start.

Install with `./install.sh`.

`aero-build-clean` removes stale Rust build artifacts daily, at background priority, while on AC power and with low system pressure.
