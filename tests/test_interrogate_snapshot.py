from pathlib import Path
import tempfile
import unittest

from workbench import cli, interrogate_snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        for name, content in {
            'original.txt': 'baseline\n', 'deleted.txt': 'remove me\n', 'untouched.txt': 'keep me\n',
            '.gitignore': 'ignored.txt\n',
        }.items():
            (self.repo / name).write_text(content)
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.baseline = cli.sha(self.repo, 'HEAD')
        self.out = self.root / 'snapshot.json'

    def _freeze(self, files=None):
        return interrogate_snapshot.freeze(repo=self.repo, baseline=self.baseline, files=files, out=self.out)

    @staticmethod
    def _reference(result):
        return {'path': result['snapshot'], 'sha256': result['sha256']}

    def _read_source(self):
        index = cli.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
        return (cli.git(self.repo, 'status', '--porcelain=v1', '--untracked-files=all'),
                cli.git(self.repo, 'symbolic-ref', 'HEAD'), cli.sha(self.repo, 'HEAD'), Path(index).read_bytes())

    def test_captures_visible_bytes_including_add_delete_and_mode_without_touching_source(self):
        (self.repo / 'original.txt').write_text('staged\n')
        (self.repo / 'untouched.txt').write_text('excluded staged edit\n')
        cli.git(self.repo, 'add', 'original.txt', 'untouched.txt')
        (self.repo / 'original.txt').write_text('visible bytes\n')
        (self.repo / 'deleted.txt').unlink()
        created = self.repo / 'new file.txt'
        created.write_text('untracked\n')
        created.chmod(0o755)
        initial = self._read_source()
        result = self._freeze(['original.txt', 'new file.txt', 'deleted.txt'])
        self.assertEqual(self._read_source(), initial)
        self.assertEqual(set(result['changes']), {'original.txt', 'new file.txt', 'deleted.txt'})
        self.assertEqual(result['changes']['deleted.txt'], {'action': 'deleted'})
        self.assertEqual(result['changes']['new file.txt']['mode'], '100755')
        candidate = result['candidate']
        self.assertEqual(cli.git(self.repo, 'show', candidate + ':original.txt'), 'visible bytes')
        self.assertEqual(cli.git(self.repo, 'show', candidate + ':untouched.txt'), 'keep me')
        self.assertEqual(cli.sha(self.repo, result['pin']), candidate)
        (self.repo / 'original.txt').write_text('another edit\n')
        loaded = interrogate_snapshot.load(self._reference(result), repo=self.repo, baseline=self.baseline)
        self.assertEqual(loaded['candidate'], candidate)
        with self.assertRaisesRegex(interrogate_snapshot.SnapshotError, 'different candidate'):
            interrogate_snapshot.load(self._reference(result), repo=self.repo, baseline=self.baseline,
                                      candidate=self.baseline)
        self.out.write_bytes(self.out.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            interrogate_snapshot.load(self._reference(result), repo=self.repo, baseline=self.baseline)

    def test_default_scope_captures_worktree_from_later_head_and_respects_ignores(self):
        (self.repo / 'original.txt').write_text('branch commit\n')
        cli.git(self.repo, 'commit', '-qam', 'a branch commit')
        head = cli.sha(self.repo, 'HEAD')
        (self.repo / 'deleted.txt').unlink()
        (self.repo / 'new.txt').write_text('new\n')
        (self.repo / 'ignored.txt').write_text('intentionally ignored\n')
        result = self._freeze()
        self.assertEqual(result['head'], head)
        self.assertEqual(result['selection'], 'worktree')
        self.assertEqual(result['paths'], ['deleted.txt', 'new.txt'])
        self.assertEqual(cli.git(self.repo, 'show', result['candidate'] + ':original.txt'), 'branch commit')

    def test_refuses_ignored_credentials_symlinks_and_parent_escape(self):
        (self.repo / 'ignored.txt').write_text('ignored\n')
        (self.repo / '.env').write_text('REDACTED=test\n')
        (self.root / 'outside.txt').write_text('outside customer content\n')
        (self.repo / 'link.txt').symlink_to(self.root / 'outside.txt')
        (self.repo / 'linkdir').symlink_to(self.root, target_is_directory=True)
        for path, reason in [('ignored.txt', 'missing or ignored'), ('.env', 'credential artifact'),
                             ('link.txt', 'regular file'), ('linkdir/outside.txt', 'regular file'),
                             ('../outside.txt', 'repository relative')]:
            with self.subTest(path=path), self.assertRaisesRegex(Exception, reason):
                self._freeze([path])
        self.assertFalse(self.out.exists())
        self.assertFalse(cli.git(self.repo, 'for-each-ref', 'refs/iboard/interrogate'))

    def test_checkout_byte_audit_rejects_a_git_normalization(self):
        (self.repo / '.gitattributes').write_text('original.txt text eol=crlf\n')
        cli.git(self.repo, 'add', '.gitattributes')
        cli.git(self.repo, 'commit', '-qm', 'set checkout conversion')
        (self.repo / 'original.txt').write_bytes(b'visible LF bytes\n')
        result = self._freeze(['original.txt'])
        checkout = self.root / 'detached'
        cli.git(self.repo, 'worktree', 'add', '--detach', str(checkout), result['candidate'])
        self.addCleanup(cli.git, self.repo, 'worktree', 'remove', '--force', str(checkout))
        with self.assertRaisesRegex(interrogate_snapshot.SnapshotError, 'bytes or mode changed'):
            interrogate_snapshot.check_checkout(checkout, result)
