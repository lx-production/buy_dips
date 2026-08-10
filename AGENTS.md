# Agent Instructions

Guidance for AI agents working in this repository.

## Restricted paths — do not read

Never open, read, display, or inspect the contents of these paths. This applies to every tool and shell command (`Read`, `cat`, `head`, `tail`, `less`, `grep` with content output, etc.).

- `data/wallet/` — wallet keystores and private key material
- `data/logs/` — runtime logs that may contain sensitive execution details
- `*.keystore.json` — encrypted keystore files (anywhere in the repo)
- `*.signed-tx` — signed transaction payloads (anywhere in the repo)

### Allowed without reading contents

You may still:

- Check existence with `test -f`, `test -d`, or `ls` (filename only, no content)
- Verify gitignore with `git check-ignore -v <path>`
- Confirm a path is listed in `.gitignore` or this file

### If you need information from a restricted file

Ask the user to paste the specific non-secret fields (e.g. public address, file size, error message). Do not ask them to share private keys, mnemonics, passwords, or full keystore JSON.

## Coding style — keep it simple

- Prefer minimal, focused changes. Solve the current task with the simplest correct approach; do not over-engineer or add abstractions "for later."
- Improvements, refactors, and optimizations can come in follow-up work — ship the straightforward version first.
- Add a comment at the top of every function. Explain what it does, how it works, and why (when relevant) — junior devs and reviewers should be able to follow without guessing.
- Inside the function, add inline comments for non-obvious steps, business rules, and edge cases.
- Match existing project patterns, naming, and file layout before introducing new conventions.
