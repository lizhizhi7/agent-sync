#!/usr/bin/env python3
"""Pin and export explicitly selected Git resources; no user-home linking or execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

MAX_BYTES = 100_000_000
MAX_FILES = 10000


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError(f'Invalid resource path: {value!r}')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')) or '.git' in path.parts:
        raise ValueError(f'Resource path must be a bounded relative path: {value!r}')
    return value


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout


def inventory(root, commit, path, exclude):
    entries = git(root, 'ls-tree', '-rlz', f'{commit}:{relative(path)}').split(b'\0')
    result = {}; size = 0
    for entry in filter(None, entries):
        info, raw_name = entry.split(b'\t', 1)
        mode, kind, oid, length = info.decode().split()
        name = relative(raw_name.decode('utf-8'))
        if name in exclude or any(name.startswith(p + '/') for p in exclude):
            continue
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError(f'Resource {path}/{name}: links and submodules are not exportable')
        size += int(length)
        if size > MAX_BYTES or len(result) >= MAX_FILES:
            raise ValueError(f'Resource {path} exceeds export bounds')
        body = git(root, 'cat-file', 'blob', oid)
        result[name] = {'sha256': hashlib.sha256(body).hexdigest(), 'mode': mode, 'bytes': len(body), 'blob': oid}
    if not result:
        raise ValueError(f'Resource {path} has no selected files')
    return result


def digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def lock(root, manifest):
    if manifest.get('version') != 1 or not isinstance(manifest.get('resources'), list):
        raise ValueError('Resource manifest requires version 1 and a resources list')
    revision=manifest.get('revision','HEAD')
    if not isinstance(revision,str) or not revision or revision.startswith('-') or len(revision)>256:
        raise ValueError('Resource revision must be a bounded Git revision')
    if len(manifest['resources'])>256:raise ValueError('At most 256 resources may be selected')
    commit = git(root, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    source = git(root, 'remote', 'get-url', 'origin').decode().strip()
    # Never retain URL userinfo or signed query strings in provenance.
    if re.match(r'https?://[^/]*@', source) or '?' in source or '#' in source:
        raise ValueError('Resource origin must not contain credentials, query or fragment')
    resources = []; seen = set()
    for item in manifest['resources']:
        identity = relative(item['id'])
        if any(identity == old or identity.startswith(old + '/') or old.startswith(identity + '/') for old in seen):
            raise ValueError(f'Overlapping export resource: {identity}')
        seen.add(identity)
        exclude = [relative(x) for x in item.get('exclude', [])]
        files = inventory(root, commit, item['path'], exclude)
        resources.append({'id': identity, 'path': item['path'], 'exclude': exclude,
                          'revision': digest(files), 'files': files})
    return {'version': 1, 'source': source, 'commit': commit, 'resources': resources}


def export(root, locked, destination):
    if locked.get('version') != 1 or not re.fullmatch(r'[a-f0-9]{40,64}', locked.get('commit', '')):
        raise ValueError('Export requires a versioned lock with an exact Git commit')
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f'Export destination already exists: {destination}; export to a fresh candidate and review it')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.resource-export-', dir=destination.parent))
    try:
        seen = set(); total = 0; count = 0
        if len(locked['resources'])>256:raise ValueError('At most 256 resources may be exported')
        for item in locked['resources']:
            identity = relative(item['id'])
            if any(identity == old or identity.startswith(old + '/') or old.startswith(identity + '/') for old in seen):
                raise ValueError(f'Overlapping export resource: {identity}')
            seen.add(identity)
            actual = inventory(root, locked['commit'], item['path'], item.get('exclude', []))
            if actual != item['files'] or digest(actual) != item['revision']:
                raise ValueError(f'Locked resource changed: {identity}')
            for name, spec in actual.items():
                total += spec['bytes']; count += 1
                if total > MAX_BYTES or count > MAX_FILES:
                    raise ValueError('Selected resources exceed export byte budget')
                target = staging / identity / relative(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(git(root, 'cat-file', 'blob', spec['blob']))
                target.chmod(0o755 if spec['mode'] == '100755' else 0o644)
        (staging / 'resources.lock').write_text(json.dumps(locked, ensure_ascii=False, indent=2) + '\n')
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('lock')
    create.add_argument('manifest'); create.add_argument('--output', required=True)
    emit = sub.add_parser('export')
    emit.add_argument('lock'); emit.add_argument('--destination', required=True)
    for command in (create, emit):
        command.add_argument('--source-dir', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'lock':
            result = lock(args.source_dir, json.loads(Path(args.manifest).read_text()))
            output = Path(args.output)
            with output.open('x') as file:
                file.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        else:
            export(args.source_dir, json.loads(Path(args.lock).read_text()), args.destination)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Resource {args.command} failed: {exc}\n')


if __name__ == '__main__':
    main()
