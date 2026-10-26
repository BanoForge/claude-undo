"""Tests for claude_undo. Run from the repo root: python3 -m unittest discover -s tests -v"""

import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_undo as cu  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.tmp, "store")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        # The test folders live under the system temp dir, which the default
        # config skips on purpose, so tests switch that exclusion off.
        os.makedirs(self.root)
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"exclude_prefixes": []}, f)
        self.store = cu.Store(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make(self, rel, text="original"):
        path = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, path):
        with open(path) as f:
            return f.read()

    def payload(self, tool, **tool_input):
        return {"tool_name": tool, "tool_input": tool_input, "cwd": self.work, "session_id": "s1"}


class SnapshotTests(Base):
    def test_edit_payload_saves_a_copy(self):
        path = self.make("notes.md")
        saved = cu.snapshot_from_payload(self.payload("Edit", file_path=path), self.store)
        self.assertEqual(saved, [path])
        entry = self.store.entries()[0]
        self.assertEqual(entry["path"], path)
        self.assertEqual(self.read(os.path.join(self.root, entry["backup"])), "original")

    def test_notebook_edit_uses_notebook_path(self):
        path = self.make("a.ipynb", "{}")
        saved = cu.snapshot_from_payload(self.payload("NotebookEdit", notebook_path=path), self.store)
        self.assertEqual(saved, [path])

    def test_write_to_new_file_saves_nothing(self):
        path = os.path.join(self.work, "new.md")
        self.assertEqual(cu.snapshot_from_payload(self.payload("Write", file_path=path), self.store), [])
        self.assertEqual(self.store.entries(), [])

    def test_excluded_dir_is_skipped(self):
        path = self.make("node_modules/x.js")
        self.assertEqual(cu.snapshot_from_payload(self.payload("Edit", file_path=path), self.store), [])

    def test_temp_dirs_are_skipped_by_default(self):
        os.remove(os.path.join(self.root, "config.json"))
        store = cu.Store(self.root)
        path = self.make("notes.md")  # self.work is under the system temp dir
        self.assertEqual(cu.snapshot_from_payload(self.payload("Edit", file_path=path), store), [])

    def test_file_over_size_limit_is_skipped(self):
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"exclude_prefixes": [], "max_file_mb": 0.000001}, f)
        store = cu.Store(self.root)
        path = self.make("big.txt", "x" * 100)
        self.assertEqual(cu.snapshot_from_payload(self.payload("Edit", file_path=path), store), [])

    def test_bad_payload_never_raises(self):
        self.assertEqual(cu.snapshot_from_payload({"tool_name": "Edit"}, self.store), [])
        self.assertEqual(cu.snapshot_from_payload({}, self.store), [])

    def test_bash_payload_saves_named_files(self):
        path = self.make("draft.md")
        saved = cu.snapshot_from_payload(self.payload("Bash", command="rm draft.md"), self.store)
        self.assertEqual(saved, [path])


class BashPathTests(Base):
    def paths(self, command):
        return sorted(cu.paths_from_bash(command, self.work))

    def test_rm_names_its_files(self):
        a, b = self.make("a.txt"), self.make("b.txt")
        self.assertEqual(self.paths("rm -f a.txt b.txt"), [a, b])

    def test_rm_recursive_includes_files_in_dir(self):
        a = self.make("dir/a.txt")
        self.assertEqual(self.paths("rm -rf dir"), [a])

    def test_rm_without_recursive_ignores_dirs(self):
        self.make("dir/a.txt")
        self.assertEqual(self.paths("rm dir"), [])

    def test_sed_in_place_names_files_not_script(self):
        a = self.make("a.txt")
        self.assertEqual(self.paths("sed -i '' 's/x/y/' a.txt"), [a])
        self.assertEqual(self.paths("sed -i.bak -e 's/x/y/' a.txt"), [a])

    def test_sed_without_in_place_is_ignored(self):
        self.make("a.txt")
        self.assertEqual(self.paths("sed 's/x/y/' a.txt"), [])

    def test_redirect_overwrite_names_target(self):
        a = self.make("a.txt")
        self.assertEqual(self.paths("echo hi > a.txt"), [a])

    def test_append_redirect_is_ignored(self):
        self.make("a.txt")
        self.assertEqual(self.paths("echo hi >> a.txt"), [])

    def test_mv_onto_existing_file_names_target(self):
        a, b = self.make("a.txt"), self.make("b.txt")
        self.assertEqual(self.paths("mv a.txt b.txt"), [a, b])

    def test_mv_into_dir_names_file_it_replaces(self):
        a = self.make("a.txt")
        old = self.make("dir/a.txt")
        self.assertEqual(self.paths("mv a.txt dir"), [a, old])

    def test_cp_names_only_the_target(self):
        self.make("a.txt")
        b = self.make("b.txt")
        self.assertEqual(self.paths("cp a.txt b.txt"), [b])

    def test_cd_changes_where_paths_resolve(self):
        a = self.make("sub/a.txt")
        self.assertEqual(self.paths("cd sub && rm a.txt"), [a])

    def test_glob_is_expanded(self):
        a, b = self.make("a.md"), self.make("b.md")
        self.make("c.txt")
        self.assertEqual(self.paths("rm *.md"), [a, b])

    def test_harmless_command_names_nothing(self):
        self.make("a.txt")
        self.assertEqual(self.paths("cat a.txt | grep x; ls -la"), [])

    def test_unparseable_command_names_nothing(self):
        self.assertEqual(self.paths("echo 'unterminated"), [])


class RestoreTests(Base):
    def test_restore_brings_back_latest_copy(self):
        path = self.make("notes.md", "v1")
        self.store.save(path, "Edit")
        with open(path, "w") as f:
            f.write("v2 broken")
        self.store.restore(path)
        self.assertEqual(self.read(path), "v1")

    def test_restore_recreates_deleted_file(self):
        path = self.make("notes.md", "keep me")
        self.store.save(path, "Bash")
        os.remove(path)
        self.store.restore(path)
        self.assertEqual(self.read(path), "keep me")

    def test_restore_is_itself_undoable(self):
        path = self.make("notes.md", "v1")
        self.store.save(path, "Edit")
        with open(path, "w") as f:
            f.write("v2")
        self.store.restore(path)
        self.store.restore(path)  # latest copy is now v2, saved before the first restore
        self.assertEqual(self.read(path), "v2")

    def test_restore_by_id_picks_that_copy(self):
        path = self.make("notes.md", "v1")
        first = self.store.save(path, "Edit")
        with open(path, "w") as f:
            f.write("v2")
        self.store.save(path, "Edit")
        self.store.restore(path, entry_id=first["id"])
        self.assertEqual(self.read(path), "v1")

    def test_restore_unknown_path_raises(self):
        with self.assertRaises(cu.UndoError):
            self.store.restore(os.path.join(self.work, "never.md"))

    def test_entries_filter_by_folder_newest_first(self):
        a = self.make("x/a.md")
        b = self.make("y/b.md")
        self.store.save(a, "Edit")
        self.store.save(b, "Edit")
        self.store.save(a, "Edit")
        got = self.store.entries(os.path.join(self.work, "x"))
        self.assertEqual([e["path"] for e in got], [a, a])
        self.assertGreater(got[0]["id"], got[1]["id"])


class DiffTests(Base):
    def test_diff_shows_removed_and_added_lines(self):
        path = self.make("notes.md", "keep\nold line\n")
        self.store.save(path, "Edit")
        with open(path, "w") as f:
            f.write("keep\nnew line\n")
        out = self.store.diff(path)
        self.assertIn("-old line", out)
        self.assertIn("+new line", out)

    def test_diff_of_deleted_file_shows_all_lines_removed(self):
        path = self.make("notes.md", "only line\n")
        self.store.save(path, "Bash")
        os.remove(path)
        self.assertIn("-only line", self.store.diff(path))

    def test_diff_without_changes_says_identical(self):
        path = self.make("notes.md", "same\n")
        self.store.save(path, "Edit")
        self.assertEqual(self.store.diff(path), "")

    def test_diff_unknown_path_raises(self):
        with self.assertRaises(cu.UndoError):
            self.store.diff(os.path.join(self.work, "never.md"))


class CleanTests(Base):
    def test_clean_removes_old_copies_only(self):
        path = self.make("notes.md")
        start = datetime.datetime(2026, 1, 1, 12, 0, 0)
        old = self.store.save(path, "Edit", now=start)
        new = self.store.save(path, "Edit", now=start + datetime.timedelta(days=80))
        removed = self.store.clean(keep_days=90, now=start + datetime.timedelta(days=91))
        self.assertEqual(removed, 1)
        self.assertEqual([e["id"] for e in self.store.entries()], [new["id"]])
        self.assertFalse(os.path.exists(os.path.join(self.root, old["backup"])))


class CliTests(Base):
    def setUp(self):
        super().setUp()
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_hook_reads_stdin_and_always_exits_zero(self):
        path = self.make("notes.md")
        code = cu.main(["hook"], stdin_text=json.dumps(self.payload("Edit", file_path=path)), root=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.store.entries()), 1)
        self.assertEqual(cu.main(["hook"], stdin_text="not json", root=self.root), 0)

    def test_install_adds_hook_once_and_keeps_other_settings(self):
        settings = os.path.join(self.tmp, "settings.json")
        with open(settings, "w") as f:
            json.dump({"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}, f)
        cu.main(["install", "--settings", settings], root=self.root)
        cu.main(["install", "--settings", settings], root=self.root)
        with open(settings) as f:
            data = json.load(f)
        self.assertEqual(data["model"], "opus")
        ours = [m for m in data["hooks"]["PreToolUse"] if "claude_undo.py" in json.dumps(m)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
        self.assertTrue(os.path.exists(settings + ".before-claude-undo"))

    def test_diff_command_prints_changes_and_fails_on_unknown_file(self):
        path = self.make("notes.md", "old\n")
        self.store.save(path, "Edit")
        with open(path, "w") as f:
            f.write("new\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cu.main(["diff", path], root=self.root)
        self.assertEqual(code, 0)
        self.assertIn("+new", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cu.main(["diff", os.path.join(self.work, "never.md")], root=self.root), 1)

    def test_installed_command_never_blocks_when_script_is_gone(self):
        # Exit code 2 from a PreToolUse hook blocks the tool call, and python
        # exits 2 when its script file is missing, e.g. after moving the repo.
        settings = os.path.join(self.tmp, "settings.json")
        cu.main(["install", "--settings", settings], root=self.root)
        with open(settings) as f:
            command = json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        moved = command.replace(os.path.abspath(cu.__file__), os.path.join(self.tmp, "moved", "claude_undo.py"))
        self.assertNotEqual(moved, command)
        result = subprocess.run(moved, shell=True, input=b"{}", capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_install_keeps_non_ascii_text_readable(self):
        settings = os.path.join(self.tmp, "settings.json")
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"note": "Straße — ok"}, f, ensure_ascii=False)
        cu.main(["install", "--settings", settings], root=self.root)
        with open(settings, encoding="utf-8") as f:
            self.assertIn("Straße — ok", f.read())

    def test_uninstall_removes_only_our_hook(self):
        settings = os.path.join(self.tmp, "settings.json")
        with open(settings, "w") as f:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}, f)
        cu.main(["install", "--settings", settings], root=self.root)
        cu.main(["uninstall", "--settings", settings], root=self.root)
        with open(settings) as f:
            data = json.load(f)
        self.assertEqual(data["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": []}])


if __name__ == "__main__":
    unittest.main()
