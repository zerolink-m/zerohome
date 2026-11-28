#!/usr/bin/env python3
"""ZeroCams release CLI (the .sh entry point is directly executable)."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode


VERSION = 'app/version.py'
WORKFLOW = '.github/workflows/ci.yml'
BRANCH = 'refs/heads/main'
TRAILER = 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
POLL_INTERVAL = 5


class ReleaseError(Exception):
    pass


def run(*args, ok=(0,), env=None, timeout=60):
    # Do not echo commands/URLs from errors (Git configuration can contain tokens).
    result = subprocess.run(args, stdout=subprocess.PIPE, env=env, timeout=timeout)
    if result.returncode not in ok:
        raise ReleaseError(f'{args[0]} command failed (exit {result.returncode}).')
    return result


def git(*args, **kwargs):
    return run('git', '--literal-pathspecs', *args, **kwargs).stdout


def git_text(*args, **kwargs):
    return os.fsdecode(git(*args, **kwargs).removesuffix(b'\n'))


def paths(raw):
    return [os.fsdecode(item) for item in raw.split(b'\0') if item]


def secret_name(path):
    for part in PurePosixPath(path.lower()).parts:
        if (part.startswith(('.env', 'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa'))
                or part in {'.ssh', '.aws', '.gnupg', '.kube', '.netrc', '.npmrc', '.pypirc',
                            '.git-credentials', '.docker', '.htpasswd', '.pgpass', 'auth.json', 'kubeconfig'}
                or re.search(r'(^|[._-])(credentials?|creds|secrets?|tokens?|private[-_]?key|passwords?)([._-]|$)', part)
                or part.endswith(('.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.kdbx'))):
            return True
    return False


def display(path):
    # Quote control characters, invalid UTF-8, and whitespace for terminals/logs.
    return repr(path) if not path.isprintable() or any(c.isspace() for c in path) else path


def changes():
    result = {}
    for entry in paths(git('status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames')):
        path = entry[3:]
        if path == VERSION or secret_name(path):
            continue
        p = Path(path)
        if p.parent.resolve() == Path.cwd() / p.parent and (
                (p.is_file() and not p.is_symlink()) or not p.exists() and not p.is_symlink()):
            result[path] = entry[:2]
    return result


def pick(options, tag):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ReleaseError('No TTY: use --list and repeat --file PATH, or pass --version-only.')
    if not options:
        raise ReleaseError('No selectable changes; use --version-only if intended.')
    import curses

    def menu(window):
        selected, cursor, top = set(), 0, 0
        while True:
            height, width = window.getmaxyx()
            window.erase()
            if height >= 5 and width >= 12:
                page = height - 3
                top = max(0, min(top, cursor), cursor - page + 1)
                window.addnstr(0, 0, f'{tag} | Up/Down, Space: toggle, Enter: release, Esc: cancel', width - 1)
                window.addnstr(1, 0, f'{len(selected)} selected + {VERSION}', width - 1)
                for row, path in enumerate(order[top:top + page], 2):
                    mark = 'x' if path in selected else ' '
                    line = f'[{mark}] {options[path]} {display(path)}'
                    window.addnstr(row, 0, line, width - 1,
                                   curses.A_REVERSE if top + row - 2 == cursor else curses.A_NORMAL)
            key = window.getch()
            if key in (27, ord('q')):
                raise ReleaseError('Selection cancelled; nothing changed.')
            if key == curses.KEY_UP:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_DOWN:
                cursor = min(len(options) - 1, cursor + 1)
            elif key == ord(' '):
                selected.symmetric_difference_update([order[cursor]])
            elif key in (10, 13, curses.KEY_ENTER) and selected:
                return [path for path in order if path in selected]

    order = list(options)
    try:
        return curses.wrapper(menu)
    except curses.error as exc:
        raise ReleaseError('Terminal menu unavailable; use --list and --file PATH.') from exc


def clean_index():
    if git('diff', '--cached', '--name-only', '-z', '--no-renames', 'HEAD', '--'):
        raise ReleaseError('The index contains staged changes. Commit or unstage them first.')
    for state in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'):
        if Path(git_text('rev-parse', '--git-path', state)).exists():
            raise ReleaseError(f'Finish the ongoing Git operation first ({state}).')


def target():
    if (git_text('symbolic-ref', '-q', 'HEAD', ok=(0, 1)) != BRANCH
            or git_text('config', '--get-all', 'branch.main.remote', ok=(0, 1)) != 'origin'
            or git_text('config', '--get-all', 'branch.main.merge', ok=(0, 1)) != BRANCH):
        raise ReleaseError('Release requires local main tracking origin/main.')
    url = git_text('remote', 'get-url', '--push', '--all', 'origin')
    repo = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
                        r'([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', url)
    if not repo or repo[1].split('/')[1] in {'.', '..'}:
        raise ReleaseError('origin must have one GitHub.com push URL (HTTPS or git@github.com SSH).')
    return url, repo[1]


def check_tag(url, tag, base=None):
    ref = f'refs/tags/{tag}'
    if run('git', 'show-ref', '--verify', '--quiet', ref, ok=(0, 1)).returncode == 0:
        raise ReleaseError(f'Local tag {tag} already exists.')
    refs = (ref, BRANCH) if base is not None else (ref,)
    remote = dict(line.split('\t', 1)[::-1] for line in git_text('ls-remote', '--refs', url, *refs).splitlines())
    if ref in remote:
        raise ReleaseError(f'Remote tag {tag} already exists.')
    if base is not None and remote.get(BRANCH) != base:
        raise ReleaseError('Local HEAD must match remote main exactly (no ahead/behind commits).')


def replace_version(data, mode):
    # Replace instead of writing through a possible symlink or hard link.
    fd, name = tempfile.mkstemp(prefix='.release-version-', dir=Path(VERSION).parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            os.fchmod(output.fileno(), mode)
        os.replace(name, VERSION)
    finally:
        Path(name).unlink(missing_ok=True)


def commit(base, chosen, version, message):
    index = Path(git_text('rev-parse', '--git-path', 'index')).resolve()
    lock = index.with_name('index.lock')
    try:
        lock.touch(exist_ok=False)
    except FileExistsError as exc:
        raise ReleaseError('Git index is locked; finish the other Git operation first.') from exc
    try:
        clean_index()
        if git_text('symbolic-ref', '-q', 'HEAD') != BRANCH or git_text('rev-parse', 'HEAD') != base:
            raise ReleaseError('HEAD changed during selection; start again.')
        if not set(chosen) <= changes().keys():
            raise ReleaseError('Selected paths changed during selection; run --list and start again.')
        p = Path(VERSION)
        info = p.lstat()
        if p.parent.resolve() != Path.cwd() / p.parent or not stat.S_ISREG(info.st_mode):
            raise ReleaseError(f'{VERSION} must be a regular file in the repository.')
        before = p.read_bytes()
        if before != git('show', f'{base}:{VERSION}') or git('diff', '--name-only', '--', VERSION):
            raise ReleaseError(f'{VERSION} has local changes; resolve them before releasing.')
        after, count = re.subn(rb'(?m)^(APP_VERSION[ \t]*=[ \t]*)([\"\'])([0-9]+\.[0-9]+\.[0-9]+)\2(?=[ \t]*(?:#.*)?\r?$)',
                               lambda m: m[1] + m[2] + version.encode('ascii') + m[2], before)
        if count != 1 or after == before:
            raise ReleaseError(f'{VERSION} must contain one APP_VERSION assignment with a different version.')
        mode = stat.S_IMODE(info.st_mode)

        with tempfile.TemporaryDirectory(prefix='zerocams-release-') as tmp:
            alt = Path(tmp) / 'index'
            shutil.copyfile(index, alt)
            env = dict(os.environ, GIT_INDEX_FILE=str(alt))
            expected = set(chosen) | {VERSION}
            replace_version(after, mode)
            try:
                git('add', '--', *chosen, VERSION, env=env)
                staged = git('diff', '--cached', '--name-only', '-z', '--no-renames', base, '--', env=env)
                if set(paths(staged)) != expected:
                    raise ReleaseError('Staged paths differ from the explicit selection; refusing to commit.')
                tree = git_text('write-tree', env=env)
                if git('show', f'{tree}:{VERSION}') != after:
                    raise ReleaseError(f'Git filters changed {VERSION}; refusing to commit.')
                shutil.copyfile(alt, lock)
                shutil.copymode(index, lock)
                git('commit', '--cleanup=verbatim', '-m', f'{message}\n\n{TRAILER}', env=env, timeout=300)
                sha, parent, actual_tree = git_text('show', '-s', '--format=%H%n%P%n%T', 'HEAD').splitlines()
                if parent != base or tree != actual_tree or git_text('symbolic-ref', '-q', 'HEAD') != BRANCH:
                    raise ReleaseError(f'Commit {sha} differs from the prepared commit; inspect HEAD and index. No push.')
                trailers = git_text('show', '-s', '--format=%(trailers:key=Co-Authored-By)', sha).splitlines()
                if TRAILER not in trailers:
                    raise ReleaseError(f'Commit {sha} is missing the expected attribution; inspect HEAD and index. No push.')
                os.replace(lock, index)
                lock = None  # Never remove a new lock acquired by Git after the replace.
                return sha
            except BaseException:
                # Restore only our version edit, and never roll back a commit or overwrite later edits.
                if (git_text('rev-parse', 'HEAD') == base and p.parent.resolve() == Path.cwd() / p.parent
                        and not p.is_symlink() and p.is_file() and p.read_bytes() == after):
                    replace_version(before, mode)
                raise
    finally:
        if lock is not None:
            lock.unlink(missing_ok=True)


def wait_ci(repo, sha, workflow_id, appear, wait):
    def api(resource, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise ReleaseError('Timed out waiting for CI.')
        return json.loads(run('gh', 'api', '--hostname', 'github.com', f'repos/{repo}/actions/{resource}',
                              timeout=min(30, left)).stdout)

    def pause(deadline):
        time.sleep(max(0, min(POLL_INTERVAL, deadline - time.monotonic())))

    def matches(item):
        return (item['head_sha'], item['head_branch'], item['event'], item['name'], item['path']) == (
            sha, 'main', 'push', 'CI', WORKFLOW)

    deadline = time.monotonic() + appear
    query = urlencode(dict(branch='main', event='push', head_sha=sha, per_page=100))
    while True:
        data = api(f'workflows/ci.yml/runs?{query}', deadline)
        runs = [item for item in data['workflow_runs'] if matches(item)]
        if data['total_count'] > len(data['workflow_runs']) or len(runs) > 1:
            raise ReleaseError('Multiple or incomplete CI run results; refusing to choose a run.')
        if runs:
            pinned = runs[0]
            break
        pause(deadline)

    run_id, attempt = (pinned[k] for k in ('id', 'run_attempt'))
    if pinned['workflow_id'] != workflow_id or not all(type(value) is int and value > 0 for value in (run_id, workflow_id, attempt)):
        raise ReleaseError('Invalid CI run identity.')
    print(f'Waiting for CI run {run_id}, attempt {attempt}, for {sha}.', flush=True)
    deadline = time.monotonic() + wait
    while True:
        current = api(f'runs/{run_id}', deadline)
        if (not matches(current) or (current['id'], current['workflow_id'], current['run_attempt'])
                != (run_id, workflow_id, attempt)):
            raise ReleaseError('CI run or attempt changed while waiting; no release.')
        if current['status'] == 'completed':
            if current['conclusion'] != 'success':
                raise ReleaseError(f"CI run {run_id}: {current['conclusion']!r}; no release.")
            break
        if current['status'] not in {'queued', 'in_progress', 'requested', 'waiting', 'pending'}:
            raise ReleaseError(f"Unexpected CI status {current['status']!r}; no release.")
        pause(deadline)

    data = api(f'runs/{run_id}/attempts/{attempt}/jobs?per_page=100', deadline)
    tests = [job for job in data['jobs'] if job['name'] == 'test']
    if (data['total_count'] != len(data['jobs']) or len(tests) != 1
            or any(tests[0][field] != value for field, value in (
                ('head_sha', sha), ('run_id', run_id), ('status', 'completed'), ('conclusion', 'success')))):
        raise ReleaseError(f'CI run {run_id} did not report exactly one successful test job; no release.')
    print(f'CI run {run_id}: test passed.', flush=True)


def main():
    parser = argparse.ArgumentParser(prog='./gh.sh', description='Create a CI-gated GitHub release.')
    sub = parser.add_subparsers(dest='command', required=True)
    release = sub.add_parser('release', allow_abbrev=False,
                             description='Release from main tracking origin/main, with an empty Git index. '
                                         'Run from the repository root. VERSION is X.Y.Z or vX.Y.Z; '
                                         f'{VERSION} is always updated. Without selection flags, opens a TTY menu.')
    release.add_argument('version', nargs='?', metavar='VERSION')
    select = release.add_mutually_exclusive_group()
    select.add_argument('--list', action='store_true', help='List selectable worktree changes; no network or writes.')
    select.add_argument('--file', action='append', metavar='PATH', help='Exact repo-relative changed file; repeat, quote paths. No directories or globs.')
    select.add_argument('--version-only', action='store_true', help='Explicitly release only the version bump.')
    release.add_argument('--message', metavar='TEXT', help='Commit message (default: Release vX.Y.Z).')
    release.add_argument('--appear-timeout', type=int, default=120, metavar='SECONDS', help='Limit for CI discovery (default: 120).')
    release.add_argument('--ci-timeout', type=int, default=1800, metavar='SECONDS', help='Limit for the pinned CI run and jobs (default: 1800).')
    args = parser.parse_args()
    if not args.list and not re.fullmatch(r'v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', args.version or ''):
        release.error('VERSION must be X.Y.Z or vX.Y.Z (no leading zeros).')
    if args.appear_timeout <= 0 or args.ci_timeout <= 0:
        release.error('Timeouts must be positive.')
    if args.message is not None and not args.message.strip():
        release.error('--message must not be blank.')
    if any(key in os.environ for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')):
        raise ReleaseError('Unset Git directory/worktree/index overrides before running this command.')
    os.environ.update(GIT_OPTIONAL_LOCKS='0', GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0')
    if Path(git_text('rev-parse', '--show-toplevel')) != Path.cwd():
        raise ReleaseError('Run from the repository root.')
    if args.list:
        for path, state in changes().items():
            print(f'{state} {display(path)}')
        print(f'{VERSION} is automatic; ignored and secret-like paths are excluded.')
        return

    clean_index()
    url, repo = target()
    base = git_text('rev-parse', 'HEAD')
    version = args.version.removeprefix('v')
    tag = f'v{version}'
    options = changes()
    if args.file:
        chosen = list(dict.fromkeys(args.file))
        for path in chosen:
            if path not in options:
                raise ReleaseError(f'Not a selectable changed file: {display(path)}. See --list.')
    elif args.version_only:
        chosen = []
    else:
        chosen = pick(options, tag)

    # Validate Actions access before changing anything, using the same repo as the push.
    workflow = json.loads(run('gh', 'api', '--hostname', 'github.com',
                              f'repos/{repo}/actions/workflows/ci.yml', timeout=30).stdout)
    workflow_id = workflow['id']
    if (workflow['name'], workflow['path'], workflow['state']) != ('CI', WORKFLOW, 'active') or (
            type(workflow_id) is not int or workflow_id <= 0):
        raise ReleaseError('Expected the active CI workflow at .github/workflows/ci.yml.')
    check_tag(url, tag, base)
    sha = commit(base, chosen, version, (args.message or f'Release {tag}').rstrip())
    print(f'Created commit {sha} for {tag}.', flush=True)
    phase = 'Push did not confirm success; verify the remote before retrying.'
    try:
        if target() != (url, repo):
            raise ReleaseError('Push destination changed; refusing to push.')
        git('-c', 'remote.origin.mirror=false', 'push', '--no-follow-tags', '--recurse-submodules=no',
            'origin', f'{sha}:{BRANCH}', timeout=300)
        phase = 'Commit was pushed; no release was attempted. See README for recovery.'
        wait_ci(repo, sha, workflow_id, args.appear_timeout, args.ci_timeout)
        check_tag(url, tag)
        phase = 'Release was attempted after CI success; verify the tag/release on GitHub before retrying.'
        result = run('gh', 'release', 'create', tag, '--repo', f'github.com/{repo}', '--target', sha,
                     '--title', tag, '--generate-notes')
        print(os.fsdecode(result.stdout).rstrip())
    except (Exception, KeyboardInterrupt):
        print(f'{tag} ({sha}): {phase}', file=sys.stderr)
        raise


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('Release interrupted.')
    except subprocess.TimeoutExpired as exc:
        sys.exit(f'{exc.cmd[0]} timed out; stopping the release flow.')
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as exc:
        sys.exit(f'Release stopped: {exc}')
