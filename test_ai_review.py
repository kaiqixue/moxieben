"""AI review uses synthetic crops, isolated SQLite and mocked providers only."""
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_ocr_alignment
import test_tenant_isolation
from settings import reread_page as real_reread_page


class AISettingsTests(unittest.TestCase):
    setUp = test_tenant_isolation.TenantIsolationTests.setUp
    token = test_tenant_isolation.TenantIsolationTests.token
    register = test_tenant_isolation.TenantIsolationTests.register

    def test_private_masked_settings_and_validation(self):
        route = '/api/settings/ai-review'
        self.assertEqual(self.a.get(route).status_code, 200)
        self.assertFalse(self.a.get(route).json['enabled'])
        config = dict(enabled=True, limit=3, base_url='https://vision.example.test/v1',
                      model='synthetic-vision', api_key='synthetic-private-key')
        response = self.a.patch(route, json=config, headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn(config['api_key'], response.get_data(as_text=True))
        self.assertTrue(response.json['api_key_configured'])
        self.assertFalse(self.b.get(route).json['api_key_configured'])
        self.assertEqual(self.a.patch(route, json=config).status_code, 403)
        self.assertEqual(self.a.patch(route, json=config, headers={'X-CSRF-Token': self.token(self.a),
            'Origin': 'https://foreign.example.test'}).status_code, 403)
        with patch('settings.reread_page', side_effect=ValueError(config['api_key'])):
            failed=self.a.post(route+'/test', json={}, headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(failed.status_code,400)
        self.assertEqual(failed.json['error_code'],'provider')
        self.assertNotIn(config['api_key'],failed.get_data(as_text=True))
        for invalid in ({'limit': True}, {'limit': 101}, {'enabled': 'yes'},
                        {'base_url': 'http://unsafe.test'}, {'unknown': 1}):
            self.assertEqual(self.a.patch(route, json=invalid,
                headers={'X-CSRF-Token': self.token(self.a)}).status_code, 400)


class AIReviewTests(unittest.TestCase):
    setUpClass = test_ocr_alignment.OCRAlignmentTests.__dict__['setUpClass']
    def setUp(self):
        test_ocr_alignment.OCRAlignmentTests.setUp(self)
        localizer=patch('ai_localization.offline_blocks',side_effect=ValueError('offline unavailable'))
        localizer.start()
        self.addCleanup(localizer.stop)
        page_model=patch('settings.reread_page',side_effect=ValueError('synthetic provider failure'))
        page_model.start()
        self.addCleanup(page_model.stop)
    post = test_ocr_alignment.OCRAlignmentTests.post
    submission = test_ocr_alignment.OCRAlignmentTests.submission
    image = test_ocr_alignment.OCRAlignmentTests.image
    seed = test_ocr_alignment.OCRAlignmentTests.seed
    setup_grammar = test_ocr_alignment.OCRAlignmentTests.setup_grammar

    def prepare(self):
        sid, page, _ = self.setup_grammar()
        crop = dict(source='automatic_region_supplement', region_strategy=3, question_numbers=[1],
            blocks=[dict(id=0, text='(1) what Western societies are known for this.',
                         box=[200, 100, 400, 200], approximate=True)])
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn, conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?', (page['id'],))
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                         (page['id'], 'now', json.dumps(crop)))
        detail = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
        response = self.client.patch('/api/settings/ai-review', json=dict(enabled=True, limit=2,
            base_url='https://vision.example.test/v1', model='synthetic', api_key='test-key'), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return sid, detail

    def test_background_page_review_covers_every_question_despite_legacy_limit(self):
        self.prepare()
        task=self.post('/api/tasks',dict(name='Ten synthetic answers',questions=[
            dict(number=n,kind='word',answer='book',points=1) for n in range(1,11)])).json
        sid=self.submission(task);page=self.image(sid).json
        self.seed(sid,page['id'],[dict(id=n,text=f'{n}. book',box=[0,n*10,30,n*10+8],confidence=.95)
                                 for n in range(1,11)],'done')
        self.post(f'/api/submissions/{sid}/auto-grade',{})
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=?',
                (json.dumps(dict(source='no_valid_answer',uncertain=True,processing_version=3,regions=[],transcript='')),sid))
        self.client.patch('/api/settings/ai-review',json={'limit':1},headers=self.headers)
        runner=self.app.extensions['grading_jobs'];job=runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',return_value=[dict(number=n,status='readable',transcript='book')
                for n in range(1,11)]) as model,patch(
                'settings.recognize_selected',side_effect=AssertionError('must reuse OCR')):
            runner.drain()
        self.assertEqual(model.call_args.args[2],list(range(1,11)))
        result=self.client.get(f'/api/grading-jobs/{job["id"]}').json['job']
        self.assertIn('生成建议 10 题',result['message'])
        self.assertEqual(self.client.get(f'/api/submissions/{sid}').json['uncertain_count'],10)

    def test_page_auto_decisions_preserve_sibling_suggestions_and_independent_undo(self):
        for both_agree in (False,True):
            with self.subTest(both_agree=both_agree):
                sid,before=self.prepare();pid=before['pages'][0]['id']
                q2=before['reviews'][1]
                evidence=dict(source='localization_ocr',uncertain=True,processing_version=3,transcript='demand',
                    regions=[dict(page_id=pid,box=[200,400,700,500],block_ids=[2])])
                with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
                    conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=? AND question_id=?',
                        (json.dumps(evidence),sid,q2['question_id']))
                runner=self.app.extensions['grading_jobs'];runner.enqueue(submission_id=sid)
                with patch('settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what'),
                        dict(number=2,status='readable',transcript='demand' if both_agree else 'other')]):
                    runner.drain()
                after=self.client.get(f'/api/submissions/{sid}').json
                self.assertEqual(len(after['visual_suggestions']),2,'own audit events must not invalidate siblings')
                self.assertEqual(after['uncertain_count'],0 if both_agree else 1)
                for q in after['reviews'][:2 if both_agree else 1]:
                    response=self.post(f'/api/submissions/{sid}/reviews/{q["question_id"]}/undo',
                        dict(event_id=q['auto_result']['ai_review']['event_id']))
                    self.assertEqual(response.status_code,200,response.json)
                    self.assertTrue(next(r for r in response.json['reviews'] if r['question_id']==q['question_id'])['uncertain'])

    def test_background_page_agreement_resolves_automatically_without_opening_question(self):
        sid,before=self.prepare();runner=self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]) as model:
            runner.drain()
        model.assert_called_once()
        after=self.client.get(f'/api/submissions/{sid}').json;q=after['reviews'][0]
        self.assertFalse(q['uncertain'])
        self.assertFalse(q['teacher_edited'])
        self.assertEqual(q['auto_result']['ai_review']['scope'],'page')
        self.assertEqual(q['machine_text'],before['reviews'][0]['machine_text'])
        self.assertEqual(q['score'],0,'agreement must not correct spelling to reference answer')
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page') as model:
            runner.drain();model.assert_not_called()
        response=self.post(f'/api/submissions/{sid}/reviews/{q["question_id"]}/undo',
            {'event_id':q['auto_result']['ai_review']['event_id']})
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['reviews'][0]['uncertain'])

    def test_background_reuses_existing_readable_suggestion_without_another_model_call(self):
        sid,before=self.prepare();qid=before['reviews'][0]['question_id']
        with patch('settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]):
            self.assertEqual(self.post(f'/api/submissions/{sid}/visual-reread-page',
                dict(page_id=before['pages'][0]['id'])).status_code,200)
        previous=self.client.get(f'/api/submissions/{sid}').json
        self.assertTrue(previous['reviews'][0]['uncertain'])
        runner=self.app.extensions['grading_jobs'];runner.enqueue(submission_id=sid)
        with patch('settings.reread_page') as model:
            runner.drain();model.assert_not_called()
        after=self.client.get(f'/api/submissions/{sid}').json
        self.assertFalse(after['reviews'][0]['uncertain'])
        self.assertEqual(after['visual_suggestions'][str(qid)]['id'],previous['visual_suggestions'][str(qid)]['id'])

    def test_page_reread_without_crop_can_be_adopted_and_undone(self):
        sid, before = self.prepare()
        pid = before['pages'][0]['id']
        q = before['reviews'][0]
        route = f'/api/submissions/{sid}/visual-reread-page'
        with patch('settings.reread_page', return_value=[dict(number=q['number'], status='readable', transcript='why')]):
            response = self.post(route, dict(page_id=pid))
        self.assertEqual(response.status_code, 200, response.json)
        after = self.client.get(f'/api/submissions/{sid}').json
        self.assertTrue(after['reviews'][0]['uncertain'])
        suggestion = after['visual_suggestions'][str(q['question_id'])]
        self.assertTrue(suggestion['can_adopt'])
        adopted = self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}", dict(visual_suggestion_id=suggestion['id']))
        self.assertEqual(adopted.status_code, 200, adopted.json)
        self.assertFalse(adopted.json['reviews'][0]['uncertain'])
        event = adopted.json['undo']['event_id']
        self.assertEqual(self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}/undo", dict(event_id=event)).status_code, 200)

    def test_page_parser_rejects_duplicate_numbers_and_hides_provider_errors(self):
        config = dict(api_key='secret-test', model='test', base_url='https://vision.example.test/v1')
        item = dict(number=1, status='readable', transcript='why')
        with patch('settings._chat', return_value=json.dumps([item,item])):
            with self.assertRaises(ValueError):
                real_reread_page(config, b'png', [1])
        sid,before=self.prepare()
        with patch('settings.reread_page',side_effect=ValueError('secret-test')):
            response=self.post(f'/api/submissions/{sid}/visual-reread-page',dict(page_id=before['pages'][0]['id']))
        self.assertEqual(response.status_code,502)
        self.assertNotIn('secret-test',response.get_data(as_text=True))
        result=self.client.get(f'/api/submissions/{sid}').json
        suggestion=result['visual_suggestions'][str(before['reviews'][0]['question_id'])]
        self.assertIn('error_message',suggestion)
        self.assertNotIn('secret-test',suggestion['error_message'])

    def test_page_content_failure_continues_batch_and_reports_incomplete(self):
        from settings import VisualReviewError
        sid,before=self.prepare()
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=?',
                (json.dumps(dict(source='no_valid_answer',uncertain=True,processing_version=3,regions=[],transcript='')),sid))
        runner=self.app.extensions['grading_jobs']
        job=runner.enqueue(submission_id=sid)
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE grading_jobs SET grade_ids=? WHERE id=?',(json.dumps([sid,sid]),job['id']))
        with patch('settings.reread_page',side_effect=VisualReviewError('output_truncated')) as model:
            runner.drain()
        self.assertEqual(model.call_count,2)
        result=self.client.get(f"/api/grading-jobs/{job['id']}").json['job']
        self.assertIn('输出被截断',result['message'])
        self.assertTrue(result['ai_incomplete'])
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn:
            options=json.loads(conn.execute('SELECT options FROM grading_jobs WHERE id=?',(job['id'],)).fetchone()[0])
        self.assertNotIn('stopped',options['ai_metrics'])
        self.assertEqual(options['ai_metrics']['failed_pages'],2)

    def test_page_reread_has_room_for_image_model_reasoning_and_json(self):
        import settings
        config=dict(api_key='secret',model='vision',base_url='https://vision.example.test/v1')
        with patch('settings._chat',return_value='[]') as model:
            real_reread_page(config,b'png',[1])
        self.assertGreaterEqual(model.call_args.args[3],2048)

    def test_authentication_failure_still_stops_remaining_paid_requests(self):
        from settings import VisualReviewError
        sid,_=self.prepare();runner=self.app.extensions['grading_jobs']
        job=runner.enqueue(submission_id=sid)
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE grading_jobs SET grade_ids=? WHERE id=?',(json.dumps([sid,sid]),job['id']))
        with patch('settings.reread_page',side_effect=VisualReviewError('authentication')) as model:
            runner.drain()
        model.assert_called_once()
        result=self.client.get(f"/api/grading-jobs/{job['id']}").json['job']
        self.assertTrue(result['ai_incomplete'])
        self.assertLess(result['completed'],result['total'])
        self.assertIn('已暂停后续 AI 请求',result['message'])

    def test_failed_page_request_can_be_retried_in_a_new_job(self):
        sid,before=self.prepare();runner=self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',side_effect=ValueError('synthetic failure')) as model:
            runner.drain();model.assert_called_once()
        self.assertTrue(self.client.get(f'/api/submissions/{sid}').json['reviews'][0]['uncertain'])
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]) as model:
            runner.drain();model.assert_called_once()
        after=self.client.get(f'/api/submissions/{sid}').json
        self.assertFalse(after['reviews'][0]['uncertain'])
        self.assertFalse(after['visual_suggestions'][str(before['reviews'][0]['question_id'])]['error_code'])

    def test_page_multi_results_and_concurrent_edit_are_version_protected(self):
        sid,before=self.prepare()
        pid=before['pages'][0]['id']
        # Make both short answers uncertain without inventing single-question geometry.
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=?',
                (json.dumps(dict(source='no_valid_answer',uncertain=True,processing_version=3,regions=[],transcript='')),sid))
        items=[dict(number=1,status='readable',transcript='why'),dict(number=2,status='readable',transcript='to demand')]
        route=f'/api/submissions/{sid}/visual-reread-page'
        with patch('settings.reread_page',return_value=items) as model:
            response=self.post(route,dict(page_id=pid))
            self.assertEqual(response.json['suggestions'],2,response.json)
            self.assertEqual(model.call_args.args[2],[1,2])
            self.assertNotIn('why',str(model.call_args.args[2]))
        detail=self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(len(detail['visual_suggestions']),2)
        with patch('settings.reread_page') as model:
            self.assertEqual(self.post(route,dict(page_id=pid)).json['requests'],0)
            model.assert_not_called()
        # A change made while the upstream request is in flight must invalidate its results.
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('DELETE FROM visual_suggestions WHERE submission_id=?',(sid,))
        def edit(*args):
            self.post(f"/api/submissions/{sid}/reviews/{before['reviews'][0]['question_id']}",
                dict(transcript='teacher',confirmed=True))
            return items
        with patch('settings.reread_page',side_effect=edit):
            self.post(route,dict(page_id=pid))
        detail=self.client.get(f'/api/submissions/{sid}').json
        self.assertTrue(detail['reviews'][0]['teacher_edited'])
        self.assertEqual(detail.get('visual_suggestions',{}),{})

    def test_background_resolves_before_done_without_teacher_confirmation_and_can_undo(self):
        sid, before = self.prepare()
        runner = self.app.extensions['grading_jobs']
        job = runner.enqueue(submission_id=sid)
        with patch('settings.reread_page', return_value=[dict(number=1,status='readable', transcript='what')]) as model:
            runner.drain()
            after = self.client.get(f'/api/submissions/{sid}').json
            q = after['reviews'][0]
            self.assertFalse(q['uncertain'])
            self.assertFalse(q['teacher_edited'])
            self.assertFalse(q['confirmed'])
            self.assertEqual(q['machine_text'], before['reviews'][0]['machine_text'])
            self.assertEqual(q['score'], 0)
            self.assertEqual(q['auto_result']['ai_review']['model'], 'synthetic')
            model.assert_called_once()
            self.assertEqual(self.client.get(f"/api/grading-jobs/{job['id']}").json['job']['state'], 'done')
            runner.enqueue(submission_id=sid)
            runner.drain()
            model.assert_called_once()
            self.assertFalse(self.client.get(f'/api/submissions/{sid}').json['reviews'][0]['uncertain'])
        undone = self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}/undo", {'event_id': q['auto_result']['ai_review']['event_id']})
        self.assertEqual(undone.status_code, 200, undone.json)
        self.assertTrue(undone.json['reviews'][0]['uncertain'])

    def test_conflict_failure_and_teacher_edit_remain_safe(self):
        for result in (dict(status='readable', transcript='why'), dict(status='blank', transcript=''), ValueError('test-key')):
            sid, before = self.prepare()
            runner = self.app.extensions['grading_jobs']
            runner.enqueue(submission_id=sid)
            kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': [dict(result,number=1)]}
            with patch('settings.reread_page', **kwargs):
                runner.drain()
            after = self.client.get(f'/api/submissions/{sid}').json
            self.assertTrue(after['reviews'][0]['uncertain'])
            self.assertNotIn('test-key', json.dumps(after))
        q = before['reviews'][0]
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}", {'transcript': 'teacher', 'confirmed': True})
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page') as model:
            runner.drain()
            model.assert_not_called()

    def test_teacher_edit_during_request_rejects_stale_machine_result(self):
        sid, before = self.prepare()
        qid = before['reviews'][0]['question_id']
        def reply(*args):
            response=self.post(f'/api/submissions/{sid}/reviews/{qid}', {'transcript':'teacher', 'confirmed':True})
            self.assertEqual(response.status_code,409,response.json)
            # Simulate a write already in flight before this job acquired its guards.
            with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
                conn.execute('UPDATE reviews SET transcript=?,confirmed=1,teacher_edited=1 WHERE submission_id=? AND question_id=?',('teacher',sid,qid))
            return [dict(number=1,status='readable',transcript='what')]
        runner=self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',side_effect=reply):
            runner.drain()
        q=self.client.get(f'/api/submissions/{sid}').json['reviews'][0]
        self.assertEqual(q['transcript'],'teacher')
        self.assertTrue(q['confirmed'])
        self.assertNotIn('ai_review',q['auto_result'])

    def test_restart_preserves_machine_identity_and_does_not_repeat_request(self):
        from app import create_app
        sid, _=self.prepare()
        runner=self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]) as model:
            runner.drain()
            restarted=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test'})
            q=restarted.test_client().get(f'/api/submissions/{sid}').json['reviews'][0]
            self.assertFalse(q['teacher_edited'])
            self.assertFalse(q['uncertain'])
            restarted.extensions['grading_jobs'].enqueue(submission_id=sid)
            restarted.extensions['grading_jobs'].drain()
            model.assert_called_once()

    def test_existing_assignment_review_only_never_repeats_ocr(self):
        sid,before=self.prepare()
        cid=self.post('/api/classes',{'name':'synthetic-review'}).json['id']
        self.post(f'/api/classes/{cid}/members',{'student_code':'001','name':'synthetic'})
        aid=self.post(f"/api/tasks/{before['task_id']}/assignments",{'class_ids':[cid]}).json['assignments'][0]['id']
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            ast=conn.execute('SELECT id FROM assignment_students WHERE assignment_id=?',(aid,)).fetchone()[0]
            conn.execute('UPDATE submissions SET assignment_student_id=? WHERE id=?',(ast,sid))
        route=f'/api/assignments/{aid}/ai-review-jobs'
        started=self.post(route,{})
        self.assertEqual(started.status_code,202,started.json)
        self.assertEqual(self.post(route,{}).json['job']['id'],started.json['job']['id'])
        with patch('settings.recognize_selected',side_effect=AssertionError('must not repeat OCR')),patch(
                'settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]) as model:
            self.app.extensions['grading_jobs'].drain()
            model.assert_called_once()
        self.assertFalse(self.client.get(f'/api/submissions/{sid}').json['reviews'][0]['uncertain'])

    def test_interrupted_paid_unit_is_not_retried_on_resume(self):
        sid,_=self.prepare()
        runner=self.app.extensions['grading_jobs']
        job=runner.enqueue(submission_id=sid)
        with patch.dict(self.app.extensions, automatic_ai_review=unittest.mock.Mock(side_effect=RuntimeError('interrupted'))):
            runner.drain()
            self.assertEqual(self.client.get(f"/api/grading-jobs/{job['id']}").json['job']['state'],'failed')
            response=self.post(f"/api/grading-jobs/{job['id']}/resume",{})
            self.assertEqual(response.status_code,202)
            runner.drain()
            self.app.extensions['automatic_ai_review'].assert_called_once()
        self.assertEqual(self.client.get(f"/api/grading-jobs/{job['id']}").json['job']['state'],'done')

    def test_unsafe_crop_uses_page_fallback_and_reports_suggestion(self):
        sid,before=self.prepare()
        qid=before['reviews'][0]['question_id']
        decision=before['reviews'][0]['auto_result']
        decision['regions'][0]['box']=[100,100,950,950]
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=? AND question_id=?',
                         (json.dumps(decision),sid,qid))
        runner=self.app.extensions['grading_jobs']
        job=runner.enqueue(submission_id=sid)
        with patch('settings.reread_handwriting') as model, patch('settings.reread_page',
                return_value=[dict(number=1,status='readable',transcript='why')]) as page_model:
            runner.drain()
            model.assert_not_called()
            page_model.assert_called_once()
        result=self.client.get(f"/api/grading-jobs/{job['id']}").json['job']
        self.assertIn('生成建议 1 题',result['message'])
        self.assertIn('自动解决 0 题',result['message'])
        self.assertTrue(self.client.get(f'/api/submissions/{sid}').json['reviews'][0]['uncertain'])
        retry=runner.enqueue(submission_id=sid)
        with patch('settings.reread_page') as page_model:
            runner.drain()
            page_model.assert_not_called()
        cached=self.client.get(f"/api/grading-jobs/{retry['id']}").json['job']
        self.assertIn('新生成建议 0 题',cached['message'])
        self.assertIn('复用已有建议 1 题',cached['message'])
        self.assertIn('本次仍待确认 1 题',cached['message'])
        self.assertNotIn('尝试复读',cached['message'])

    def test_historical_cached_summary_is_corrected_without_database_change(self):
        from grading_jobs import GradingJobs
        job=dict(id=1,assignment_id=1,submission_id=None,state='done',stage='ai_review',
            message='AI 复核结束：尝试复读 18 题，图片请求 0 次，有效返回 0 次，生成建议 18 题，自动解决 0 题。剩余异常请对照原图核对。',
            completed=1,total=1,errors='[]',created_at='now',updated_at='now',
            options=json.dumps(dict(ai_used=18,ai_resolved=0,ai_metrics=dict(requests=0,valid=0,suggestions=18))))
        original=dict(job)
        result=GradingJobs.dto(job)
        self.assertIn('新生成建议 0 题',result['message'])
        self.assertIn('复用已有建议 18 题',result['message'])
        self.assertIn('本次仍待确认 18 题',result['message'])
        self.assertTrue(result['message'].endswith('剩余异常请对照原图核对。'))
        self.assertEqual(job,original)

    def test_page_review_reads_missing_region_without_repeating_ocr_or_guessing_identity(self):
        sid,before=self.prepare()
        q=before['reviews'][0]
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=? AND question_id=?',
                         (json.dumps(dict(source='no_valid_answer',uncertain=True,processing_version=3,regions=[],transcript='')),sid,q['question_id']))
        pid=before['pages'][0]['id']
        blocks=[dict(id=0,text='(1) what Western societies are known for this.',box=[200,100,700,140],confidence=.95),
                dict(id=1,text='(2) to demand that special rights be given today.',box=[200,400,700,440],confidence=.95)]
        runner=self.app.extensions['grading_jobs'];runner.enqueue(submission_id=sid)
        with patch('ai_localization.offline_blocks',return_value={pid:blocks}) as localizer,patch(
                'settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]) as model,patch(
                'settings.recognize_selected',side_effect=AssertionError('must not rerun cloud OCR')):
            runner.drain()
            localizer.assert_not_called()
            model.assert_called_once()
        after=self.client.get(f'/api/submissions/{sid}').json
        self.assertTrue(after['reviews'][0]['uncertain'])
        self.assertFalse(after['reviews'][0]['teacher_edited'])
        self.assertEqual(after['visual_suggestions'][str(q['question_id'])]['transcript'],'what')
        self.assertEqual(after['reviews'][0]['auto_result']['source'],'no_valid_answer')
        self.assertEqual(after['pages'][0]['blocks'],before['pages'][0]['blocks'])

    def test_duplicate_page_question_numbers_never_auto_confirm(self):
        sid,before=self.prepare()
        q=before['reviews'][0]
        blocks=before['pages'][0]['blocks']
        blocks.append(dict(id=99,text='(1) another answer',box=[200,700,700,740],confidence=.95))
        self.seed(sid,before['pages'][0]['id'],blocks,'done')
        runner=self.app.extensions['grading_jobs'];runner.enqueue(submission_id=sid)
        with patch('settings.reread_page',return_value=[dict(number=1,status='readable',transcript='what')]) as model:
            runner.drain()
            model.assert_called_once()
        after=self.client.get(f'/api/submissions/{sid}').json
        self.assertTrue(after['reviews'][0]['uncertain'])
        self.assertNotIn('ai_review',after['reviews'][0]['auto_result'])
        self.assertEqual(after['visual_suggestions'][str(q['question_id'])]['transcript'],'what')


class AIJobPermissionTests(unittest.TestCase):
    setUp=test_tenant_isolation.TenantIsolationTests.setUp
    token=test_tenant_isolation.TenantIsolationTests.token
    register=test_tenant_isolation.TenantIsolationTests.register
    post=test_tenant_isolation.TenantIsolationTests.post

    def test_page_endpoint_rejects_foreign_submission_and_csrf(self):
        tid=self.post(self.a,'/api/tasks',dict(name='synthetic',questions=[dict(number=1,kind='word',answer='book',points=1)])).json['id']
        sid=self.post(self.a,f'/api/tasks/{tid}/submissions',dict(student_code='001')).json['id']
        for client in (self.a,self.b):
            client.patch('/api/settings/ai-review',json=dict(enabled=True,api_key='test',model='test',base_url='https://vision.example.test'),headers={'X-CSRF-Token':self.token(client)})
        route=f'/api/submissions/{sid}/visual-reread-page'
        with patch('settings.reread_page') as model:
            self.assertEqual(self.post(self.b,route,dict(page_id=1)).status_code,404)
            self.assertEqual(self.a.post(route,json=dict(page_id=1)).status_code,403)
            model.assert_not_called()

    def test_assignment_ownership_archive_and_duplicate_job(self):
        cid=self.post(self.a,'/api/classes',{'name':'synthetic'}).json['id']
        tid=self.post(self.a,'/api/tasks',{'name':'synthetic','questions':[
            dict(number=1,kind='word',answer='book',points=1)]}).json['id']
        aid=self.post(self.a,f'/api/tasks/{tid}/assignments',{'class_ids':[cid]}).json['assignments'][0]['id']
        route=f'/api/assignments/{aid}/ai-review-jobs'
        self.assertEqual(self.post(self.b,route,{}).status_code,404)
        self.assertEqual(self.post(self.a,'/api/assignments/999999/ai-review-jobs',{}).status_code,404)
        self.assertEqual(self.post(self.a,route,{}).status_code,409)
        self.a.patch('/api/settings/ai-review',json=dict(enabled=True,api_key='test',model='test',base_url='https://vision.example.test'),headers={'X-CSRF-Token':self.token(self.a)})
        first=self.post(self.a,route,{})
        self.assertEqual(first.status_code,202,first.json)
        self.assertEqual(first.json['job']['stage'],'ai_review')
        self.assertEqual(self.post(self.a,route,{}).json['job']['id'],first.json['job']['id'])
        self.assertEqual(self.b.get('/api/grading-jobs/'+str(first.json['job']['id'])).status_code,404)
        self.app.extensions['grading_jobs'].drain()
        with closing(sqlite3.connect(Path(self.data_dir)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE classes SET archived=1 WHERE id=?',(cid,))
        self.assertEqual(self.post(self.b,route,{}).status_code,404)
        self.assertEqual(self.post(self.a,route,{}).status_code,409)


class AIProductionTests(unittest.TestCase):
    import test_production as fixtures
    setUp=fixtures.ProductionTests.setUp
    app=fixtures.ProductionTests.app
    token=fixtures.ProductionTests.token
    signup=fixtures.ProductionTests.signup

    def test_platform_credentials_remain_inaccessible_to_teacher(self):
        app=self.app()
        client=app.test_client()
        self.assertEqual(self.signup(client,'synthetic-ai@example.test').status_code,303)
        token=self.token(client,'/')
        app.config['VISUAL_REVIEW_CONFIG']=dict(api_key='platform-test-secret',model='test',base_url='https://vision.example.test')
        for method,path in [('get','/api/settings/ai-review'),('patch','/api/settings/ai-review'),('post','/api/settings/ai-review/test')]:
            res=getattr(client,method)(path,base_url='https://school.example.test',json={},
                headers={'X-CSRF-Token':token,'Origin':'https://school.example.test'})
            self.assertEqual(res.status_code,403)
            self.assertNotIn('platform-test-secret',res.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
