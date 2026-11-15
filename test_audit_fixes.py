"""Audit regressions: isolated SQLite and mocked providers, never production data."""
import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image
from test_grading_jobs import GradingJobTests
from test_ocr_alignment import OCRAlignmentTests
from test_paddle_cloud import PaddleCloudTests


class AuditFlowTests(unittest.TestCase):
    setUp = GradingJobTests.setUp
    post = GradingJobTests.post
    klass = GradingJobTests.klass
    task = GradingJobTests.task
    restart = GradingJobTests.restart
    db = GradingJobTests.db
    photo = GradingJobTests.photo
    ocr = GradingJobTests.ocr

    def identify(self, pid):
        with patch('settings.recognize_selected', return_value=self.ocr()):
            return self.post(f'/api/batch-photos/{pid}/identify', {'defer_link': True})

    def link(self, pid, **extra):
        roster = self.client.get(f'/api/assignments/{self.aid}').json['students'][0]
        return self.post(f'/api/batch-photos/{pid}/confirm', {
            'confirmed': True, 'assignment_student_id': roster['id'], **extra})

    def test_41_photo_batch_waiting_is_not_teacher_attention(self):
        photos = [self.photo() for _ in range(41)]
        outside = self.photo()
        job = self.runner.enqueue(assignment_id=self.aid, photo_ids=photos)
        tid = self.client.get(f'/api/assignments/{self.aid}').json['task_id']
        url = f'/api/assignments/{self.aid}/attention'
        for phase in ('queued', 'running'):
            with self.db() as conn:
                conn.execute('UPDATE grading_jobs SET state=? WHERE id=?', (phase, job['id']))
            data = self.client.get(url).json
            self.assertEqual(data['summary']['identity_photos'], 1)
            self.assertEqual(data['summary']['processing_photos'], 41)
            self.assertEqual([p['photo_id'] for p in data['items']], [outside])
            self.assertEqual(self.client.get(f'/api/tasks/{tid}').json['pending_identity_photos'], 1)
        with patch('settings.recognize_selected', return_value=self.ocr()):
            self.runner.run_next()
        listing = self.client.get(f'/api/assignments/{self.aid}/photos').json['photos']
        self.assertTrue(next(p for p in listing if p['id'] == photos[0])['processing_identity'])
        self.assertFalse(next(p for p in listing if p['id'] == outside)['processing_identity'])
        self.assertEqual(self.link(photos[0]).status_code, 409)
        with self.db() as conn:
            conn.execute("UPDATE grading_jobs SET state='interrupted' WHERE id=?", (job['id'],))
        self.assertEqual(self.client.get(url).json['summary']['identity_photos'], 42)
        with self.db() as conn:
            conn.execute("UPDATE grading_jobs SET state='running',stage='matching' WHERE id=?", (job['id'],))
        self.assertEqual(self.client.get(url).json['summary']['processing_photos'], 41)
        self.runner.run_next()
        # Matching has finished: unresolved photos are now actionable even during grading.
        self.assertEqual(self.client.get(url).json['summary']['processing_photos'], 0)
        self.assertEqual(self.client.get(url).json['summary']['identity_photos'], 41)

    def test_identity_failure_keeps_safe_provider_reason_in_photo_and_job(self):
        pid = self.photo()
        with self.db() as conn:
            conn.execute("UPDATE settings SET value='paddle_cloud' WHERE key='provider'")
            conn.execute("UPDATE settings SET value='private-test-secret' WHERE key='paddle_token'")
        self.runner.enqueue(assignment_id=self.aid, photo_ids=[pid])
        with self.assertLogs(self.app.logger, level='WARNING') as logs, patch('paddle_cloud.recognize', side_effect=ValueError('PaddleOCR 请求过多或额度不足，请检查控制台额度。 private-test-secret https://provider.invalid/?token=secret')):
            self.runner.run_next()
        photo = self.client.get(f'/api/assignments/{self.aid}/photos').json['photos'][0]
        self.assertIn('额度', photo['error'])
        self.assertNotIn('private-test-secret', photo['error'])
        self.assertNotIn('provider.invalid', photo['error'])
        self.assertNotIn('private-test-secret', ''.join(logs.output))
        self.assertNotIn('provider.invalid', ''.join(logs.output))
        job = self.client.get(f'/api/assignments/{self.aid}/grading-jobs').json['job']
        self.assertIn('额度', job['errors'][0]['message'])
        self.assertNotIn('private-test-secret', json.dumps(job))
        self.assertEqual(photo['status'], 'error')
        with self.client.get(photo['url']) as image:
            self.assertEqual(image.status_code, 200)

    def test_unknown_identity_failure_never_exposes_raw_exception(self):
        pid = self.photo()
        with patch('settings.recognize_selected', side_effect=RuntimeError('private-test-secret https://provider.invalid/?token=secret')):
            response = self.post(f'/api/batch-photos/{pid}/identify', {})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-test-secret', response.get_data(as_text=True))
        self.assertNotIn('provider.invalid', response.get_data(as_text=True))

    def test_exclude_restore_photo_updates_task_attention_and_never_changes_score(self):
        pid = self.photo()
        self.identify(pid)
        tid = self.client.get(f'/api/assignments/{self.aid}').json['task_id']
        task = self.client.get(f'/api/tasks/{tid}').json
        self.assertEqual(task.get('pending_identity_photos'), 1)
        summary = next(t for t in self.client.get('/api/tasks').json['tasks'] if t['id'] == tid)
        self.assertEqual(summary.get('pending_identity_photos'), 1)
        self.assertEqual(self.post(f'/api/batch-photos/{pid}/exclude', {'excluded': True}).status_code, 200)
        self.assertEqual(self.client.get(f'/api/tasks/{tid}/attention').json['summary']['identity_photos'], 0)
        self.assertEqual(self.link(pid).status_code, 409)
        self.restart()
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/assignments/{self.aid}/photos/match', {})
            self.assertEqual(result.json['summary']['pending'], 0)
            self.assertEqual(self.post(f'/api/batch-photos/{pid}/exclude', {'excluded': False}).status_code, 200)
            cloud.assert_not_called()
        self.assertEqual(self.client.get(f'/api/tasks/{tid}/attention').json['summary']['identity_photos'], 1)

    def test_duplicate_append_requires_explicit_choice_and_rejects_same_image(self):
        first, duplicate = self.photo(), self.photo()
        self.identify(first)
        linked = self.link(first).json
        sid = linked['submission_id']
        self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True})
        before = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(self.link(duplicate).status_code, 409)
        self.assertEqual(self.link(duplicate, append_page=True).status_code, 409)
        after = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual((after['score'], len(after['pages'])), (before['score'], 1))

    def test_linked_photo_cannot_be_excluded(self):
        pid = self.photo()
        self.identify(pid)
        self.link(pid)
        self.assertEqual(self.post(f'/api/batch-photos/{pid}/exclude', {'excluded': True}).status_code, 409)

    def test_confirmation_enqueues_before_response_and_queued_job_blocks_edits(self):
        pid = self.photo()
        self.identify(pid)
        linked = self.link(pid, auto_grade=True)
        self.assertEqual(linked.status_code, 200)
        sid, jid = linked.json['submission_id'], linked.json['job']['id']
        sub = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(sub['processing_job']['id'], jid)
        self.assertEqual(self.post(f"/api/submissions/{sid}/reviews/{sub['reviews'][0]['question_id']}",
            {'transcript': 'receive', 'confirmed': True}).status_code, 409)
        with patch('settings.recognize_selected') as cloud:
            self.runner.drain()
            cloud.assert_not_called()
        self.assertTrue(self.client.get(f'/api/submissions/{sid}').json['is_final'])

    def test_quick_upload_enqueues_without_another_browser_request(self):
        image = io.BytesIO()
        Image.new('RGB', (640,240), 'white').save(image,'PNG')
        response = self.client.post(f'/api/assignments/{self.aid}/photos', data={
            'image': (io.BytesIO(image.getvalue()),'photo.png'), 'auto_process': 'true'}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['job']['state'], 'queued')
        tid = self.client.get(f'/api/assignments/{self.aid}').json['task_id']
        self.assertEqual(self.client.get(f'/api/tasks/{tid}').json['background_jobs'], 1)
        with patch('settings.recognize_selected', return_value=self.ocr()):
            self.runner.drain()
        self.assertEqual(self.client.get(f'/api/assignments/{self.aid}').json['counts']['completed'], 1)

    def test_legacy_queue_migration_retains_progress_and_foreign_keys(self):
        self.photo()
        job = self.post(f'/api/assignments/{self.aid}/grading-jobs', {}).json['job']
        with self.db() as conn:
            conn.execute('DROP INDEX one_active_submission_job')
            conn.execute('ALTER TABLE grading_jobs DROP COLUMN submission_id')
            conn.execute('ALTER TABLE grading_jobs DROP COLUMN options')
        self.restart()
        recovered = self.client.get(f"/api/grading-jobs/{job['id']}").json['job']
        self.assertEqual(recovered['state'], 'interrupted')
        self.assertEqual((recovered['total'],recovered['completed']), (job['total'],job['completed']))
        with self.db() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_queued_temporary_submission_blocks_task_deletion_and_can_resume(self):
        tid = self.task()
        sid = self.post(f'/api/tasks/{tid}/submissions', {'student_code':'temporary'}).json['id']
        image = io.BytesIO(); Image.new('RGB',(640,240),'white').save(image,'PNG')
        response = self.client.post(f'/api/submissions/{sid}/pages', data={
            'image':(io.BytesIO(image.getvalue()),'temp.png'),'auto_grade':'true'}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code,201)
        preview = self.client.get(f'/api/tasks/{tid}/deletion-preview').json
        self.assertFalse(preview['can_delete'])
        self.restart()
        job = self.client.get(f'/api/submissions/{sid}/grading-jobs').json['job']
        self.post(f"/api/grading-jobs/{job['id']}/resume", {})
        with patch('settings.recognize_selected', return_value=self.ocr()):
            self.runner.drain()
        self.assertEqual(self.client.get(f'/api/submissions/{sid}').json['score'], 2)
        self.assertTrue(self.client.get(f'/api/tasks/{tid}/deletion-preview').json['can_delete'])

    def test_single_photo_job_continues_without_browser_and_is_idempotent(self):
        pid = self.photo()
        url = f'/api/batch-photos/{pid}/process'
        first = self.post(url, {})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.post(url, {}).json['job']['id'], first.json['job']['id'])
        with patch('settings.recognize_selected', return_value=self.ocr()) as cloud:
            self.runner.drain()
            self.assertEqual(cloud.call_count, 1)
        photos = self.client.get(f'/api/assignments/{self.aid}/photos').json['photos']
        sub = self.client.get(f"/api/submissions/{photos[0]['submission_id']}").json
        self.assertTrue(sub['is_final'])
        self.assertEqual(sub['teacher_reviewed_count'], 0)

    def test_submission_job_refresh_only_reads_and_restart_requires_resume(self):
        pid = self.photo()
        self.identify(pid)
        sid = self.link(pid).json['submission_id']
        url = f'/api/submissions/{sid}/grading-jobs'
        first = self.post(url, {'reextract': True})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.post(url, {'reextract': True}).json['job']['id'], first.json['job']['id'])
        self.assertEqual(self.client.get(f'/api/submissions/{sid}').json['recognition']['state'], 'processing')
        self.restart()
        job = self.client.get(url).json['job']
        self.assertEqual(job['state'], 'interrupted')
        self.assertEqual(self.post(f"/api/grading-jobs/{job['id']}/resume", {}).status_code, 202)
        with patch('settings.recognize_selected') as cloud:
            self.runner.drain()
            cloud.assert_not_called()
        self.assertTrue(self.client.get(f'/api/submissions/{sid}').json['is_final'])

    def test_normal_and_quick_review_both_reject_running_page(self):
        pid = self.photo()
        self.identify(pid)
        sid = self.link(pid).json['submission_id']
        sub = self.client.get(f'/api/submissions/{sid}').json
        qid = sub['reviews'][0]['question_id']
        with self.db() as conn:
            conn.execute("UPDATE pages SET status='running' WHERE submission_id=?", (sid,))
        for body in ({'transcript': 'receive', 'confirmed': True}, {'quick_decision': 'correct'}):
            self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{qid}', body).status_code, 409)

    def test_manual_ocr_resets_both_retry_flags(self):
        pid = self.photo()
        self.identify(pid)
        linked = self.link(pid).json
        with self.db() as conn:
            conn.execute('UPDATE pages SET auto_supplement_attempted=1,region_supplement_attempted=1 WHERE id=?', (linked['page_id'],))
        with patch('settings.recognize_selected', return_value=self.ocr()):
            result = self.post(f"/api/pages/{linked['page_id']}/ocr", {})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['pages'][0]['auto_supplement_attempted'], 0)
        self.assertEqual(result.json['pages'][0]['region_supplement_attempted'], 0)


class AuditEvidenceTests(unittest.TestCase):
    setUpClass = OCRAlignmentTests.__dict__['setUpClass']
    setUp = OCRAlignmentTests.setUp
    post = OCRAlignmentTests.post
    submission = OCRAlignmentTests.submission
    image = OCRAlignmentTests.image
    seed = OCRAlignmentTests.seed
    setup_grammar = OCRAlignmentTests.setup_grammar

    def test_crop_only_never_claims_full_page_supplement(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True})
        crop = {'blocks': [{'id': 0, 'text': 'This is probably (1) why Western societies are known for this.',
                           'box': [5, 5, 300, 150], 'approximate': True}]}
        with patch('settings.recognize_selected', return_value=crop) as cloud:
            data = self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True}).json
        self.assertEqual(cloud.call_count, 1)
        evidence = data['reviews'][1]['auto_result']['recognition_evidence']
        self.assertEqual(evidence, [{'source': 'existing_ocr', 'transcript': 'demand'}])


class AuditCloudTests(unittest.TestCase):
    setUp = PaddleCloudTests.setUp
    response = PaddleCloudTests.response
    result = PaddleCloudTests.result

    def test_timeout_reuses_persisted_job_after_app_restart_without_resubmitting(self):
        from app import create_app
        from settings import recognize_selected
        from contextlib import contextmanager
        app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name})
        @contextmanager
        def db():
            conn = sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')
            conn.row_factory = sqlite3.Row
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        with db() as conn:
            conn.executemany('UPDATE settings SET value=? WHERE key=?', [(v, k) for k, v in self.config.items()])
        with patch('paddle_cloud.requests.post', return_value=self.response({'data': {'jobId': 'resume-job'}})) as submit:
            with patch('paddle_cloud.requests.get', side_effect=requests.Timeout()):
                with self.assertRaises(RuntimeError):
                    recognize_selected(db, self.path, lambda p: None)
            create_app({'TESTING': True, 'DATA_DIR': self.tmp.name})
            done = self.response({'data': {'state': 'done', 'jobId': 'resume-job', 'resultUrl': {'jsonUrl': 'https://example.test/r'}}})
            with patch('paddle_cloud.requests.get', side_effect=[done, self.result()]) as get:
                result = recognize_selected(db, self.path, lambda p: None)
            self.assertEqual(submit.call_count, 1)
            self.assertTrue(get.call_args_list[0].args[0].endswith('/resume-job'))
            self.assertEqual(result['job_id'], 'resume-job')
        with db() as conn:
            saved = json.dumps([dict(r) for r in conn.execute('SELECT * FROM cloud_ocr_jobs')])
        self.assertNotIn(self.config['paddle_token'], saved)


del GradingJobTests, OCRAlignmentTests, PaddleCloudTests

if __name__ == '__main__':
    unittest.main()
