"""Durable jobs use temporary SQLite and fake OCR; no real provider calls."""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app import create_app, now
import test_classes


class GradingJobTests(unittest.TestCase):
    post = test_classes.ClassTests.post
    klass = test_classes.ClassTests.klass
    task = test_classes.ClassTests.task

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('grading_jobs'), 'Durable grading jobs are missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.headers = {'X-CSRF-Token': 'test'}
        self.restart()
        cid = self.klass()
        for code, name in [('001', '张同学'), ('002', '王同学')]:
            self.post(f'/api/classes/{cid}/members', {'student_code': code, 'name': name})
        tid = self.task()
        self.aid = self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [cid]}).json['assignments'][0]['id']

    @contextmanager
    def db(self):
        conn = sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def restart(self):
        from grading_jobs import register
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'CSRF_TOKEN': 'test'})
        if 'grading_jobs' not in self.app.extensions:
            def row(conn, table, record_id):
                from flask import abort
                found = conn.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone()
                if found is None:
                    abort(404)
                return dict(found)
            register(self.app, self.db, row, now)
        self.client = self.app.test_client()
        self.runner = self.app.extensions['grading_jobs']

    def photo(self):
        image = io.BytesIO()
        Image.new('RGB', (640, 240), 'white').save(image, 'PNG')
        response = self.client.post(f'/api/assignments/{self.aid}/photos', data={
            'image': (io.BytesIO(image.getvalue()), 'photo.png')}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def job(self):
        return self.client.get(f'/api/assignments/{self.aid}/grading-jobs').json['job']

    def start(self):
        result = self.post(f'/api/assignments/{self.aid}/grading-jobs', {})
        self.assertEqual(result.status_code, 202, result.json)
        return result.json['job']

    def ocr(self, code='001', name='张同学'):
        return {'blocks': [{'id': 0, 'text': f'学号：{code} 姓名：{name}', 'box': [0, 0, 600, 40]},
                           {'id': 1, 'text': '1. receive', 'box': [0, 50, 600, 90]}]}

    def test_snapshot_and_active_start_are_idempotent_and_complete_without_browser(self):
        self.assertIsNone(self.job())
        self.photo()
        job = self.start()
        self.assertEqual(job['state'], 'queued')
        self.assertEqual(job['total'], 1)
        self.assertEqual(self.start()['id'], job['id'])
        image = io.BytesIO()
        Image.new('RGB', (640, 240), 'white').save(image, 'PNG')
        for state in ('queued','running','interrupted','failed'):
            with self.db() as conn:
                conn.execute('UPDATE grading_jobs SET state=? WHERE id=?', (state,job['id']))
            response = self.client.post(f'/api/assignments/{self.aid}/photos', data={
                'image': (io.BytesIO(image.getvalue()), 'another-tab.png')}, headers=self.headers)
            response.request.environ['wsgi.input'].close()
            self.assertEqual(response.status_code,409, 'Other tabs must not upload outside the job snapshot')
        with self.db() as conn:
            conn.execute("UPDATE grading_jobs SET state='queued' WHERE id=?", (job['id'],))
        with patch('settings.recognize_selected', return_value=self.ocr()) as cloud:
            self.runner.drain()
            self.assertEqual(cloud.call_count, 1)
        self.assertEqual(self.job()['state'], 'done')
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM batch_photos').fetchone()[0],1)
            sub = conn.execute('SELECT id FROM submissions').fetchone()[0]
        self.assertEqual(self.client.get(f'/api/submissions/{sub}').json['score'], 2)

    def test_restart_requires_resume_and_reuses_completed_identity(self):
        self.photo(); self.photo()
        job = self.start()
        with patch('settings.recognize_selected', return_value=self.ocr()) as cloud:
            self.runner.run_next()
            self.assertEqual(cloud.call_count, 1)
        self.restart()
        self.assertEqual(self.job()['state'], 'interrupted')
        with patch('settings.recognize_selected', return_value=self.ocr('002', '王同学')) as cloud:
            self.runner.drain()
            cloud.assert_not_called()
            result = self.post(f"/api/grading-jobs/{job['id']}/resume", {})
            self.assertEqual(result.status_code, 202, result.json)
            self.assertEqual(self.start()['id'], job['id'])
            self.runner.drain()
            self.assertEqual(cloud.call_count, 1)
        self.assertEqual(self.job()['state'], 'done')
        self.assertEqual(self.job()['errors'], [])

    def test_identity_failure_does_not_abort_other_photos_or_leak_provider_error(self):
        bad = self.photo(); self.photo()
        self.start()
        with patch('settings.recognize_selected', side_effect=[RuntimeError('secret-api-key'), self.ocr()]):
            self.runner.drain()
        job = self.job()
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['errors'][0]['photo_id'], bad)
        self.assertNotIn('secret-api-key', json.dumps(job))
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions WHERE grading_state='final'").fetchone()[0], 1)

    def test_failed_grade_is_saved_and_next_student_still_finishes(self):
        self.photo(); self.photo()
        self.start()
        with patch('settings.recognize_selected', side_effect=[self.ocr(), self.ocr('002', '王同学')]):
            while self.job()['stage'] != 'grading':
                self.runner.run_next()
        with self.db() as conn:
            first = conn.execute('SELECT id FROM submissions ORDER BY id LIMIT 1').fetchone()[0]
            conn.execute("UPDATE pages SET blocks='[]',status='uploaded' WHERE submission_id=?", (first,))
        with self.assertLogs(self.app.logger, level='WARNING') as logs:
            with patch('settings.recognize_selected', side_effect=RuntimeError('secret-provider-error')):
                self.runner.drain()
        job = self.job()
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['errors'][0]['submission_id'], first)
        self.assertNotIn('secret-provider-error', json.dumps(job))
        self.assertNotIn('secret-provider-error', '\n'.join(logs.output))
        with self.db() as conn:
            states = [s[0] for s in conn.execute('SELECT grading_state FROM submissions ORDER BY id')]
        self.assertEqual(states, ['failed', 'final'])

    def test_restart_after_grade_commit_reuses_final_result_even_if_cursor_was_not_saved(self):
        self.photo()
        job = self.start()
        with patch('settings.recognize_selected', return_value=self.ocr()) as cloud:
            while self.job()['stage'] != 'grading':
                self.runner.run_next()
            self.runner.run_next()
            self.assertEqual(cloud.call_count, 1)
        with self.db() as conn:
            conn.execute('UPDATE grading_jobs SET grade_cursor=0,completed=0 WHERE id=?', (job['id'],))
            events = conn.execute('SELECT COUNT(*) FROM review_events').fetchone()[0]
        self.restart()
        self.post(f"/api/grading-jobs/{job['id']}/resume", {})
        with patch('settings.recognize_selected') as cloud:
            self.runner.drain()
            cloud.assert_not_called()
        self.assertEqual(self.job()['state'], 'done')
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_events').fetchone()[0], events)

    def test_missing_empty_and_invalid_requests_do_not_create_jobs(self):
        self.assertEqual(self.post('/api/assignments/9999/grading-jobs', {}).status_code, 404)
        self.assertEqual(self.post(f'/api/assignments/{self.aid}/grading-jobs', {}).status_code, 400)
        self.photo()
        self.assertEqual(self.post(f'/api/assignments/{self.aid}/grading-jobs', []).status_code, 400)
        self.assertEqual(self.post('/api/grading-jobs/9999/resume', {}).status_code, 404)
        self.assertIsNone(self.job())

    def cached_photo_and_single_job(self):
        cached=self.photo();selected=self.photo()
        with patch('settings.recognize_selected',return_value=self.ocr()):
            result=self.post(f'/api/batch-photos/{cached}/identify',{'defer_link':True})
            self.assertEqual(result.status_code,200,result.json)
        job=self.post(f'/api/batch-photos/{selected}/process',{}).json['job']
        return cached,selected,job

    def test_single_photo_job_grades_all_newly_matched_cached_photos_without_reidentifying(self):
        cached,selected,job=self.cached_photo_and_single_job()
        with patch('settings.recognize_selected',return_value=self.ocr('002','王同学')) as cloud:
            self.runner.drain()
            self.assertEqual(cloud.call_count,1,'Only the selected, previously unrecognized photo uses OCR')
        with self.db() as conn:
            photos=conn.execute('SELECT id,submission_id FROM batch_photos ORDER BY id').fetchall()
            grades=conn.execute('SELECT id,grading_state FROM submissions ORDER BY id').fetchall()
            snapshot=json.loads(conn.execute('SELECT photo_ids FROM grading_jobs WHERE id=?',(job['id'],)).fetchone()[0])
        self.assertEqual([s['grading_state'] for s in grades],['final','final'])
        self.assertEqual(set(snapshot),{cached,selected})
        self.assertEqual(len({p['submission_id'] for p in photos}),2)
        self.assertEqual(self.job()['total'],2)

    def test_restart_after_matching_commit_still_grades_extra_cached_photos(self):
        cached,selected,job=self.cached_photo_and_single_job()
        with patch('settings.recognize_selected',return_value=self.ocr('002','王同学')):
            while self.job()['stage']!='matching':self.runner.run_next()
        save=self.runner.save
        def crash_after_match(current,**changes):
            if changes.get('stage')=='grading':raise SystemExit('synthetic process termination')
            return save(current,**changes)
        with patch.object(self.runner,'save',side_effect=crash_after_match):
            with self.assertRaises(SystemExit):self.runner.run_next()
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM batch_photos WHERE submission_id IS NOT NULL').fetchone()[0],2)
        self.restart()
        self.assertEqual(self.job()['state'],'interrupted')
        self.assertEqual(self.post(f"/api/grading-jobs/{job['id']}/resume",{}).status_code,202)
        with patch('settings.recognize_selected') as cloud:
            self.runner.drain();cloud.assert_not_called()
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions WHERE grading_state='final'").fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0],2)

    def test_cached_match_keeps_existing_final_grade_and_teacher_history(self):
        original=self.photo();self.start()
        with patch('settings.recognize_selected',return_value=self.ocr()):self.runner.drain()
        with self.db() as conn:
            sid=conn.execute('SELECT submission_id FROM batch_photos WHERE id=?',(original,)).fetchone()[0]
        detail=self.client.get(f'/api/submissions/{sid}').json
        saved=self.post(f"/api/submissions/{sid}/reviews/{detail['reviews'][0]['question_id']}",
                        {'transcript':'teacher correction','confirmed':True})
        self.assertEqual(saved.status_code,200,saved.json)
        with self.db() as conn:
            before={t:[tuple(r) for r in conn.execute(f'SELECT * FROM {t} WHERE '+
                    ('id=?' if t=='submissions' else 'submission_id=?')+' ORDER BY rowid',(sid,))]
                    for t in ('submissions','reviews','review_events')}
        cached=self.photo()
        with patch('settings.recognize_selected',return_value=self.ocr('002','王同学')):
            self.assertEqual(self.post(f'/api/batch-photos/{cached}/identify',{'defer_link':True}).status_code,200)
        self.assertEqual(self.post(f'/api/batch-photos/{original}/process',{}).status_code,202)
        with patch('settings.recognize_selected') as cloud:
            self.runner.drain();cloud.assert_not_called()
        with self.db() as conn:
            for table,rows in before.items():
                self.assertEqual([tuple(r) for r in conn.execute(f'SELECT * FROM {table} WHERE '+
                    ('id=?' if table=='submissions' else 'submission_id=?')+' ORDER BY rowid',(sid,))],rows)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions WHERE grading_state='final'").fetchone()[0],2)


if __name__ == '__main__':
    unittest.main()
