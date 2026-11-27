"""Automatic grading acceptance tests; isolated SQLite and mocked OCR only."""
import csv
import io
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
import test_app
import test_batch


class AutoGradeTests(unittest.TestCase):
    setUpClass = test_app.WebsiteTests.__dict__['setUpClass']
    setUp = test_app.WebsiteTests.setUp
    post = test_app.WebsiteTests.post
    create_task = test_app.WebsiteTests.create_task
    submission = test_app.WebsiteTests.submission
    image = test_app.WebsiteTests.image

    def blocks(self, *texts):
        return [{'id':i,'text':t,'box':[0,i*45,600,i*45+40],'confidence':None}
                for i,t in enumerate(texts)]

    def test_ungraded_reviews_have_no_machine_decision(self):
        task=self.create_task();sid=self.submission(task)
        result=self.client.get(f'/api/submissions/{sid}').json
        self.assertFalse(result['is_final'])
        self.assertIsNone(result['reviews'][0]['auto_result'])
        self.assertFalse(result['reviews'][0]['graded'])

    def test_cloud_selection_refreshes_local_cache_but_preserves_teacher(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        self.seed(sid,page['id'],self.blocks('1. recieve','2. I went home.'))
        before=self.grade(sid)
        q=before['reviews'][1]
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':'Teacher decision','confirmed':True})
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                (page['id'],'now',json.dumps({'model':'PP-OCRv5_server_det + en_PP-OCRv5_mobile_rec','blocks':self.blocks('1. recieve','2. I went home.')})))
        changed=self.client.patch('/api/settings',json={'provider':'paddle_cloud','paddle_token':'private-test-token'},headers=self.headers)
        self.assertEqual(changed.status_code,200)
        recognized={'provider':'paddle_cloud','model':'PaddleOCR-VL-1.6','blocks':self.blocks('1. receive','2. I went home.')}
        with patch('settings.recognize_selected',return_value=recognized) as cloud:
            result=self.grade(sid)
            self.assertEqual(cloud.call_count,1)
            self.assertEqual(result['reviews'][0]['transcript'],'receive')
            self.assertEqual(result['reviews'][1]['transcript'],'Teacher decision')
            self.assertTrue(result['reviews'][1]['confirmed'])
            self.assertEqual(self.grade(sid)['history_count'],result['history_count'])
            self.assertEqual(cloud.call_count,1,'Current cloud results must be reused without another paid call')

    def grade(self, sid):
        response = self.post(f'/api/submissions/{sid}/auto-grade', {})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json

    def seed(self, sid, pid, blocks, status='uploaded'):
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn, conn:
            conn.execute('UPDATE pages SET blocks=?,status=? WHERE id=?', (json.dumps(blocks),status,pid))

    def test_real_inline_fixture_final_four_without_teacher_or_repeat_calls(self):
        path = Path(__file__).parent/'tests/fixtures/grammar_inline_ocr.json'
        if not path.is_file():
            self.skipTest('真实 OCR 样本仅保存在本机')
        fixture = json.loads(path.read_text('utf-8'))
        answers = ['when','with','is favored','why','have adopted','to demand','making','until','incorporated','What']
        task = self.post('/api/tasks', {'name':'自动语法', 'questions':[
            dict(q,answer=a,points=1) for q,a in zip(fixture['questions'],answers)]}).json
        sid = self.submission(task)
        from PIL import Image
        buf=io.BytesIO(); Image.new('RGB',(1100,1500),'white').save(buf,'PNG')
        page=self.image(sid,content=buf.getvalue()).json
        self.seed(sid,page['id'],fixture['blocks'])
        with patch('settings.recognize_selected') as cloud:
            result=self.grade(sid)
            self.assertEqual(result['score'],4)
            self.assertTrue(result['is_final'])
            self.assertEqual(result['teacher_reviewed_count'],0)
            self.assertEqual(result['machine_graded_count'],10)
            self.assertFalse(any(q['confirmed'] for q in result['reviews']))
            self.assertEqual(result['reviews'][8]['transcript'],'uncorporated')
            self.assertEqual(result['reviews'][9]['transcript'],'what')
            self.assertEqual(self.grade(sid)['score'],4)
            cloud.assert_not_called()
        restarted=self.factory({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test-token'}).test_client()
        self.assertTrue(restarted.get(f'/api/submissions/{sid}').json['is_final'])
        self.assertIn('机器自动批改',self.client.get(f'/submissions/{sid}/report').get_data(as_text=True))
        rows=list(csv.reader(io.StringIO(self.client.get(f"/tasks/{task['id']}/grades.csv").get_data(as_text=True).lstrip('\ufeff'))))
        self.assertIn('最终成绩',rows[1]); self.assertIn('机器自动批改',rows[1])

    def test_supplement_conflict_missing_and_teacher_override_survive_new_page(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        first=self.blocks('1. recieve')
        second=self.blocks('1. receive','2. I went home.')
        self.seed(sid,page['id'],first)
        with patch('settings.recognize_selected',return_value={'blocks':second}) as cloud:
            result=self.grade(sid); self.assertEqual(cloud.call_count,1)
        self.assertIsNone(result['score']);self.assertEqual(result['uncertain_count'],1)
        self.assertEqual(result['reviews'][0]['machine_text'],'recieve')
        self.assertEqual(result['reviews'][0]['transcript'],'receive')
        q=result['reviews'][0]
        edited=self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{
            'transcript':'recieve','confirmed':False,'regions':q['regions'], 'score_override':1,'reason':'教师给部分分'}).json
        self.assertTrue(edited['is_final']);self.assertEqual(edited['score'],4)
        self.assertTrue(edited['reviews'][0]['confirmed'])
        page2=self.image(sid).json
        self.assertFalse(self.client.get(f'/api/submissions/{sid}').json['is_final'])
        self.seed(sid,page2['id'],self.blocks('无关文字'))
        with patch('settings.recognize_selected',return_value={'blocks':second}):
            after=self.grade(sid)
        self.assertEqual(after['reviews'][0]['transcript'],'recieve')
        self.assertEqual(after['reviews'][0]['score'],1)
        self.assertGreater(after['history_count'],result['history_count'])

    def test_success_with_no_answers_stays_pending_and_no_fake_blank(self):
        task=self.create_task();sid=self.submission(task);self.image(sid)
        with patch('settings.recognize_selected',return_value={'blocks':[]}) as cloud:
            result=self.grade(sid);self.assertEqual(cloud.call_count,2)
            self.grade(sid);self.assertEqual(cloud.call_count,2)
        self.assertFalse(result['is_final']);self.assertIsNone(result['score'])
        self.assertEqual(result['uncertain_count'],2)
        self.assertFalse(any(q['confirmed_blank'] for q in result['reviews']))

    def test_service_failure_retry_retains_image_history_and_hides_secret(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        with patch('settings.recognize_selected',side_effect=RuntimeError('secret-token-do-not-log')):
            with self.assertLogs(self.app.logger,level='WARNING') as captured:
                response=self.post(f'/api/submissions/{sid}/auto-grade',{})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('secret-token',response.get_data(as_text=True)+' '.join(captured.output))
        detail=self.client.get(f'/api/submissions/{sid}').json
        self.assertFalse(detail['is_final']);self.assertEqual(detail['grading_state'],'failed')
        with self.client.get(page['url']) as original:
            self.assertEqual(original.status_code,200)
        with patch('settings.recognize_selected',return_value={'blocks':self.blocks('1. receive','2. I went home.')}):
            self.assertEqual(self.grade(sid)['score'],5)

    def test_duplicate_running_and_restart_are_retryable(self):
        task=self.create_task();sid=self.submission(task);self.image(sid)
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute("UPDATE submissions SET grading_state='running' WHERE id=?",(sid,))
        with patch('settings.recognize_selected') as cloud:
            self.assertEqual(self.post(f'/api/submissions/{sid}/auto-grade',{}).status_code,409)
            cloud.assert_not_called()
        restarted=self.factory({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test-token'}).test_client()
        self.assertEqual(restarted.get(f'/api/submissions/{sid}').json['grading_state'],'failed')

    def test_unreadable_candidate_is_not_a_confident_wrong_answer(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        self.seed(sid,page['id'],self.blocks('1. [无法辨认]','2. I went home.'))
        with patch('settings.recognize_selected',return_value={'blocks':self.blocks('1. [无法辨认]','2. I went home.')}) as cloud:
            result=self.grade(sid)
            self.assertEqual(cloud.call_count,1)
        self.assertFalse(result['is_final']);self.assertTrue(result['reviews'][0]['uncertain'])
        self.assertEqual(result['reviews'][0]['auto_result']['source'],'no_valid_answer')
        self.assertIsNone(result['score'])
        self.assertEqual(result['known_score'],3)

    def test_missing_second_pass_clears_stale_regions_keeps_first_literal_reading(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        self.seed(sid,page['id'],self.blocks('1. recieve'))
        with patch('settings.recognize_selected',return_value={'blocks':self.blocks('没有可关联题号')}):
            result=self.grade(sid)
        q=result['reviews'][0]
        self.assertEqual(q['transcript'],'recieve');self.assertEqual(q['regions'],[])
        self.assertTrue(q['uncertain']);self.assertIn('保留',q['auto_result']['reason'])

    def test_partial_failure_retry_reuses_successful_page_and_preserves_human_draft(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json;self.image(sid)
        qid=task['questions'][0]['id']
        self.post(f'/api/submissions/{sid}/reviews/{qid}',{'transcript':'teacher draft','confirmed':False})
        with patch('settings.recognize_selected',side_effect=[{'blocks':self.blocks('1. receive')},RuntimeError('timeout')]):
            with self.assertLogs(self.app.logger,level='WARNING'):
                self.assertEqual(self.post(f'/api/submissions/{sid}/auto-grade',{}).status_code,503)
        with patch('settings.recognize_selected',return_value={'blocks':self.blocks('2. I went home.')}) as cloud:
            result=self.grade(sid);self.assertEqual(cloud.call_count,1)
        self.assertTrue(result['is_final']);self.assertEqual(result['reviews'][0]['transcript'],'teacher draft')
        self.assertEqual(result['score'],3);self.assertEqual(result['teacher_reviewed_count'],0)

    def test_explicit_rerun_preserves_teacher_and_rejects_unconfirmed_empty_edit(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        self.seed(sid,page['id'],self.blocks('1. receive','2. I went home.'))
        result=self.grade(sid);q=result['reviews'][0]
        self.assertEqual(self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':''}).status_code,400)
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':'teacher edit','confirmed':False,'regions':q['regions']})
        with patch('settings.recognize_selected',return_value={'blocks':self.blocks('1. receive','2. I went home.')}):
            rerun=self.post(f'/api/submissions/{sid}/auto-grade',{'rerun':True}).json
        self.assertTrue(rerun['is_final']);self.assertEqual(rerun['score'],3)
        self.assertEqual(rerun['reviews'][0]['transcript'],'teacher edit')
        self.assertTrue(rerun['reviews'][0]['confirmed'])
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':'teacher edit','regions':rerun['reviews'][0]['regions']})
        with patch('app.recognize',return_value={'blocks':self.blocks('1. receive','2. I went home.')}):
            refreshed=self.post(f"/api/pages/{page['id']}/ocr",{}).json
        self.assertTrue(refreshed['reviews'][0]['confirmed'])

    def test_supplement_targets_page_with_visible_unresolved_number(self):
        task=self.create_task();sid=self.submission(task);page1=self.image(sid).json;page2=self.image(sid).json
        self.seed(sid,page1['id'],self.blocks('1. recieve'))
        self.seed(sid,page2['id'],self.blocks('2. [无法辨认]'))
        with patch('settings.recognize_selected',return_value={'blocks':self.blocks('2. I went home.')}) as cloud:
            result=self.grade(sid)
        self.assertEqual(cloud.call_count,1)
        self.assertEqual(result['score'],3)

    def test_rerun_empty_cannot_reuse_old_final_answers_as_current(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        self.seed(sid,page['id'],self.blocks('1. receive','2. I went home.'))
        self.assertEqual(self.grade(sid)['score'],5)
        with patch('settings.recognize_selected',return_value={'blocks':[]}):
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'rerun':True}).json
        self.assertFalse(result['is_final']);self.assertIsNone(result['score'])
        self.assertEqual(result['uncertain_count'],2)
        self.assertEqual(result['reviews'][0]['transcript'],'')

    def test_answer_sheet_placeholder_needs_supplement(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        blocks=self.blocks('1. ______','2. I went home.')
        self.seed(sid,page['id'],blocks)
        with patch('settings.recognize_selected',return_value={'blocks':blocks}) as cloud:
            result=self.grade(sid)
        self.assertEqual(cloud.call_count,1);self.assertTrue(result['reviews'][0]['uncertain'])

    def test_direct_ocr_clears_machine_regions_not_teacher_confirmation(self):
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        blocks=self.blocks('1. receive','2. I went home.')
        self.seed(sid,page['id'],blocks)
        q=self.grade(sid)['reviews'][0]
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':'teacher edit','regions':q['regions']})
        with patch('app.recognize',return_value={'blocks':blocks}):
            result=self.post(f"/api/pages/{page['id']}/ocr",{}).json
        self.assertTrue(result['reviews'][0]['confirmed'])
        self.assertEqual(result['reviews'][0]['regions'],[])


class AutoIdentityTests(unittest.TestCase):
    setUp = test_batch.BatchTests.setUp
    post = test_batch.BatchTests.post
    patch = test_batch.BatchTests.patch
    klass = test_batch.BatchTests.klass
    task = test_batch.BatchTests.task
    setup_assignment = test_batch.BatchTests.setup_assignment
    upload_batch = test_batch.BatchTests.upload_batch

    def test_unique_links_automatically_and_exports_same_final_score(self):
        aid,students=self.setup_assignment();photo=self.upload_batch(aid)
        blocks=[{'id':0,'text':'学号：001 姓名：张同学','box':[0,0,400,40]},
                {'id':1,'text':'1. receive','box':[0,60,400,100]}]
        with patch('batch_photos.recognize',return_value={'blocks':blocks}):
            identified=self.post(f"/api/batch-photos/{photo['id']}/identify",{}).json
        self.assertEqual(identified['status'],'confirmed')
        self.assertEqual(identified['identity_source'],'machine')
        sid=identified['submission_id']
        with patch('settings.recognize_selected') as cloud:
            graded=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
            cloud.assert_not_called()
        self.assertTrue(graded['is_final'])
        assignment=self.client.get(f'/api/assignments/{aid}').json
        self.assertEqual(assignment['final_average'],2)
        self.assertEqual(assignment['counts']['completed'],1)
        rows=list(load_workbook(io.BytesIO(self.client.get(f'/assignments/{aid}/grades.xlsx').data)).active.values)
        self.assertEqual(rows[1][7],2);self.assertIn('机器自动批改',rows[1])
        payload={'assignment_student_id':students[0]['id'],'confirmed':True}
        self.assertEqual(self.post(f"/api/batch-photos/{photo['id']}/confirm",payload).json['page_id'],identified['page_id'])

    def test_conflict_and_ambiguous_stay_unassigned(self):
        aid,students=self.setup_assignment()
        for value in ['姓名：李同学','学号：001 姓名：李同学','无身份']:
            photo=self.upload_batch(aid)
            with patch('batch_photos.recognize',return_value={'blocks':[{'id':0,'text':value,'box':[0,0,400,40]}]}):
                result=self.post(f"/api/batch-photos/{photo['id']}/identify",{}).json
            self.assertIsNone(result['submission_id'])


if __name__ == '__main__':
    unittest.main()
