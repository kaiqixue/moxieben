import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from app import create_app
from PIL import Image


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = directory.name
        self.app = create_app({'DATA_DIR': directory.name, 'TESTING': True, 'TEACHER_TENANT_MODE': True})
        self.a = self.app.test_client()
        self.b = self.app.test_client()
        self.register(self.a, 'teacher-a@example.test')
        self.register(self.b, 'teacher-b@example.test')

    def test_login_session_survives_local_restart(self):
        cookie = self.a.get_cookie(self.app.config['SESSION_COOKIE_NAME'])
        restarted = create_app({'DATA_DIR': self.data_dir, 'TESTING': True, 'TEACHER_TENANT_MODE': True})
        browser = restarted.test_client()
        browser.set_cookie(cookie.key, cookie.value)
        self.assertEqual(browser.get('/api/classes').status_code, 200)

    def token(self, client):
        html = client.get('/').get_data(as_text=True)
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def register(self, client, email):
        html = client.get('/teacher/register').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        response = client.post('/teacher/register', data={'csrf_token': token, 'name': email,
            'email': email, 'password': 'example-1234', 'confirm_password': 'example-1234'})
        self.assertEqual(response.status_code, 303)

    def login(self, client, email):
        html = client.get('/teacher/login').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        response = client.post('/teacher/login', data={'csrf_token': token,
            'email': email, 'password': 'example-1234'})
        self.assertEqual(response.status_code, 303)

    def post(self, client, path, data):
        return client.post(path, json=data, headers={'X-CSRF-Token': self.token(client)})

    def word_file(self, lines):
        from docx import Document
        document = Document()
        for line in lines:
            document.add_paragraph(line)
        stream = io.BytesIO()
        document.save(stream)
        stream.seek(0)
        return stream, 'synthetic.docx'

    def test_word_previews_are_private_and_do_not_create_records_or_uploads(self):
        from pathlib import Path
        uploads = Path(self.data_dir) / 'uploads'
        before = set(uploads.iterdir())
        drafts = []
        for client, answer in [(self.a, 'book'), (self.b, 'pencil')]:
            token = self.token(client)
            grammar = client.post('/api/tasks/import-word', data={'file': self.word_file([
                '每题 1 分', 'This is a (1) ______.', '答案', f'1. {answer}'])},
                headers={'X-CSRF-Token': token})
            self.assertEqual(grammar.status_code, 200, grammar.json)
            self.assertEqual(grammar.json['questions'][0]['answer'], answer)
            drafts.append(grammar.json)
            answers = client.post('/api/tasks/import-word', data={'mode': 'answers',
                'file': self.word_file(['半句', f'1. {answer}'])}, headers={'X-CSRF-Token': token})
            self.assertEqual(answers.status_code, 200, answers.json)
            self.assertEqual(answers.json['groups'][0]['items'][0]['answer'], answer)
            self.assertEqual(client.get('/api/tasks').json['tasks'], [])
        self.assertEqual(set(uploads.iterdir()), before)
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn:
            for table in ('tasks', 'questions', 'assignments', 'submissions'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        saved = self.post(self.a, '/api/tasks', drafts[0])
        self.assertEqual(saved.status_code, 201, saved.json)
        tid = saved.json['id']
        self.assertEqual(self.a.get(f'/api/tasks/{tid}').status_code, 200)
        self.assertEqual(self.b.get(f'/api/tasks/{tid}').status_code, 404)
        self.assertEqual(self.b.get('/api/tasks').json['tasks'], [])
        own_class = self.post(self.a, '/api/classes', {'name': '合成班级'}).json['id']
        self.assertEqual(self.post(self.a, f'/api/tasks/{tid}/assignments',
                                  {'class_ids': [own_class]}).status_code, 201)
        self.assertEqual(self.post(self.b, f'/api/tasks/{tid}/assignments',
                                  {'class_ids': [own_class]}).status_code, 404)

    def test_word_preview_keeps_login_csrf_origin_role_and_file_validation(self):
        self.assertEqual(self.app.test_client().post('/api/tasks/import-word').status_code, 401)
        token = self.token(self.a)
        self.assertEqual(self.a.post('/api/tasks/import-word').status_code, 403)
        self.assertEqual(self.a.post('/api/tasks/import-word', headers={
            'X-CSRF-Token': token, 'Origin': 'https://evil.example.test'}).status_code, 403)
        for mode in ('', 'answers'):
            with self.subTest(mode=mode):
                response = self.a.post('/api/tasks/import-word', data={
                    'mode': mode, 'file': (io.BytesIO(b'not a docx'), 'invalid.docx')},
                    headers={'X-CSRF-Token': token})
                self.assertEqual(response.status_code, 400, response.json)
        admin_token = self.token(self.b)
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn, conn:
            conn.execute("UPDATE users SET role='platform_admin' WHERE email=?", ('teacher-b@example.test',))
        self.assertEqual(self.b.post('/api/tasks/import-word',
                                    headers={'X-CSRF-Token': admin_token}).status_code, 403)

    def test_class_and_task_are_visible_only_to_owner(self):
        home = self.a.get('/').get_data(as_text=True)
        self.assertIn('teacher-a@example.test', home)
        self.assertIn('href="/settings"', home)
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        task = self.post(self.a, '/api/tasks', {'name': '甲作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        self.assertEqual(self.b.get('/api/classes').json['classes'], [])
        self.assertEqual(self.b.get('/api/tasks').json['tasks'], [])
        self.assertEqual(self.b.get(f'/api/classes/{klass}').status_code, 404)
        self.assertEqual(self.b.get(f'/api/tasks/{task}').status_code, 404)
        self.assertEqual(self.b.get(f'/tasks/{task}/grades.csv').status_code, 404)
        self.assertEqual(self.a.get(f'/tasks/{task}/grades.csv').status_code, 200)
        self.assertEqual(self.b.patch(f'/api/classes/{klass}', json={'name': '篡改'},
            headers={'X-CSRF-Token': self.token(self.b)}).status_code, 404)
        self.assertEqual(self.a.get(f'/api/classes/{klass}').json['name'], '甲班')
        self.assertEqual(self.b.get('/api/settings').status_code, 200)
        self.assertFalse(self.b.get('/api/settings').json['api_key_configured'])

    def test_local_ocr_settings_are_private_persistent_and_do_not_inherit_shared_keys(self):
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn, conn:
            conn.execute("UPDATE settings SET value='synthetic-legacy-secret' WHERE key='api_key'")
        self.assertEqual(self.a.get('/settings').status_code, 200)
        self.assertFalse(self.a.get('/api/settings').json['api_key_configured'])
        for client, model, secret in [(self.a, 'model-a', 'synthetic-secret-a'),
                                      (self.b, 'model-b', 'synthetic-secret-b')]:
            response = client.patch('/api/settings', json={'provider': 'openai_compatible',
                'base_url': 'https://vision.example.test/v1', 'model': model, 'api_key': secret},
                headers={'X-CSRF-Token': self.token(client)})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertTrue(response.json['api_key_configured'])
            self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertEqual(self.a.get('/api/settings').json['model'], 'model-a')
        self.assertEqual(self.b.get('/api/settings').json['model'], 'model-b')
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn:
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0],
                             'synthetic-legacy-secret')
        restarted = create_app({'DATA_DIR': self.data_dir, 'TESTING': True, 'TEACHER_TENANT_MODE': True})
        client = restarted.test_client()
        self.login(client, 'teacher-a@example.test')
        self.assertEqual(client.get('/api/settings').json['model'], 'model-a')
        client.patch('/api/settings', json={'model': 'model-a-updated'},
                     headers={'X-CSRF-Token': self.token(client)})
        with patch('settings.test_openai_compatible', return_value={'message': 'synthetic ok'}) as connection:
            response = client.post('/api/settings/test', headers={'X-CSRF-Token': self.token(client)})
        self.assertEqual(response.status_code, 200, response.json)
        connection.assert_called_once_with('https://vision.example.test/v1', 'model-a-updated', 'synthetic-secret-a')
        self.assertNotIn('synthetic-secret-a', response.get_data(as_text=True))
        response = client.patch('/api/settings', json={'provider': 'paddle_cloud',
            'paddle_token': 'synthetic-paddle-a'}, headers={'X-CSRF-Token': self.token(client)})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['paddle_token_configured'])
        self.assertNotIn('synthetic-paddle-a', response.get_data(as_text=True))
        self.assertFalse(self.b.get('/api/settings').json['paddle_token_configured'])
        with patch('paddle_cloud.test_connection', return_value={'ok': True, 'message': 'synthetic ok'}) as paddle:
            self.assertEqual(client.post('/api/settings/test',
                headers={'X-CSRF-Token': self.token(client)}).status_code, 200)
        self.assertEqual(paddle.call_args.args[0]['paddle_token'], 'synthetic-paddle-a')
        response = client.patch('/api/settings', json={'provider': 'local', 'api_key': '',
            'paddle_token': ''}, headers={'X-CSRF-Token': self.token(client)})
        self.assertFalse(response.json['api_key_configured'])
        self.assertFalse(response.json['paddle_token_configured'])
        self.assertTrue(self.b.get('/api/settings').json['api_key_configured'])

    def test_local_ocr_dispatch_uses_the_resource_owners_api_for_direct_and_background_requests(self):
        import teacher_auth
        resources = []
        for client, model, secret in [(self.a, 'model-a', 'synthetic-secret-a'),
                                      (self.b, 'model-b', 'synthetic-secret-b')]:
            response = client.patch('/api/settings', json={'provider': 'openai_compatible',
                'base_url': 'https://vision.example.test/v1', 'model': model, 'api_key': secret},
                headers={'X-CSRF-Token': self.token(client)})
            self.assertEqual(response.status_code, 200, response.json)
            tid = self.post(client, '/api/tasks', {'name': '合成作业', 'questions': [
                {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
            sid = self.post(client, f'/api/tasks/{tid}/submissions', {'student_code': '001'}).json['id']
            image = io.BytesIO()
            Image.new('RGB', (30, 30), 'white').save(image, 'PNG')
            page = client.post(f'/api/submissions/{sid}/pages',
                data={'image': (io.BytesIO(image.getvalue()), 'synthetic.png')},
                headers={'X-CSRF-Token': self.token(client)})
            self.assertEqual(page.status_code, 201, page.json)
            resources.append(page.json['id'])
        with patch('settings.recognize_openai', return_value={'blocks': [], 'model': 'synthetic'}) as cloud:
            result = self.a.post(f'/api/pages/{resources[0]}/ocr', json={},
                                 headers={'X-CSRF-Token': self.token(self.a)})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(cloud.call_args.args[1]['api_key'], 'synthetic-secret-a')
            owner = None
            with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn:
                owner = conn.execute("SELECT id FROM users WHERE email='teacher-b@example.test'").fetchone()[0]
            result = teacher_auth.internal_post(self.app, f'/api/pages/{resources[1]}/ocr', {}, owner_id=owner)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(cloud.call_args.args[1]['api_key'], 'synthetic-secret-b')
            calls = cloud.call_count
            result = self.a.post(f'/api/pages/{resources[1]}/ocr', json={},
                                 headers={'X-CSRF-Token': self.token(self.a)})
            self.assertEqual(result.status_code, 404)
            self.assertEqual(cloud.call_count, calls)

    def test_local_ocr_settings_keep_session_csrf_origin_and_role_guards(self):
        from settings import read_settings
        from werkzeug.exceptions import Forbidden
        with self.app.app_context(), self.assertRaises(Forbidden):
            read_settings(None)
        self.assertEqual(self.app.test_client().get('/api/settings').status_code, 401)
        token = self.token(self.a)
        self.assertEqual(self.a.patch('/api/settings', json={'provider': 'local'}).status_code, 403)
        self.assertEqual(self.a.patch('/api/settings', json={'provider': 'local'},
            headers={'X-CSRF-Token': token, 'Origin': 'https://evil.example.test'}).status_code, 403)
        token_b = self.token(self.b)
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn, conn:
            conn.execute("UPDATE users SET role='platform_admin' WHERE email='teacher-b@example.test'")
        self.assertEqual(self.b.get('/api/settings').status_code, 403)
        self.assertEqual(self.b.patch('/api/settings', json={'provider': 'local'},
                                     headers={'X-CSRF-Token': token_b}).status_code, 403)

    def test_cross_teacher_assignment_is_rejected(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        task = self.post(self.b, '/api/tasks', {'name': '乙作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        response = self.post(self.b, f'/api/tasks/{task}/assignments', {'class_ids': [klass]})
        self.assertEqual(response.status_code, 404)

    def test_student_roster_and_assignment_are_isolated(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        member = self.post(self.a, f'/api/classes/{klass}/members',
                           {'name': '虚构学生甲', 'student_code': '1001'}).json
        student = member['student_id']
        self.assertEqual(self.b.get('/api/students').json['students'], [])
        self.assertEqual(self.b.get(f'/api/students/{student}').status_code, 404)
        self.assertEqual(self.b.get(f'/api/classes/{klass}').status_code, 404)
        task = self.post(self.a, '/api/tasks', {'name': '甲作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        assignment = self.post(self.a, f'/api/tasks/{task}/assignments',
                               {'class_ids': [klass]}).json['assignments'][0]['id']
        self.assertEqual([item['id'] for item in self.a.get('/api/workbench/assignments').json['assignments']],
                         [assignment])
        self.assertEqual(self.b.get('/api/workbench/assignments').json['assignments'], [])
        self.assertEqual(self.b.get(f'/api/assignments/{assignment}').status_code, 404)
        self.assertEqual(self.b.get(f'/assignments/{assignment}/grades.csv').status_code, 404)
        self.assertEqual(self.b.get(f'/api/assignments/{assignment}/analysis').status_code, 404)
        self.assertEqual(self.a.get(f'/api/assignments/{assignment}/analysis').status_code, 200)
        self.assertEqual(self.b.get(f'/api/classes/{klass}/identity-memories').status_code, 404)
        self.assertEqual(self.a.get(f'/api/classes/{klass}/identity-memories').status_code, 200)
        submission = self.post(self.a, f'/api/assignment-students/{self.a.get(f"/api/assignments/{assignment}").json["students"][0]["id"]}/submission', {}).json['id']
        self.assertEqual(self.a.get(f'/api/submissions/{submission}').status_code, 200)
        self.assertEqual(self.b.get(f'/api/submissions/{submission}').status_code, 404)

    def test_existing_unowned_data_is_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(f'{directory}/dictation.sqlite3')) as conn, conn:
                conn.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
                conn.execute("INSERT INTO tasks(name) VALUES ('既有任务')")
            with self.assertRaisesRegex(ValueError, '需要先核对教师归属'):
                create_app({'DATA_DIR': directory, 'TESTING': True, 'TEACHER_TENANT_MODE': True})

    def test_legacy_student_schema_is_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(f'{directory}/dictation.sqlite3')) as conn, conn:
                conn.execute('CREATE TABLE students (id INTEGER PRIMARY KEY, school_code TEXT UNIQUE)')
            with self.assertRaisesRegex(ValueError, '学生档案结构'):
                create_app({'DATA_DIR': directory, 'TESTING': True, 'TEACHER_TENANT_MODE': True})

    def test_partial_student_migration_keeps_global_unique_code_out(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(f'{directory}/dictation.sqlite3')) as conn, conn:
                conn.execute('CREATE TABLE students (id INTEGER PRIMARY KEY, owner_id INTEGER, school_code TEXT UNIQUE)')
            with self.assertRaisesRegex(ValueError, '全局学号约束'):
                create_app({'DATA_DIR': directory, 'TESTING': True, 'TEACHER_TENANT_MODE': True})

    def test_school_code_can_be_reused_by_another_teacher(self):
        a_class = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        b_class = self.post(self.b, '/api/classes', {'name': '乙班'}).json['id']
        first = self.post(self.a, f'/api/classes/{a_class}/members',
                          {'name': '虚构甲', 'student_code': '1001'}).json['student_id']
        second = self.post(self.b, f'/api/classes/{b_class}/members',
                           {'name': '虚构乙', 'student_code': '1001'}).json['student_id']
        for client, student in ((self.a, first), (self.b, second)):
            response = client.patch(f'/api/students/{student}', json={'school_code': 'S1001'},
                                    headers={'X-CSRF-Token': self.token(client)})
            self.assertEqual(response.status_code, 200)

    def test_cross_teacher_member_edits_leave_roster_unchanged(self):
        a_class = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        b_class = self.post(self.b, '/api/classes', {'name': '乙班'}).json['id']
        member = self.post(self.a, f'/api/classes/{a_class}/members',
                           {'name': '虚构甲', 'student_code': '1001'}).json
        mid = member['id']
        self.assertEqual(self.b.patch(f'/api/members/{mid}', json={'student_code': 'HACK'},
                                      headers={'X-CSRF-Token': self.token(self.b)}).status_code, 404)
        self.assertEqual(self.post(self.b, f'/api/members/{mid}/transfer',
                                   {'class_id': b_class, 'student_code': 'HACK'}).status_code, 404)
        self.assertEqual(self.post(self.a, f'/api/members/{mid}/transfer',
                                   {'class_id': b_class, 'student_code': '1001'}).status_code, 404)
        listing = self.a.get(f'/api/classes/{a_class}').json['members']
        self.assertEqual([(item['student_code'], item['active']) for item in listing], [('1001', 1)])
        self.assertEqual(self.b.get(f'/api/classes/{b_class}').json['members'], [])

    def test_database_rejects_unowned_and_cross_teacher_assignment(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        task = self.post(self.b, '/api/tasks', {'name': '乙作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO classes(name,term,created_at) VALUES('缺归属','', '')")
            conn.rollback()
            owner = conn.execute("SELECT id FROM users WHERE email='teacher-b@example.test'").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('''INSERT INTO assignments(task_id,class_id,class_name,term,created_at,owner_id)
                                VALUES(?,?, '越权', '', '', ?)''', (task, klass, owner))

    def test_uploaded_photo_and_image_are_private(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        task = self.post(self.a, '/api/tasks', {'name': '甲作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        assignment = self.post(self.a, f'/api/tasks/{task}/assignments',
                               {'class_ids': [klass]}).json['assignments'][0]['id']
        image = io.BytesIO()
        Image.new('RGB', (100, 100), 'white').save(image, 'PNG')
        upload = self.a.post(f'/api/assignments/{assignment}/photos',
                             data={'images': (io.BytesIO(image.getvalue()), 'fictional.png')},
                             headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(upload.status_code, 201)
        photo_id = upload.json['photos'][0]['id']
        image_response = self.a.get(f'/api/batch-photos/{photo_id}/image')
        self.assertEqual(image_response.status_code, 200)
        image_response.close()
        self.assertEqual(self.b.get(f'/api/batch-photos/{photo_id}/image').status_code, 404)
        self.assertEqual(self.b.get(f'/api/batch-photos/{photo_id}').status_code, 404)
        roster = self.post(self.a, f'/api/classes/{klass}/members',
                           {'name': '虚构学生', 'student_code': '1001'}).json
        self.post(self.a, f'/api/assignments/{assignment}/students', {'student_id': roster['student_id']})
        response = self.post(self.b, f'/api/batch-photos/{photo_id}/confirm',
                             {'confirmed': True, 'assignment_student_id': 1})
        self.assertEqual(response.status_code, 404)
        rid = self.a.get(f'/api/assignments/{assignment}').json['students'][0]['id']
        response = self.post(self.a, f'/api/batch-photos/{photo_id}/confirm',
                             {'confirmed': True, 'assignment_student_id': rid})
        self.assertEqual(response.status_code, 200)
        sid = response.json['submission_id']
        page_id = response.json['page_id']
        detail = self.a.get(f'/api/submissions/{sid}')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(self.b.get(f'/api/submissions/{sid}').status_code, 404)
        self.assertEqual(self.b.get(f'/api/pages/{page_id}/image').status_code, 404)
        qid = detail.json['reviews'][0]['question_id']
        path = f'/api/submissions/{sid}/reviews/{qid}'
        self.assertEqual(self.post(self.b, path, {'quick_decision': 'correct'}).status_code, 404)
        self.assertEqual(self.post(self.a, path, {'quick_decision': 'correct'}).status_code, 200)
        self.assertEqual(self.a.get(f'/api/submissions/{sid}/history').status_code, 200)
        self.assertEqual(self.b.get(f'/api/submissions/{sid}/history').status_code, 404)
        self.assertEqual(self.b.get(f'/api/assignments/{assignment}/attention').status_code, 404)

    def test_saved_ocr_matches_only_own_roster_without_new_cloud_call(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        self.post(self.a, f'/api/classes/{klass}/members',
                  {'name': '测试学生', 'student_code': '1001'})
        task = self.post(self.a, '/api/tasks', {'name': '甲作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        assignment = self.post(self.a, f'/api/tasks/{task}/assignments',
                               {'class_ids': [klass]}).json['assignments'][0]['id']
        image = io.BytesIO()
        Image.new('RGB', (100, 100), 'white').save(image, 'PNG')
        upload = self.a.post(f'/api/assignments/{assignment}/photos',
                             data={'images': (io.BytesIO(image.getvalue()), 'fictional.png')},
                             headers={'X-CSRF-Token': self.token(self.a)})
        pid = upload.json['photos'][0]['id']
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn, conn:
            conn.execute("UPDATE batch_photos SET status='identified',blocks=? WHERE id=?",
                         ('[{"id":0,"text":"学号：1001 姓名：测试学生","box":[0,0,99,20]}]', pid))
        self.assertEqual(self.post(self.b, f'/api/assignments/{assignment}/photos/match', {}).status_code, 404)
        with patch('settings.recognize_selected', side_effect=AssertionError('unexpected OCR')):
            result = self.post(self.a, f'/api/assignments/{assignment}/photos/match', {})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['summary']['matched'], 1)
        self.assertEqual(self.post(self.a, f'/api/assignments/{assignment}/photos/match', {}).json['summary']['matched'], 1)
        self.assertEqual(self.post(self.a, f'/api/assignments/{assignment}/photos/match',
                                   {'auto_grade': True}).status_code, 200)

    def test_temporary_submission_and_page_stay_with_teacher(self):
        task = self.post(self.a, '/api/tasks', {'name': '临时作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        self.assertEqual(self.post(self.b, f'/api/tasks/{task}/submissions',
                                   {'student_code': '1001'}).status_code, 404)
        sid = self.post(self.a, f'/api/tasks/{task}/submissions',
                        {'student_code': '1001'}).json['id']
        image = io.BytesIO()
        Image.new('RGB', (100, 100), 'white').save(image, 'PNG')
        path = f'/api/submissions/{sid}/pages'
        self.assertEqual(self.b.post(path, data={'image': (io.BytesIO(image.getvalue()), 'fictional.png')},
                                     headers={'X-CSRF-Token': self.token(self.b)}).status_code, 404)
        response = self.a.post(path, data={'image': (io.BytesIO(image.getvalue()), 'fictional.png')},
                               headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.b.get(response.json['url']).status_code, 404)
        queued = self.a.post(path, data={'image': (io.BytesIO(image.getvalue()), 'fictional.png'),
                                         'auto_grade': 'true'},
                             headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(self.b.get(f"/api/grading-jobs/{queued.json['job']['id']}").status_code, 404)

    def test_background_grading_keeps_teacher_identity_through_ocr_and_scores(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        self.post(self.a, f'/api/classes/{klass}/members',
                  {'name': '测试学生', 'student_code': '1001'})
        task = self.post(self.a, '/api/tasks', {'name': '甲作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        assignment = self.post(self.a, f'/api/tasks/{task}/assignments',
                               {'class_ids': [klass]}).json['assignments'][0]['id']
        image = io.BytesIO()
        Image.new('RGB', (640, 240), 'white').save(image, 'PNG')
        upload = self.a.post(f'/api/assignments/{assignment}/photos',
                             data={'images': (io.BytesIO(image.getvalue()), 'fictional.png'),
                                   'auto_process': 'true'},
                             headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(upload.status_code, 201)
        job_id = upload.json['job']['id']
        self.assertEqual(self.b.get(f'/api/grading-jobs/{job_id}').status_code, 404)
        self.assertEqual(self.post(self.b, f'/api/grading-jobs/{job_id}/resume', {}).status_code, 404)
        ocr = {'blocks': [
            {'id': 0, 'text': '学号：1001 姓名：测试学生', 'box': [0, 0, 600, 40]},
            {'id': 1, 'text': '1. book', 'box': [0, 50, 600, 90]}]}
        with patch('settings.recognize_selected', return_value=ocr) as cloud:
            self.app.extensions['grading_jobs'].drain()
        self.assertEqual(cloud.call_count, 1)
        job = self.a.get(f'/api/grading-jobs/{job_id}').json['job']
        self.assertEqual(job['state'], 'done')
        self.assertEqual(self.a.get(f'/api/assignments/{assignment}').json['students'][0]['score'], 1)
        self.assertEqual(self.b.get(f'/assignments/{assignment}/grades.csv').status_code, 404)

    def test_interrupted_job_resumes_under_original_teacher_after_restart(self):
        klass = self.post(self.a, '/api/classes', {'name': '甲班'}).json['id']
        self.post(self.a, f'/api/classes/{klass}/members',
                  {'name': '测试学生', 'student_code': '1001'})
        task = self.post(self.a, '/api/tasks', {'name': '甲作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        assignment = self.post(self.a, f'/api/tasks/{task}/assignments',
                               {'class_ids': [klass]}).json['assignments'][0]['id']
        image = io.BytesIO()
        Image.new('RGB', (640, 240), 'white').save(image, 'PNG')
        upload = self.a.post(f'/api/assignments/{assignment}/photos',
                             data={'images': (io.BytesIO(image.getvalue()), 'fictional.png'),
                                   'auto_process': 'true'},
                             headers={'X-CSRF-Token': self.token(self.a)})
        jid = upload.json['job']['id']
        restarted = create_app({'DATA_DIR': self.data_dir, 'TESTING': True, 'TEACHER_TENANT_MODE': True})
        a, b = restarted.test_client(), restarted.test_client()
        self.login(a, 'teacher-a@example.test')
        self.login(b, 'teacher-b@example.test')
        self.assertEqual(a.get(f'/api/grading-jobs/{jid}').json['job']['state'], 'interrupted')
        self.assertEqual(b.get(f'/api/grading-jobs/{jid}').status_code, 404)
        self.assertEqual(self.post(b, f'/api/grading-jobs/{jid}/resume', {}).status_code, 404)
        self.assertEqual(self.post(a, f'/api/grading-jobs/{jid}/resume', {}).status_code, 202)
        ocr = {'blocks': [
            {'id': 0, 'text': '学号：1001 姓名：测试学生', 'box': [0, 0, 600, 40]},
            {'id': 1, 'text': '1. book', 'box': [0, 50, 600, 90]}]}
        with patch('settings.recognize_selected', return_value=ocr):
            restarted.extensions['grading_jobs'].drain()
        self.assertEqual(a.get(f'/api/grading-jobs/{jid}').json['job']['state'], 'done')

    def test_two_teachers_grade_same_code_without_sharing_results(self):
        work = []
        for client, label in ((self.a, '甲'), (self.b, '乙')):
            klass = self.post(client, '/api/classes', {'name': label + '班'}).json['id']
            self.post(client, f'/api/classes/{klass}/members',
                      {'name': '测试学生', 'student_code': '1001'})
            task = self.post(client, '/api/tasks', {'name': label + '作业', 'questions': [
                {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
            aid = self.post(client, f'/api/tasks/{task}/assignments',
                            {'class_ids': [klass]}).json['assignments'][0]['id']
            image = io.BytesIO()
            Image.new('RGB', (640, 240), 'white').save(image, 'PNG')
            photo = client.post(f'/api/assignments/{aid}/photos', data={
                'images': (io.BytesIO(image.getvalue()), label + '.png'), 'auto_process': 'true'},
                headers={'X-CSRF-Token': self.token(client)})
            self.assertEqual(photo.status_code, 201)
            work.append((client, aid, photo.json['job']['id']))
        ocr = {'blocks': [
            {'id': 0, 'text': '学号：1001 姓名：测试学生', 'box': [0, 0, 600, 40]},
            {'id': 1, 'text': '1. book', 'box': [0, 50, 600, 90]}]}
        with patch('settings.recognize_selected', return_value=ocr) as cloud:
            self.app.extensions['grading_jobs'].drain()
        self.assertEqual(cloud.call_count, 2)
        for client, aid, jid in work:
            self.assertEqual(client.get(f'/api/grading-jobs/{jid}').json['job']['state'], 'done')
            self.assertEqual(client.get(f'/api/assignments/{aid}').json['students'][0]['score'], 1)
            other = self.b if client is self.a else self.a
            self.assertEqual(other.get(f'/api/assignments/{aid}').status_code, 404)
            self.assertEqual(other.get(f'/api/grading-jobs/{jid}').status_code, 404)

    def test_deleting_own_task_preserves_other_teacher_photo_and_scores(self):
        tasks = []
        for client, label in ((self.a, '甲'), (self.b, '乙')):
            klass = self.post(client, '/api/classes', {'name': label + '班'}).json['id']
            task = self.post(client, '/api/tasks', {'name': label + '作业', 'questions': [
                {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
            assignment = self.post(client, f'/api/tasks/{task}/assignments',
                                   {'class_ids': [klass]}).json['assignments'][0]['id']
            image = io.BytesIO()
            Image.new('RGB', (100, 100), 'white').save(image, 'PNG')
            photo = client.post(f'/api/assignments/{assignment}/photos',
                                data={'images': (io.BytesIO(image.getvalue()), label + '.png')},
                                headers={'X-CSRF-Token': self.token(client)}).json['photos'][0]
            tasks.append((task, photo['id']))
        a_task, _ = tasks[0]
        b_task, b_photo = tasks[1]
        self.assertEqual(self.b.get(f'/api/tasks/{a_task}/deletion-preview').status_code, 404)
        preview = self.a.get(f'/api/tasks/{a_task}/deletion-preview')
        self.assertEqual(preview.status_code, 200)
        deleted = self.a.delete(f'/api/tasks/{a_task}', json={
            'confirmed': True, 'expected_counts': preview.json['counts']},
            headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.b.get(f'/api/tasks/{b_task}').status_code, 200)
        image_response = self.b.get(f'/api/batch-photos/{b_photo}/image')
        self.assertEqual(image_response.status_code, 200)
        image_response.close()
