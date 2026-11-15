"""Offline consistent backup/restore. Stop the application before invoking."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def check_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity check failed')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Database foreign key check failed')
        for table in ('pages', 'batch_photos'):
            for row in conn.execute(f'SELECT original,image FROM {table}'):
                for name in row:
                    file = path.parent / 'uploads' / name
                    if file.resolve().parent != (path.parent / 'uploads').resolve() or not file.is_file():
                        raise ValueError('Referenced image is missing or unsafe')


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.is_relative_to(source) or not (source / 'dictation.sqlite3').is_file():
        raise ValueError('Use an existing database and an external, new backup directory')
    # Refuse links rather than following them outside the authorized data directory.
    paths = [source / 'uploads'] + list((source / 'uploads').rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise ValueError('Symlinks are not permitted in uploads')
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    with closing(sqlite3.connect((source / 'dictation.sqlite3').as_uri() + '?mode=ro', uri=True)) as conn, \
            closing(sqlite3.connect(target / 'dictation.sqlite3')) as out:
        conn.backup(out)
    shutil.copytree(source / 'uploads', target / 'uploads')
    check_database(target / 'dictation.sqlite3')
    files = {p.relative_to(target).as_posix(): digest(p) for p in target.rglob('*') if p.is_file()}
    (target / 'manifest.json').write_text(json.dumps({
        'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': files
    }, indent=2), encoding='utf-8')
    return len(files)


def verify(source):
    source = Path(source).resolve()
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('Invalid backup manifest')
    expected = manifest['files']
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()} - {'manifest.json'}
    if set(expected) != actual or any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('Backup file list does not match')
    for name, checksum in expected.items():
        path = (source / name).resolve()
        if not path.is_relative_to(source) or digest(path) != checksum:
            raise ValueError('Backup checksum mismatch')
    check_database(source / 'dictation.sqlite3')
    return len(expected)


def restore(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    verify(source)
    if target.is_relative_to(source):
        raise ValueError('Restore target must be outside the backup directory')
    if target.exists():
        raise FileExistsError('Restore target must not exist; original data is never overwritten')
    shutil.copytree(source, target)
    verify(target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'verify', 'restore'))
    parser.add_argument('source', type=Path)
    parser.add_argument('target', nargs='?', type=Path)
    parser.add_argument('--app-stopped', action='store_true', help='Confirm the application and all writers are stopped')
    args = parser.parse_args()
    if args.action != 'verify' and (not args.app_stopped or args.target is None):
        parser.error('backup/restore require target and --app-stopped')
    if args.action == 'verify':
        verify(args.source)
    else:
        {'backup': backup, 'restore': restore}[args.action](args.source, args.target)
    print('Backup operation verified.')
