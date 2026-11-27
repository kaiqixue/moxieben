"""Identity rereads stay in the durable queue; providers and data are isolated."""
import json
import unittest
from unittest.mock import patch

import test_grading_jobs as fixtures


class IdentityJobTests(unittest.TestCase):
    # Reuse setup/helpers without importing the fixture's test cases into discovery.
    setUp = fixtures.GradingJobTests.setUp
    post = fixtures.GradingJobTests.post
    klass = fixtures.GradingJobTests.klass
    task = fixtures.GradingJobTests.task
    db = fixtures.GradingJobTests.db
    restart = fixtures.GradingJobTests.restart
    photo = fixtures.GradingJobTests.photo
    job = fixtures.GradingJobTests.job
    start = fixtures.GradingJobTests.start
    ocr = fixtures.GradingJobTests.ocr

    def enable(self, enabled=True):
        result = self.client.patch('/api/settings/ai-review', json=dict(enabled=enabled,
            base_url='https://vision.example.test/v1', model='synthetic', api_key='synthetic-key'),
            headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)

    @staticmethod
    def model(name='张同学', code='001', class_name='', confidence='high'):
        return json.dumps(dict(name=name, student_code=code, class_name=class_name,
                              confidence=confidence), ensure_ascii=False)

    def advance_to_review(self):
        for _ in range(12):
            if self.job()['stage'] == 'identity_review':
                return
            self.assertTrue(self.runner.run_next())
        self.fail(f'Queue never reached identity_review: {self.job()}')

    def photos(self):
        return self.client.get(f'/api/assignments/{self.aid}/photos').json['photos']

    def test_disabled_auto_review_keeps_pending_without_calling_model(self):
        self.photo(); self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat') as model):
            self.runner.drain()
        model.assert_not_called()
        self.assertEqual(self.job()['state'], 'done')
        self.assertIsNone(self.photos()[0]['submission_id'])

    def test_pending_identity_uses_same_queue_and_grades_after_review(self):
        self.enable(); pid = self.photo(); job = self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat', return_value=self.model()) as model,
             patch('settings.reread_page', return_value=[])):
            self.advance_to_review()
            self.assertEqual(self.job()['id'], job['id'])
            model.assert_not_called()
            self.runner.drain()
        model.assert_called_once()
        self.assertEqual(self.job()['state'], 'done', self.job())
        photo = next(p for p in self.photos() if p['id'] == pid)
        self.assertIsNotNone(photo['submission_id'], photo)
        self.assertEqual(photo['identity_review']['read_name'], '张同学')
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grading_jobs').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT grading_state FROM submissions').fetchone()[0], 'final')

    def test_exact_and_duplicate_photos_do_not_call_identity_model(self):
        self.enable(); self.photo(); self.photo(); self.start()
        with patch('settings.recognize_selected', return_value=self.ocr()), patch('settings._chat') as model:
            self.runner.drain()
        model.assert_not_called()
        self.assertEqual(self.job()['state'], 'done')
        self.assertTrue(all(p['match_details']['method'] == 'duplicate' for p in self.photos()))

    def test_provider_failure_stops_fanout_and_hides_secrets(self):
        self.enable(); self.photo(); self.photo(); self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat', side_effect=RuntimeError('synthetic-key')) as model):
            self.runner.drain()
        model.assert_called_once()
        self.assertEqual(self.job()['state'], 'done')
        self.assertTrue(self.job()['errors'])
        self.assertNotIn('synthetic-key', json.dumps(self.job()))
        self.assertTrue(all(p['submission_id'] is None for p in self.photos()))

    def test_truncated_identity_does_not_skip_remaining_photos(self):
        import settings
        self.enable();self.photo();self.photo();self.start()
        with (patch('settings.recognize_selected',return_value=self.ocr('', '未知姓名')),
              patch('settings._chat',side_effect=[settings.VisualReviewError('output_truncated'),self.model(name='王同学',code='002')]) as model,
              patch('settings.reread_page',return_value=[])):
            self.runner.drain()
        self.assertEqual(model.call_count,2)
        photos=self.photos()
        self.assertIsNone(photos[0]['submission_id'])
        self.assertIsNotNone(photos[1]['submission_id'])

    def test_restart_after_review_commit_reuses_saved_result_when_cursor_was_not_saved(self):
        self.enable(); self.photo(); job = self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat', return_value=self.model()) as model):
            self.advance_to_review()
            save = self.runner.save
            def crash_after_call(current, **changes):
                if 'identity_review_cursor' in json.loads(changes.get('options', '{}')):
                    raise SystemExit('synthetic worker termination')
                return save(current, **changes)
            with patch.object(self.runner, 'save', side_effect=crash_after_call):
                with self.assertRaises(SystemExit):
                    self.runner.run_next()
            model.assert_called_once()
        self.restart()
        self.assertEqual(self.job()['state'], 'interrupted')
        self.assertEqual(self.post(f"/api/grading-jobs/{job['id']}/resume", {}).status_code, 202)
        with (patch('settings.recognize_selected') as ocr, patch('settings._chat') as model,
             patch('settings.reread_page', return_value=[])):
            self.runner.drain()
        ocr.assert_not_called(); model.assert_not_called()
        self.assertEqual(self.job()['state'], 'done')
        self.assertIsNotNone(self.photos()[0]['submission_id'])

    def test_wrong_class_review_never_moves_photo_to_another_class(self):
        self.enable(); self.photo(); self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat', return_value=self.model(class_name='其他班')) as model):
            self.runner.drain()
        model.assert_called_once()
        self.assertEqual(self.job()['state'], 'done')
        self.assertIsNone(self.photos()[0]['submission_id'])

    def test_restart_during_paid_request_leaves_manual_work_without_resending(self):
        self.enable(); self.photo(); job = self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat', side_effect=SystemExit('synthetic termination')) as model):
            self.advance_to_review()
            with self.assertRaises(SystemExit):
                self.runner.run_next()
            model.assert_called_once()
        self.restart()
        self.assertEqual(self.post(f"/api/grading-jobs/{job['id']}/resume", {}).status_code, 202)
        with patch('settings.recognize_selected') as ocr, patch('settings._chat') as model:
            self.runner.drain()
        ocr.assert_not_called(); model.assert_not_called()
        self.assertEqual(self.job()['state'], 'done')
        self.assertIsNone(self.photos()[0]['submission_id'])

    def test_restart_after_provider_error_preserves_stop_before_other_photos(self):
        self.enable(); self.photo(); self.photo(); job = self.start()
        with (patch('settings.recognize_selected', return_value=self.ocr('', '未知姓名')),
             patch('settings._chat', side_effect=RuntimeError('provider unavailable')) as model):
            self.advance_to_review()
            save = self.runner.save
            def crash_after_call(current, **changes):
                if 'identity_review_cursor' in json.loads(changes.get('options', '{}')):
                    raise SystemExit('synthetic worker termination')
                return save(current, **changes)
            with patch.object(self.runner, 'save', side_effect=crash_after_call):
                with self.assertRaises(SystemExit):
                    self.runner.run_next()
            model.assert_called_once()
        self.restart()
        self.post(f"/api/grading-jobs/{job['id']}/resume", {})
        with patch('settings._chat') as model:
            self.runner.drain()
        model.assert_not_called()
        self.assertEqual(self.job()['state'], 'done')

    def test_explicit_batch_retries_failed_identity_without_repeating_ocr(self):
        self.enable(False); pid=self.photo(); self.start()
        with patch('settings.recognize_selected',return_value=self.ocr('', '未知姓名')):
            self.runner.drain()
        with patch('settings._chat',side_effect=TimeoutError()):
            self.assertEqual(self.post(f'/api/batch-photos/{pid}/identity-review',{}).status_code,503)
        response=self.post(f'/api/assignments/{self.aid}/identity-review-jobs',{})
        self.assertEqual(response.status_code,202,response.json)
        with (patch('settings.recognize_selected') as ocr,
              patch('settings._chat',return_value=self.model()) as model,
              patch('settings.reread_page',return_value=[])):
            self.runner.drain()
        ocr.assert_not_called(); model.assert_called_once()
        photo=self.photos()[0]
        self.assertIsNotNone(photo['submission_id'],photo)
        self.assertEqual(photo['identity_review']['history'][0]['status'],'error')

    def test_name_only_review_finishes_grading_in_same_batch_job(self):
        self.enable();self.photo();job=self.start()
        with (patch('settings.recognize_selected',return_value=self.ocr('', '未知姓名')),
              patch('settings._chat',return_value=self.model(code='')) as model,
              patch('settings.reread_page',return_value=[])):
            self.runner.drain()
        model.assert_called_once()
        self.assertEqual(self.job()['id'],job['id'])
        self.assertEqual(self.job()['state'],'done',self.job())
        photo=self.photos()[0]
        self.assertTrue(photo['submission_id'])
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grading_jobs').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT grading_state FROM submissions').fetchone()[0],'final')

    def test_verified_near_name_finishes_grading_in_same_batch_job(self):
        self.enable();self.photo();self.start()
        free=self.model(name='张同字',code='',confidence='low')
        verified=json.dumps(dict(name='张同学',decision='match',confidence='high',evidence='签名字形一致'))
        with (patch('settings.recognize_selected',return_value={'blocks':[{'text':'未知身份','box':[0,0,600,40]}]}),
              patch('settings._chat',side_effect=[free,free,verified]) as model,
              patch('settings.reread_page',return_value=[])):
            self.runner.drain()
        self.assertEqual(model.call_count,3)
        photo=self.photos()[0]
        self.assertTrue(photo['submission_id'])
        self.assertEqual(photo['identity_review']['read_name'],'张同字')
        self.assertEqual(self.job()['state'],'done',self.job())
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grading_jobs').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT grading_state FROM submissions').fetchone()[0],'final')

    def test_verification_provider_failure_stops_remaining_paid_identity_requests(self):
        self.enable();self.photo();self.photo();self.start()
        free=self.model(name='张同字',code='',confidence='low')
        with (patch('settings.recognize_selected',return_value={'blocks':[{'text':'未知身份','box':[0,0,600,40]}]}),
              patch('settings._chat',side_effect=[free,free,TimeoutError()]) as model):
            self.runner.drain()
        self.assertEqual(model.call_count,3)
        self.assertTrue(self.job()['errors'])
        self.assertTrue(all(p['submission_id'] is None for p in self.photos()))


if __name__ == '__main__':
    unittest.main()
