# Aero Assistant

A local Arch Linux assistant. `aero "request"` (also `-aero` and `aero-assistant`), or no arguments for a conversation.

- **Rules first:** `triggers.conf` answers common requests with exact facts, without loading the model. Two word groups must match, and actions fire only on a command, never on a question.
- **Model second:** anything else goes to Qwen3.6-35B on Janas, which reads the system through checked tools and the offline Arch Wiki. It closes after 5 idle minutes; `aero-assistant stop` closes it now.
- **Nothing changes without a yes.** `src/guard.rs` lists what may run freely and checks each proposal.

Teach it through `triggers.conf` and `knowledge/*.md`, then run `./install.sh`. Run `cargo test --release` after any change. `--model` skips the rules, and `--no-exec` never runs proposals.

Janas is GPL-3.0 and runs as a separate program.
