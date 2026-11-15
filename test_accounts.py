import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from app import create_app


class AccountPreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.database = self.data / 'accounts.sqlite3'
        self.app = create_app({'DATA_DIR': directory.name, 'TESTING': True, 'TEACHER_ACCOUNT_PREVIEW': True})
        self.teacher = self.app.test_client()
        self.other = self.app.test_client()
        self.runner = self.app.test_cli_runner()

    def post(self, client, path, **fields):
        page = client.get('/' if path.endswith('/logout') else path)
        self.assertEqual(page.status_code, 200)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        return client.post(path, data={'csrf_token': token, **fields})

    def signup(self, client, email='teacher-a@example.test', name='演示老师甲', password='book-789'):
        return self.post(client, '/teacher/register', name=name, email=email, password=password,
                         confirm_password=password, role='platform_admin')

    def login(self, client, email, password='book-789', **fields):
        return self.post(client, '/teacher/login', email=email, password=password, **fields)

    def test_login_and_registration_share_the_account_layout(self):
        register = self.teacher.get('/teacher/register').get_data(as_text=True)
        login = self.teacher.get('/teacher/login').get_data(as_text=True)
        for page in (register, login):
            self.assertIn('class="register-layout"', page)
            self.assertIn('class="register-intro"', page)
            self.assertIn('images/teacher-desk.png', page)
            self.assertIn('class="password-toggle"', page)
        self.assertIn('注册教师账号', register)
        self.assertIn('教师登录', login)
        self.assertIn('name="email"', login)
        self.assertIn('name="password"', login)
        self.assertNotIn('name="confirm_password"', login)
        self.assertNotIn('name="name"', login)

    def test_registers_teacher_with_hashed_password_and_blocks_business_access(self):
        self.assertEqual(self.teacher.post('/teacher/register').status_code, 403)
        response = self.signup(self.teacher)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, '/')
        workbench = self.teacher.get('/')
        self.assertIn('演示老师甲', workbench.get_data(as_text=True))
        self.assertIn('no-store', workbench.headers['Cache-Control'])
        with closing(sqlite3.connect(self.database)) as conn:
            role, password_hash = conn.execute('SELECT role,password_hash FROM users').fetchone()
        self.assertEqual(role, 'teacher')
        self.assertNotIn('book-789', password_hash)
        for path in ('/api/classes', '/api/settings', '/api/pages/1/image', '/assignments/1/grades.csv'):
            self.assertEqual(self.teacher.get(path).status_code, 403, path)

    def test_preview_starts_without_business_database_or_routes(self):
        self.assertFalse((self.data / 'dictation.sqlite3').exists())
        self.assertFalse((self.data / 'uploads').exists())
        self.assertNotIn('/api/tasks', {rule.rule for rule in self.app.url_map.iter_rules()})

    def test_head_requests_never_register_an_account_or_log_in(self):
        fields = {'name': '演示', 'email': 'teacher-a@example.test', 'password': 'book-789',
                  'confirm_password': 'book-789'}
        self.assertEqual(self.teacher.head('/teacher/register', data=fields).status_code, 200)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM users').fetchone()[0], 0)
        self.assertEqual(self.signup(self.teacher).status_code, 303)
        self.assertEqual(self.other.head('/teacher/login', data=fields).status_code, 200)
        self.assertEqual(self.other.get('/').status_code, 302)

    def test_two_accounts_log_in_and_one_logout_revokes_only_its_session(self):
        self.assertEqual(self.signup(self.teacher).status_code, 303)
        self.assertEqual(self.signup(self.other, 'teacher-b@example.test', '演示老师乙').status_code, 303)
        replay = self.app.test_client()
        cookie = self.teacher.get_cookie(self.app.config['SESSION_COOKIE_NAME'])
        replay.set_cookie(cookie.key, cookie.value)
        self.assertEqual(self.post(self.teacher, '/teacher/logout').status_code, 303)
        self.assertEqual(replay.get('/').status_code, 302)
        self.assertIn('演示老师乙', self.other.get('/').get_data(as_text=True))
        self.assertEqual(self.login(self.teacher, 'teacher-a@example.test', password='bad').status_code, 401)
        self.assertEqual(self.login(self.teacher, 'TEACHER-A@example.test', next='//outside.example').location, '/')
        self.assertIn('演示老师甲', self.teacher.get('/').get_data(as_text=True))

    def test_duplicate_normalized_email_and_form_validation(self):
        self.assertEqual(self.signup(self.teacher, 'Teacher-A@example.test').status_code, 303)
        self.assertEqual(self.signup(self.other, ' teacher-a@EXAMPLE.TEST ').status_code, 409)
        self.assertEqual(self.signup(self.other, 'invalid').status_code, 400)
        self.assertEqual(self.signup(self.other, 'teacher-b@example.test', password='abc1234').status_code, 400)
        response = self.post(self.other, '/teacher/register', name='演示', email='teacher-b@example.test',
                             password='book-789', confirm_password='different')
        self.assertEqual(response.status_code, 400)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM users').fetchone()[0], 1)

    def test_admin_cli_account_disable_and_expiry(self):
        result = self.runner.invoke(args=['platform-admin'],
                                    input='平台演示\nadmin@example.test\nExample-admin-789\nExample-admin-789\n')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.login(self.other, 'admin@example.test', 'Example-admin-789').status_code, 303)
        self.assertIn('平台管理员', self.other.get('/').get_data(as_text=True))
        self.assertEqual(self.other.get('/api/classes').status_code, 403)
        self.assertEqual(self.signup(self.teacher).status_code, 303)
        result = self.runner.invoke(args=['account-status', 'teacher-a@example.test', '--disable'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.teacher.get('/').status_code, 302)
        self.assertEqual(self.login(self.teacher, 'teacher-a@example.test').status_code, 401)
        result = self.runner.invoke(args=['account-status', 'teacher-a@example.test', '--enable'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.teacher.get('/').status_code, 302)
        self.assertEqual(self.login(self.teacher, 'teacher-a@example.test').status_code, 303)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute('UPDATE user_sessions SET expires_at=?', (int(time.time()) - 1,))
        self.assertEqual(self.teacher.get('/').status_code, 302)

    def test_registration_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'DATA_DIR': directory, 'TESTING': True, 'TEACHER_AUTH_REQUIRED': True})
            client = app.test_client()
            self.assertEqual(client.get('/teacher/register').status_code, 404)
            self.assertNotIn('/teacher/register', client.get('/teacher/login').get_data(as_text=True))
