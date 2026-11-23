"""AI review uses synthetic crops, isolated SQLite and mocked providers only."""
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_ocr_alignment
import test_tenant_isolation


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
        with patch('settings.reread_handwriting', side_effect=ValueError(config['api_key'])):
            failed=self.a.post(route+'/test', json={}, headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(failed.status_code,400)
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
        from settings import reread_page
        config = dict(api_key='secret-test', model='test', base_url='https://vision.example.test/v1')
        item = dict(number=1, status='readable', transcript='why')
        with patch('settings._chat', return_value=json.dumps([item,item])):
            with self.assertRaises(ValueError):
                reread_page(config, b'png', [1])
        sid,before=self.prepare()
        with patch('settings.reread_page',side_effect=ValueError('secret-test')):
            response=self.post(f'/api/submissions/{sid}/visual-reread-page',dict(page_id=before['pages'][0]['id']))
        self.assertEqual(response.status_code,502)
        self.assertNotIn('secret-test',response.get_data(as_text=True))

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
        with patch('settings.reread_handwriting', return_value=dict(status='readable', transcript='what')) as model:
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
            kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with patch('settings.reread_handwriting', **kwargs):
                runner.drain()
            after = self.client.get(f'/api/submissions/{sid}').json
            self.assertTrue(after['reviews'][0]['uncertain'])
            self.assertNotIn('test-key', json.dumps(after))
        q = before['reviews'][0]
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}", {'transcript': 'teacher', 'confirmed': True})
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_handwriting') as model:
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
            return dict(status='readable',transcript='what')
        runner=self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_handwriting',side_effect=reply):
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
        with patch('settings.reread_handwriting',return_value=dict(status='readable',transcript='what')) as model:
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
                'settings.reread_handwriting',return_value=dict(status='readable',transcript='what')) as model:
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
        runner.enqueue(submission_id=sid)
        with patch('settings.reread_page') as page_model:
            runner.drain()
            page_model.assert_not_called()

    def test_offline_localization_recovers_missing_region_without_regrading_original_ocr(self):
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
                'settings.reread_handwriting',return_value=dict(status='readable',transcript='what')) as model,patch(
                'settings.recognize_selected',side_effect=AssertionError('must not rerun cloud OCR')):
            runner.drain()
            localizer.assert_called_once()
            model.assert_called_once()
        after=self.client.get(f'/api/submissions/{sid}').json
        self.assertFalse(after['reviews'][0]['uncertain'])
        self.assertFalse(after['reviews'][0]['teacher_edited'])
        self.assertEqual(after['reviews'][0]['score'],0)
        self.assertEqual(after['reviews'][0]['auto_result']['source'],'localization_ocr')
        self.assertEqual(after['pages'][0]['blocks'],before['pages'][0]['blocks'])

    def test_duplicate_local_question_numbers_never_trigger_ai(self):
        sid,before=self.prepare()
        q=before['reviews'][0]
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=? AND question_id=?',
                         (json.dumps(dict(source='no_valid_answer',uncertain=True,processing_version=3,regions=[],transcript='')),sid,q['question_id']))
        block=dict(id=0,text='(1) what Western societies are known for this.',box=[200,100,700,140],confidence=.95)
        runner=self.app.extensions['grading_jobs'];runner.enqueue(submission_id=sid)
        with patch('ai_localization.offline_blocks',return_value={before['pages'][0]['id']:[block,dict(block,id=1)]}),patch('settings.reread_handwriting') as model:
            runner.drain()
            model.assert_not_called()


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
