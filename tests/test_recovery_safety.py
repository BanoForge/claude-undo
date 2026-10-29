"""Safety regressions: assert recovery guarantees, not historical bug behavior."""
import contextlib
import datetime
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from test_claude_undo import Base
import claude_undo as cu


def concurrent_worker(root, path, action, entered, release, attempted, finished, connection):
    """Pause after reading the manifest, while the repaired Store holds its lock."""
    try:
        store = cu.Store(root)
        if entered is not None:
            original = store._read_index
            first = [True]
            def paused_read():
                entries = original()
                if first[0]:
                    first[0] = False
                    entered.set()
                    if not release.wait(8):
                        raise RuntimeError('test synchronization timed out')
                return entries
            store._read_index = paused_read
        attempted.set()
        if action == 'clean':
            result = store.clean(now=datetime.datetime(2026, 6, 1))
        else:
            result = store.save(path, 'Edit', now=datetime.datetime(2026, 6, 1))['id']
        connection.send(('ok', result))
    except BaseException as err:
        connection.send(('error', repr(err)))
    finally:
        finished.set()
        connection.close()


def batch_writer(root, path, connection):
    try:
        store = cu.Store(root)
        ids = [store.save(path, 'Edit', now=datetime.datetime(2026, 6, 1))['id'] for _ in range(4)]
        connection.send(('ok', ids))
    except BaseException as err:
        connection.send(('error', repr(err)))
    finally:
        connection.close()


class SafeRestoreTests(Base):
    def seed(self, name='a', value='original'):
        path = self.make(name, value)
        entry = self.store.save(path, 'Edit')
        Path(path).write_text('current')
        return Path(path), entry

    def assert_refused(self, path, message=None):
        before = len(self.store.entries())
        with self.assertRaises(cu.UndoError) as err:
            self.store.restore(str(path))
        if message:
            self.assertIn(message, str(err.exception))
        self.assertEqual(len(self.store.entries()), before)
        return str(err.exception)

    def test_replacement_symlink_leaves_referent_and_history_untouched(self):
        path, _ = self.seed()
        other = Path(self.make('other', 'unrelated'))
        path.unlink(); path.symlink_to(other)
        self.assert_refused(path, 'unsafe restore destination')
        self.assertTrue(path.is_symlink())
        self.assertEqual(other.read_text(), 'unrelated')

    def test_dangling_symlink_does_not_create_referent(self):
        path, _ = self.seed()
        other = Path(self.work) / 'absent'
        path.unlink(); path.symlink_to(other)
        self.assert_refused(path)
        self.assertTrue(path.is_symlink())
        self.assertFalse(other.exists())

    def test_symlinked_parent_directory_is_refused(self):
        path, _ = self.seed('dir/a')
        path.unlink(); path.parent.rmdir()
        other = Path(self.work) / 'other'; other.mkdir()
        (other / 'a').write_text('unrelated')
        path.parent.symlink_to(other, target_is_directory=True)
        self.assert_refused(path, 'parent directory')
        self.assertEqual((other / 'a').read_text(), 'unrelated')
        self.assertEqual(sorted(p.name for p in other.iterdir()), ['a'])

    def test_hard_link_destination_does_not_change_peer(self):
        path, _ = self.seed()
        other = Path(self.make('other', 'unrelated'))
        path.unlink(); os.link(other, path)
        self.assert_refused(path, 'hard links')
        self.assertEqual(path.read_text(), 'unrelated')
        self.assertEqual(other.read_text(), 'unrelated')
        self.assertEqual(path.stat().st_ino, other.stat().st_ino)

    def test_parent_traversal_cannot_hide_a_symlink_component(self):
        path, _ = self.seed()
        other = Path(self.work) / 'other'; other.mkdir()
        alias = Path(self.work) / 'alias'; alias.symlink_to(other, target_is_directory=True)
        self.assert_refused(str(alias) + '/../a', 'parent directory')
        self.assertEqual(path.read_text(), 'current')
        self.assertEqual(list(other.iterdir()), [])

    def test_directory_target_has_no_false_success_or_extra_file(self):
        path, _ = self.seed(); path.unlink(); path.mkdir()
        self.assert_refused(path, 'directories')
        self.assertEqual(list(path.iterdir()), [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cu.main(['restore', str(path)], root=self.root)
        self.assertEqual(result, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('unsafe restore destination', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

    def test_fifo_destination_is_refused_without_blocking(self):
        path, _ = self.seed(); path.unlink(); os.mkfifo(path)
        self.assert_refused(path)

    def test_deleted_nested_file_is_recreated_with_mode_and_bytes(self):
        path = Path(self.make('dir/sub/a'))
        original = b'original\x00\xff\n'
        path.write_bytes(original); path.chmod(0o640)
        self.store.save(str(path), 'Edit')
        path.unlink(); path.parent.rmdir(); path.parent.parent.rmdir()
        self.store.restore(str(path))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_destination_replaced_during_staging_is_not_overwritten(self):
        path, _ = self.seed()
        other = Path(self.make('other', 'unrelated'))
        copy = cu._copy_stream
        def replace_with_link(source, dest):
            result = copy(source, dest)
            path.unlink(); path.symlink_to(other)
            return result
        with mock.patch.object(cu, '_copy_stream', side_effect=replace_with_link):
            self.assert_refused(path)
        self.assertEqual(other.read_text(), 'unrelated')
        self.assertTrue(path.is_symlink())
        self.assertEqual(list(Path(self.work).glob('.claude-undo-restore-*')), [])

    def test_parent_replaced_during_staging_never_follows_new_link(self):
        path, _ = self.seed('dir/a')
        other = Path(self.work) / 'other'; other.mkdir()
        (other/'a').write_text('unrelated')
        moved = Path(self.work) / 'moved'
        copy = cu._copy_stream
        def replace_parent(source, dest):
            result = copy(source, dest)
            path.parent.rename(moved)
            path.parent.symlink_to(other, target_is_directory=True)
            return result
        with mock.patch.object(cu, '_copy_stream', side_effect=replace_parent):
            self.assert_refused(path, 'parent directory changed')
        self.assertEqual((other/'a').read_text(), 'unrelated')
        self.assertEqual((moved/'a').read_text(), 'current')
        self.assertEqual(sorted(p.name for p in moved.iterdir()), ['a'])

    def test_changed_regular_destination_is_preserved(self):
        path, _ = self.seed()
        copy = cu._copy_stream
        def change_current(source, dest):
            result = copy(source, dest)
            path.write_text('concurrent edit')
            return result
        with mock.patch.object(cu, '_copy_stream', side_effect=change_current):
            self.assert_refused(path, 'destination changed')
        self.assertEqual(path.read_text(), 'concurrent edit')

    def test_atomic_restore_does_not_write_through_old_open_inode(self):
        path, _ = self.seed()
        with path.open('rb') as old_descriptor:
            self.store.restore(str(path))
            self.assertEqual(old_descriptor.read(), b'current')
        self.assertEqual(path.read_text(), 'original')
        self.store.restore(str(path))
        self.assertEqual(path.read_text(), 'current')

    def test_changed_destination_after_commit_does_not_report_success(self):
        path, _ = self.seed()
        replace = cu.os.replace
        def external_replacement(src, dst, **kwargs):
            result = replace(src, dst, **kwargs)
            if 'dst_dir_fd' in kwargs:
                path.unlink(); path.mkdir()
            return result
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cu.os, 'replace', side_effect=external_replacement):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cu.main(['restore', str(path)], root=self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('unsafe restore destination', err.getvalue())
        self.assertEqual(list(path.iterdir()), [])


class IntegrityBase(Base):
    seed = SafeRestoreTests.seed
    assert_refused = SafeRestoreTests.assert_refused


class IntegrityTests(IntegrityBase):
    def test_new_snapshot_records_checksum_and_format(self):
        path, entry = self.seed()
        self.assertEqual(entry['format'], 2)
        self.assertEqual(entry['sha256'], hashlib.sha256(b'original').hexdigest())
        self.assertEqual(entry['size'], 8)

    def test_missing_backup_fails_clearly_and_preserves_current(self):
        path, entry = self.seed()
        (Path(self.root)/entry['backup']).unlink()
        self.assert_refused(path, 'missing')
        self.assertEqual(path.read_text(), 'current')

    def test_same_size_corruption_fails_checksum_without_touching_target(self):
        path, entry = self.seed()
        (Path(self.root)/entry['backup']).write_text('CORRUPT!')
        self.assert_refused(path, 'SHA-256 mismatch')
        self.assertEqual(path.read_text(), 'current')
        with self.assertRaisesRegex(cu.UndoError, 'SHA-256 mismatch'):
            self.store.diff(str(path))

    def test_truncated_backup_fails_size_even_with_override(self):
        path, entry = self.seed()
        (Path(self.root)/entry['backup']).write_text('short')
        with self.assertRaisesRegex(cu.UndoError, 'size mismatch'):
            self.store.restore(str(path), allow_unverified=True)
        self.assertEqual(path.read_text(), 'current')

    def test_symlink_backup_blob_is_rejected(self):
        path, entry = self.seed()
        blob = Path(self.root)/entry['backup']; blob.unlink(); blob.symlink_to(path)
        self.assert_refused(path)
        self.assertEqual(path.read_text(), 'current')

    def test_legacy_requires_explicit_opt_in_and_still_checks_size(self):
        path, entry = self.seed()
        del entry['sha256']; del entry['format']
        Path(self.store.index_path).write_text(json.dumps(entry)+'\n')
        self.assert_refused(path, 'no checksum')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cu.main(['restore', str(path), '--allow-unverified'], root=self.root)
        self.assertEqual(code, 0)
        self.assertIn('only size was verified', err.getvalue())
        self.assertEqual(path.read_text(), 'original')
        self.assertNotIn('sha256', self.store.entries()[-1])

    def test_modern_record_missing_digest_is_not_downgraded_to_legacy(self):
        path, entry = self.seed(); del entry['sha256']
        Path(self.store.index_path).write_text(json.dumps(entry)+'\n')
        with self.assertRaisesRegex(cu.UndoError, 'invalid or missing sha256'):
            self.store.restore(str(path), allow_unverified=True)
        self.assertEqual(path.read_text(), 'current')

    def test_failed_staging_write_leaves_current_file_intact(self):
        path, _ = self.seed()
        def interrupted(source, dest):
            dest.write(b'partial')
            raise OSError('injected disk full')
        with mock.patch.object(cu, '_copy_stream', side_effect=interrupted):
            self.assert_refused(path, 'disk full')
        self.assertEqual(path.read_text(), 'current')
        self.assertEqual(list(Path(self.work).glob('.claude-undo-restore-*')), [])

    def test_failed_staging_write_does_not_create_deleted_target(self):
        path, _ = self.seed(); path.unlink()
        with mock.patch.object(cu, '_copy_stream', side_effect=OSError('injected write failure')):
            self.assert_refused(path)
        self.assertFalse(path.exists())
        self.assertEqual(list(Path(self.work).glob('.claude-undo-restore-*')), [])

    def test_pre_restore_save_failure_leaves_destination_intact(self):
        path, _ = self.seed()
        with mock.patch.object(self.store, '_write_index', side_effect=OSError('injected manifest write failure')):
            self.assert_refused(path, 'manifest write failure')
        self.assertEqual(path.read_text(), 'current')
        self.assertEqual(len(list(Path(self.root).glob('files/*/*'))), 1)

    def test_failed_atomic_replace_keeps_current_and_reports_no_success(self):
        path, _ = self.seed()
        replace = cu.os.replace
        def fail_destination(src, dst, **kwargs):
            if 'dst_dir_fd' in kwargs:
                raise OSError('injected replacement failure')
            return replace(src, dst, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cu.os, 'replace', side_effect=fail_destination):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cu.main(['restore', str(path)], root=self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('replacement failure', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        self.assertEqual(path.read_text(), 'current')
        self.assertEqual(list(Path(self.work).glob('.claude-undo-restore-*')), [])
        self.assertEqual(len(self.store.entries()), 2)  # Current preimage remains recoverable.
        self.store.restore(str(path), self.store.entries()[-1]['id'])
        self.assertEqual(path.read_text(), 'original')

    def test_failed_save_copy_does_not_publish_partial_snapshot(self):
        path = self.make('a')
        with mock.patch.object(cu, '_copy_stream', side_effect=OSError('injected copy failure')):
            with self.assertRaises(cu.UndoError):
                self.store.save(path, 'Edit')
        self.assertEqual(self.store.entries(), [])
        self.assertEqual(list(Path(self.root).glob('files/*/*')), [])

    def test_failed_manifest_commit_keeps_older_copy_and_removes_new_blob(self):
        path, entry = self.seed()
        original_index = Path(self.store.index_path).read_bytes()
        replace = cu.os.replace
        def fail_index(src, dst, **kwargs):
            if dst == self.store.index_path:
                raise OSError('injected index commit failure')
            return replace(src, dst, **kwargs)
        with mock.patch.object(cu.os, 'replace', side_effect=fail_index):
            with self.assertRaisesRegex(cu.UndoError, 'index commit failure'):
                self.store.save(str(path), 'Edit')
        self.assertEqual(Path(self.store.index_path).read_bytes(), original_index)
        self.assertEqual(len(list(Path(self.root).glob('files/*/*'))), 1)
        self.assertEqual(list(Path(self.root).glob('.index-*')), [])
        self.store.restore(str(path), entry['id'])
        self.assertEqual(path.read_text(), 'original')

    def test_fsync_failure_never_replaces_target(self):
        path, _ = self.seed()
        with mock.patch.object(cu.os, 'fsync', side_effect=OSError('injected fsync failure')):
            self.assert_refused(path, 'fsync failure')
        self.assertEqual(path.read_text(), 'current')

    def test_index_schema_error_is_clear_and_preserves_manifest_and_target(self):
        path, _ = self.seed()
        with open(self.store.index_path, 'a') as f:f.write('{}\n')
        data = Path(self.store.index_path).read_bytes()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cu.main(['restore', str(path)], root=self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('invalid backup index line 2', err.getvalue())
        self.assertEqual(Path(self.store.index_path).read_bytes(), data)
        self.assertEqual(path.read_text(), 'current')

    def test_failed_hook_copy_is_quiet_but_recorded(self):
        path = self.make('a')
        out = io.StringIO()
        with mock.patch.object(cu, '_copy_stream', side_effect=OSError('injected copy failure')):
            with contextlib.redirect_stdout(out):
                code = cu.main(['hook'], stdin_text=json.dumps(self.payload('Edit', file_path=path)), root=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(self.store.entries(), [])
        self.assertIn('copy failure', ''.join(self.store.status()))
        with contextlib.redirect_stdout(io.StringIO()) as status:
            self.assertEqual(cu.main(['status'], root=self.root), 1)
        self.assertIn('history, not current health', status.getvalue())

    def test_invalid_config_is_quiet_for_hook_and_clear_for_cli(self):
        for data in ['null', '{bad', '{"keep_days":-1}']:
            Path(self.root, 'config.json').write_text(data)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cu.main(['hook'], stdin_text='{}', root=self.root), 0)
                self.assertEqual(cu.main(['list'], root=self.root), 1)
            self.assertEqual(out.getvalue(), '')
            self.assertIn('cannot load config', err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())

    def test_clean_manifest_failure_does_not_delete_any_copy(self):
        path = self.make('a')
        old = self.store.save(path, 'Edit', now=datetime.datetime(2020, 1, 1))
        with mock.patch.object(self.store, '_write_index', side_effect=OSError('injected disk full')):
            with self.assertRaisesRegex(cu.UndoError, 'disk full'):
                self.store.clean()
        self.assertEqual(self.store.entries(), [old])
        self.assertTrue((Path(self.root)/old['backup']).exists())

    def test_clean_unlink_failure_is_reported_without_success_message(self):
        path = self.make('a')
        old = self.store.save(path, 'Edit', now=datetime.datetime(2020, 1, 1))
        unlink = cu.os.unlink
        def fail_blob(name, **kwargs):
            if name == os.path.basename(old['backup']):
                raise PermissionError('injected delete failure')
            return unlink(name, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cu.os, 'unlink', side_effect=fail_blob):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cu.main(['clean'], root=self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('index committed', err.getvalue())
        self.assertTrue((Path(self.root)/old['backup']).exists())


class ConcurrentStoreTests(Base):
    def test_clean_then_save_serializes_and_new_backup_is_restorable(self):
        self.run_interleaving('clean', 'save')

    def test_save_then_clean_serializes_and_new_backup_is_restorable(self):
        self.run_interleaving('save', 'clean')

    def run_interleaving(self, first_action, second_action):
        old_path = self.make('old')
        new_path = self.make('new', 'new content')
        self.store.save(old_path, 'Edit', now=datetime.datetime(2020, 1, 1))
        ctx = multiprocessing.get_context('spawn')
        entered, release = ctx.Event(), ctx.Event()
        attempts = [ctx.Event(), ctx.Event()]
        finishes = [ctx.Event(), ctx.Event()]
        pipes = [ctx.Pipe(False), ctx.Pipe(False)]
        processes = []
        for i, action in enumerate([first_action, second_action]):
            p = ctx.Process(target=concurrent_worker, args=(self.root, new_path, action,
                entered if i == 0 else None, release, attempts[i], finishes[i], pipes[i][1]))
            processes.append(p)
        try:
            processes[0].start(); pipes[0][1].close()
            self.assertTrue(entered.wait(5), 'first worker did not reach manifest read')
            processes[1].start(); pipes[1][1].close()
            self.assertTrue(attempts[1].wait(5), 'second worker did not attempt operation')
            self.assertFalse(finishes[1].wait(0.25), 'second operation escaped the shared store lock')
        finally:
            release.set()
            for p in processes:
                if p.pid is not None:
                    p.join(10)
                    if p.is_alive():p.terminate();p.join()
        for p, (reader, _) in zip(processes, pipes):
            self.assertEqual(p.exitcode, 0)
            self.assertTrue(reader.poll(1))
            outcome = reader.recv(); reader.close()
            self.assertEqual(outcome[0], 'ok', outcome)
        entries = self.store.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['path'], new_path)
        os.remove(new_path)
        self.store.restore(new_path)
        self.assertEqual(self.read(new_path), 'new content')

    def test_simultaneous_saves_keep_unique_ids_and_every_file_recoverable(self):
        ctx = multiprocessing.get_context('spawn')
        paths = [self.make('p%d/a'%i, 'content%d'%i) for i in range(4)]
        workers, readers = [], []
        try:
            for path in paths:
                reader, writer = ctx.Pipe(False)
                p = ctx.Process(target=batch_writer, args=(self.root, path, writer))
                p.start(); writer.close(); workers.append(p); readers.append(reader)
            for p, reader in zip(workers, readers):
                p.join(10);self.assertFalse(p.is_alive())
                self.assertEqual(p.exitcode, 0)
                self.assertTrue(reader.poll(1))
                result=reader.recv();self.assertEqual(result[0], 'ok', result)
        finally:
            for p in workers:
                if p.is_alive():p.terminate();p.join()
            for reader in readers:reader.close()
        entries = self.store.entries()
        self.assertEqual(len(entries), 16)
        self.assertEqual(len({e['id'] for e in entries}), 16)
        self.assertEqual(len({e['backup'] for e in entries}), 16)
        for i, path in enumerate(paths):
            os.remove(path);self.store.restore(path)
            self.assertEqual(self.read(path), 'content%d'%i)


class CliRecoveryIntegrationTests(Base):
    def test_installed_hook_and_real_bash_mutation_restore_original_bytes(self):
        settings = Path(self.tmp)/'settings.json'
        env = dict(os.environ, CLAUDE_UNDO_DIR=self.root, PYTHONDONTWRITEBYTECODE='1')
        def cli(*args, payload=None):
            return subprocess.run([sys.executable, cu.__file__]+list(args), input=payload,
                text=True, capture_output=True, cwd=self.work, env=env, timeout=15)
        self.assertEqual(cli('install', '--settings', str(settings)).returncode, 0)
        command = json.loads(settings.read_text())['hooks']['PreToolUse'][0]['hooks'][0]['command']
        a, b, c = [self.make(name, 'v1\n') for name in ['a.md','b.md','c.md']]
        def hook(payload):
            run=subprocess.run(command, shell=True, input=json.dumps(payload), text=True,
                capture_output=True, cwd=self.work, env=env, timeout=15)
            self.assertEqual((run.returncode,run.stdout,run.stderr), (0,'',''))
        hook(self.payload('Edit',file_path=a))
        Path(a).write_text('v2\n')  # File-tool dispatch itself is the user's live check.
        shell="sed -i '' 's/v1/v2/' b.md && rm c.md" if sys.platform=='darwin' else "sed -i 's/v1/v2/' b.md && rm c.md"
        hook(self.payload('Bash',command=shell))
        result=subprocess.run(['/bin/bash','-c',shell],cwd=self.work,env=env,capture_output=True)
        self.assertEqual(result.returncode,0)
        self.assertEqual(len(self.store.entries()),3)
        for path in [a,b,c]:
            result=cli('restore',path)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(Path(path).read_bytes(),b'v1\n')
        self.assertEqual(cli('status').returncode,0)
