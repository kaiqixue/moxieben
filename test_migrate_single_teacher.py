"""A copied legacy workspace becomes one teacher's tenant without changing its source."""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from werkzeug.security import generate_password_hash

from app import create_app
from scripts.migrate_single_teacher import migrate


class SingleTeacherMigrationTests(unittest.TestCase):
    def test_copy_keeps_ids_photos_and_login_while_source_stays_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / 'legacy', Path(root) / 'tenant'
            source.mkdir()
            app = create_app({'DATA_DIR': str(source), 'TESTING': True, 'CSRF_TOKEN': 'test'})
            client = app.test_client()
            headers = {'X-CSRF-Token': 'test'}
            klass = client.post('/api/classes', json={'name': '虚构班'}, headers=headers).json['id']
            client.post(f'/api/classes/{klass}/members', json={
                'name': '虚构学生', 'student_code': '1001'}, headers=headers)
            task = client.post('/api/tasks', json={'name': '虚构作业', 'questions': [
                {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}, headers=headers).json['id']
            assignment = client.post(f'/api/tasks/{task}/assignments', json={
                'class_ids': [klass]}, headers=headers).json['assignments'][0]['id']
            image = io.BytesIO()
            Image.new('RGB', (100, 100), 'white').save(image, 'PNG')
            uploaded = client.post(f'/api/assignments/{assignment}/photos', data={
                'images': (io.BytesIO(image.getvalue()), 'fictional.png')}, headers=headers)
            self.assertEqual(uploaded.status_code, 201)
            account_db = Path(root) / 'accounts.sqlite3'
            with closing(sqlite3.connect(account_db)) as conn, conn:
                conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, password_hash TEXT, role TEXT, disabled_at INTEGER)')
                conn.execute('INSERT INTO users VALUES (7,?,?,?,?,NULL)',
                             ('虚构教师', 'teacher@example.test', generate_password_hash('example-1234'), 'teacher'))
            migrate(source, account_db, target, 'teacher@example.test')
            with closing(sqlite3.connect(source / 'dictation.sqlite3')) as conn:
                self.assertNotIn('owner_id', [r[1] for r in conn.execute('PRAGMA table_info(tasks)')])
            with closing(sqlite3.connect(target / 'dictation.sqlite3')) as conn:
                self.assertEqual(conn.execute('SELECT owner_id FROM classes WHERE id=?', (klass,)).fetchone()[0], 7)
                self.assertEqual(conn.execute('SELECT owner_id FROM grading_jobs LIMIT 1').fetchone(), None)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            tenant = create_app({'DATA_DIR': str(target), 'TESTING': True, 'TEACHER_TENANT_MODE': True})
            browser = tenant.test_client()
            page = browser.get('/teacher/login').get_data(as_text=True)
            import re
            token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
            login = browser.post('/teacher/login', data={'csrf_token': token,
                'email': 'teacher@example.test', 'password': 'example-1234'})
            self.assertEqual(login.status_code, 303)
            self.assertEqual(browser.get(f'/api/classes/{klass}').status_code, 200)
            photo = uploaded.json['photos'][0]['id']
            response = browser.get(f'/api/batch-photos/{photo}/image')
            self.assertEqual(response.status_code, 200)
            response.close()
            with patch('app.ROOT',Path(root)), patch.dict(os.environ, {'MOXIEBEN_TENANT_DIR': str(target)}):
                selected = create_app()
            self.assertTrue(selected.config['TEACHER_TENANT_MODE'])
            self.assertEqual(Path(selected.config['DATA_DIR']), target)
            with self.assertRaises(FileExistsError):
                migrate(source, account_db, target, 'teacher@example.test')
