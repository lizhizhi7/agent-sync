#!/usr/bin/env python3
"""Scoped AgTrace repository memory, isolated sessions and durable exact-request retries."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import webbrowser

MAX_FILES = 512
MAX_BYTES = 8 * 1024 * 1024
MAX_FILE = 2_000_000
IDENTITY = re.compile(r'^[0-9a-f]{32}$')


class Failure(Exception):
    pass


class Unavailable(Failure):
    pass


class Refused(Failure):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def root():
    return Path(os.environ.get('AGENT_SYNC_MEMORY_HOME',
        str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'agent-sync/memory'))).absolute()


def safe_directory(directory):
    directory = Path(directory).absolute()
    for part in [*reversed(directory.parents), directory]:
        if part.is_symlink():
            raise Failure(f'Refusing a symlink directory: {part}')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def atomic(path, value):
    path = Path(path)
    safe_directory(path.parent)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def read(path):
    if Path(path).is_symlink():
        raise Failure(f'Refusing symlink state: {path}')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        return json.load(stream)


@contextmanager
def lock(path):
    safe_directory(Path(path).parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('Another memory command owns this state; retry after it finishes') from None
        yield
    finally:
        os.close(fd)


def profile():
    path = root() / 'profile.json'
    return read(path) if path.exists() else {'version': 1, 'workspaces': {}, 'repositories': {}}


def save_profile(value):
    atomic(root() / 'profile.json', value)


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
    if result.returncode:
        raise Failure('This command requires a Git repository with a configured origin')
    return result.stdout.strip()


def repository(path):
    raw = git(path, 'remote', 'get-url', 'origin')
    parsed = urlsplit(raw)
    if parsed.scheme:
        host, project = parsed.hostname, parsed.path.lstrip('/')
        if parsed.scheme not in ('https', 'ssh', 'http') or parsed.password or parsed.query or parsed.fragment:
            raise Failure('The Git origin must be a credential-free HTTPS or SSH repository URL')
    else:
        match = re.fullmatch(r'(?:[^/@:]+@)?([^/:]+):(.+)', raw)
        if not match:
            raise Failure('The Git origin is not a supported provider repository identity')
        host, project = match.groups()
    project = project.removesuffix('.git').strip('/')
    if not host or not project or any(p in ('', '.', '..') for p in project.split('/')):
        raise Failure('The Git origin has an invalid provider/project identity')
    common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    return {'host': host.lower(), 'project': project, 'key': host.lower() + '/' + project,
            'checkout': str(common.parent)}


def canonical(path):
    repo = repository(path)
    device = repo['checkout'].replace('/', '-')
    data = os.environ.get('AGENT_SYNC_DIR', '')
    mapping = Path(data) / '.agent-sync-projects' if data else None
    if mapping and mapping.is_file():
        for line in mapping.read_text().splitlines():
            values = line.split()
            if len(values) == 2 and values[0] == device:
                return values[1]
    prefix = str(Path.home())
    workdir = os.environ.get('AGENT_SYNC_WORKDIR', '')
    if workdir:
        prefix += '/' + workdir.strip('/')
    checkout = repo['checkout']
    return checkout[len(prefix):].replace('/', '-') if checkout.startswith(prefix + '/') else device


def external_projects():
    data = os.environ.get('AGENT_SYNC_DIR', '')
    marker = Path(data) / '.agent-sync-memory-owned' if data else None
    if not marker or not marker.exists():
        return set()
    values = set(marker.read_text().splitlines())
    if any(not re.fullmatch(r'[-_A-Za-z0-9.]+', value) or value in ('.', '..') for value in values):
        raise Failure('Invalid project name in .agent-sync-memory-owned')
    return values


def server_origin(value):
    parsed = urlsplit(value)
    local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local) or
            not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ('', '/')):
        raise Failure('--server must be an HTTPS origin without credentials or a path (HTTP is allowed only on loopback)')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure('Memory API redirected; verify --server and sign in through workspace link')


def request(server, path, *, token='', body=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers['Content-Type'] = 'application/json'
    try:
        with build_opener(NoRedirect()).open(Request(server + '/api/memory-sync/v1' + path,
                data=data, headers=headers), timeout=30) as response:
            raw = response.read(MAX_BYTES * 3 + 1)
            if len(raw) > MAX_BYTES * 3:
                raise Failure('Memory API exceeded its bounded response size')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise Failure('Memory API returned an invalid response')
            return value
    except HTTPError as exc:
        raw = exc.read(4096)
        try:
            detail = json.loads(raw).get('detail', 'Memory API refused the request')
            detail = detail if isinstance(detail, str) else 'Memory API rejected the request fields'
        except (ValueError, AttributeError):
            detail = 'Memory API returned a non-JSON error'
        if exc.code >= 500:
            raise Unavailable(f'Memory service unavailable ({exc.code}); exact pending requests are retained') from None
        raise Refused(exc.code, f'Memory API {exc.code}: {detail}') from None
    except (URLError, TimeoutError, OSError) as exc:
        raise Unavailable('Memory service unavailable; cached snapshots and pending requests are retained') from exc
    except ValueError:
        raise Failure('Memory API returned invalid JSON; check the server origin') from None


class Credentials:
    def _helper(self, operation, key, token=''):
        helper = os.environ.get('AGENT_SYNC_MEMORY_CREDENTIAL_HELPER')
        if helper:
            command = [helper, operation, key]
            result = subprocess.run(command, input=token, capture_output=True, text=True)
        elif sys.platform == 'darwin':
            if operation == 'get':
                command = ['/usr/bin/security', 'find-generic-password', '-s', 'agent-sync-memory', '-a', key, '-w']
                result = subprocess.run(command, capture_output=True, text=True)
            else:
                # Interactive security input avoids exposing the token in a process argv.
                line = ('add-generic-password -U -s agent-sync-memory -a ' + shlex.quote(key) +
                        ' -w ' + shlex.quote(token) if operation == 'store' else
                        'delete-generic-password -s agent-sync-memory -a ' + shlex.quote(key))
                result = subprocess.run(['/usr/bin/security', '-i'], input=line + '\n', capture_output=True, text=True)
        else:
            raise Failure('Configure AGENT_SYNC_MEMORY_CREDENTIAL_HELPER for your OS credential store')
        if result.returncode:
            raise Failure('OS credential store refused access; reconnect with memory workspace link or memory bind')
        return result.stdout.strip() if operation == 'get' else ''

    def get(self, key):
        return self._helper('get', key)

    def store(self, key, token):
        self._helper('store', key, token)
        if not secrets.compare_digest(self.get(key), token):
            raise Failure('OS credential store did not retain the approved grant; reconnect before using memory')


def connection_key(server, workspace):
    return digest((server + '\0' + workspace).encode())[:32]


def select_workspace(value, workspace, server=''):
    matches = [row for row in value['workspaces'].values() if row['workspace'] == workspace and
               (not server or row['server'] == server_origin(server))]
    if len(matches) != 1:
        raise Failure('Select one linked workspace with --workspace and --server; link it first with memory workspace link')
    return matches[0]


def pair(server, workspace, *, registry_id='', mode='discover', credentials=None):
    credentials = credentials or Credentials()
    verifier = secrets.token_hex(32)
    value = request(server, '/pairings', body={'workspace': workspace, 'registry_id': registry_id,
        'mode': mode, 'challenge': digest(verifier.encode()), 'label': socket.gethostname()[:80] or 'agent-sync'})
    uri = value['verification_uri']
    if not uri.startswith('/api/me/memory-clients/pairings/') or uri.startswith('//'):
        raise Failure('Pairing returned an unexpected verification path')
    print('Approve this exact workspace and access in your browser:', server + uri, flush=True)
    print('Verification code:', value['user_code'], flush=True)
    if not os.environ.get('AGENT_SYNC_MEMORY_NO_BROWSER'):
        webbrowser.open(server + uri)
    deadline = datetime.fromisoformat(value['expires_at']).timestamp()
    while time.time() < deadline:
        result = request(server, '/pairings/' + value['id'] + '/token', body={'verifier': verifier})
        if result['state'] == 'approved':
            if result['workspace'] != workspace or result['registry_id'] != registry_id or result['mode'] != mode:
                raise Failure('Approved pairing scope differs from the requested workspace and registration')
            key = connection_key(server, workspace) + ':' + (registry_id or 'discover')
            credentials.store(key, result.pop('token'))
            return {**result, 'credential_key': key, 'server': server}
        time.sleep(max(1, min(int(value['interval']), 10)))
    raise Failure('Pairing expired; run the same link/bind command again')


def checked_path(name):
    if (not isinstance(name, str) or not name or len(name) > 512 or '\\' in name or
            name.startswith('/') or any(ord(c) < 32 for c in name) or
            any(p in ('', '.', '..', '.git') or len(p.encode()) > 255 for p in name.split('/')) or
            len(name.split('/')) > 12 or not name.endswith('.md')):
        raise Failure(f'Invalid Markdown memory path: {name!r}')
    return name


def collect(directory):
    files = {}
    total = visited = 0
    def visit(fd, prefix):
        nonlocal total, visited
        with os.scandir(fd) as entries:
            names = []
            for entry in entries:
                visited += 1
                if visited > 8192:
                    raise Failure('Memory content exceeds 8192 entries')
                names.append(entry.name)
        for name in sorted(names):
            path = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if path.startswith('/') or any(p in ('.', '..', '.git') for p in path.split('/')) or len(path.split('/')) >= 12:
                    raise Failure(f'Invalid memory directory: {path}')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, path + '/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                checked_path(path)
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    before = os.fstat(child)
                    with os.fdopen(child, 'rb', closefd=False) as stream:
                        raw = stream.read(MAX_FILE + 1)
                    after = os.fstat(child)
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise Failure(f'{path}: file changed during capture; retry at a quiescent boundary')
                finally:
                    os.close(child)
                if len(raw) > MAX_FILE:
                    raise Failure(f'{path}: exceeds {MAX_FILE} bytes')
                try:
                    text = raw.decode('utf-8')
                except UnicodeError:
                    raise Failure(f'{path}: memory must be UTF-8') from None
                if '\0' in text:
                    raise Failure(f'{path}: memory cannot contain NUL bytes')
                total += len(raw)
                if total > MAX_BYTES or len(files) >= MAX_FILES:
                    raise Failure('Memory content exceeds its file or byte budget')
                files[path] = base64.b64encode(raw).decode()
            else:
                raise Failure(f'{path}: memory cannot contain symlinks or special files')
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(fd, '')
    finally:
        os.close(fd)
    return files


def delta(base, files):
    return [{'path': name, 'expected': base.get(name, {}).get('revision', ''), 'content': files.get(name)}
            for name in sorted(base.keys() | files.keys()) if base.get(name, {}).get('content') != files.get(name)]


def binding(value, repo, workspace='', server=''):
    config = value['repositories'].get(repo['key'])
    if not config:
        raise Failure('No AgTrace memory binding for this Git identity; run memory bind first')
    if workspace:
        matches = [item for item in config['bindings'].values() if item['workspace'] == workspace and
                   (not server or item['server'] == server_origin(server))]
        if len(matches) != 1:
            raise Failure('Select one registry with --workspace and --server')
        return config, matches[0]
    if not config.get('primary') or config['primary'] not in config['bindings']:
        choices = ', '.join(item['workspace'] for item in config['bindings'].values())
        raise Failure('Choose a primary registry with memory primary --workspace <workspace>; linked: ' + choices)
    return config, config['bindings'][config['primary']]


def snapshot(item, credentials=None, *, offline=False):
    credentials = credentials or Credentials()
    namespace = connection_key(item['server'], item['workspace']) + '-' + item['registry_id']
    cache_path = root() / 'cache' / namespace / 'snapshot.json'
    cache = read(cache_path) if cache_path.exists() else None
    if offline:
        if not cache:
            raise Failure('No complete cached memory snapshot; reconnect before opening this registry')
        return cache, True
    try:
        value = request(item['server'], '/registries/' + item['registry_id'] + '/snapshot' +
            ('?' + urlencode({'generation': cache['generation']}) if cache else ''), token=credentials.get(item['credential_key']))
    except Unavailable:
        if cache:
            return cache, True
        raise
    if value.get('unchanged'):
        if not cache or value['id'] != item['registry_id'] or value['generation'] != cache['generation']:
            raise Failure('Server snapshot acknowledgment does not match this cache')
        value = {**cache, 'fetched_at': datetime.now(timezone.utc).isoformat()}
    else:
        if value.get('protocol') != 1 or value.get('id') != item['registry_id'] or value.get('scope') != item['workspace']:
            raise Failure('Snapshot identity or protocol differs from the bound registration')
        if len(value.get('files', {})) > 8192:
            raise Failure('Snapshot exceeds 8192 document heads, including tombstones')
        total = 0
        for name, entry in value['files'].items():
            checked_path(name)
            if not IDENTITY.fullmatch(entry.get('revision', '')):
                raise Failure('Snapshot has an invalid document revision')
            if entry['content'] is not None:
                raw = base64.b64decode(entry['content'], validate=True)
                raw.decode('utf-8')
                total += len(raw)
                if len(raw) > MAX_FILE or digest(raw) != entry['sha256']:
                    raise Failure(f'{name}: snapshot content failed its digest or byte budget')
        if total > MAX_BYTES:
            raise Failure('Snapshot exceeds its byte budget')
        value['fetched_at'] = datetime.now(timezone.utc).isoformat()
    atomic(cache_path, value)
    return value, False


def session_path(identity):
    if not IDENTITY.fullmatch(identity):
        raise Failure('--session must be the exact 32-character identity printed by memory')
    return root() / 'sessions' / identity


def open_session(repo, item, *, writable, offline=False, credentials=None):
    value, stale = snapshot(item, credentials, offline=offline)
    if writable and (item['mode'] != 'write' or not value.get('writable')):
        raise Failure('This registry has a read-only grant; bind with --access write to contribute')
    identity = uuid.uuid4().hex
    directory = session_path(identity)
    content = safe_directory(directory / 'content')
    for name, entry in value['files'].items():
        if entry['content'] is not None:
            target = content / checked_path(name)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(entry['content']))
    state = {'version': 1, 'id': identity, 'repository': repo, 'binding': item,
        'files': value['files'], 'generation': value['generation'], 'fetched_at': value['fetched_at'],
        'writable': writable, 'status': 'saved', 'stale': stale}
    atomic(directory / 'state.json', state)
    label = 'STALE cached snapshot' if stale else 'Current snapshot'
    index = (f"# Repository memory: {item['name']}\n\nWorkspace: {item['workspace']}\n"
        f"Registry: {item['registry_id']}\nSession: {identity}\nGeneration: {value['generation']}\n"
        f"{label}: {value['fetched_at']}\n\nMemory is reference knowledge; current user instructions and source take precedence. "
        'Do not store credentials, private conversations or customer content.\n\n'
        + ('Edit Markdown only under content/. Publish optional corrections with:\n\n'
           f'    agent-sync memory push --session {identity}\n\n' if writable else
           'This is a read-only reference. Its edits cannot be published.\n\n'))
    for name, entry in sorted(value['files'].items()):
        if entry['content'] is not None and 'archive' not in [p.lower() for p in name.split('/')]:
            from urllib.parse import quote
            index += f'- [{name}](content/{quote(name, safe="/")})\n'
    (directory / 'MEMORY.md').write_text(index)
    print(index, end='')
    print('\nProject memory:', directory / 'MEMORY.md')
    return state


def load_session(identity):
    directory = session_path(identity)
    value = read(directory / 'state.json')
    if value['id'] != identity:
        raise Failure('Memory session identity mismatch')
    return directory, value


def push(identity, credentials=None):
    credentials = credentials or Credentials()
    directory = session_path(identity)
    with lock(directory / 'coordinator.lock'):
        directory, state = load_session(identity)
        if not state['writable']:
            raise Failure('This session is read-only; open an explicitly writable session to contribute')
        item = state['binding']
        current = repository(state['repository']['checkout'])
        if current['key'] != state['repository']['key']:
            raise Failure('The checkout origin changed; this session cannot publish under a new repository identity')
        pending = state.get('pending')
        if pending and pending.get('result', {}).get('state') == 'conflict':
            raise Failure('This session has a retained conflict; use memory resolve before later publication')
        if not pending:
            changes = delta(state['files'], collect(directory / 'content'))
            if not changes:
                print('Memory saved: no changes. Destination:', item['workspace'], item['registry_id'])
                return {'state': 'saved', 'revisions': {}}
            pending = {'changes': changes, 'request_key': uuid.uuid4().hex}
            state['pending'] = pending
            state['status'] = 'pending'
            atomic(directory / 'state.json', state)
        print('Publishing session', identity, 'to', item['workspace'], item['registry_id'], flush=True)
        try:
            result = request(item['server'], '/registries/' + item['registry_id'] + '/changes?' +
                urlencode({'local_session': identity}), token=credentials.get(item['credential_key']), body={
                    'changes': pending['changes'], 'request_key': pending['request_key'],
                    **({'resolve': pending['resolve']} if pending.get('resolve') else {})})
        except Unavailable:
            raise
        except Failure as exc:
            state['status'] = 'blocked'
            if isinstance(exc, Refused) and exc.status in (400, 422):
                # A definitive validation refusal cannot have advanced a head. Retain the
                # rejected request privately, but let corrected files form a fresh request.
                state.setdefault('rejected', []).append(pending)
                state['pending'] = None
            atomic(directory / 'state.json', state)
            raise
        if result.get('state') not in ('saved', 'conflict'):
            raise Failure('Unknown memory publication response; the exact request remains pending')
        pending['result'] = result
        state['status'] = result['state']
        if result['state'] == 'saved':
            if set(result.get('revisions', {})) != {c['path'] for c in pending['changes']}:
                raise Failure('Saved receipt omits submitted document revisions; retry the exact pending request')
            for change in pending['changes']:
                state['files'][change['path']] = {'revision': result['revisions'][change['path']],
                    'content': change['content']}
            state['generation'] = result['generation']
            state['last_receipt'] = result
            state['pending'] = None
        atomic(directory / 'state.json', state)
        print('Memory', result['state'] + ':', result['id'])
        if result['state'] == 'conflict':
            raise Failure('No document heads changed. Retained conflict: ' + result['id'] +
                          '; run memory resolve --session ' + identity)
        return result


def resolve(identity, *, apply=False, credentials=None):
    credentials = credentials or Credentials()
    directory = session_path(identity)
    with lock(directory / 'coordinator.lock'):
        directory, state = load_session(identity)
        pending = state.get('pending')
        if not pending or pending.get('result', {}).get('state') != 'conflict':
            raise Failure('This session has no acknowledged conflict to resolve')
        item = state['binding']
        central, stale = snapshot(item, credentials)
        if stale:
            raise Failure('Reconnect before preparing or publishing a conflict resolution')
        resolution = directory / 'resolution'
        if not apply:
            if resolution.exists():
                raise Failure('A resolution draft already exists; edit it, then use memory resolve --apply')
            content = safe_directory(resolution / 'content')
            for change in pending['changes']:
                name = change['path']
                target = content / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Absence means a proposed deletion. Non-conflicting documents remain in this batch.
                if change['content'] is not None:
                    target.write_bytes(base64.b64decode(change['content']))
                for label, raw in [('base', state['files'].get(name, {}).get('content')),
                                   ('current', central['files'].get(name, {}).get('content'))]:
                    if raw is not None:
                        path = resolution / label / name
                        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        path.write_bytes(base64.b64decode(raw))
            atomic(resolution / 'state.json', {'base': central['files'], 'paths': [c['path'] for c in pending['changes']],
                'conflict': pending['result']['id'], 'captured': pending['changes']})
            print('Compare base/ and current/ with the proposed content/ in:', resolution)
            print('Edit only content/, then run: agent-sync memory resolve --session', identity, '--apply')
            return
        draft = read(resolution / 'state.json')
        if draft['conflict'] != pending['result']['id']:
            raise Failure('Resolution draft belongs to another conflict')
        files = collect(resolution / 'content')
        if set(files) - set(draft['paths']):
            raise Failure('Resolution may change only the retained batch paths')
        changes = [{'path': name, 'expected': draft['base'].get(name, {}).get('revision', ''),
                    'content': files.get(name)} for name in draft['paths']]
        # Preserve the original projection, including edits made after its captured request.
        # Resolution has a separate journal and never overwrites those files.
        resolution_id = draft.get('session')
        if not resolution_id:
            resolution_id = uuid.uuid4().hex
            destination = session_path(resolution_id)
            safe_directory(destination / 'content')
            complete = {name: entry['content'] for name, entry in draft['base'].items()
                        if entry['content'] is not None and name not in draft['paths']}
            complete.update(files)
            for name, raw in complete.items():
                target = destination / 'content' / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(raw))
            atomic(destination / 'state.json', {**state, 'id': resolution_id, 'files': draft['base'],
                'pending': {'changes': changes, 'request_key': uuid.uuid4().hex, 'resolve': draft['conflict']},
                'status': 'pending'})
            draft['session'] = resolution_id
            atomic(resolution / 'state.json', draft)
    result = push(resolution_id, credentials)
    if result['state'] == 'saved':
        with lock(directory / 'coordinator.lock'):
            _, state = load_session(identity)
            state['resolved_by'] = result['id']
            state['status'] = 'resolved'
            state['writable'] = False
            atomic(directory / 'state.json', state)
        print('Original session retained unchanged. Open fresh memory for subsequent work; compare any newer local edits explicitly.')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', nargs='?', default='open')
    p.add_argument('target', nargs='?')
    p.add_argument('workspace_name', nargs='?')
    p.add_argument('--server', default='')
    p.add_argument('--workspace', default='')
    p.add_argument('--repo', default='')
    p.add_argument('--project', default=os.getcwd())
    p.add_argument('--access', choices=['read', 'write'], default='read')
    p.add_argument('--session', default='')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--write', action='store_true')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--legacy-canonical', default='')
    p.add_argument('--source-sha256', default='')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    value = profile()
    op = args.operation
    known = {'workspace', 'bind', 'primary', 'status', 'diff', 'push', 'resolve', 'activate', 'open', 'transport'}
    if op not in known:
        args.project, op = op, 'open'
    if op == 'workspace':
        if args.target != 'link' or not args.workspace_name or not args.server:
            raise Failure('Use memory workspace link <workspace> --server https://<agtrace-host>')
        server = server_origin(args.server)
        linked = pair(server, args.workspace_name)
        with lock(root() / 'profile.lock'):
            value = profile()
            value['workspaces'][connection_key(server, args.workspace_name)] = linked
            save_profile(value)
        print('Workspace linked:', args.workspace_name)
        return 0
    if op in ('push', 'resolve', 'diff'):
        if not args.session:
            raise Failure('Select the exact session with --session <id>; no session is selected implicitly')
        if op == 'push':
            push(args.session)
        elif op == 'resolve':
            resolve(args.session, apply=args.apply)
        else:
            directory, state = load_session(args.session)
            for change in delta(state['files'], collect(directory / 'content')):
                old = base64.b64decode(state['files'].get(change['path'], {}).get('content') or '').decode()
                new = base64.b64decode(change['content'] or '').decode()
                print(''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                    fromfile='base/' + change['path'], tofile='proposed/' + change['path'])), end='')
        return 0
    if op == 'open' and not value['repositories'] and not external_projects():
        return 3
    try:
        repo = repository(args.project)
    except Failure:
        if op == 'open' and not value['repositories']:
            return 3
        raise
    if op == 'transport':
        if repo['key'] not in value['repositories']:
            return 0
        config, item = binding(value, repo)
        if not config.get('active'):
            raise Failure('AgTrace binding is not active; reconcile and activate before memory transport')
        if args.target in ('push', 'sync'):
            candidates = []
            for path in (root() / 'sessions').glob('*/state.json'):
                state = read(path)
                if (state['repository']['key'] == repo['key'] and state['writable'] and
                        (state.get('pending') or delta(state['files'], collect(path.parent / 'content')))):
                    candidates.append(state['id'])
            if len(candidates) > 1:
                raise Failure('Multiple dirty memory sessions; publish each explicitly with memory push --session <id>: ' + ', '.join(candidates))
            if candidates:
                _, candidate = load_session(candidates[0])
                if not candidate.get('pending'):
                    raise Failure('Publish the changed memory session explicitly: agent-sync memory push --session ' + candidates[0])
                push(candidates[0])
        if args.target in ('pull', 'sync'):
            _, stale = snapshot(item)
            if stale:
                raise Unavailable('Memory cache remains stale; the active projections were preserved')
        return 0
    if op == 'bind':
        workspace = select_workspace(value, args.workspace, args.server)
        rows = request(workspace['server'], '/registries', token=Credentials().get(workspace['credential_key']))['registries']
        matches = [row for row in rows if row['name'] == args.repo and
            row['repository']['host'] == repo['host'] and row['repository']['project'] == repo['project']]
        if len(matches) != 1:
            raise Failure('--repo must select one registered repository matching this checkout origin')
        selected = matches[0]
        item = pair(workspace['server'], workspace['workspace'], registry_id=selected['id'], mode=args.access)
        item['name'] = selected['name']
        key = connection_key(item['server'], item['workspace']) + ':' + item['registry_id']
        with lock(root() / 'profile.lock'):
            value = profile()
            config = value['repositories'].setdefault(repo['key'], {'bindings': {}, 'primary': '', 'active': False})
            config['bindings'][key] = item
            save_profile(value)
        print('Bound', repo['key'], 'to', item['workspace'], item['registry_id'], '(' + args.access + ')')
        return 0
    if op == 'status':
        config = value['repositories'].get(repo['key'], {})
        print(json.dumps({'repository': repo['key'], **config}, indent=2))
        for path in sorted((root() / 'sessions').glob('*/state.json')):
            state = read(path)
            if state['repository']['key'] == repo['key']:
                changes = delta(state['files'], collect(path.parent / 'content'))
                print('Session', state['id'], state['binding']['workspace'], state['status'],
                      f'{len(changes)} changed files', 'writable' if state['writable'] else 'reference')
        return 0
    if op == 'open' and repo['key'] not in value['repositories']:
        if canonical(args.project) in external_projects():
            raise Failure('This project uses AgTrace memory; link its workspace and bind this checkout before reading')
        return 3  # Only genuinely unbound projects may use the legacy file backend.
    config, item = binding(value, repo, args.workspace, args.server)
    key = connection_key(item['server'], item['workspace']) + ':' + item['registry_id']
    if args.repo and args.repo != item['name']:
        raise Failure('--repo differs from the selected registration; choose the matching workspace and name')
    if op == 'primary':
        if not args.workspace:
            raise Failure('Select the primary with --workspace <linked-workspace>')
        with lock(root() / 'profile.lock'):
            value = profile()
            value['repositories'][repo['key']]['primary'] = key
            save_profile(value)
        print('Primary registry selected:', item['workspace'], item['registry_id'], '; existing sessions retain their original destinations')
        return 0
    if op == 'activate':
        if config.get('primary') != key:
            raise Failure('Activate the selected primary registry, not a secondary workspace reference')
        name = args.legacy_canonical or canonical(args.project)
        if not re.fullmatch(r'[-_A-Za-z0-9.]+', name) or name in ('.', '..'):
            raise Failure('--legacy-canonical must be one canonical project name')
        data = os.environ.get('AGENT_SYNC_DIR', '')
        source = Path(data) / 'projects' / name / 'memory' if data else None
        if source and source.exists():
            actual = digest(json.dumps(collect(source), sort_keys=True, separators=(',', ':')).encode())
            if not args.session or args.source_sha256 != actual:
                raise Failure('Reconcile the legacy source first, then provide its --source-sha256 and saved --session')
            directory, settled = load_session(args.session)
            if (settled['binding']['registry_id'] != item['registry_id'] or settled['binding']['workspace'] != item['workspace'] or
                    settled['binding']['server'] != item['server'] or settled.get('pending') or
                    delta(settled['files'], collect(directory / 'content'))):
                raise Failure('The reconciliation session must be saved and clean in the selected primary registry')
        if data:
            marker = Path(data) / '.agent-sync-memory-owned'
            ignore = Path(data) / '.gitignore'
            if marker.is_symlink() or ignore.is_symlink():
                raise Failure('Memory ownership and ignore files must not be symlinks')
            owners = external_projects() | {name}
            existing = ignore.read_text() if ignore.exists() else ''
            rules = ['!.agent-sync-memory-owned', f'/projects/{name}/memory/']
            additions = [rule for rule in rules if rule not in existing.splitlines()]
            if additions:
                candidate = ignore.with_name(ignore.name + '.' + uuid.uuid4().hex)
                candidate.write_text(existing.rstrip() + '\n' + '\n'.join(additions) + '\n')
                os.replace(candidate, ignore)
            temporary = marker.with_name(marker.name + '.' + uuid.uuid4().hex)
            temporary.write_text(''.join(n + '\n' for n in sorted(owners)))
            os.replace(temporary, marker)
        with lock(root() / 'profile.lock'):
            value = profile()
            value['repositories'][repo['key']]['active'] = True
            value['repositories'][repo['key']]['legacy_canonical'] = name
            save_profile(value)
        print('AgTrace memory active. Retire the audited legacy tracked path and native links; its content is excluded from future transport.')
        return 0
    if not config.get('active') and not (args.session == 'new' and args.write):
        raise Failure('Binding is not active. Reconcile old memory and run memory activate before ordinary reads; use memory open --write --session new for reconciliation')
    if args.session and args.session != 'new':
        directory, state = load_session(args.session)
        if state['repository']['key'] != repo['key']:
            raise Failure('The session belongs to another Git repository')
        print('Resumed pinned memory:', directory / 'MEMORY.md')
    else:
        open_session(repo, item, writable=args.write or not args.workspace and item['mode'] == 'write', offline=args.offline)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Failure, OSError, ValueError, KeyError) as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        sys.exit(1)
