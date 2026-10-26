# claude-undo

A PreToolUse hook that copies files before Claude Code changes them, plus `list` and `restore`. Private notes for the maintainer live in `CLAUDE.local.md` (git-ignored); read it too if it exists.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

All tests must pass with no output besides the test lines. For a change to the hook itself, also run the end-to-end check below.

## End-to-end check

Run in `.e2e/` (git-ignored): create `work/a.md`, `b.md`, `c.md`, run `CLAUDE_UNDO_DIR=.e2e/store python3 claude_undo.py install --settings .e2e/settings.json`, then from `.e2e/work` run `claude -p --model haiku --max-budget-usd 0.5 --setting-sources project --settings <abs path>/.e2e/settings.json --permission-mode acceptEdits` with a prompt that edits `a.md` with the Edit tool and runs `sed -i '' 's/v1/v2/' b.md && rm c.md`. `list` must show three copies and `restore` must bring all three back. Start the shell outside `.e2e` so it can be deleted afterwards.

## Rules

- Standard library only, Python 3.9 compatible: no `match`, no `X | Y` type syntax, no third-party packages.
- The `hook` command must always exit 0 and must never print to stdout, because Claude Code reads hook stdout as instructions. Wrap new hook logic in the existing try/except.
- `install` and `uninstall` must keep every other key in settings.json and write the `.before-claude-undo` copy first, and must not escape non-ASCII text (`ensure_ascii=False`).
- The installed command ends in `2>/dev/null || true`. Keep it: python exits 2 when the script is missing, and exit 2 from a PreToolUse hook blocks every tool call. `test_installed_command_never_blocks_when_script_is_gone` guards this.
- After moving the repo, run `uninstall` then `install` from the new location: `install` alone says "Already installed" and keeps the old path.
- Shell parsing is best effort. When adding a command, add a test in `BashPathTests` that names the exact files, and list it in the README's "What it catches" section.
- Hook tool_input fields, checked against code.claude.com/docs/en/hooks on 26.10.2026: Edit and Write use `file_path`, NotebookEdit uses `notebook_path`, Bash uses `command`, every payload carries `cwd` and `session_id`. Re-check the docs before relying on any other field.
- No em-dashes in README or any other text.
