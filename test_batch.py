"""Batch photos stay unassigned until a teacher confirms the identity."""
import io
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from PIL import Image
import test_classes


class BatchTests(test_classes.ClassTests):
    # Reuse the real SQLite helpers, without re-running ClassTests in this suite.
    def setup_assignment(self):
        cid = self.klass()
        for code, name in [('001','张同学'),('002','李同学'),('003','李同学')]:
            self.post(f'/api/classes/{cid}/members', {'student_code':code,'name':name})
        tid = self.task()
        aid = self.post(f'/api/tasks/{tid}/assignments', {'class_ids':[cid]}).json['assignments'][0]['id']
        students = self.client.get(f'/api/assignments/{aid}').json['students']
        return aid, students

    def upload_batch(self, aid, color="white"):
        buf = io.BytesIO()
        Image.new('RGB',(640,240),color).save(buf,'PNG')
        response = self.client.post(f'/api/assignments/{aid}/photos', data={
            'image':(io.BytesIO(buf.getvalue()),'photo.png')},headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code,201,response.json)
        return response.json

    def test_upload_receipt_survives_response_loss_and_replay(self):
        aid, students = self.setup_assignment()
        existing = self.upload_batch(aid, 'black')
        linked = self.post(f"/api/batch-photos/{existing['id']}/confirm", {
            'assignment_student_id': students[0]['id'], 'confirmed': True}).json
        path = f'/api/assignments/{aid}/photos'
        sid = linked['submission_id']
        before = self.client.get(f'/api/submissions/{sid}').json
        buf = io.BytesIO()
        Image.new('RGB', (640, 240), 'white').save(buf, 'PNG')
        upload_id = '1' * 32

        def send(token, layout='inline'):
            return self.client.post(path, data={
                'image': (io.BytesIO(buf.getvalue()), 'receipt.png'),
                'upload_id': token, 'answer_layout': layout,
            }, headers=self.headers)

        first = send(upload_id)
        self.assertEqual(first.status_code, 201)
        archived = self.client.get(path + '?summary=1').json['photos']
        receipt = next(p for p in archived if p.get('upload_id') == upload_id)
        self.assertEqual(receipt['id'], first.json['id'])
        replay = send(upload_id)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json['id'], receipt['id'])
        self.assertEqual(send(upload_id, 'separate').status_code, 409)
        self.assertEqual(send('invalid').status_code, 400)
        self.assertEqual(len(self.client.get(path).json['photos']), 2)
        self.assertEqual(len(list((Path(self.tmp.name) / 'uploads').iterdir())), 4)
        after = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(after, before)


    def test_excel_export_preserves_codes_missing_scores_and_literal_names(self):
        aid, students = self.setup_assignment()
        response = self.client.get(f'/assignments/{aid}/grades.xlsx')
        self.assertEqual(response.status_code,200)
        book = load_workbook(io.BytesIO(response.data))
        rows = list(book.active.values)
        self.assertEqual(rows[1][2],'001')
        self.assertIsNone(rows[1][7])
        self.assertEqual(rows[1][4],'未交')
        self.assertEqual(book.active['C2'].data_type,'s')
        self.patch(f"/api/assignment-students/{students[1]['id']}", {'exempt':True,'reason':'请假'})
        book = load_workbook(io.BytesIO(self.client.get(f'/assignments/{aid}/grades.xlsx').data))
        self.assertEqual(book.active['E3'].value,'免交')
        self.assertIsNone(book.active['H3'].value)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.tmp.name+'/dictation.sqlite3')) as conn, conn:
            conn.execute('UPDATE assignment_students SET name=? WHERE id=?', ('=1+1',students[0]['id']))
        book = load_workbook(io.BytesIO(self.client.get(f'/assignments/{aid}/grades.xlsx').data))
        self.assertEqual(book.active['D2'].value,'=1+1')
        self.assertEqual(book.active['D2'].data_type,'s')

    def test_batch_identity_confirmation_idempotence_and_existing_review_preserved(self):
        aid, students = self.setup_assignment()
        photo = self.upload_batch(aid)
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['submitted'],0)
        blocks=[{'id':0,'text':'学号：001 姓名：张同学','confidence':.96,'box':[0,0,400,50]}]
        with patch('batch_photos.recognize',return_value={'blocks':blocks,'model':'test-only'}):
            result=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['suggested_student_id'],students[0]['id'])
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['submitted'],1)
        self.assertEqual(result.json['identity_source'],'machine')
        path=f"/api/batch-photos/{photo['id']}/confirm"
        payload={'assignment_student_id':students[0]['id'],'confirmed':True}
        self.assertEqual(self.post(path,{'assignment_student_id':students[0]['id']}).status_code,400)
        linked=self.post(path,payload)
        self.assertEqual(linked.status_code,200,linked.json)
        sid=linked.json['submission_id']
        self.assertEqual(self.post(path,payload).json['page_id'],linked.json['page_id'])
        self.assertEqual(self.post(path,{'assignment_student_id':students[1]['id'],'confirmed':True}).status_code,409)
        sub=self.client.get(f'/api/submissions/{sid}').json
        qid=sub['reviews'][0]['question_id']
        self.post(f'/api/submissions/{sid}/reviews/{qid}',{'transcript':'receive','confirmed':True,'regions':[]})
        photo2=self.upload_batch(aid, color='black')
        linked2=self.post(f"/api/batch-photos/{photo2['id']}/confirm",dict(payload,append_page=True))
        self.assertEqual(linked2.json['submission_id'],sid)
        sub=self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(len(sub['pages']),2)
        self.assertEqual(sub['reviews'][0]['transcript'],'receive')
        self.assertTrue(sub['reviews'][0]['confirmed'])
        self.assertEqual(sub['status'],'pending')
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['submitted'],1)

    def test_extract_reuses_existing_identity_ocr_without_another_job(self):
        import sqlite3
        from contextlib import closing
        aid, students = self.setup_assignment()
        photo = self.upload_batch(aid)
        blocks = [{'id':0,'text':'学号：001 姓名：张同学','box':[0,0,400,40]},
                  {'id':1,'text':'1. recieve','box':[0,60,400,100]}]
        with patch('batch_photos.recognize',return_value={'blocks':blocks}):
            self.post(f"/api/batch-photos/{photo['id']}/identify",{'defer_link': True})
        # Older photos only retained blocks; migration cannot invent missing raw metadata.
        with closing(sqlite3.connect(self.tmp.name + '/dictation.sqlite3')) as conn, conn:
            conn.execute("UPDATE batch_photos SET ocr_result='{}' WHERE id=?", (photo['id'],))
        linked = self.post(f"/api/batch-photos/{photo['id']}/confirm",{'assignment_student_id':students[0]['id'],'confirmed':True}).json
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f"/api/submissions/{linked['submission_id']}/extract-answers",{})
            cloud.assert_not_called()
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['extracted_count'],1)
        self.assertEqual(result.json['pages'][0]['status'],'done')
        self.assertFalse(result.json['pages'][0]['needs_review'])
        self.assertEqual(result.json['reviews'][0]['transcript'],'recieve')
        self.assertFalse(result.json['reviews'][0]['confirmed'])

    def test_photo_listing_includes_submission_grading_summary(self):
        aid, students = self.setup_assignment()
        photo = self.upload_batch(aid)
        blocks = [
            {'id': 0, 'text': '学号：001 姓名：张同学', 'box': [0, 0, 400, 40]},
            {'id': 1, 'text': '1. receive', 'box': [0, 60, 400, 100]},
        ]
        with patch('batch_photos.recognize', return_value={'blocks': blocks}):
            self.post(f"/api/batch-photos/{photo['id']}/identify", {'defer_link': True})
        linked = self.post(f"/api/batch-photos/{photo['id']}/confirm", {
            'assignment_student_id': students[0]['id'], 'confirmed': True,
        })
        self.assertEqual(linked.status_code, 200, linked.json)
        self.assertEqual(self.post(f"/api/submissions/{linked.json['submission_id']}/auto-grade", {}).status_code, 200)

        listed = self.client.get(f'/api/assignments/{aid}/photos').json['photos'][0]
        self.assertEqual(listed['score'], 2)
        self.assertEqual(listed['total_points'], 2)
        self.assertTrue(listed['is_final'])
        self.assertEqual(listed['grading_state'], 'final')
        self.assertEqual(listed['grading_source'], '机器自动批改')
        self.assertEqual(listed['uncertain_count'], 0)

    def test_photo_listing_hides_zero_before_submission_is_graded(self):
        aid, students = self.setup_assignment()
        photo = self.upload_batch(aid)
        blocks = [{'id': 0, 'text': '学号：001 姓名：张同学', 'box': [0, 0, 400, 40]}]
        with patch('batch_photos.recognize', return_value={'blocks': blocks}):
            self.post(f"/api/batch-photos/{photo['id']}/identify", {'defer_link': True})
        linked = self.post(f"/api/batch-photos/{photo['id']}/confirm", {
            'assignment_student_id': students[0]['id'], 'confirmed': True,
        })
        self.assertEqual(linked.status_code, 200, linked.json)
        listed = self.client.get(f'/api/assignments/{aid}/photos').json['photos'][0]
        self.assertIsNone(listed['score'])
        self.assertEqual(listed['grading_source'], '未批改')

    def test_background_identity_reuse_preserves_orientation_and_raw_result(self):
        import json
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        from app import create_app
        aid, students = self.setup_assignment()
        photo = self.upload_batch(aid)
        blocks = [{'id': 0, 'text': '学号：001 姓名：张同学', 'box': [0, 0, 400, 40]},
                  {'id': 1, 'text': '1. recieve', 'box': [0, 60, 400, 100]}]
        recognized = {'blocks': blocks, 'raw': {'pruned_result': {'doc_preprocessor_res': {'angle': 90}}}}
        database = Path(self.tmp.name) / 'dictation.sqlite3'
        with closing(sqlite3.connect(database)) as conn:
            original = conn.execute('SELECT original FROM batch_photos WHERE id=?', (photo['id'],)).fetchone()[0]
        original_path = Path(self.tmp.name) / 'uploads' / original
        before = original_path.read_bytes()
        self.assertEqual(self.post(f'/api/assignments/{aid}/grading-jobs', {}).status_code, 202)
        with patch('settings.recognize_selected', return_value=recognized) as cloud:
            self.app.extensions['grading_jobs'].drain()
            self.assertEqual(cloud.call_count, 1)
        item = self.client.get(f'/api/assignments/{aid}').json['students'][0]
        sub = self.client.get(f"/api/submissions/{item['submission_id']}").json
        self.assertTrue(sub['is_final'])
        self.assertEqual(sub['score'], 0)
        self.assertEqual(sub['reviews'][0]['transcript'], 'recieve')
        self.assertEqual(sub['pages'][0]['display_rotation'], 90)
        with Image.open(io.BytesIO(self.client.get(f"/api/pages/{sub['pages'][0]['id']}/preview").data)) as image:
            self.assertEqual(image.size, (240, 640))
        self.assertEqual(original_path.read_bytes(), before)
        with closing(sqlite3.connect(database)) as conn:
            saved = [json.loads(r[0]) for r in conn.execute('SELECT result FROM ocr_runs')]
        self.assertTrue(any(r.get('raw') == recognized['raw'] and r['blocks'] == blocks for r in saved))
        self.post(f"/api/batch-photos/{photo['id']}/confirm", {'assignment_student_id': students[0]['id'], 'confirmed': True})
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ocr_runs').fetchone()[0], len(saved))
        restarted = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'CSRF_TOKEN': 'test'}).test_client()
        self.assertEqual(restarted.get(f"/api/submissions/{sub['id']}").json['pages'][0]['display_rotation'], 90)

    def test_ambiguous_identity_failure_restart_and_isolation(self):
        aid, students=self.setup_assignment()
        photo=self.upload_batch(aid)
        blocks=[{'id':0,'text':'姓名：李同学','confidence':.9,'box':[0,0,200,40]}]
        with patch('batch_photos.recognize',return_value={'blocks':blocks,'model':'test-only'}):
            result=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
        self.assertIsNone(result.json['suggested_student_id'])
        self.assertEqual(len(result.json['candidates']),2)
        conflict=[{'id':0,'text':'学号：001 姓名：李同学','confidence':.9,'box':[0,0,400,40]}]
        with patch('batch_photos.recognize',return_value={'blocks':conflict,'model':'test-only'}):
            result=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
        self.assertIsNone(result.json['suggested_student_id'])
        with patch('batch_photos.recognize',side_effect=RuntimeError('model unavailable')):
            result=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
        self.assertEqual(result.status_code,503)
        photos=self.client.get(f'/api/assignments/{aid}/photos').json['photos']
        self.assertEqual(photos[0]['status'],'error')
        with self.client.get(photos[0]['url']) as original:
            self.assertEqual(original.status_code,200)
        other,_=self.setup_assignment()
        foreign=self.client.get(f'/api/assignments/{other}').json['students'][0]['id']
        self.assertEqual(self.post(f"/api/batch-photos/{photo['id']}/confirm",{'assignment_student_id':foreign,'confirmed':True}).status_code,400)

    def test_batch_validation_restart_and_confirmed_photo_cannot_be_reidentified(self):
        aid, students = self.setup_assignment()
        invalid = self.client.post(f'/api/assignments/{aid}/photos',data={'image':(io.BytesIO(b'wrong'),'p.png')},headers=self.headers)
        invalid.request.environ['wsgi.input'].close()
        self.assertEqual(invalid.status_code,400)
        self.assertEqual(self.client.get(f'/api/assignments/{aid}/photos').json['photos'],[])
        photo=self.upload_batch(aid)
        blocks=[{'id':0,'text':'学号：999 姓名：张同学','confidence':.9,'box':[0,0,400,50]}]
        with patch('batch_photos.recognize',return_value={'blocks':blocks}):
            result=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
        self.assertIsNone(result.json['suggested_student_id'],'Unknown explicit code must not be ignored.')
        linked=self.post(f"/api/batch-photos/{photo['id']}/confirm",{'assignment_student_id':students[0]['id'],'confirmed':True})
        with patch('batch_photos.recognize') as engine:
            self.assertEqual(self.post(f"/api/batch-photos/{photo['id']}/identify",{}).status_code,409)
            engine.assert_not_called()
        from app import create_app
        restarted=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test'}).test_client()
        self.assertEqual(restarted.get(f'/api/assignments/{aid}/photos').json['photos'][0]['page_id'],linked.json['page_id'])

    def test_cloud_setting_is_used_for_batch_identity(self):
        aid, students=self.setup_assignment(); photo=self.upload_batch(aid)
        self.patch('/api/settings',{'provider':'openai_compatible','base_url':'https://example.test/v1','model':'vision','api_key':'secret'})
        with patch('settings.recognize_openai',return_value={'blocks':[{'id':0,'text':'学号：001 姓名：张同学','box':[0,0,400,50],'confidence':None}]}) as cloud, patch('batch_photos.recognize') as local:
            response=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
        self.assertEqual(response.json['suggested_student_id'],students[0]['id'])
        cloud.assert_called_once(); local.assert_not_called()

    def test_multiple_identity_labels_and_bare_names_require_safe_matching(self):
        aid, students=self.setup_assignment(); photo=self.upload_batch(aid)
        for content, expected in [('张同学', students[0]['id']),('李同学',None),
                                  ('学号：001 学号：002 姓名：张同学',None),
                                  ('姓名：张同学 姓名：李同学',None)]:
            photo=self.upload_batch(aid)
            with patch('batch_photos.recognize',return_value={'blocks':[{'text':content}]}):
                result=self.post(f"/api/batch-photos/{photo['id']}/identify",{})
            self.assertEqual(result.json['suggested_student_id'],expected,content)


