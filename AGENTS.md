# Agent Instructions

Guidance for AI agents working in this repository.

## Restricted paths — do not read

Never open, read, display, or inspect the contents of these paths. This applies to every tool and shell command (`Read`, `cat`, `head`, `tail`, `less`, `grep` with content output, etc.).

- `data/wallet/` — wallet keystores and private key material
- `data/logs/` — runtime logs that may contain sensitive execution details
- `*.keystore.json` — encrypted keystore files (anywhere in the repo)
- `*.signed-tx` — signed transaction payloads (anywhere in the repo)
- `.env` — environment variables (anywhere in the repo)

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

## Imports

Follow these rules in every Python file:

1. Put `from __future__ import annotations` first when the file uses postponed annotation evaluation.

2. Use single-line imports only — keep each import on one line, even if it is long. Do not split one import across multiple lines.

3. Group imports with a blank line between groups, in this order:
   - Standard library
   - Third-party packages
   - Local/project imports (`from .…` or `from src.…`)

4. Within the standard-library and third-party groups, put runtime imports first, then type-only imports (`typing`, `collections.abc`, etc.), separated by a blank line.

5. Within each of those sub-groups, split into two blocks separated by a blank line:
   - Bare imports — lines starting with `import `
   - From imports — lines starting with `from `

6. Sort each sub-group by full line length, shortest to longest.

## Documentation — keep docs in sync

When you change code, update related documentation in the same change. Do not leave docs stale unless the user explicitly asked for code-only changes.

Check and update docs that describe the behavior you changed, for example:

- `README.md` — CLI commands, workflows, setup steps, and user-visible behavior
- `config.example.yaml` — new, renamed, or removed config fields and defaults
- Other repo docs that mention the feature, flag, or command you touched

If a code change affects how someone runs or configures the bot, the docs should reflect that before you finish the task.
