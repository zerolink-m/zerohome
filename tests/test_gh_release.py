"""Offline CLI integration tests: python3 tests/test_gh_release.py."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'gh.sh'

FAKE_SSH = '''#!/usr/bin/env python3
import os, shlex, sys
args = sys.argv[1:]
assert args[-2] == 'git@github.com', args
command, repo = shlex.split(args[-1])
assert command in ('git-receive-pack', 'git-upload-pack') and repo == 'example/project.git', args
os.execvp(command, [command, os.environ['FAKE_REMOTE']])
'''
FAKE_GH = '''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

args = sys.argv[1:]
with Path(os.environ['GH_LOG']).open('a') as log:
    log.write(json.dumps(args) + '\\n')
mode = os.environ.get('FAKE_CI', '')
workflow = dict(id=7, name='CI', path='.github/workflows/ci.yml', state='active')
if args == ['api', '--hostname', 'github.com', 'repos/example/project/actions/workflows/ci.yml']:
    print(json.dumps(workflow))
    sys.exit(0)
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
pushed = subprocess.check_output(['git', '--git-dir', os.environ['FAKE_REMOTE'], 'rev-parse', 'main'], text=True).strip()
assert sha == pushed
run = dict(id=42, workflow_id=7, run_attempt=1, head_sha=sha, head_branch='main',
           name='CI', path=workflow['path'], event='push', status='completed',
           conclusion=mode if mode in ('failure', 'cancelled', 'skipped') else 'success')
if args[:2] == ['release', 'create']:
    assert args == ['release', 'create', 'v2.0.0', '--repo', 'github.com/example/project',
                    '--target', sha, '--title', 'v2.0.0', '--generate-notes'], args
    print('fake release published')
    sys.exit(0)
assert args[:3] == ['api', '--hostname', 'github.com'], args
endpoint = urlsplit(args[3])
base = 'repos/example/project/actions/'
if endpoint.path == base + 'workflows/ci.yml/runs':
    assert parse_qs(endpoint.query) == dict(branch=['main'], event=['push'], head_sha=[sha], per_page=['100'])
    if mode == 'api-error':
        sys.exit(1)
    runs = [dict(run, id=12, event='pull_request'), dict(run, id=13, head_branch='feature'),
            dict(run, id=14, head_sha='0'*40), dict(run, id=15, path='.github/workflows/other.yml')]
    if mode != 'missing':
        runs.append(run)
    data = dict(workflow_runs=runs, total_count=len(runs))
elif endpoint.path == base + 'runs/42':
    data = dict(run, run_attempt=2) if mode == 'rerun' else run
elif endpoint.path == base + 'runs/42/attempts/1/jobs':
    jobs = [dict(name='test', run_id=42, head_sha=sha, status='completed',
                 conclusion='skipped' if mode == 'job-skipped' else 'success')]
    data = dict(jobs=jobs, total_count=len(jobs))
else:
    raise AssertionError(args)
print(json.dumps(data))
'''


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo, self.remote, self.log = root / 'repo', root / 'remote.git', root / 'gh.log'
        self.repo.mkdir()
        bindir = root / 'bin'
        bindir.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'GH_'))}
        self.env.update(PATH=f"{bindir}:{self.env['PATH']}", GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_CONFIG_NOSYSTEM='1', FAKE_REMOTE=str(self.remote), GH_LOG=str(self.log))
        for name, content in [('ssh', FAKE_SSH), ('gh', FAKE_GH)]:
            path = bindir / name
            path.write_text(content)
            path.chmod(0o755)
        self.env.update(GIT_SSH=str(bindir / 'ssh'), GIT_SSH_VARIANT='simple')
        self.git('init', '--bare', str(self.remote))
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Release Test')
        self.write('app/version.py', '# comment\nAPP_VERSION = "1.0.0"\n')
        self.write('tracked', 'original\n')
        self.write('deleted', 'original\n')
        self.write('.gitignore', 'ignored\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('remote', 'add', 'origin', 'git@github.com:example/project.git')
        self.git('push', '-u', 'origin', 'main')
        self.script = self.repo / 'gh.sh'
        shutil.copy2(SCRIPT, self.script)
        self.script.chmod(0o755)

    def write(self, path, text):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, env=self.env,
                                       stderr=subprocess.PIPE, text=True)

    def cli(self, *args, mode=''):
        return subprocess.run([str(self.script), 'release', *args], cwd=self.repo,
                              env=dict(self.env, FAKE_CI=mode), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)

    def releases(self):
        if not self.log.exists():
            return []
        return [args for line in self.log.read_text().splitlines() if (args := json.loads(line))[0] == 'release']

    def test_release_only_selected_literal_paths(self):
        self.write('tracked', 'unrelated\n')
        self.write('name [x].txt', 'selected\n')
        self.write('name x.txt', 'unselected\n')
        self.write(':name', 'other literal\n')
        (self.repo / 'deleted').unlink()
        self.git('tag', '-am', 'unrelated annotated tag', 'outside-release')
        self.git('config', 'push.followTags', 'true')
        result = self.cli('v2.0.0', '--file', 'name [x].txt', '--file', ':name', '--file', 'deleted',
                          '--message', 'Explicit commit title')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git('diff', '--name-only', 'HEAD^', 'HEAD').splitlines(),
                         [':name', 'app/version.py', 'deleted', 'name [x].txt'])
        self.assertEqual(self.git('show', 'HEAD:app/version.py'), '# comment\nAPP_VERSION = "2.0.0"\n')
        self.assertEqual(self.git('show', '-s', '--format=%B', 'HEAD').strip(),
                         'Explicit commit title\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertEqual(self.git('diff', '--name-only').strip(), 'tracked')
        self.assertTrue((self.repo / 'name x.txt').exists())
        self.assertEqual(self.git('ls-remote', '--tags', 'origin'), '')
        self.assertEqual(len(self.releases()), 1)

    def test_failed_ci_leaves_commit_and_push_without_release(self):
        result = self.cli('2.0.0', '--version-only', mode='failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'failure'", result.stderr)
        self.assertIn('Commit was pushed', result.stderr)
        self.assertNotEqual(self.git('rev-parse', 'HEAD').strip(), self.base)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('--git-dir', str(self.remote), 'rev-parse', 'main'))
        self.assertEqual(self.git('ls-remote', '--tags', 'origin'), '')
        self.assertFalse(self.releases())

    def test_nonpassing_job_cancelled_skipped_api_error_rerun_and_timeout(self):
        for mode in ['job-skipped', 'cancelled', 'skipped', 'api-error', 'rerun', 'missing']:
            with self.subTest(mode=mode):
                result = self.cli('2.0.0', '--version-only', '--appear-timeout', '1', mode=mode)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Commit was pushed', result.stderr)
                self.assertFalse(self.releases())
                # Restore only this fixture for the next subtest.
                self.git('reset', '--hard', self.base)
                self.git('--git-dir', str(self.remote), 'update-ref', 'refs/heads/main', self.base)

    def test_rejects_staged_changes_before_touching_index_or_version(self):
        self.write('tracked', 'staged\n')
        self.git('add', 'tracked')
        self.write('tracked', 'unstaged too\n')
        index = self.repo / '.git/index'
        before = index.read_bytes()
        result = self.cli('2.0.0', '--version-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('index contains staged changes', result.stderr)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((self.repo / 'tracked').read_text(), 'unstaged too\n')
        self.assertEqual((self.repo / 'app/version.py').read_text(), '# comment\nAPP_VERSION = "1.0.0"\n')
        self.assertFalse(self.log.exists())

    def test_list_and_flags_filter_secret_directory_and_unexpected_arguments(self):
        for path in ['.env.local', 'nested/credentials.json', 'ignored', 'picked/path']:
            self.write(path, 'dummy\n')
        result = self.cli('--list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('picked/path', result.stdout)
        self.assertNotIn('.env.local', result.stdout)
        self.assertNotIn('credentials.json', result.stdout)
        for args in [('--file', '.env.local'), ('--file', 'nested/credentials.json'),
                     ('--file', 'picked'), ('--file', 'picked/*'), ('--file', 'picked/path', 'extra'), ()]:
            with self.subTest(args=args):
                self.assertNotEqual(self.cli('2.0.0', *args).returncode, 0)
        self.assertFalse(self.log.exists())

    def test_rejects_local_and_remote_tags_without_a_commit(self):
        self.git('tag', 'v2.0.0')
        result = self.cli('2.0.0', '--version-only')
        self.assertIn('Local tag', result.stderr)
        self.git('push', 'origin', 'refs/tags/v2.0.0')
        self.git('tag', '-d', 'v2.0.0')
        result = self.cli('2.0.0', '--version-only')
        self.assertIn('Remote tag', result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)
        self.assertFalse(self.releases())

    def test_refuses_feature_branch_and_locally_edited_version(self):
        self.git('checkout', '-b', 'feature')
        result = self.cli('2.0.0', '--version-only')
        self.assertIn('main tracking origin/main', result.stderr)
        self.assertFalse(self.log.exists())
        self.git('checkout', 'main')
        local_version = 'APP_VERSION = "1.5.0"\n'
        self.write('app/version.py', local_version)
        result = self.cli('2.0.0', '--version-only')
        self.assertIn('app/version.py has local changes', result.stderr)
        self.assertEqual((self.repo / 'app/version.py').read_text(), local_version)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)
        self.assertFalse(self.releases())

    def test_commit_hook_failure_restores_version_and_index(self):
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        self.write('some change', 'new\n')
        index = self.repo / '.git/index'
        before = index.read_bytes()
        result = self.cli('2.0.0', '--file', 'some change')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((self.repo / 'app/version.py').read_text(), '# comment\nAPP_VERSION = "1.0.0"\n')
        self.assertEqual((self.repo / 'some change').read_text(), 'new\n')

    def test_terminal_arrow_space_enter(self):
        self.write('a-first', 'not selected\n')
        self.write('b-second', 'selected\n')
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        process = subprocess.Popen([str(self.script), 'release', '2.0.0'], cwd=self.repo,
                                   env=dict(self.env, TERM='xterm'), stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = b''
        sent = False
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and select.select([master], [], [], deadline - time.monotonic())[0]:
                try:
                    output += os.read(master, 8192)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break  # Slave closed.
                    raise
                if b'b-second' in output and not sent:
                    os.write(master, b'\x1bOB \r')  # xterm application-mode Down, Space, Enter.
                    sent = True
            self.assertEqual(process.wait(timeout=1), 0, output.decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertTrue(sent)
        self.assertEqual(self.git('diff', '--name-only', 'HEAD^', 'HEAD').splitlines(), ['app/version.py', 'b-second'])
        self.assertEqual(len(self.releases()), 1)


if __name__ == '__main__':
    unittest.main()
