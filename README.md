# claude-undo

An undo history for every file Claude Code changes, in folders with or without git.

Claude Code is great at editing files. It is also happy to `rm` the wrong one or run `sed -i` across a folder you never committed. `claude-undo` is a small hook that copies each file just before Claude Code touches it, plus two commands to get it back:

```bash
claude-undo list ~/thesis
claude-undo restore ~/thesis/chapter3.md
```

One Python file, standard library only, nothing leaves your machine.

## Why not /rewind?

Claude Code's built-in checkpoints are good, but by design they leave gaps ([Claude Code docs, Checkpointing](https://code.claude.com/docs/en/checkpointing)):

| Situation | `/rewind` | `claude-undo` |
|---|---|---|
| Edit and Write tool changes | yes | yes |
| `rm`, `mv`, `cp`, `sed -i`, `perl -i`, `truncate`, `tee`, `>` on a file Claude never edited with its file tools | no | yes |
| Copies after the session is closed | until the retention sweep, about 30 days by default | 90 days by default, configurable |
| Folders without git | yes | yes |

Git is still the best undo. This is for everything that is not in git yet: a thesis, notes, a client folder, a quick prototype.

## Install

Requires Python 3.9 or newer and macOS or Linux.

```bash
git clone https://github.com/BlendiFevziu/claude-undo.git ~/claude-undo
python3 ~/claude-undo/claude_undo.py install
```

`install` adds one `PreToolUse` hook to `~/.claude/settings.json` (a copy of the old file is kept next to it as `settings.json.before-claude-undo`). Restart Claude Code afterwards. Run it again and nothing changes.

The hook points at the folder you cloned into. If you move that folder, run `uninstall` and then `install` with the script at its new location. Until you do, the hook does nothing (it never blocks Claude Code).

Optional shortcut:

```bash
alias claude-undo="python3 ~/claude-undo/claude_undo.py"
```

## Use

```bash
claude-undo list                         # last 30 copies, newest first
claude-undo list ~/thesis -n 100         # only files under a folder
claude-undo restore ~/thesis/notes.md    # put back the newest copy
claude-undo restore ~/thesis/notes.md --id 20261026-123028-363384
claude-undo clean --days 30              # delete old copies now
```

A restore first saves the current version of the file, so running `restore` twice takes you back to where you were.

Copies live in `~/.claude-undo/` (set `CLAUDE_UNDO_DIR` to move them). Copies older than 90 days are removed automatically, at most once a day.

## What it catches, and what it does not

Caught, for files that exist at the moment of the call:

- `Edit`, `Write`, `NotebookEdit`
- Shell commands that name the file: `rm` (and `rm -r` on a folder), `mv`, `cp` onto an existing file, `sed -i`, `perl -i`, `truncate`, `tee` without `-a`, and `>` redirects. Globs like `*.md` and a leading `cd folder &&` are understood.

Not caught, because the hook cannot see which files they will touch:

- scripts (`python fix.py`, `npm run clean`), `find ... -delete`, `xargs rm`, paths inside variables
- changes made outside Claude Code

Skipped on purpose (edit `~/.claude-undo/config.json` to change):

| Setting | Default |
|---|---|
| `keep_days` | 90 |
| `max_file_mb` | 5 |
| `max_dir_files` | 200 (an `rm -r` on a bigger folder is not copied) |
| `exclude_dirs` | `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.next`, `.cache` |
| `exclude_names` | `.env`, `.env.local`, `.env.production`, `.DS_Store` |
| `exclude_suffixes` | `.lock`, `.pyc` |
| `exclude_prefixes` | `/tmp`, `/private/tmp`, `/var/folders`, `/private/var/folders` |

Secrets in `.env` files are not copied by default. Remove a name from `exclude_names` if you want them covered.

The hook never blocks Claude Code: if anything goes wrong it gives up silently and the tool call goes ahead.

## Uninstall

```bash
python3 ~/claude-undo/claude_undo.py uninstall
rm -r ~/.claude-undo ~/claude-undo
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
