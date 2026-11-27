"""Teacher workflow: real temporary SQLite, no OCR or production writes."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_teacher_workbench as fixtures


class WorkflowOptimizationTests(unittest.TestCase):
    setUp = fixtures.TeacherWorkbenchTests.setUp
    post = fixtures.TeacherWorkbenchTests.post
    conn = fixtures.TeacherWorkbenchTests.conn
    page = fixtures.TeacherWorkbenchTests.page

    def graded(self, index=0):
        sid,pid=self.page(index)
        with self.conn() as conn,conn:
            conn.execute("UPDATE pages SET status='done',needs_review=0 WHERE id=?",(pid,))
            conn.execute("UPDATE submissions SET grading_state='final' WHERE id=?",(sid,))
            conn.execute('UPDATE reviews SET transcript=?,machine_text=?,auto_result=? WHERE submission_id=?',
                ('recieve','recieve',json.dumps({'uncertain':True,'reason':'识别冲突'}),sid))
        return sid,pid,self.task['questions'][0]['id']

    def test_undo_each_quick_decision_restores_exact_review_and_keeps_history(self):
        for index,decision in enumerate(('correct','incorrect','blank')):
            sid,pid,qid=self.graded(index)
            with self.conn() as conn:
                before=dict(conn.execute('SELECT * FROM reviews WHERE submission_id=?',(sid,)).fetchone())
            saved=self.post(f'/api/submissions/{sid}/reviews/{qid}',{'quick_decision':decision})
            self.assertIn('undo',saved.json)
            event=saved.json['undo']['event_id']
            undone=self.post(f'/api/submissions/{sid}/reviews/{qid}/undo',{'event_id':event})
            self.assertEqual(undone.status_code,200,undone.json)
            self.assertEqual(undone.json['uncertain_count'],1)
            self.assertIsNone(undone.json['score'])
            with self.conn() as conn:
                self.assertEqual(dict(conn.execute('SELECT * FROM reviews WHERE submission_id=?',(sid,)).fetchone()),before)
                events=conn.execute('SELECT snapshot FROM review_events WHERE submission_id=? ORDER BY id',(sid,)).fetchall()
                self.assertEqual([json.loads(e[0]).get('action') for e in events],['teacher_quick_decision','undo_teacher_quick_decision'])
            self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{qid}/undo',{'event_id':event}).status_code,409)

    def test_undo_rejects_later_edit_new_ocr_processing_wrong_question_and_bad_input(self):
        sid,pid,qid=self.graded()
        for change in ('later_edit','new_ocr','new_page','processing'):
            with self.subTest(change=change):
                saved=self.post(f'/api/submissions/{sid}/reviews/{qid}',{'quick_decision':'correct'})
                self.assertIn('undo',saved.json)
                event=saved.json['undo']['event_id']
                with self.conn() as conn,conn:
                    if change=='later_edit':conn.execute("UPDATE reviews SET transcript='receive' WHERE submission_id=?",(sid,))
                    if change=='new_ocr':conn.execute("INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,'now',1,'{}')",(pid,))
                    if change=='new_page':conn.execute("UPDATE pages SET needs_review=1 WHERE id=?",(pid,))
                    if change=='processing':conn.execute("UPDATE submissions SET grading_state='running' WHERE id=?",(sid,))
                result=self.post(f'/api/submissions/{sid}/reviews/{qid}/undo',{'event_id':event})
                self.assertEqual(result.status_code,409,result.json)
                with self.conn() as conn,conn:
                    conn.execute("UPDATE submissions SET grading_state='final' WHERE id=?",(sid,))
                    conn.execute('UPDATE pages SET needs_review=0 WHERE id=?',(pid,))
        self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{qid+999}/undo',{'event_id':event}).status_code,404)
        for payload in ({},{'event_id':True},{'event_id':0},{'event_id':'1'}):
            self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{qid}/undo',payload).status_code,400)
        self.assertEqual(self.client.post(f'/api/submissions/{sid}/reviews/{qid}/undo',json={'event_id':event}).status_code,403)

    def test_summary_endpoints_keep_scores_and_state_with_bounded_queries(self):
        ids=[self.graded(i)[0] for i in range(4)]
        qid=self.task['questions'][0]['id']
        self.post(f'/api/submissions/{ids[1]}/reviews/{qid}',{'quick_decision':'correct'})
        with self.conn() as conn,conn:
            conn.execute("UPDATE submissions SET grading_state='failed',grading_error='timeout' WHERE id=?",(ids[2],))
            conn.execute("UPDATE submissions SET grading_state='running' WHERE id=?",(ids[3],))
        expected={sid:self.client.get(f'/api/submissions/{sid}').json for sid in ids}
        connect=sqlite3.connect
        statements=[]
        def traced(*args,**kwargs):
            conn=connect(*args,**kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch('sqlite3.connect',traced):
            for path in [f"/api/assignments/{self.assignment['id']}",f"/api/tasks/{self.task['id']}",'/api/tasks']:
                statements.clear()
                response=self.client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertLess(sum(s.lstrip().upper().startswith('SELECT') for s in statements),35,path)
                rows=response.json.get('students',response.json.get('submissions',[]))
                for item in rows:
                    sid=item.get('submission_id',item['id'])
                    for key in ['score','is_final','uncertain_count','confirmed_count','recognition','grading_source']:
                        self.assertEqual(item[key],expected[sid][key],(path,sid,key))
        self.assertEqual(self.client.get(f"/api/assignments/{self.assignment['id']}/attention").json['summary']['uncertain_questions'],2)

    def test_photo_summary_defers_linked_ocr_without_losing_pending_evidence(self):
        import test_batch
        from io import BytesIO
        from PIL import Image
        aid=self.assignment['id']
        def upload():
            image=BytesIO();Image.new('RGB',(60,40),'white').save(image,'PNG')
            return self.client.post(f'/api/assignments/{aid}/photos',data={'image':(BytesIO(image.getvalue()),'photo.png')},headers=self.headers).json
        linked,pending=upload(),upload()
        blocks=[{'id':1,'text':'姓名：学生01','box':[0,0,50,20]}]
        with self.conn() as conn,conn:
            for photo in (linked,pending):
                conn.execute("UPDATE batch_photos SET status='identified',blocks=?,identified_text=?,ocr_result=? WHERE id=?",
                    (json.dumps(blocks),'姓名：学生01',json.dumps({'blocks':blocks}),photo['id']))
        self.post(f"/api/batch-photos/{linked['id']}/confirm",{'assignment_student_id':self.assignment['students'][0]['id'],'confirmed':True,'remember_identity':False})
        with patch('settings.recognize_selected') as ocr:
            full=self.client.get(f'/api/assignments/{aid}/photos').json
            brief=self.client.get(f'/api/assignments/{aid}/photos?summary=1').json
            first,second=brief['photos']
            self.assertFalse(first['evidence_loaded'])
            self.assertNotIn('blocks',first)
            self.assertNotIn('identified_text',first)
            self.assertEqual(second['blocks'],blocks)
            detail=self.client.get(f"/api/batch-photos/{linked['id']}").json
            self.assertEqual(detail['blocks'],blocks)
            self.assertEqual(full['photos'][0]['blocks'],detail['blocks'])
            ocr.assert_not_called()


if __name__=='__main__':unittest.main()
