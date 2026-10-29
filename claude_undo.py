#!/usr/bin/env python3
"""claude-undo: an undo history for folders Claude Code edits, git or not.

A PreToolUse hook copies eligible existing files before supported changes through
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
  claude_undo.py diff PATH [--id ID]          what changed since the latest (or a given) copy
  claude_undo.py restore PATH [--id ID]       put back the latest (or a given) copy
  claude_undo.py clean [--days N]             delete copies older than N days
  claude_undo.py status                       recent recorded hook errors
  claude_undo.py hook                         what Claude Code runs; reads JSON on stdin
"""

import argparse
import contextlib
import datetime
import difflib
import fcntl
import glob
import hashlib
import json
import math
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
import uuid

DEFAULT_ROOT = os.environ.get("CLAUDE_UNDO_DIR") or os.path.expanduser("~/.claude-undo")
DEFAULT_SETTINGS = os.path.expanduser("~/.claude/settings.json")
HOOK_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
BACKUP_SUFFIX = ".before-claude-undo"

DEFAULT_CONFIG = {
    "keep_days": 90,
    "max_file_mb": 5,
    # A Bash call exceeding this many eligible targets is skipped in full.
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


def _fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@contextlib.contextmanager
def _regular_file(path, dir_fd=None):
    # NONBLOCK avoids hanging if a file was replaced with a FIFO.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise UndoError("not a regular file: %s" % path)
        yield source


@contextlib.contextmanager
def _parent_directory(path, create=False):
    """Pin each directory without following links; recheck ancestry before commit."""
    parts = path.split(os.sep)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = [os.open(os.sep, flags)]
    edges = []
    try:
        for part in parts[1:-1]:
            if not part:
                continue
            parent = descriptors[-1]
            try:
                child = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=parent)
            except OSError as err:
                raise UndoError("unsafe parent directory in %s: %s" % (path, err))
            descriptors.append(child)
            edges.append((parent, part, child))

        def verify():
            for parent, name, child in edges:
                actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
                pinned = os.fstat(child)
                if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise UndoError("parent directory changed during restore: %s" % path)
        yield descriptors[-1], parts[-1], verify
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _destination_state(parent, name):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UndoError("unsafe restore destination %s: expected a single-link regular file or an absent path; symlinks, hard links and directories are refused" % name)
    return _fingerprint(info)


def _copy_stream(source, destination):
    before = os.fstat(source.fileno())
    digest, size = hashlib.sha256(), 0
    while True:
        block = source.read(1024 * 1024)
        if not block:
            break
        destination.write(block)
        digest.update(block)
        size += len(block)
    if _fingerprint(before) != _fingerprint(os.fstat(source.fileno())):
        raise UndoError("source changed while copying; retry when writes have stopped")
    destination.flush()
    os.fchmod(destination.fileno(), stat.S_IMODE(before.st_mode))
    os.utime(destination.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
    os.fsync(destination.fileno())
    return size, digest.hexdigest()


def _record_error(root, operation, error):
    """Best effort only: an unavailable store cannot record its own failure."""
    try:
        root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        os.makedirs(root, exist_ok=True)
        event = {"time": datetime.datetime.now().isoformat(), "operation": operation,
                 "error": str(error)[:4000]}
        fd = os.open(os.path.join(root, "errors.jsonl"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(event) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


class Store:
    """All manifest operations share a POSIX process lock. Blobs are immutable."""

    def __init__(self, root=DEFAULT_ROOT):
        # A deliberately configured store alias is resolved once. Restore target
        # paths are not resolved: a link there must be refused, never followed.
        self.root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        self.index_path = os.path.join(self.root, "index.jsonl")
        self.config = dict(DEFAULT_CONFIG)
        try:
            with open(os.path.join(self.root, "config.json")) as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("expected an object")
            self.config.update(config)
            for key in ("keep_days", "max_file_mb", "max_dir_files"):
                value = self.config[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("%s must be a finite nonnegative number" % key)
            if type(self.config["max_dir_files"]) is not int:
                raise ValueError("max_dir_files must be an integer")
            for key in ("exclude_dirs", "exclude_names", "exclude_suffixes", "exclude_prefixes"):
                value = self.config[key]
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError("%s must be a list of strings" % key)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, OverflowError) as err:
            raise UndoError("cannot load config in %s: %s" % (self.root, err))

    @contextlib.contextmanager
    def _locked(self):
        fd = None
        try:
            os.makedirs(self.root, exist_ok=True)
            fd = os.open(os.path.join(self.root, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise UndoError("backup store is busy; retry: %s" % self.root)
                    time.sleep(0.02)
            yield
        except OSError as err:
            raise UndoError("backup operation failed: %s" % err) from err
        finally:
            if fd is not None:
                os.close(fd)  # Closing releases the process lock, including on errors.

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

    def _write_index(self, entries):
        fd, temp = tempfile.mkstemp(prefix=".index-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.index_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def save(self, path, tool, session="", now=None):
        with self._locked(), _regular_file(path) as source:
            return self._save_locked(path, source, tool, session, now)

    def _save_locked(self, path, source, tool, session="", now=None):
        entries = self._read_index()
        ids = {e["id"] for e in entries}
        now = now or datetime.datetime.now()
        while True:
            entry_id = now.strftime("%Y%m%d-%H%M%S-%f")
            rel = os.path.join("files", now.strftime("%Y-%m-%d"), "%s__%s" % (entry_id, os.path.basename(path)))
            dest = os.path.join(self.root, rel)
            if entry_id not in ids and not os.path.lexists(dest):
                break
            now += datetime.timedelta(microseconds=1)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".copy-", dir=os.path.dirname(dest))
        installed = False
        try:
            with os.fdopen(fd, "wb") as out:
                size, digest = _copy_stream(source, out)
            entry = {"id": entry_id, "time": now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                     "path": os.path.abspath(path), "backup": rel, "size": size,
                     "tool": tool, "session": session, "format": 2, "sha256": digest}
            os.replace(temp, dest)
            installed = True
            self._write_index(entries + [entry])
            return entry
        except Exception:
            if installed:
                os.unlink(dest)  # Index commit failed; do not advertise this copy.
            raise
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _read_index(self):
        entries, seen = [], set()
        try:
            with open(self.index_path, encoding="utf-8") as f:
                for number, line in enumerate(f, 1):
                    try:
                        e = json.loads(line)
                        if not isinstance(e, dict):
                            raise ValueError("expected an object")
                        for key in ("id", "time", "path", "backup", "tool", "session"):
                            if not isinstance(e.get(key), str):
                                raise ValueError("invalid %s" % key)
                        _parse_time(e["time"])
                        if not os.path.isabs(e["path"]) or type(e.get("size")) is not int or e["size"] < 0:
                            raise ValueError("invalid path or size")
                        if not e["backup"].startswith("files/") or ".." in e["backup"].split("/") or os.path.normpath(e["backup"]) != e["backup"]:
                            raise ValueError("unsafe backup path")
                        if not e["id"] or e["id"] in seen:
                            raise ValueError("empty or duplicate ID")
                        if e.get("format", 1) not in (1, 2):
                            raise ValueError("unsupported entry format")
                        digest = e.get("sha256")
                        if digest is not None or e.get("format") == 2:
                            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                                raise ValueError("invalid or missing sha256")
                        seen.add(e["id"])
                        entries.append(e)
                    except (ValueError, TypeError) as err:
                        raise UndoError("invalid backup index line %d: %s" % (number, err)) from err
        except FileNotFoundError:
            pass
        except UnicodeError as err:
            raise UndoError("invalid backup index encoding: %s" % err) from err
        return entries

    def entries(self, under=None):
        with self._locked():
            items = self._read_index()
        if under:
            under = os.path.abspath(os.path.expanduser(under))
            items = [e for e in items if e["path"] == under or e["path"].startswith(under.rstrip(os.sep) + os.sep)]
        return sorted(items, key=lambda e: e["id"], reverse=True)

    def _find(self, path, entry_id=None):
        path = os.path.abspath(os.path.expanduser(path))
        candidates = [e for e in self._read_index() if e["path"] == path and (not entry_id or e["id"] == entry_id)]
        if not candidates:
            raise UndoError("no saved copy of %s%s" % (path, " with id " + entry_id if entry_id else ""))
        entry = max(candidates, key=lambda e: e["id"])
        return path, entry, os.path.join(self.root, entry["backup"])

    @contextlib.contextmanager
    def _backup(self, source):
        try:
            with _parent_directory(source) as (parent, name, verify):
                with _regular_file(name, dir_fd=parent) as f:
                    verify()
                    yield f
        except FileNotFoundError as err:
            raise UndoError("saved copy is missing: %s" % source) from err

    def _verify_copy(self, entry, size, digest, allow_unverified):
        if size != entry["size"]:
            raise UndoError("corrupted copy %s: size mismatch" % entry["id"])
        if "sha256" in entry:
            if digest != entry["sha256"]:
                raise UndoError("corrupted copy %s: SHA-256 mismatch" % entry["id"])
        elif not allow_unverified:
            raise UndoError("legacy copy %s has no checksum; use --allow-unverified only after independent inspection" % entry["id"])

    def diff(self, path, entry_id=None, allow_unverified=False):
        with self._locked():
            path, entry, source = self._find(path, entry_id)
            with self._backup(source) as f:
                data = f.read()
            self._verify_copy(entry, len(data), hashlib.sha256(data).hexdigest(), allow_unverified)
            before = data.decode("utf-8", errors="replace").splitlines(keepends=True)
            after = []
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    after = f.readlines()
            return "".join(difflib.unified_diff(before, after,
                fromfile="%s (copy %s)" % (path, entry["id"]),
                tofile="%s (now)" % path if os.path.isfile(path) else "%s (deleted)" % path))

    def restore(self, path, entry_id=None, allow_unverified=False):
        # Walk the requested spelling too: normalizing link/../file first would
        # erase a symlink component and could select a different destination.
        requested = os.path.expanduser(path)
        if not os.path.isabs(requested):
            requested = os.path.join(os.getcwd(), requested)
        with self._locked():
            path, entry, source = self._find(path, entry_id)
            with _parent_directory(requested, create=True) as (parent, name, verify):
                state = _destination_state(parent, name)
                temp = ".claude-undo-restore-" + uuid.uuid4().hex
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                try:
                    with os.fdopen(fd, "wb") as staged, self._backup(source) as original:
                        size, digest = _copy_stream(original, staged)
                        info = os.fstat(staged.fileno())
                        staged_identity = (info.st_dev, info.st_ino)
                    self._verify_copy(entry, size, digest, allow_unverified)
                    verify()
                    if _destination_state(parent, name) != state:
                        raise UndoError("restore destination changed while preparing: %s" % path)
                    if state is not None:
                        with _regular_file(name, dir_fd=parent) as current:
                            if _fingerprint(os.fstat(current.fileno())) != state:
                                raise UndoError("restore destination changed before saving: %s" % path)
                            self._save_locked(path, current, "restore")
                    verify()
                    if _destination_state(parent, name) != state:
                        raise UndoError("restore destination changed before commit: %s" % path)
                    os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                    verify()
                    final = _destination_state(parent, name)
                    if final is None or final[:2] != staged_identity:
                        raise UndoError("restore destination changed after commit: %s" % path)
                finally:
                    try:
                        os.unlink(temp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            return entry

    def clean(self, keep_days=None, now=None):
        with self._locked():
            return self._clean_locked(keep_days, now)

    def _clean_locked(self, keep_days=None, now=None):
        days = self.config["keep_days"] if keep_days is None else keep_days
        if type(days) not in (int, float) or not math.isfinite(days) or days < 0:
            raise UndoError("cleanup days must be finite and nonnegative")
        cutoff = (now or datetime.datetime.now()) - datetime.timedelta(days=days)
        items = self._read_index()
        expired = [e for e in items if _parse_time(e["time"]) < cutoff]
        if not expired:
            return 0
        # Commit discovery first. An interrupted unlink leaves an orphan rather
        # than destroying a still-indexed copy. All writers hold the same lock.
        self._write_index([e for e in items if _parse_time(e["time"]) >= cutoff])
        failures = []
        for e in expired:
            try:
                source = os.path.join(self.root, e["backup"])
                with _parent_directory(source) as (parent, name, verify):
                    verify()
                    os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass
            except (OSError, UndoError) as err:
                failures.append(str(err))
        if failures:
            raise UndoError("cleanup index committed, but expired files could not be deleted: %s" % "; ".join(failures))
        return len(expired)

    def clean_daily(self):
        with self._locked():
            stamp = os.path.join(self.root, ".last-clean")
            today = datetime.date.today().isoformat()
            try:
                with open(stamp) as f:
                    if f.read().strip() == today:
                        return
            except FileNotFoundError:
                pass
            self._clean_locked()
            with open(stamp, "w") as f:
                f.write(today)

    def status(self):
        """Report recorded errors, not a claim that every tool call was covered."""
        with self._locked():
            self._read_index()
            try:
                with open(os.path.join(self.root, "errors.jsonl")) as f:
                    return f.readlines()[-30:]
            except FileNotFoundError:
                return []


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
            except (OSError, UndoError) as err:
                _record_error(store.root, "save " + p, err)
                continue
        return saved
    except Exception as err:
        _record_error(store.root, "snapshot", err)
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
    if argv[:1] == ["hook"]:
        try:
            store = Store(root or DEFAULT_ROOT)
            text = sys.stdin.read() if stdin_text is None else stdin_text
            snapshot_from_payload(json.loads(text), store)
            store.clean_daily()
        except Exception as err:
            _record_error(root or DEFAULT_ROOT, "hook", err)
        return 0

    parser = argparse.ArgumentParser(prog="claude-undo", description="Undo history for files Claude Code changes.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--settings", default=DEFAULT_SETTINGS)
    p = sub.add_parser("list")
    p.add_argument("path", nargs="?")
    p.add_argument("-n", type=int, default=30, help="how many to show (default 30)")
    for name in ("restore", "diff"):
        p = sub.add_parser(name)
        p.add_argument("path")
        p.add_argument("--id")
        p.add_argument("--allow-unverified", action="store_true", help="allow legacy copies without a checksum; size is still checked")
    p = sub.add_parser("clean")
    p.add_argument("--days", type=int)
    sub.add_parser("status", help="show recent recorded hook errors (not a coverage guarantee)")
    args = parser.parse_args(argv)
    try:
        store = Store(root or DEFAULT_ROOT)
        return _run_command(args, store)
    except (UndoError, OSError, ValueError, OverflowError) as err:
        print("claude-undo: %s" % err, file=sys.stderr)
        return 1


def _run_command(args, store):
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
        e = store.restore(args.path, args.id, args.allow_unverified)
        if "sha256" not in e:
            print("Warning: legacy copy has no checksum; only size was verified.", file=sys.stderr)
        print("Restored %s from the copy taken %s (%s)." % (e["path"], e["time"][:19].replace("T", " "), e["id"]))
    elif args.cmd == "diff":
        out = store.diff(args.path, args.id, args.allow_unverified)
        if args.allow_unverified:
            print("Warning: --allow-unverified permits legacy copies without checksum verification.", file=sys.stderr)
        print(out if out else "No changes since that copy.", end="" if out else "\n")
    elif args.cmd == "status":
        events = store.status()
        print("Recent recorded hook errors in %s (history, not current health):" % store.root)
        print("".join(events) if events else "No recorded errors. This does not verify backup coverage.", end="" if events else "\n")
        if events:
            return 1
    elif args.cmd == "clean":
        removed = store.clean(args.days)
        print("Removed %d copies older than %d days." % (removed, args.days if args.days is not None else store.config["keep_days"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
