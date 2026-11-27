"""Isolated attention/preview checks: python -m unittest -v test_teacher_workbench."""
import hashlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app import create_app


class TeacherWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'CSRF_TOKEN': 'test'})
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': 'test'}
        self.task = self.post('/api/tasks', {'name': 'Attention', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'receive', 'points': 2}]}).json
        cid = self.post('/api/classes', {'name': '一班'}).json['id']
        for code in ('01', '02', '03', '04'):
            self.post(f'/api/classes/{cid}/members', {'student_code': code, 'name': f'学生{code}'})
        self.assignment = self.post(f"/api/tasks/{self.task['id']}/assignments", {'class_ids': [cid]}).json['assignments'][0]

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers=self.headers)

    def conn(self):
        conn = sqlite3.connect(self.root / 'dictation.sqlite3')
        conn.row_factory = sqlite3.Row
        return closing(conn)

    def page(self, index=0):
        sid = self.post(f"/api/assignment-students/{self.assignment['students'][index]['id']}/submission", {}).json['id']
        image = Image.new('RGB', (6, 4), 'white')
        image.putpixel((0, 0), (255, 0, 0))
        buf = io.BytesIO()
        image.save(buf, 'PNG')
        response = self.client.post(f'/api/submissions/{sid}/pages', data={'image': (io.BytesIO(buf.getvalue()), 'sheet.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return sid, response.json['id']

    def run_record(self, pid, angle=None, source='automatic_primary', success=1):
        result = {'source': source, 'blocks': []}
        if angle is not None:
            result['raw'] = {'pruned_result': {'doc_preprocessor_res': {'angle': angle}}}
        with self.conn() as conn, conn:
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,?,?)', (pid, '2026-10-25', success, json.dumps(result)))

    def test_uncertainty_stays_pending_and_wrong_answers_are_not_attention(self):
        sid, pid = self.page()
        sid2, pid2 = self.page(1)
        qid = self.task['questions'][0]['id']
        with self.conn() as conn, conn:
            conn.execute("UPDATE pages SET status='done',needs_review=0")
            conn.execute("UPDATE submissions SET grading_state='final'")
            conn.execute('UPDATE reviews SET transcript=?,auto_result=? WHERE submission_id=?', ('recieve', json.dumps({'uncertain': True, 'reason': '两次识别冲突'}), sid))
            conn.execute('UPDATE reviews SET transcript=?,auto_result=? WHERE submission_id=?', ('recieve', json.dumps({'uncertain': False}), sid2))
        result = self.client.get(f"/api/assignments/{self.assignment['id']}/attention")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['summary']['uncertain_questions'], 1)
        self.assertEqual(result.json['summary']['ungraded_submissions'], 1)
        self.assertEqual([(i['kind'], i['submission_id'], i['question_id']) for i in result.json['items']], [('question', sid, qid)])
        summary = self.client.get(f"/api/assignments/{self.assignment['id']}").json
        self.assertEqual(summary['counts']['completed'], 1)
        self.assertEqual(summary['counts']['attention_submissions'], 1)
        self.assertEqual(summary['counts']['uncertain_questions'], 1)
        self.assertEqual(summary['students'][0]['state'], 'review_pending')
        self.assertIsNone(summary['students'][0]['score'])
        self.post(f'/api/submissions/{sid}/reviews/{qid}', {'transcript': 'recieve', 'confirmed': True})
        self.assertEqual(self.client.get(f"/api/assignments/{self.assignment['id']}/attention").json['summary']['attention_count'], 0)
        self.assertEqual(self.client.get(f"/api/assignments/{self.assignment['id']}").json['counts']['teacher_reviewed_questions'], 1)

    def test_identity_failed_ungraded_exempt_and_stable_ids(self):
        sid, pid = self.page()
        exempt_sid, exempt_pid = self.page(1)
        pending_sid, _ = self.page(2)
        with self.conn() as conn, conn:
            conn.execute("UPDATE submissions SET grading_state='failed',grading_error='批改失败' WHERE id=?", (sid,))
            conn.execute("UPDATE pages SET status='error' WHERE id=?", (pid,))
            conn.execute('UPDATE assignment_students SET exempt=1 WHERE id=?', (self.assignment['students'][1]['id'],))
            for status, suggested, linked in [('identified', None, None), ('error', self.assignment['students'][0]['id'], None), ('confirmed', None, sid)]:
                conn.execute('''INSERT INTO batch_photos(assignment_id,original,image,width,height,status,suggested_student_id,submission_id,created_at,updated_at,match_details)
                    VALUES(?, 'x', 'x', 6,4,?,?,?,?,?,?)''', (self.assignment['id'], status, suggested, linked, 'now', 'now', json.dumps({'method': 'duplicate', 'reason': '疑似重复'})))
        url = f"/api/assignments/{self.assignment['id']}/attention"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json['summary'], {'identity_photos': 2, 'processing_photos': 0, 'failed_submissions': 1, 'ungraded_submissions': 1, 'uncertain_questions': 0, 'uncertain_submissions': 0, 'attention_count': 4})
        self.assertNotIn(exempt_sid, [i['submission_id'] for i in first.json['items']])
        self.assertIn(pending_sid, [i['submission_id'] for i in first.json['items']])
        self.assertEqual(first.json, self.client.get(url).json)
        self.assertEqual(self.client.get('/api/assignments/99999/attention').status_code, 404)

    def test_reviewed_supplement_error_leaves_attention_without_erasing_evidence(self):
        sid, pid = self.page()
        qid = self.task['questions'][0]['id']
        error = '局部补识别失败，原图与整页识别结果已保留，可再次点击重试。'
        with self.conn() as conn, conn:
            conn.execute("UPDATE pages SET status='done',needs_review=0,error=? WHERE id=?", (error, pid))
            conn.execute("UPDATE submissions SET grading_state='final' WHERE id=?", (sid,))
            conn.execute('UPDATE reviews SET transcript=?,auto_result=? WHERE submission_id=?',
                         ('recieve', json.dumps({'uncertain': True, 'reason': '补识别失败'}), sid))
        url = f"/api/assignments/{self.assignment['id']}/attention"
        self.assertEqual(self.client.get(url).json['summary']['failed_submissions'], 1)
        self.assertEqual(self.client.get(url).json['summary']['uncertain_questions'], 1)
        with patch('settings.recognize_selected') as cloud:
            confirmed = self.post(f'/api/submissions/{sid}/reviews/{qid}', {'transcript': 'recieve', 'confirmed': True})
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(self.client.get(url).json['summary']['failed_submissions'], 1)
            reviewed = self.post(f'/api/pages/{pid}/reviewed', {'confirmed': True})
            self.assertEqual(reviewed.status_code, 200)
            self.assertEqual(self.client.get(url).json['summary']['attention_count'], 0)
            cloud.assert_not_called()
        detail = reviewed.json
        self.assertEqual(detail['score'], 0)
        self.assertEqual(detail['grading_state'], 'final')
        self.assertEqual(detail['pages'][0]['error'], error)
        self.assertEqual(detail['pages'][0]['status'], 'done')
        self.assertTrue(detail['reviews'][0]['confirmed'])
        summary = self.client.get(f"/api/assignments/{self.assignment['id']}").json
        self.assertEqual(summary['counts']['attention_submissions'], 0)
        self.assertEqual(summary['counts']['completed'], 1)
        # A later machine decision invalidates the old manual acknowledgement even in the same second.
        with self.conn() as conn, conn:
            reviewed_at = conn.execute('SELECT MAX(created_at) FROM review_events WHERE submission_id=?', (sid,)).fetchone()[0]
            conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                         (sid, qid, reviewed_at, json.dumps({'action': 'automatic_decision'})))
        self.assertEqual(self.client.get(url).json['summary']['failed_submissions'], 1)
        self.post(f'/api/pages/{pid}/reviewed', {'confirmed': True})
        self.assertEqual(self.client.get(url).json['summary']['attention_count'], 0)
        # New OCR evidence also invalidates an acknowledgement when no decision was saved.
        from datetime import datetime, timedelta
        with self.conn() as conn:
            reviewed_at = conn.execute('SELECT MAX(created_at) FROM review_events WHERE submission_id=?', (sid,)).fetchone()[0]
        newer = (datetime.fromisoformat(reviewed_at) + timedelta(seconds=1)).isoformat(timespec='seconds')
        with self.conn() as conn, conn:
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,0,?)',
                         (pid, newer, json.dumps({'source': 'automatic_region_supplement', 'error': error})))
        self.assertEqual(self.client.get(url).json['summary']['failed_submissions'], 1)

    def test_manual_acknowledgement_does_not_hide_primary_or_submission_failure(self):
        sid, pid = self.page()
        qid = self.task['questions'][0]['id']
        with self.conn() as conn, conn:
            conn.execute("UPDATE pages SET status='error',needs_review=0,error='整页识别失败' WHERE id=?", (pid,))
            conn.execute("UPDATE submissions SET grading_state='final' WHERE id=?", (sid,))
            conn.execute('UPDATE reviews SET transcript=?,auto_result=? WHERE submission_id=?',
                         ('receive', json.dumps({'uncertain': False}), sid))
        self.post(f'/api/submissions/{sid}/reviews/{qid}', {'transcript': 'receive', 'confirmed': True})
        self.post(f'/api/pages/{pid}/reviewed', {'confirmed': True})
        url = f"/api/assignments/{self.assignment['id']}/attention"
        self.assertEqual(self.client.get(url).json['summary']['failed_submissions'], 1)
        with self.conn() as conn, conn:
            conn.execute("UPDATE pages SET status='done' WHERE id=?", (pid,))
            conn.execute("UPDATE submissions SET grading_state='failed' WHERE id=?", (sid,))
        self.assertEqual(self.client.get(url).json['summary']['failed_submissions'], 1)

    def test_task_status_counts_and_anonymous_attention_update_after_correction(self):
        sid,pid=self.page()
        anonymous=self.post(f"/api/tasks/{self.task['id']}/submissions",{'student_code':'temporary'}).json['id']
        image=io.BytesIO();Image.new('RGB',(60,40),'white').save(image,'PNG')
        response=self.client.post(f'/api/submissions/{anonymous}/pages',data={'image':(io.BytesIO(image.getvalue()),'sheet.png')},headers=self.headers)
        self.assertEqual(response.status_code,201)
        qid=self.task['questions'][0]['id']
        with self.conn() as c,c:
            c.execute("UPDATE pages SET status='done',needs_review=0")
            c.execute("UPDATE submissions SET grading_state='final'")
            c.execute('UPDATE reviews SET transcript=?,auto_result=?',('recieve',json.dumps({'uncertain':True,'reason':'识别冲突'})))
        before=self.client.get(f"/api/tasks/{self.task['id']}/attention")
        self.assertEqual(before.status_code,200)
        self.assertEqual(before.json['summary']['uncertain_questions'],2)
        self.assertEqual({i['submission_id'] for i in before.json['items']},{sid,anonymous})
        summary=next(t for t in self.client.get('/api/tasks').json['tasks'] if t['id']==self.task['id'])
        self.assertEqual(summary['completed_count'],0)
        self.assertEqual(summary['attention_count'],2)
        self.assertEqual(summary['uncertain_questions'],2)
        details=self.client.get(f"/api/tasks/{self.task['id']}").json
        self.assertTrue(all(s['recognition']['state']=='uncertain' for s in details['submissions']))
        self.assertTrue(all(s['recognition']['first_question_id']==qid for s in details['submissions']))
        self.post(f'/api/submissions/{anonymous}/reviews/{qid}',{'transcript':'receive','confirmed':True})
        after=self.client.get(f"/api/tasks/{self.task['id']}/attention").json
        self.assertEqual(after['summary']['uncertain_questions'],1)
        self.assertEqual(self.client.get('/api/tasks/99999/attention').status_code,404)

    def test_quick_verdict_updates_queue_class_and_exports(self):
        from openpyxl import load_workbook
        sid,pid=self.page();qid=self.task['questions'][0]['id']
        with self.conn() as c,c:
            c.execute("UPDATE pages SET status='done',needs_review=0")
            c.execute("UPDATE submissions SET grading_state='final'")
            c.execute('UPDATE reviews SET auto_result=?',(json.dumps({'uncertain':True}),))
        response=self.post(f'/api/submissions/{sid}/reviews/{qid}',{'quick_decision':'correct'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get(f"/api/tasks/{self.task['id']}/attention").json['items'],[])
        assignment=self.client.get(f"/api/assignments/{self.assignment['id']}").json
        self.assertEqual(assignment['final_average'],2)
        self.assertEqual(assignment['counts']['uncertain_questions'],0)
        self.assertEqual(assignment['counts']['teacher_reviewed_questions'],1)
        csv=self.client.get(f"/assignments/{self.assignment['id']}/grades.csv").get_data(as_text=True)
        self.assertIn('机器批改／教师改判',csv)
        with self.client.get(f"/assignments/{self.assignment['id']}/grades.xlsx") as exported:
            book=load_workbook(io.BytesIO(exported.data))
            values=list(book.active.values)
            self.assertEqual(values[1][7],2)
            book.close()

    def test_processing_and_empty_are_not_reported_as_recognition_failures(self):
        sid,pid=self.page()
        with self.conn() as c,c:
            c.execute("UPDATE submissions SET grading_state='running',grading_error='previous failure' WHERE id=?",(sid,))
            c.execute("UPDATE pages SET status='running',error='previous failure' WHERE id=?",(pid,))
            c.execute('UPDATE reviews SET auto_result=? WHERE submission_id=?',(json.dumps({'uncertain':True}),sid))
        sub=self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(sub['recognition']['state'],'processing')
        self.assertFalse(sub['recognition']['needs_attention'])
        self.assertEqual(self.client.get(f"/api/tasks/{self.task['id']}/attention").json['items'],[])
        empty=self.post(f"/api/tasks/{self.task['id']}/submissions",{'student_code':'empty'}).json['id']
        with self.conn() as c,c:c.execute('UPDATE reviews SET auto_result=? WHERE submission_id=?',(json.dumps({'uncertain':True}),empty))
        sub=self.client.get(f'/api/submissions/{empty}').json
        self.assertEqual(sub['recognition']['state'],'not_uploaded')
        self.assertEqual(sub['recognition']['uncertain_count'],0)

    def test_task_attention_counts_failed_and_uncertain_once_and_excludes_exempt(self):
        sid,pid=self.page();exempt_sid,_=self.page(1)
        with self.conn() as c,c:
            c.execute("UPDATE pages SET status='done',needs_review=0")
            c.execute("UPDATE submissions SET grading_state='failed'")
            c.execute('UPDATE reviews SET auto_result=?',(json.dumps({'uncertain':True}),))
            c.execute('UPDATE assignment_students SET exempt=1 WHERE id=?',(self.assignment['students'][1]['id'],))
        task=self.client.get(f"/api/tasks/{self.task['id']}").json
        self.assertEqual(task['attention_count'],1)
        self.assertEqual(task['failed_count'],1)
        self.assertEqual(task['uncertain_questions'],1)
        queue=self.client.get(f"/api/tasks/{self.task['id']}/attention").json
        self.assertEqual({i['submission_id'] for i in queue['items']},{sid})
        self.assertEqual(len(queue['items']),2)
        counts=self.client.get(f"/api/assignments/{self.assignment['id']}").json['counts']
        self.assertEqual(counts['failed_submissions'],1)
        self.assertEqual(counts['uncertain_submissions'],1)

    def test_preview_rotation_uses_raw_and_preserves_original(self):
        self.assertIsNotNone(importlib.util.find_spec('teacher_workbench'), 'Missing teacher workbench')
        from teacher_workbench import preview_info
        _, pid = self.page()
        with self.conn() as conn:
            record = conn.execute('SELECT * FROM pages WHERE id=?', (pid,)).fetchone()
            original = self.root / 'uploads' / record['original']
            image_path = self.root / 'uploads' / record['image']
            before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, image_path)]
        for angle in (90, 270):
            self.run_record(pid, angle)
            with self.conn() as conn:
                self.assertEqual(preview_info(conn, pid)['display_rotation'], angle)
            with patch('settings.recognize_selected') as cloud:
                response = self.client.get(f'/api/pages/{pid}/preview')
                cloud.assert_not_called()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'image/png')
            with Image.open(io.BytesIO(response.data)) as rotated:
                self.assertEqual(rotated.size, (4, 6))
                self.assertEqual(rotated.getpixel((0, 5) if angle == 90 else (3, 0)), (255, 0, 0))
        response = self.client.get(f'/api/pages/{pid}/preview?rotation=0')
        with Image.open(io.BytesIO(response.data)) as unchanged:
            self.assertEqual(unchanged.size, (6, 4))
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, image_path)])
        for invalid in ('45', '-90', '90.0', 'x', ''):
            self.assertEqual(self.client.get(f'/api/pages/{pid}/preview?rotation={invalid}').status_code, 400)
        self.assertEqual(self.client.get('/api/pages/99999/preview').status_code, 404)

    def test_stale_region_angles_do_not_override_new_full_run(self):
        self.assertIsNotNone(importlib.util.find_spec('teacher_workbench'), 'Missing teacher workbench')
        from teacher_workbench import preview_info
        _, pid = self.page()
        self.run_record(pid, 0)
        self.run_record(pid, 270, 'automatic_region_supplement')
        with self.conn() as conn, conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?', (pid,))
            self.assertEqual(preview_info(conn, pid)['display_rotation'], 270)
        self.run_record(pid, 90)
        with self.conn() as conn:
            self.assertEqual(preview_info(conn, pid)['display_rotation'], 90)
        self.run_record(pid, 180, 'automatic_region_supplement')
        with self.conn() as conn, conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=0 WHERE id=?', (pid,))
            self.assertEqual(preview_info(conn, pid)['display_rotation'], 90)
        self.run_record(pid, None)
        with self.conn() as conn:
            self.assertEqual(preview_info(conn, pid)['display_rotation'], 0)


if __name__ == '__main__':
    unittest.main()
