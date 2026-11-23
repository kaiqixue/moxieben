"""Release boundaries; fake Docker operations and synthetic temporary SQLite only."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import deploy_staging as release
from scripts import ci_verify


DIGEST = 'sha256:' + 'a' * 64
REVISION = 'b' * 40


class DeploymentTests(unittest.TestCase):
    def test_compose_omitted_false_flag_stays_safe_but_explicit_true_is_rejected(self):
        config={'name':'moxieben-staging','services':{'app':{
            'environment':{'APP_ENV':'staging','REGISTRATION_OPEN':'0','DATA_DIR':'/var/lib/moxieben','APP_HOST':'school.example.test'},
            'volumes':[{'type':'bind','source':str(release.DATA),'target':'/var/lib/moxieben','bind':{}}]},'caddy':{}}}
        self.assertEqual(release.check_compose(config),'school.example.test')
        config['services']['app']['volumes'][0]['bind']['create_host_path']=True
        with self.assertRaises(release.ReleaseError):
            release.check_compose(config)

    def test_forced_command_accepts_only_digest_and_full_revision(self):
        self.assertEqual(release.parse_command(f'deploy {DIGEST} {REVISION}'), (DIGEST, REVISION))
        for command in ('bash', f'deploy latest {REVISION}', f'deploy {DIGEST} main',
                        f'deploy {DIGEST} {REVISION}; id', f'deploy {DIGEST} {REVISION}\n',
                        f'deploy {DIGEST} {REVISION} extra'):
            with self.subTest(command=command), self.assertRaises(release.ReleaseError):
                release.parse_command(command)

    def test_compose_rejects_wrong_environment_data_or_registration(self):
        config = {'name': 'moxieben-staging', 'services': {'app': {
            'environment': {'APP_ENV': 'staging', 'REGISTRATION_OPEN': '0',
                            'DATA_DIR': '/var/lib/moxieben', 'APP_HOST': 'school.example.test'},
            'volumes': [{'type': 'bind', 'source': str(release.DATA),
                         'target': '/var/lib/moxieben', 'read_only': False,
                         'bind': {'create_host_path': False}}]}, 'caddy': {}}}
        release.check_compose(config)
        for key, value in [('APP_ENV', 'production'), ('REGISTRATION_OPEN', '1'),
                           ('DATA_DIR', '/tmp'), ('APP_HOST', '*')]:
            with self.subTest(key=key):
                other = json.loads(json.dumps(config))
                other['services']['app']['environment'][key] = value
                with self.assertRaises(release.ReleaseError):
                    release.check_compose(other)
        config['services']['app']['volumes'][0]['source'] = '/srv/moxieben/production/data'
        with self.assertRaises(release.ReleaseError):
            release.check_compose(config)

    def test_schema_fingerprint_is_read_only_and_detects_schema_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'synthetic.sqlite3'
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute('CREATE TABLE example (id INTEGER PRIMARY KEY)')
            before = db.read_bytes()
            signature = release.schema_signature(db)
            self.assertEqual(db.read_bytes(), before)
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute('ALTER TABLE example ADD COLUMN extra TEXT')
            self.assertNotEqual(signature, release.schema_signature(db))

    def scenario(self, *, failure=None, schema_ok=True, busy=False):
        events = []
        deployment = release.Deployment.__new__(release.Deployment)
        deployment.repository = 'registry.example.test/team/app'
        deployment.old_image = deployment.repository + '@sha256:' + 'c' * 64
        deployment.old = {'Image': 'synthetic-old-id'}
        deployment.new_id = 'synthetic-new-id'
        deployment.backup_path = Path('synthetic-backup')
        methods = ['preflight', 'pull', 'begin', 'pause', 'drain', 'stop_app', 'backup',
                   'set_image', 'start_app', 'restart_old', 'healthy', 'resume', 'https_check', 'record', 'block']
        def operation(name):
            def call(*args, **kwargs):
                events.append(name)
                if name == failure and events.count(name) == 1:
                    raise release.ReleaseError('synthetic failure')
            return call
        with patch.multiple(deployment, **{name: operation(name) for name in methods}), \
                patch.object(deployment, 'idle', return_value=not busy), \
                patch.object(deployment, 'schema_unchanged', return_value=schema_ok):
            try:
                deployment.deploy(DIGEST, REVISION)
                error = None
            except release.ReleaseError as exc:
                error = exc
        return events, error

    def test_busy_app_is_not_stopped(self):
        events, error = self.scenario(busy=True)
        self.assertIsNotNone(error)
        self.assertNotIn('pause', events)
        self.assertNotIn('stop_app', events)

    def test_success_backs_up_before_image_change_and_opens_traffic_last(self):
        events, error = self.scenario()
        self.assertIsNone(error)
        for first, second in [('pause', 'stop_app'), ('stop_app', 'backup'),
                              ('backup', 'set_image'), ('healthy', 'resume'),
                              ('https_check', 'record')]:
            self.assertLess(events.index(first), events.index(second))
        self.assertNotIn('block', events)

    def test_backup_failure_recovers_old_service_without_changing_image(self):
        events, error = self.scenario(failure='backup')
        self.assertIsNotNone(error)
        self.assertNotIn('set_image', events)
        self.assertIn('restart_old', events)
        self.assertNotIn('start_app', events)
        self.assertIn('resume', events)

    def test_failure_before_traffic_rolls_back_image_without_restoring_database(self):
        events, error = self.scenario(failure='healthy')
        self.assertIsNotNone(error)
        self.assertEqual(events.count('set_image'), 2)
        self.assertIn('resume', events)
        self.assertNotIn('block', events)

    def test_block_stops_traffic_even_if_review_marker_cannot_be_written(self):
        deployment = release.Deployment.__new__(release.Deployment)
        with patch.object(deployment, 'pause') as pause, \
                patch.object(Path, 'write_text', side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError):
                deployment.block()
        pause.assert_called_once()

    def test_schema_change_or_failure_after_traffic_requires_manual_review(self):
        for kwargs in ({'schema_ok': False}, {'failure': 'https_check'}, {'failure': 'resume'}):
            with self.subTest(kwargs=kwargs):
                events, error = self.scenario(**kwargs)
                self.assertIsNotNone(error)
                self.assertIn('block', events)
                self.assertEqual(events.count('set_image'), 1)

    def test_linux_gate_rejects_skipped_or_missing_gunicorn_test(self):
        required = ci_verify.RUNTIME_TEST
        for passed in ([], ['another.test']):
            with self.assertRaises(RuntimeError):
                ci_verify.require_runtime(passed)
        ci_verify.require_runtime([required])

    def test_release_metadata_does_not_invalidate_verified_data_backup(self):
        import backup_data
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, backups = root / 'data', root / 'backups'
            data.mkdir()
            backups.mkdir()
            (data / 'uploads').mkdir()
            (data / 'uploads/synthetic.txt').write_text('synthetic')
            with closing(sqlite3.connect(data / 'dictation.sqlite3')) as connection, connection:
                for table in ('pages', 'batch_photos'):
                    connection.execute(f'CREATE TABLE {table} (original TEXT,image TEXT)')
            (root / 'compose').write_text('synthetic compose')
            (root / 'env').write_text('SYNTHETIC=1')
            deployment = release.Deployment.__new__(release.Deployment)
            deployment.config = {'compose_file': str(root / 'compose'), 'env_file': str(root / 'env')}
            def command(*args):
                if args[3] == 'backup':
                    backup_data.backup(args[4], args[5])
                elif args[3] == 'verify':
                    backup_data.verify(args[4])
                else:
                    self.fail('Unexpected subprocess requested')
            with patch.object(release, 'DATA', data), patch.object(release, 'BACKUPS', backups), \
                    patch.object(release, 'OVERRIDE', root / 'absent.yml'), \
                    patch.object(deployment, 'run', side_effect=command):
                deployment.backup()
            backup_data.verify(deployment.backup_path)
            self.assertTrue((deployment.backup_path.parent / 'deployment-inputs/env_file').is_file())

    def test_image_override_rejects_another_repository_and_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            override = Path(folder) / 'image.yml'
            deployment = release.Deployment.__new__(release.Deployment)
            deployment.repository = 'registry.example.test/team/app'
            with patch.object(release, 'OVERRIDE', override):
                image = deployment.repository + '@' + DIGEST
                deployment.set_image(image)
                before = override.read_bytes()
                self.assertIn(json.dumps(image), before.decode())
                with self.assertRaises(release.ReleaseError):
                    deployment.set_image('registry.example.test/other/app@' + DIGEST)
                self.assertEqual(override.read_bytes(), before)

    def test_failed_new_container_can_be_stopped_for_rollback_but_old_stop_must_be_clean(self):
        deployment = release.Deployment.__new__(release.Deployment)
        with patch.object(deployment, 'compose'), patch.object(deployment, 'app', return_value={
                'State': {'Running': False, 'ExitCode': 1}}):
            with self.assertRaises(release.ReleaseError):
                deployment.stop_app()
            deployment.stop_app(clean=False)


if __name__ == '__main__':
    unittest.main()
