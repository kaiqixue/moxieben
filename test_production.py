"""Production boundary checks; all data and credentials are synthetic."""
import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class ProductionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in [('session', 'test-only-' * 8), ('paddle', 'fake-token-not-a-real-key')]:
            (self.root / name).write_text(value, encoding='utf-8')
        self.env = {'APP_ENV': 'staging', 'APP_HOST': 'school.example.test',
                    'DATA_DIR': str(self.root / 'data'),
                    'SECRET_KEY_FILE': str(self.root / 'session'),
                    'PADDLE_OCR_TOKEN_FILE': str(self.root / 'paddle'),
                    'PADDLE_RESULT_HOSTS': 'results.example.test',
                    'REGISTRATION_OPEN': '1'}

    def app(self):
        from production import create_production_app
        app = create_production_app(self.env)
        app.config['TESTING'] = True
        app.config['GRADING_JOBS_AUTOSTART'] = False
        return app

    def token(self, client, path='/teacher/register'):
        page = client.get(path, base_url='https://school.example.test')
        html = page.get_data(as_text=True)
        return re.search(r'name="csrf(?:-|_)token" (?:content|value)="([^"]+)"', html).group(1)

    def signup(self, client, email):
        return client.post('/teacher/register', base_url='https://school.example.test',
            headers={'Origin': 'https://school.example.test'}, data={
                'csrf_token': self.token(client), 'name': '虚构教师', 'email': email,
                'password': 'example-1234', 'confirm_password': 'example-1234'})

    def test_configuration_fails_closed_before_creating_database(self):
        from production import load_config
        for field in ('APP_HOST', 'DATA_DIR', 'SECRET_KEY_FILE', 'PADDLE_OCR_TOKEN_FILE', 'PADDLE_RESULT_HOSTS'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_config({k: v for k, v in self.env.items() if k != field})
        for field, value in [('APP_HOST', 'https://wrong.test'), ('APP_HOST', '*'),
                             ('DATA_DIR', 'relative'), ('FLASK_DEBUG', '1'),
                             ('REGISTRATION_OPEN', 'maybe')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_config(dict(self.env, **{field: value}))
        self.assertFalse((self.root / 'data').exists())

    def test_domain_origin_cookie_health_and_persistent_session(self):
        app = self.app()
        client = app.test_client()
        self.assertEqual(client.get('/teacher/login', base_url='https://evil.example.test').status_code, 400)
        self.assertEqual(client.get('/healthz').json, {'status': 'ok'})
        self.assertEqual(client.get('/api/tasks', base_url='https://school.example.test').status_code, 401)
        self.assertEqual(client.get('/teacher/login').status_code, 403)
        self.assertEqual(client.post('/teacher/register', base_url='https://school.example.test').status_code, 403)
        self.assertEqual(self.signup(client, 'teacher-a@example.test').status_code, 303)
        cookie = client.get_cookie('moxieben_accounts', domain='school.example.test')
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.http_only)
        restarted = self.app().test_client()
        restarted.set_cookie(cookie.key, cookie.value, domain='school.example.test')
        self.assertEqual(restarted.get('/', base_url='https://school.example.test').status_code, 200)
        self.assertFalse(app.debug)
        self.assertEqual(client.get('/api/settings', base_url='https://school.example.test').status_code, 403)
        with closing(sqlite3.connect(self.root / 'data/dictation.sqlite3')) as conn:
            self.assertNotIn('fake-token-not-a-real-key', repr(conn.execute('SELECT * FROM settings').fetchall()))
        with app.app_context():
            from settings import read_settings
            self.assertEqual(read_settings(None)['provider'], 'paddle_cloud')
            self.assertEqual(read_settings(None)['paddle_token'], 'fake-token-not-a-real-key')

    def test_forwarded_address_uses_one_trusted_proxy_hop_and_registration_can_close(self):
        app = self.app()
        client = app.test_client()
        with client:
            response = client.get('/teacher/login', base_url='http://school.example.test', headers={
                'X-Forwarded-For': 'spoofed, 198.51.100.7', 'X-Forwarded-Proto': 'https',
                'X-Forwarded-Host': 'evil.example.test'})
            from flask import request
            self.assertEqual(response.status_code, 200)
            self.assertEqual(request.remote_addr, '198.51.100.7')
            self.assertEqual(request.host, 'school.example.test')
        self.env['REGISTRATION_OPEN'] = '0'
        closed = self.app().test_client()
        self.assertEqual(closed.get('/teacher/register', base_url='https://school.example.test').status_code, 403)
        self.assertNotIn('/teacher/register', closed.get('/teacher/login',
                         base_url='https://school.example.test').get_data(as_text=True))

    def test_registration_limits_by_proxy_client_and_email(self):
        client = self.app().test_client()
        for i in range(8):
            self.assertEqual(self.signup(client, f'invalid-{i}').status_code, 400)
        self.assertEqual(self.signup(client, 'valid@example.test').status_code, 429)

    def test_production_delete_is_blocked_before_creating_full_database_copy(self):
        app = self.app()
        client = app.test_client()
        self.signup(client, 'teacher-a@example.test')
        base = 'https://school.example.test'
        headers = {'Origin': base, 'X-CSRF-Token': self.token(client, '/')}
        task = client.post('/api/tasks', base_url=base, headers=headers, json={
            'name': '虚构任务', 'questions': [{'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        response = client.delete(f'/api/tasks/{task}', base_url=base, headers=headers,
                                 json={'confirmed': True, 'expected_counts': {}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get(f'/api/tasks/{task}', base_url=base).status_code, 200)
        self.assertFalse((self.root / 'data/backups').exists())

    def test_two_teachers_upload_grade_export_and_restart_without_network(self):
        app = self.app()
        a, b = app.test_client(), app.test_client()
        self.assertEqual(self.signup(a, 'teacher-a@example.test').status_code, 303)
        self.assertEqual(self.signup(b, 'teacher-b@example.test').status_code, 303)
        base = 'https://school.example.test'
        def post(client, path, data):
            return client.post(path, base_url=base, json=data, headers={
                'Origin': base, 'X-CSRF-Token': self.token(client, '/')})
        task = post(a, '/api/tasks', {'name': '虚构英语作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        sid = post(a, f'/api/tasks/{task}/submissions', {'student_code': '1001'}).json['id']
        stream = io.BytesIO()
        Image.new('RGB', (320, 120), 'white').save(stream, 'PNG')
        upload = a.post(f'/api/submissions/{sid}/pages', base_url=base,
            headers={'Origin': base, 'X-CSRF-Token': self.token(a, '/')},
            data={'image': (io.BytesIO(stream.getvalue()), 'synthetic.png'), 'answer_layout': 'separate'})
        self.assertEqual(upload.status_code, 201)
        self.assertEqual(b.get(upload.json['url'], base_url=base).status_code, 404)
        ocr = {'blocks': [{'id': 0, 'text': '1. book', 'box': [0, 0, 300, 80]}]}
        with patch('paddle_cloud.recognize', return_value=ocr) as cloud:
            graded = post(a, f'/api/submissions/{sid}/auto-grade', {})
        self.assertEqual(graded.status_code, 200, graded.json)
        self.assertGreaterEqual(cloud.call_count, 1)
        self.assertEqual(b.get(f'/api/submissions/{sid}', base_url=base).status_code, 404)
        for url in (f'/tasks/{task}/grades.csv', f'/tasks/{task}/answer-card/download?format=pdf',
                    f'/tasks/{task}/answer-card/download?format=docx'):
            self.assertEqual(b.get(url, base_url=base).status_code, 404)
            result = a.get(url, base_url=base)
            self.assertEqual(result.status_code, 200, url)
            self.assertGreater(len(result.data), 30)
        self.assertTrue(list((self.root / 'data/uploads').iterdir()))
        with a.get(upload.json['url'], base_url=base) as response:
            image_before = response.data
        cookie = a.get_cookie('moxieben_accounts', domain='school.example.test')
        restarted = self.app().test_client()
        restarted.set_cookie(cookie.key, cookie.value, domain='school.example.test')
        with restarted.get(upload.json['url'], base_url=base) as response:
            self.assertEqual(response.data, image_before)
        self.assertEqual(restarted.get(f'/api/submissions/{sid}', base_url=base).json['id'], sid)

    def test_background_job_restart_preserves_owner_and_uses_internal_origin_boundary(self):
        app = self.app()
        a, b = app.test_client(), app.test_client()
        self.signup(a, 'teacher-a@example.test')
        self.signup(b, 'teacher-b@example.test')
        base = 'https://school.example.test'
        def post(client, path, data):
            return client.post(path, base_url=base, json=data, headers={
                'Origin': base, 'X-CSRF-Token': self.token(client, '/')})
        task = post(a, '/api/tasks', {'name': '虚构后台任务', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        sid = post(a, f'/api/tasks/{task}/submissions', {'student_code': '1001'}).json['id']
        stream = io.BytesIO()
        Image.new('RGB', (320, 120), 'white').save(stream, 'PNG')
        upload = a.post(f'/api/submissions/{sid}/pages', base_url=base,
            headers={'Origin': base, 'X-CSRF-Token': self.token(a, '/')}, data={
                'image': (io.BytesIO(stream.getvalue()), 'synthetic.png'), 'auto_grade': 'true',
                'answer_layout': 'separate'})
        jid = upload.json['job']['id']
        cookies = [client.get_cookie('moxieben_accounts', domain='school.example.test') for client in (a,b)]
        restarted = self.app()
        a, b = restarted.test_client(), restarted.test_client()
        for client, cookie in zip((a,b), cookies):
            client.set_cookie(cookie.key, cookie.value, domain='school.example.test')
        self.assertEqual(a.get(f'/api/grading-jobs/{jid}', base_url=base).json['job']['state'], 'interrupted')
        self.assertEqual(post(b, f'/api/grading-jobs/{jid}/resume', {}).status_code, 404)
        self.assertEqual(post(a, f'/api/grading-jobs/{jid}/resume', {}).status_code, 202)
        ocr = {'blocks': [{'id': 0, 'text': '1. book', 'box': [0, 0, 300, 80]}]}
        with patch('paddle_cloud.recognize', return_value=ocr):
            restarted.extensions['grading_jobs'].drain()
        self.assertEqual(a.get(f'/api/grading-jobs/{jid}', base_url=base).json['job']['state'], 'done')
        self.assertEqual(b.get(f'/api/grading-jobs/{jid}', base_url=base).status_code, 404)

    def test_outbound_download_rejects_private_or_unlisted_addresses(self):
        from production import validate_outbound_url
        public = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with patch('production.socket.getaddrinfo', return_value=public):
            validate_outbound_url('https://results.example.test/job?signature=synthetic', ['results.example.test'])
            for url in ('http://results.example.test/x', 'https://evil.test/x',
                        'https://results.example.test@evil.test/x', 'https://results.example.test:8443/x'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    validate_outbound_url(url, ['results.example.test'])
        private = [(2, 1, 6, '', ('169.254.169.254', 443))]
        with patch('production.socket.getaddrinfo', return_value=private), self.assertRaises(ValueError):
            validate_outbound_url('https://results.example.test/x', ['results.example.test'])

    def test_backup_restores_database_images_and_detects_tampering(self):
        from backup_data import backup, restore, verify
        app = self.app()
        self.assertEqual(self.signup(app.test_client(), 'teacher-a@example.test').status_code, 303)
        (self.root / 'data/uploads/synthetic.png').write_bytes(b'synthetic-image')
        target = self.root / 'backup'
        backup(self.root / 'data', target)
        verify(target)
        restored = self.root / 'restored'
        restore(target, restored)
        self.assertEqual((restored / 'uploads/synthetic.png').read_bytes(), b'synthetic-image')
        with closing(sqlite3.connect(restored / 'dictation.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM users').fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            restore(target, restored)
        with self.assertRaises(ValueError):
            restore(target, target / 'recursive')
        (target / 'uploads/synthetic.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            verify(target)


if __name__ == '__main__':
    unittest.main()
