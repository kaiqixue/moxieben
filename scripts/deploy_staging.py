#!/usr/bin/python3
"""Root-owned, forced-command Staging updater. No database restoration or migrations.

Install separately after review; GitHub can request only a digest and full Git SHA.
Configuration is /etc/moxieben-staging-deploy.json (see docs/DEPLOY/GITHUB-CICD-PLAN.md).
"""
import hashlib
import http.client
import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


CONFIG = Path('/etc/moxieben-staging-deploy.json')
SCRIPT = '/usr/local/libexec/moxieben-staging-deploy.py'
BACKUP_TOOL = '/usr/local/libexec/moxieben-backup.py'
DATA = Path('/srv/moxieben/staging/data')
BACKUPS = Path('/srv/moxieben/staging/backups')
OVERRIDE = Path('/etc/moxieben-staging-image.yml')
BLOCKED = BACKUPS / 'NEEDS_REVIEW'
PENDING = BACKUPS / 'IN_PROGRESS'
DIGEST = r'sha256:[a-f0-9]{64}'
REVISION = r'[a-f0-9]{40}'


class ReleaseError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def parse_command(command):
    match = re.fullmatch(rf'deploy ({DIGEST}) ({REVISION})', command)
    require(match is not None, 'Only deploy <sha256:digest> <full Git SHA> is allowed')
    return match.groups()


def protected(path):
    """Do not execute root commands using configuration writable by a deploy user."""
    path = Path(path)
    require(path.is_absolute() and path.exists(), 'Required protected path is missing')
    for entry in (path, *path.parents):
        info = entry.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                and not info.st_mode & 0o022, 'Deployment paths must be root-owned and protected')


def schema_signature(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        rows = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def check_compose(config):
    require(config.get('name') == 'moxieben-staging', 'Wrong Compose project')
    services = config.get('services', {})
    require(set(services) == {'app', 'caddy'}, 'Unexpected Compose services')
    app = services['app']
    env = app.get('environment', {})
    require(env.get('APP_ENV') == 'staging' and str(env.get('REGISTRATION_OPEN')) == '0'
            and env.get('DATA_DIR') == '/var/lib/moxieben', 'Staging configuration guard failed')
    require(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', env.get('APP_HOST', '')) is not None,
            'Expected a DNS hostname without scheme or port')
    volumes = [v for v in app.get('volumes', []) if v.get('target') == '/var/lib/moxieben']
    require(len(volumes) == 1 and volumes[0].get('type') == 'bind'
            and volumes[0].get('source') == str(DATA) and not volumes[0].get('read_only')
            and volumes[0].get('bind', {}).get('create_host_path') is False,
            'Existing persistent bind mount must be preserved')
    return env['APP_HOST']


class Deployment:
    def __init__(self):
        protected(CONFIG)
        protected(SCRIPT)
        protected(BACKUP_TOOL)
        self.config = json.loads(CONFIG.read_text(encoding='utf-8'))
        self.repository = self.config['repository']
        require(re.fullmatch(r'[a-z0-9.-]+/[a-z0-9._/-]+', self.repository) is not None,
                'Invalid fixed registry/repository')
        for key in ('project_dir', 'compose_file', 'env_file'):
            protected(self.config[key])
        if self.config.get('ca_file'):
            protected(self.config['ca_file'])
        protected(BACKUPS)
        require(not BLOCKED.exists() and not PENDING.exists(), 'Previous deployment needs manual review')
        require(DATA.is_dir() and DATA.resolve() == DATA
                and (DATA / 'dictation.sqlite3').is_file(), 'Existing Staging database is required')
        if OVERRIDE.exists():
            protected(OVERRIDE)

    def run(self, *args, timeout=360):
        # Never print captured Compose/Docker output: it may contain environment values.
        try:
            result = subprocess.run(args, capture_output=True, text=True, check=False,
                                    timeout=timeout, cwd=self.config['project_dir'],
                                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
                                         'DOCKER_CONFIG': '/root/.docker'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError('Deployment command failed or timed out; inspect locally') from exc
        require(result.returncode == 0, 'Deployment command failed; inspect locally (output withheld)')
        return result.stdout.strip()

    def compose(self, *args):
        command = ['/usr/bin/docker', 'compose', '--project-name', 'moxieben-staging',
                   '--project-directory', self.config['project_dir'],
                   '--env-file', self.config['env_file'], '-f', self.config['compose_file']]
        if OVERRIDE.exists():
            command += ['-f', str(OVERRIDE)]
        return self.run(*command, *args)

    def inspect(self, target):
        return json.loads(self.run('/usr/bin/docker', 'inspect', target))[0]

    def app(self):
        identifier = self.compose('ps', '--all', '-q', 'app')
        require(bool(identifier) and '\n' not in identifier, 'Expected exactly one app container')
        return self.inspect(identifier)

    def preflight(self):
        config = json.loads(self.compose('config', '--format', 'json'))
        self.host = check_compose(config)
        self.expected_env = config['services']['app']['environment']
        # The fixed trusted Compose files control the secret paths, never GitHub input.
        for item in config.get('secrets', {}).values():
            protected(item['file'])
        for service in config['services'].values():
            for volume in service.get('volumes', []):
                if volume.get('type') == 'bind' and volume['source'] != str(DATA):
                    protected(volume['source'])
        self.old = self.app()
        require(self.old['State'].get('Health', {}).get('Status') == 'healthy', 'Old app is unhealthy')
        self.check_runtime(self.old)
        images = self.inspect(self.old['Image']).get('RepoDigests', [])
        allowed = [image for image in images
                   if re.fullmatch(re.escape(self.repository) + '@' + DIGEST, image)]
        require(bool(allowed), 'Old image needs a registry digest before enabling automatic deployment')
        self.old_image = allowed[0]
        caddy = self.inspect(self.compose('ps', '-q', 'caddy'))
        require(caddy['State']['Running'], 'Caddy is not running')
        # Refuse a second running container with a writable overlapping data mount.
        for identifier in self.run('/usr/bin/docker', 'ps', '-q').split():
            info = self.inspect(identifier)
            if info['Id'] == self.old['Id']:
                continue
            for mount in info.get('Mounts', []):
                source = Path(mount.get('Source', '/nonexistent')).resolve()
                require(not (mount.get('RW') and (DATA.is_relative_to(source) or source.is_relative_to(DATA))),
                        'Another container may write Staging data')
        free = shutil.disk_usage(BACKUPS).free
        size = sum(file.stat().st_size for file in DATA.rglob('*') if file.is_file())
        require(free > size * 2 + 1024 ** 3, 'Insufficient space for backup and image update')

    def check_runtime(self, app):
        env = dict(item.split('=', 1) for item in app['Config']['Env'] if '=' in item)
        require(all(env.get(key) == value for key, value in {
            'APP_ENV': 'staging', 'REGISTRATION_OPEN': '0', 'DATA_DIR': '/var/lib/moxieben'
        }.items()), 'Running container configuration differs')
        require(all(env.get(key) == str(value) for key, value in self.expected_env.items()),
                'Compose environment differs from the running application')
        require(not any(app.get('NetworkSettings', {}).get('Ports', {}).values()),
                'App ports must not bypass the Caddy maintenance boundary')
        mounts = [m for m in app['Mounts'] if m['Destination'] == '/var/lib/moxieben']
        require(len(mounts) == 1 and mounts[0]['Source'] == str(DATA) and mounts[0]['RW'],
                'Running container data mount differs')

    def pull(self, image, revision):
        self.run('/usr/bin/docker', 'pull', image)
        new = self.inspect(image)
        require(new['Os'] == 'linux' and new['Architecture'] == 'amd64'
                and new['Config'].get('User') == '10001:10001'
                and new['Config'].get('Labels', {}).get('org.opencontainers.image.revision') == revision,
                'Image platform/revision mismatch')
        self.new_id = new['Id']

    def idle(self):
        # Standalone stdlib only; importing app/wsgi would run startup database writes.
        program = """import sqlite3
c=sqlite3.connect('file:/var/lib/moxieben/dictation.sqlite3?mode=ro',uri=True)
c.execute('PRAGMA query_only=ON')
queries=["SELECT count(*) FROM grading_jobs WHERE state IN ('queued','running')",
         "SELECT count(*) FROM pages WHERE status='running'",
         "SELECT count(*) FROM submissions WHERE grading_state='running'",
         "SELECT count(*) FROM batch_photos WHERE status='identifying'"]
print(sum(c.execute(q).fetchone()[0] for q in queries))
c.close()
"""
        return self.run('/usr/bin/docker', 'exec', self.app()['Id'], 'python', '-B', '-c', program) == '0'

    def pause(self):
        self.compose('stop', 'caddy')

    def begin(self, image, revision):
        with PENDING.open('x', encoding='utf-8') as stream:
            json.dump({'image': image, 'revision': revision, 'previous_image': self.old_image}, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def drain(self):
        deadline = time.monotonic() + 330
        while time.monotonic() < deadline:
            if self.idle():
                return
            time.sleep(2)
        raise ReleaseError('Work is still active; deployment refused')

    def stop_app(self, *, clean=True):
        self.compose('stop', '--timeout', '330', 'app')
        state = self.app()['State']
        require(not state['Running'] and (not clean or state['ExitCode'] == 0), 'App did not stop safely')

    def backup(self):
        release_dir = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        release_dir.mkdir(mode=0o700)
        self.backup_path = release_dir / 'data'
        self.run('/usr/bin/python3', '-B', BACKUP_TOOL, 'backup', str(DATA), str(self.backup_path), '--app-stopped')
        self.run('/usr/bin/python3', '-B', BACKUP_TOOL, 'verify', str(self.backup_path))
        self.before_schema = schema_signature(self.backup_path / 'dictation.sqlite3')
        # Archive the fixed deployment inputs without printing secret values.
        inputs = release_dir / 'deployment-inputs'
        inputs.mkdir(mode=0o700)
        for key in ('compose_file', 'env_file'):
            shutil.copyfile(self.config[key], inputs / key)
        if OVERRIDE.exists():
            shutil.copyfile(OVERRIDE, inputs / 'image.yml')

    def set_image(self, image):
        require(re.fullmatch(re.escape(self.repository) + '@' + DIGEST, image) is not None,
                'Image must belong to the fixed repository')
        descriptor, name = tempfile.mkstemp(prefix='.moxieben-image-', dir=OVERRIDE.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write('services:\n  app:\n    image: ' + json.dumps(image) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, OVERRIDE)
        finally:
            Path(name).unlink(missing_ok=True)

    def start_app(self):
        self.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'app')

    def restart_old(self):
        # Backup/stop failure: start the original container, never a mutable Compose tag.
        self.run('/usr/bin/docker', 'start', self.old['Id'])

    def healthy(self, expected_id=None):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            app = self.app()
            self.check_runtime(app)
            if expected_id:
                require(app['Image'] == expected_id, 'Unexpected running image')
            if app['State'].get('Health', {}).get('Status') == 'healthy':
                return
            require(app['State']['Running'], 'App exited')
            time.sleep(2)
        raise ReleaseError('App health check timed out')

    def schema_unchanged(self):
        return schema_signature(DATA / 'dictation.sqlite3') == self.before_schema

    def resume(self):
        self.compose('start', 'caddy')

    def https_check(self):
        context = ssl.create_default_context(cafile=self.config.get('ca_file') or None)
        for route, status, marker in [('/healthz', 200, b'"ok"'),
                                      ('/teacher/login', 200, b'login-form'),
                                      ('/static/theme.css', 200, b':root'),
                                      ('/teacher/register', 403, None)]:
            last_error = None
            for _ in range(15):
                connection = http.client.HTTPSConnection(self.host, timeout=5, context=context)
                try:
                    # Local ECS connection; retain real SNI, Host and certificate verification.
                    connection.sock = context.wrap_socket(socket.create_connection(('127.0.0.1', 443), 5),
                                                          server_hostname=self.host)
                    connection.request('GET', route, headers={'Host': self.host})
                    response = connection.getresponse()
                    body = response.read(1024 * 1024)
                    require(response.status == status and (marker is None or marker in body),
                            'HTTPS application smoke check failed')
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(1)
                finally:
                    connection.close()
            require(last_error is None, 'HTTPS endpoint is unavailable')

    def record(self, image, revision):
        (BACKUPS / 'last-release.json').write_text(json.dumps({
            'image': image, 'revision': revision, 'previous_image': self.old_image,
            'backup': str(self.backup_path), 'time': datetime.now(timezone.utc).isoformat()
        }, indent=2), encoding='utf-8')
        PENDING.unlink()
        print(f'Staging verified: {revision}; backup: {self.backup_path.parent.name}/data', flush=True)

    def block(self):
        try:
            self.pause()
        finally:
            BLOCKED.write_text('Manual review required. Do not restore the live database automatically.\n', encoding='utf-8')

    def deploy(self, digest, revision):
        parse_command(f'deploy {digest} {revision}')
        image = self.repository + '@' + digest
        print('Staging: checking protected configuration and current service', flush=True)
        self.preflight()
        self.pull(image, revision)
        require(self.idle(), 'Active background work; retry after it completes')
        self.begin(image, revision)
        paused = stopped = changed = traffic = False
        try:
            paused = True
            self.pause()
            self.drain()
            stopped = True
            self.stop_app()
            print('Staging: writers stopped; creating and verifying backup', flush=True)
            self.backup()
            changed = True
            self.set_image(image)
            print('Staging: starting the requested immutable image', flush=True)
            self.start_app()
            self.healthy(self.new_id)
            require(self.schema_unchanged(), 'Database schema changed; manual review required')
            # A partially successful Caddy start can already admit writes.
            traffic = True
            self.resume()
            print('Staging: checking HTTPS and application pages', flush=True)
            self.https_check()
            self.record(image, revision)
        except Exception as error:
            try:
                if changed and not traffic:
                    self.stop_app(clean=False)
                if traffic or (changed and not self.schema_unchanged()):
                    self.block()
                else:
                    if changed:
                        self.set_image(self.old_image)
                        self.start_app()
                    elif stopped:
                        self.restart_old()
                    if stopped:
                        self.healthy(self.old['Image'])
                    if paused:
                        self.resume()
            except Exception:
                self.block()
            raise ReleaseError('Release failed; see protected server backup/review state') from error


def main():
    if len(sys.argv) == 1 and os.geteuid() != 0:
        digest, revision = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        os.execv('/usr/bin/sudo', ['sudo', '-n', SCRIPT, 'deploy', digest, revision])
    require(os.geteuid() == 0 and len(sys.argv) == 4 and sys.argv[1] == 'deploy',
            'Use the installed forced-command entry point')
    digest, revision = parse_command(' '.join(sys.argv[1:]))
    os.umask(0o077)
    import fcntl  # Linux only; unit tests can import this module on Windows.
    with open('/run/lock/moxieben-staging-deploy.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError('Another deployment is running') from exc
        def interrupted(signum, frame):
            raise ReleaseError('Deployment interrupted; review the persistent IN_PROGRESS marker')
        for event in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(event, interrupted)
        Deployment().deploy(digest, revision)


if __name__ == '__main__':
    try:
        main()
    except ReleaseError as error:
        print(f'Staging deployment refused or failed: {error}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Tracebacks / subprocess output can contain deployment configuration.
        print('Staging deployment refused or failed; administrator review required.', file=sys.stderr)
        sys.exit(1)
