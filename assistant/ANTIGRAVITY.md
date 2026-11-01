# Teaching Aero Assistant

You are improving a local Arch Linux assistant in `assistant/` of this repository. Paste-ready tasks are below. Write everything in English.

## Rules
- Do not edit `src/guard.rs` to allow more commands unless a test in the same file covers the new allowance. Never allow a command that changes the system.
- Do not change `aero-assistant-server.service`, `/tmp`, packages or anything outside `assistant/`.
- Do not run approved actions, `sudo`, or anything destructive. Test with `aero-assistant --no-exec`.
- Do not commit or push. Report what changed and which tests ran.
- After each task: `cargo test --release`, then `./install.sh`.

## Task 1: more triggers
Add lines to `triggers.conf` for common daily requests that are still missing, such as kernel version, installed package version and top CPU process. Both word groups must be specific. For each line, add a firing case and a similar case that must not fire to the tests in `src/triggers.rs`. Skip anything where one word could mean two things.

## Task 2: machine knowledge
Add short English notes to `knowledge/*.md` about how this desktop works: read `README.md`, `install.md`, `bin/` and `config/hypr/`. Use `## Section` headings. Write only what those files show, and name the file each fact came from.

## Task 3: find wrong answers
Ask `aero-assistant --model --no-exec` 30 varied questions. Compare each answer with the real command output. List every wrong number, invented option or unneeded refusal, with the question, the answer and the truth. Do not fix the model's replies by adding the exact question to a prompt; propose a trigger, a `system_info` topic or a note instead.

## Task 4: regression list
Save the questions and the commands that give the true answers in `tests/questions.txt`, one per line: `question|||command`. Never include a command that changes the system.
