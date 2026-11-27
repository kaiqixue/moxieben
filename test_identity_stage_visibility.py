import unittest
import io
from unittest.mock import patch
import test_grading_jobs as fixtures
from teacher_workbench import processing_identity_photo_ids


class IdentityStageVisibilityTests(unittest.TestCase):
    def test_name_auto_link_and_continuation_job_keep_teacher_ownership(self):
        import json
        import sqlite3
        import test_tenant_isolation as tenant
        from pathlib import Path
        from PIL import Image
        fixture=tenant.TenantIsolationTests('test_class_and_task_are_visible_only_to_owner')
        fixture.setUp()
        try:
            cid=fixture.post(fixture.a,'/api/classes',{'name':'隔离归属班'}).json['id']
            fixture.post(fixture.a,f'/api/classes/{cid}/members',{'name':'张同学','student_code':'001'})
            tid=fixture.post(fixture.a,'/api/tasks',{'name':'姓名归属验收','questions':[{'number':1,'kind':'word','answer':'book','points':1}]}).json['id']
            aid=fixture.post(fixture.a,f'/api/tasks/{tid}/assignments',{'class_ids':[cid]}).json['assignments'][0]['id']
            image=io.BytesIO();Image.new('RGB',(640,240),'white').save(image,'PNG');image.seek(0)
            pid=fixture.a.post(f'/api/assignments/{aid}/photos',data={'image':(image,'synthetic.png')},
                               headers={'X-CSRF-Token':fixture.token(fixture.a)}).json['id']
            config=dict(enabled=False,base_url='https://fake.invalid',model='synthetic',api_key='synthetic')
            self.assertEqual(fixture.a.patch('/api/settings/ai-review',json=config,headers={'X-CSRF-Token':fixture.token(fixture.a)}).status_code,200)
            with patch('settings.recognize_selected',return_value={'blocks':[{'id':0,'text':'未知身份','box':[0,0,600,40]},{'id':1,'text':'1. book','box':[0,50,600,90]}]}):
                self.assertEqual(fixture.post(fixture.a,f'/api/batch-photos/{pid}/identify',{'defer_link':True}).status_code,200)
            with patch('settings._chat',return_value=json.dumps(dict(name='张同学',student_code='',class_name='',confidence='high'))) as model:
                self.assertEqual(fixture.post(fixture.b,f'/api/batch-photos/{pid}/identity-review',{}).status_code,404)
                model.assert_not_called()
                response=fixture.post(fixture.a,f'/api/batch-photos/{pid}/identity-review',{})
            self.assertEqual(response.status_code,200,response.json)
            sid=response.json['submission_id'];self.assertIsNotNone(sid)
            with sqlite3.connect(Path(fixture.data_dir)/'dictation.sqlite3') as conn:
                owner=conn.execute('SELECT owner_id FROM assignments WHERE id=?',(aid,)).fetchone()[0]
                self.assertEqual(conn.execute('SELECT owner_id FROM submissions WHERE id=?',(sid,)).fetchone()[0],owner)
                job=conn.execute('SELECT id,owner_id FROM grading_jobs WHERE submission_id=?',(sid,)).fetchone()
                self.assertEqual(job[1],owner)
            self.assertEqual(fixture.b.get(f'/api/submissions/{sid}').status_code,404)
            self.assertEqual(fixture.b.get(f'/api/grading-jobs/{job[0]}').status_code,404)
            self.assertEqual(fixture.b.get(f'/api/batch-photos/{pid}/image').status_code,404)
            with patch('settings.recognize_selected') as ocr,patch('settings._chat') as model:
                fixture.app.extensions['grading_jobs'].drain()
            ocr.assert_not_called();model.assert_not_called()
            with sqlite3.connect(Path(fixture.data_dir)/'dictation.sqlite3') as conn:
                self.assertEqual(conn.execute('SELECT grading_state FROM submissions WHERE id=?',(sid,)).fetchone()[0],'final')
        finally:
            fixture.doCleanups()

    def test_identity_endpoints_keep_teacher_csrf_and_origin_isolation(self):
        import test_tenant_isolation as tenant
        from PIL import Image
        fixture=tenant.TenantIsolationTests('test_class_and_task_are_visible_only_to_owner')
        fixture.setUp()
        try:
            klass=fixture.post(fixture.a,'/api/classes',{'name':'虚构班'}).json['id']
            fixture.post(fixture.a,f'/api/classes/{klass}/members',{'name':'虚构学生','student_code':'001'})
            task=fixture.post(fixture.a,'/api/tasks',{'name':'虚构作业','questions':[{'number':1,'kind':'word','answer':'book','points':1}]}).json['id']
            aid=fixture.post(fixture.a,f'/api/tasks/{task}/assignments',{'class_ids':[klass]}).json['assignments'][0]['id']
            image=io.BytesIO();Image.new('RGB',(40,40),'white').save(image,'PNG');image.seek(0)
            response=fixture.a.post(f'/api/assignments/{aid}/photos',data={'image':(image,'synthetic.png')},headers={'X-CSRF-Token':fixture.token(fixture.a)})
            pid=response.json['id']
            with patch('settings._chat') as model:
                for path in (f'/api/batch-photos/{pid}/identity-review',f'/api/batch-photos/{pid}/identity-review/retry',f'/api/assignments/{aid}/identity-review-jobs'):
                    self.assertEqual(fixture.app.test_client().post(path,json={}).status_code,401)
                    self.assertEqual(fixture.post(fixture.b,path,{}).status_code,404)
                    self.assertEqual(fixture.a.post(path,json={}).status_code,403)
                    self.assertEqual(fixture.a.post(path,json={},headers={'X-CSRF-Token':fixture.token(fixture.a),'Origin':'https://evil.example.test'}).status_code,403)
                self.assertEqual(fixture.b.get(f'/api/batch-photos/{pid}/identity-review/image').status_code,404)
                self.assertEqual(fixture.a.get(f'/api/batch-photos/{pid}/identity-review/image').status_code,200)
                model.assert_not_called()
        finally:
            fixture.doCleanups()

    def test_explicit_batch_queues_existing_photos_without_enabling_answer_review(self):
        fixture = fixtures.GradingJobTests('test_snapshot_and_active_start_are_idempotent_and_complete_without_browser')
        fixture.setUp()
        try:
            photo = fixture.photo()
            with fixture.db() as conn:
                conn.execute("UPDATE batch_photos SET status='identified' WHERE id=?", (photo,))
            fixture.app.extensions['read_ai_review'] = lambda: dict(enabled=False, base_url='https://fake.test', model='fake', api_key='fake')
            response = fixture.post(f'/api/assignments/{fixture.aid}/identity-review-jobs', {})
            self.assertEqual(response.status_code, 202, response.json)
            self.assertEqual(response.json['job']['stage'], 'matching')
            self.assertEqual(fixture.start()['id'], response.json['job']['id'])
        finally:
            fixture.doCleanups()

    def test_identity_review_remains_owned_by_background_job(self):
        fixture = fixtures.GradingJobTests('test_snapshot_and_active_start_are_idempotent_and_complete_without_browser')
        fixture.setUp()
        try:
            photo = fixture.photo()
            job = fixture.start()
            with fixture.db() as conn:
                conn.execute("UPDATE grading_jobs SET state='running',stage='identity_review' WHERE id=?", (job['id'],))
                self.assertIn(photo, processing_identity_photo_ids(conn, [fixture.aid]))
        finally:
            fixture.doCleanups()
