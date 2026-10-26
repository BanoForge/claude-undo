#!/usr/bin/env python3
"""claude-undo: an undo history for folders Claude Code edits, git or not.

A PreToolUse hook copies every file just before Claude Code changes it, through
Edit, Write, MultiEdit, NotebookEdit, or a shell command that names the file
(rm, mv, cp, sed -i, perl -i, truncate, tee, and > redirects). Copies are listed
and restored from the command line.

Why: Claude Code's /rewind skips most changes made through shell commands and
deletes its snapshots after about 30 days, and many folders people point Claude
at (a thesis, documents, a small site) have no git.

Standard library only, Python 3.9+. The hook never blocks a tool call: it
always exits 0, even when something goes wrong.

Usage:
  claude_undo.py install [--settings PATH]    add the hook to Claude Code settings
  claude_undo.py uninstall [--settings PATH]  remove it again
  claude_undo.py list [PATH]                  copies, newest first, optionally under PATH
  claude_undo.py restore PATH [--id ID]       put back the latest (or a given) copy
  claude_undo.py clean [--days N]             delete copies older than N days
  claude_undo.py hook                         what Claude Code runs; reads JSON on stdin
"""

import argparse
import datetime
import glob
import json
import os
import shlex
import shutil
import sys

DEFAULT_ROOT = os.environ.get("CLAUDE_UNDO_DIR") or os.path.expanduser("~/.claude-undo")
DEFAULT_SETTINGS = os.path.expanduser("~/.claude/settings.json")
HOOK_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
BACKUP_SUFFIX = ".before-claude-undo"

DEFAULT_CONFIG = {
    "keep_days": 90,
    "max_file_mb": 5,
    # rm -r on a folder with more files than this is skipped rather than copied.
    "max_dir_files": 200,
    "exclude_dirs": [
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        "dist", "build", ".next", ".cache",
    ],
    # Secrets stay where they are. Remove a name here to have it backed up too.
    "exclude_names": [".env", ".env.local", ".env.production", ".DS_Store"],
    "exclude_suffixes": [".lock", ".pyc"],
    # Scratch space Claude writes to all day; copying it only fills the store.
    "exclude_prefixes": ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"],
}


class UndoError(Exception):
    pass


def _parse_time(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f")


class Store:
    """Copies live in <root>/files/<YYYY-MM-DD>/, one line per copy in index.jsonl."""

    def __init__(self, root=DEFAULT_ROOT):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.index_path = os.path.join(self.root, "index.jsonl")
        self.config = dict(DEFAULT_CONFIG)
        try:
            with open(os.path.join(self.root, "config.json")) as f:
                self.config.update(json.load(f))
        except (OSError, ValueError):
            pass

    def is_excluded(self, path):
        cfg = self.config
        if path == self.root or path.startswith(self.root + os.sep):
            return True
        if any(path == p or path.startswith(p.rstrip(os.sep) + os.sep) for p in cfg["exclude_prefixes"]):
            return True
        if set(path.split(os.sep)) & set(cfg["exclude_dirs"]):
            return True
        name = os.path.basename(path)
        return name in cfg["exclude_names"] or name.endswith(tuple(cfg["exclude_suffixes"]))

    def wants(self, path):
        if not os.path.isfile(path) or os.path.islink(path) or self.is_excluded(path):
            return False
        try:
            return os.path.getsize(path) <= self.config["max_file_mb"] * 1024 * 1024
        except OSError:
            return False

    def save(self, path, tool, session="", now=None):
        now = now or datetime.datetime.now()
        entry_id = now.strftime("%Y%m%d-%H%M%S-%f")
        while any(e["id"] == entry_id for e in self._read_index()):
            now += datetime.timedelta(microseconds=1)
            entry_id = now.strftime("%Y%m%d-%H%M%S-%f")
        rel = os.path.join("files", now.strftime("%Y-%m-%d"), "%s__%s" % (entry_id, os.path.basename(path)))
        dest = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        entry = {
            "id": entry_id,
            "time": now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "path": path,
            "backup": rel,
            "size": os.path.getsize(dest),
            "tool": tool,
            "session": session,
        }
        with open(self.index_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def _read_index(self):
        entries = []
        try:
            with open(self.index_path) as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return entries

    def entries(self, under=None):
        """Copies newest first, optionally only for files at or below `under`."""
        items = self._read_index()
        if under:
            under = os.path.abspath(os.path.expanduser(under))
            items = [e for e in items if e["path"] == under or e["path"].startswith(under.rstrip(os.sep) + os.sep)]
        return sorted(items, key=lambda e: e["id"], reverse=True)

    def restore(self, path, entry_id=None):
        path = os.path.abspath(os.path.expanduser(path))
        candidates = [e for e in self.entries() if e["path"] == path]
        if entry_id:
            candidates = [e for e in candidates if e["id"] == entry_id]
        if not candidates:
            raise UndoError("no saved copy of %s%s" % (path, " with id " + entry_id if entry_id else ""))
        entry = candidates[0]
        source = os.path.join(self.root, entry["backup"])
        if not os.path.isfile(source):
            raise UndoError("the copy %s is missing from %s" % (entry["id"], self.root))
        # Save what is there now first, so a restore can be undone the same way.
        if os.path.isfile(path):
            self.save(path, "restore")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy2(source, path)
        return entry

    def clean(self, keep_days=None, now=None):
        keep_days = self.config["keep_days"] if keep_days is None else keep_days
        cutoff = (now or datetime.datetime.now()) - datetime.timedelta(days=keep_days)
        keep, removed = [], 0
        for e in self._read_index():
            if _parse_time(e["time"]) < cutoff:
                try:
                    os.remove(os.path.join(self.root, e["backup"]))
                except OSError:
                    pass
                removed += 1
            else:
                keep.append(e)
        if removed:
            tmp = self.index_path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(json.dumps(e) + "\n" for e in keep)
            os.replace(tmp, self.index_path)
            for d in glob.glob(os.path.join(self.root, "files", "*")):
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
        return removed

    def clean_daily(self):
        """Run clean() at most once a day, from the hook."""
        stamp = os.path.join(self.root, ".last-clean")
        today = datetime.date.today().isoformat()
        try:
            with open(stamp) as f:
                if f.read().strip() == today:
                    return
        except OSError:
            pass
        self.clean()
        os.makedirs(self.root, exist_ok=True)
        with open(stamp, "w") as f:
            f.write(today)


# Shell parsing. Best effort by design: a command that hides its targets
# (a script, find -delete, a variable) is not caught. The README says so.

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
WRAPPERS = {"sudo", "command", "nohup", "time", "env"}


def _segments(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    seg = []
    for tok in tokens:
        if tok in SEPARATORS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def _expand(arg, cwd):
    path = os.path.join(cwd, os.path.expanduser(arg))
    if any(c in arg for c in "*?["):
        return sorted(glob.glob(path))
    return [path]


def _files_in(path, recursive):
    if os.path.isfile(path):
        return [path]
    if recursive and os.path.isdir(path):
        found = []
        for dirpath, _dirs, files in os.walk(path):
            found.extend(os.path.join(dirpath, f) for f in files)
        return found
    return []


def _split_flags(args, value_flags=()):
    """Return (flags, operands). Flags in value_flags swallow the next token."""
    flags, operands, i, done = [], [], 0, False
    while i < len(args):
        a = args[i]
        if done or not a.startswith("-") or a == "-":
            operands.append(a)
        elif a == "--":
            done = True
        else:
            flags.append(a)
            if a in value_flags and i + 1 < len(args):
                flags.append(args[i + 1])
                i += 1
        i += 1
    return flags, operands


def paths_from_bash(command, cwd):
    """Existing files a shell command is about to change or delete."""
    try:
        segments = list(_segments(command))
    except ValueError:
        return []
    found = []
    for seg in segments:
        # Redirects first: "> file" overwrites, ">> file" appends and is harmless.
        args, i = [], 0
        while i < len(seg):
            tok = seg[i]
            if tok in (">", ">|") and i + 1 < len(seg):
                found.extend(p for p in _expand(seg[i + 1], cwd) if os.path.isfile(p))
                i += 2
                continue
            if tok in (">>", "<", "<<", "<<<", ">&", "<&") and i + 1 < len(seg):
                i += 2
                continue
            args.append(tok)
            i += 1
        while args and ("=" in args[0] and not args[0].startswith("-") or args[0] in WRAPPERS):
            args = args[1:]
        if not args:
            continue
        name, rest = os.path.basename(args[0]), args[1:]

        if name == "cd":
            target = rest[0] if rest else "~"
            cwd = os.path.join(cwd, os.path.expanduser(target))
        elif name == "rm":
            flags, ops = _split_flags(rest)
            short = "".join(f for f in flags if not f.startswith("--"))
            recursive = "--recursive" in flags or "r" in short or "R" in short
            for op in ops:
                for p in _expand(op, cwd):
                    found.extend(_files_in(p, recursive))
        elif name in ("mv", "cp"):
            _flags, ops = _split_flags(rest, value_flags=("-t", "-S"))
            dests = _expand(ops[-1], cwd) if len(ops) >= 2 else []
            if not dests:
                continue
            sources, dest = ops[:-1], dests[0]
            if name == "mv":
                for s in sources:
                    found.extend(p for p in _expand(s, cwd) if os.path.isfile(p))
            if os.path.isdir(dest):
                for s in sources:
                    for p in _expand(s, cwd):
                        target = os.path.join(dest, os.path.basename(p))
                        if os.path.isfile(target):
                            found.append(target)
            elif os.path.isfile(dest):
                found.append(dest)
        elif name in ("sed", "gsed"):
            in_place = any(a.startswith("-i") or a.startswith("--in-place") for a in rest)
            if not in_place:
                continue
            has_script_flag = any(a in ("-e", "-f", "--expression", "--file") for a in rest)
            _flags, ops = _split_flags(rest, value_flags=("-e", "-f", "--expression", "--file"))
            # macOS writes "sed -i '' ...": the empty suffix arrives as its own token.
            ops = [o for o in ops if o != ""]
            if not has_script_flag:
                ops = ops[1:]
            for op in ops:
                found.extend(p for p in _expand(op, cwd) if os.path.isfile(p))
        elif name == "perl":
            if not any(a.startswith("-") and "i" in a and not a.startswith("--") for a in rest):
                continue
            _flags, ops = _split_flags(rest, value_flags=("-e", "-E", "-M", "-I"))
            for op in ops:
                found.extend(p for p in _expand(op, cwd) if os.path.isfile(p))
        elif name == "truncate":
            _flags, ops = _split_flags(rest, value_flags=("-s", "-r", "--size", "--reference"))
            for op in ops:
                found.extend(p for p in _expand(op, cwd) if os.path.isfile(p))
        elif name == "tee":
            flags, ops = _split_flags(rest)
            if any(f in ("-a", "--append") for f in flags):
                continue
            for op in ops:
                found.extend(p for p in _expand(op, cwd) if os.path.isfile(p))

    seen, unique = set(), []
    for p in found:
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def snapshot_from_payload(payload, store):
    """Save a copy of every file this tool call is about to change. Never raises."""
    try:
        tool = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        cwd = payload.get("cwd") or os.getcwd()
        if tool == "Bash":
            paths = paths_from_bash(tool_input.get("command") or "", cwd)
            files = [p for p in paths if store.wants(p)]
            if len(files) > store.config["max_dir_files"]:
                return []
        else:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not target:
                return []
            target = os.path.abspath(os.path.join(cwd, os.path.expanduser(target)))
            files = [target] if store.wants(target) else []
        saved = []
        for p in files:
            try:
                store.save(p, tool, payload.get("session_id") or "")
                saved.append(p)
            except OSError:
                continue
        return saved
    except Exception:
        return []


# Settings install / uninstall

def _is_ours(matcher_entry):
    return "claude_undo.py" in json.dumps(matcher_entry)


def _load_settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _write_settings(path, data):
    if os.path.exists(path):
        shutil.copy2(path, path + BACKUP_SUFFIX)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def install(settings_path):
    data = _load_settings(settings_path)
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if any(_is_ours(m) for m in pre):
        return False
    # "|| true": python exits 2 when the script has been moved or deleted, and
    # exit 2 from a PreToolUse hook would block every edit.
    command = "%s %s hook 2>/dev/null || true" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))
    pre.append({
        "matcher": HOOK_MATCHER,
        "hooks": [{"type": "command", "command": command, "timeout": 10}],
    })
    _write_settings(settings_path, data)
    return True


def uninstall(settings_path):
    data = _load_settings(settings_path)
    pre = data.get("hooks", {}).get("PreToolUse", [])
    kept = [m for m in pre if not _is_ours(m)]
    if len(kept) == len(pre):
        return False
    data["hooks"]["PreToolUse"] = kept
    _write_settings(settings_path, data)
    return True


def _human(size):
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return "%d %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def main(argv=None, stdin_text=None, root=None):
    argv = sys.argv[1:] if argv is None else argv
    store = Store(root or DEFAULT_ROOT)

    if argv[:1] == ["hook"]:
        try:
            text = sys.stdin.read() if stdin_text is None else stdin_text
            snapshot_from_payload(json.loads(text), store)
            store.clean_daily()
        except Exception:
            pass
        return 0

    parser = argparse.ArgumentParser(prog="claude-undo", description="Undo history for files Claude Code changes.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--settings", default=DEFAULT_SETTINGS)
    p = sub.add_parser("list")
    p.add_argument("path", nargs="?")
    p.add_argument("-n", type=int, default=30, help="how many to show (default 30)")
    p = sub.add_parser("restore")
    p.add_argument("path")
    p.add_argument("--id")
    p = sub.add_parser("clean")
    p.add_argument("--days", type=int)
    args = parser.parse_args(argv)

    if args.cmd == "install":
        changed = install(args.settings)
        print("Hook added to %s" % args.settings if changed else "Already installed in %s" % args.settings)
        print("Copies go to %s. Restart Claude Code to pick it up." % store.root)
    elif args.cmd == "uninstall":
        changed = uninstall(args.settings)
        print("Hook removed from %s" % args.settings if changed else "Not installed in %s" % args.settings)
    elif args.cmd == "list":
        items = store.entries(args.path)
        if not items:
            print("No saved copies%s." % (" under " + args.path if args.path else ""))
        for e in items[: args.n]:
            print("%s  %s  %8s  %-12s %s" % (e["id"], e["time"][:19].replace("T", " "), _human(e["size"]), e["tool"], e["path"]))
    elif args.cmd == "restore":
        try:
            e = store.restore(args.path, args.id)
        except UndoError as err:
            print("claude-undo: %s" % err, file=sys.stderr)
            return 1
        print("Restored %s from the copy taken %s (%s)." % (e["path"], e["time"][:19].replace("T", " "), e["id"]))
    elif args.cmd == "clean":
        removed = store.clean(args.days)
        print("Removed %d copies older than %d days." % (removed, args.days if args.days is not None else store.config["keep_days"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
