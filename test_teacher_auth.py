import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import create_app


class TeacherAuthTests(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.data = Path(data.name)
        self.app = create_app({'DATA_DIR': data.name, 'TESTING': True, 'TEACHER_AUTH_REQUIRED': True})
        self.client = self.app.test_client()
        self.runner = self.app.test_cli_runner()

    def account(self, reset=False):
        result = self.runner.invoke(args=['teacher-account'] + (['--reset'] if reset else []),
                                    input='demo_teacher\nTest-passphrase-123\nTest-passphrase-123\n')
        self.assertEqual(result.exit_code, 0, result.output)

    def csrf(self, html):
        return re.search(r'(?:name="csrf_token" value|name="csrf-token" content)="([^"]+)"', html).group(1)

    def login(self, password='Test-passphrase-123', target='/', client=None):
        client = client or self.client
        page = client.get('/teacher/login', query_string={'next': target})
        return client.post('/teacher/login', data={'csrf_token': self.csrf(page.get_data(as_text=True)),
                           'username': 'demo_teacher', 'password': password, 'next': target})

    def test_teacher_login_guards_pages_and_data_and_logout_revokes_cookie(self):
        self.assertIn('/teacher/login', self.client.get('/').location or '')
        self.account()
        for path in ['/api/tasks', '/api/settings', '/api/pages/10/image', '/api/submissions/10/history']:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get('/tasks/10/grades.csv').status_code, 302)
        forged = self.client.post('/api/tasks', json={}, headers={'X-Internal-Request': '1'})
        self.assertEqual(forged.status_code, 401)
        self.assertEqual(self.client.post('/teacher/login', data={'username':'demo_teacher',
                                                                'password':'Test-passphrase-123'}).status_code, 403)
        self.assertEqual(self.login(password='bad-password').status_code, 401)
        result = self.login(target='//outside.example')
        self.assertEqual(result.location, '/')
        with closing(sqlite3.connect(self.data/'dictation.sqlite3')) as conn:
            self.assertNotIn('Test-passphrase-123', str(list(conn.iterdump())))
        self.assertEqual(self.client.get('/api/tasks').status_code, 200)
        other = self.app.test_client()
        cookie = self.client.get_cookie(self.app.config['SESSION_COOKIE_NAME'])
        other.set_cookie(cookie.key, cookie.value)
        csrf = self.csrf(self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.client.post('/teacher/logout').status_code, 403)
        self.assertEqual(self.client.post('/teacher/logout', data={'csrf_token': csrf}).status_code, 303)
        self.assertEqual(other.get('/api/tasks').status_code, 401)
        self.assertEqual(self.login(target='/settings').location, '/settings')
        self.account(reset=True)
        self.assertEqual(self.client.get('/api/tasks').status_code, 401)

    def test_login_limits_repeated_errors(self):
        self.account()
        for _ in range(5):
            self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_account_password_requires_at_least_eight_characters(self):
        short = self.runner.invoke(args=['teacher-account'], input='demo_teacher\nAbc1234\nAbc1234\n')
        self.assertEqual(short.exit_code, 1)
        valid = self.runner.invoke(args=['teacher-account'], input='demo_teacher\nAbc12345\nAbc12345\n')
        self.assertEqual(valid.exit_code, 0, valid.output)
        self.assertEqual(self.login(password='Abc12345').status_code, 303)

    def test_queued_grading_continues_after_teacher_logs_out(self):
        self.account()
        self.login()
        csrf = self.csrf(self.client.get('/').get_data(as_text=True))
        headers = {'X-CSRF-Token': csrf}
        task = self.client.post('/api/tasks', headers=headers, json={'name': 'Demo', 'questions': [
            {'number':1, 'kind':'word', 'answer':'book', 'points':1}]}).json
        sid = self.client.post(f"/api/tasks/{task['id']}/submissions", headers=headers,
                               json={'student_code': 'test'}).json['id']
        buf = io.BytesIO()
        Image.new('RGB', (200, 200), 'white').save(buf, 'PNG')
        upload = self.client.post(f'/api/submissions/{sid}/pages', headers=headers,
                                  data={'image':(io.BytesIO(buf.getvalue()), 'sample.png'),
                                        'answer_layout':'separate', 'auto_grade':'true'})
        self.assertEqual(upload.status_code, 201)
        pid = upload.json['id']
        upload.request.environ['wsgi.input'].close()
        image = self.client.get(f'/api/pages/{pid}/image')
        self.assertEqual(image.status_code, 200)
        image.close()
        self.client.post('/teacher/logout', data={'csrf_token':csrf})
        self.assertEqual(self.client.get(f'/api/pages/{pid}/image').status_code, 401)
        with patch('settings.recognize_selected', return_value={'blocks': [
                {'id':0,'text':'1. book','box':[0,0,100,30]}]}) as ocr:
            self.app.extensions['grading_jobs'].drain()
        ocr.assert_called_once()
        self.login()
        self.assertEqual(self.client.get(f'/api/submissions/{sid}').json['score'], 1)
