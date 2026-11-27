"""Read-only class reports on temporary data; no real OCR or student uploads."""
import json
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

import test_teacher_workbench as fixtures


class ClassAnalysisTests(unittest.TestCase):
    setUp = fixtures.TeacherWorkbenchTests.setUp
    post = fixtures.TeacherWorkbenchTests.post
    conn = fixtures.TeacherWorkbenchTests.conn
    page = fixtures.TeacherWorkbenchTests.page

    def seed(self, index=0, transcript='recieve', uncertain=False):
        sid,pid=self.page(index)
        with self.conn() as conn,conn:
            conn.execute("UPDATE submissions SET grading_state='final' WHERE id=?",(sid,))
            conn.execute("UPDATE pages SET status='done',needs_review=0 WHERE id=?",(pid,))
            conn.execute('UPDATE reviews SET transcript=?,auto_result=? WHERE submission_id=?',
                         (transcript,json.dumps({'uncertain':uncertain,'reason':'模拟识别'}),sid))
        return sid,pid

    def analysis(self):
        response=self.client.get(f"/api/assignments/{self.assignment['id']}/analysis")
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        return response.json

    def test_wrong_rates_exclude_uncertainty_exempt_and_keep_teacher_decisions(self):
        sid,_=self.seed(0,'recieve');self.seed(1,'receive',True)
        corrected,_=self.seed(2,'recieve');self.seed(3,'recieve')
        qid=self.task['questions'][0]['id']
        self.post(f'/api/submissions/{corrected}/reviews/{qid}',{'quick_decision':'correct'})
        with self.conn() as conn,conn:
            conn.execute('UPDATE assignment_students SET exempt=1 WHERE id=?',(self.assignment['students'][3]['id'],))
        report=self.analysis();q=report['questions'][0]
        self.assertEqual(report['counts']['expected'],3)
        self.assertEqual(report['counts']['final'],2)
        self.assertEqual((q['wrong_count'],q['correct_count'],q['pending_count'],q['valid_count']),(1,1,1,2))
        self.assertEqual(q['wrong_rate'],50)
        self.assertEqual([r['submission_id'] for r in q['mistakes']],[sid])
        self.assertEqual(report['counts']['wrong_students'],1)
        self.assertEqual(q['common_answers'][0]['text'],'recieve')

    def test_not_uploaded_failed_processing_and_empty_do_not_become_wrong_answers(self):
        failed,pid=self.seed(0);processing,_=self.seed(1);pending,_=self.seed(2)
        with self.conn() as conn,conn:
            conn.execute("UPDATE submissions SET grading_state='failed',grading_error='timeout' WHERE id=?",(failed,))
            conn.execute("UPDATE pages SET status='error' WHERE id=?",(pid,))
            conn.execute("UPDATE submissions SET grading_state='running' WHERE id=?",(processing,))
            conn.execute("UPDATE submissions SET grading_state='idle' WHERE id=?",(pending,))
        report=self.analysis();c=report['counts'];q=report['questions'][0]
        self.assertEqual((c['failed'],c['processing'],c['ungraded'],c['not_uploaded'],c['final']),(1,1,1,1,0))
        self.assertEqual(q['wrong_count'],0)
        self.assertIsNone(q['wrong_rate'])

    def test_read_only_download_scopes_student_and_escapes_untrusted_text(self):
        sid,_=self.seed(0,'<script>alert(1)</script>');self.seed(1,'Receive')
        aid=self.assignment['id'];rid=self.assignment['students'][0]['id']
        with self.conn() as conn:
            before=list(conn.iterdump())
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'uploads').iterdir()}
        with patch('settings.recognize_selected') as cloud:
            page=self.client.get(f'/assignments/{aid}/analysis')
            self.assertEqual(page.status_code,200)
            self.assertEqual(page.data,self.client.get('/').data)
            report=self.client.get(f'/assignments/{aid}/analysis/download')
            self.assertIn('&lt;script&gt;',report.get_data(as_text=True))
            self.assertNotIn('<script>alert(1)</script>',report.get_data(as_text=True))
            download=self.client.get(f'/assignments/{aid}/analysis/download?view=students&student={rid}')
            self.assertEqual(download.status_code,200)
            self.assertIn('attachment;',download.headers['Content-Disposition'])
            html=download.get_data(as_text=True)
            self.assertIn('data-correction-student="'+str(rid)+'"',html)
            self.assertNotIn('data-correction-student="'+str(self.assignment['students'][1]['id'])+'"',html)
            self.assertNotIn('学生02',html,'A single-person download must not contain other students in a hidden picker')
            self.assertIn('离线快照',html)
            cloud.assert_not_called()
        with self.conn() as conn:self.assertEqual(list(conn.iterdump()),before)
        self.assertEqual({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'uploads').iterdir()},hashes)
        for query in ('?view=wrong','?view=students&student=0','?view=students&student=abc','?view=students&student='+'1'*5000):
            self.assertEqual(self.client.get(f'/assignments/{aid}/analysis/download'+query).status_code,400)
        normalized=self.client.get(f'/assignments/{aid}/analysis/download?view=students&student=00{rid}').get_data(as_text=True)
        self.assertIn(f'data-correction-student="{rid}"',normalized)
        self.assertEqual(self.client.get(f'/assignments/{aid}/analysis/download?view=students&student=9999').status_code,404)
        self.assertEqual(self.client.get('/api/assignments/9999/analysis').status_code,404)

    def test_teacher_edit_recomputes_report_and_common_answers_keep_case_and_punctuation(self):
        ids=[self.seed(i,t)[0] for i,t in enumerate((' Receive ','Receive','receive.','recieve'))]
        qid=self.task['questions'][0]['id']
        q=self.analysis()['questions'][0]
        self.assertEqual([(g['text'],g['count']) for g in q['common_answers']],[('Receive',2),('receive.',1),('recieve',1)])
        self.post(f'/api/submissions/{ids[0]}/reviews/{qid}',{'transcript':'receive','confirmed':True})
        self.post(f'/api/submissions/{ids[1]}/reviews/{qid}',{'quick_decision':'blank'})
        result=self.analysis();q=result['questions'][0]
        self.assertEqual((q['correct_count'],q['wrong_count']),(1,3))
        self.assertEqual(next(m for m in q['mistakes'] if m['submission_id']==ids[1])['answer_status'],'blank')
        self.assertEqual(result['counts']['wrong_answers'],3)

    def test_answer_groups_link_only_their_students_and_keep_status_separate(self):
        ids=[self.seed(i,t)[0] for i,t in enumerate((' adopt ','adopt','Adopt','adopt.'))]
        q=self.analysis()['questions'][0]
        for group in q['common_answers']:
            self.assertIn('id',group,'Answer groups need a shared identifier for their student links')
            members=[m for m in q['mistakes'] if m['answer_group']==group['id']]
            self.assertEqual(len(members),group['count'])
            self.assertEqual([m['submission_id'] for m in members],ids[:2] if group['text']=='adopt' else [ids[2] if group['text']=='Adopt' else ids[3]])
        qid=self.task['questions'][0]['id']
        with self.conn() as conn,conn:
            conn.execute("UPDATE reviews SET transcript='' WHERE submission_id IN (?,?)",ids[:2])
        self.post(f'/api/submissions/{ids[0]}/reviews/{qid}',{'quick_decision':'blank'})
        self.post(f'/api/submissions/{ids[1]}/reviews/{qid}',{'quick_decision':'incorrect'})
        q=self.analysis()['questions'][0]
        groups={g['answer_status']:g['id'] for g in q['common_answers']}
        self.assertNotEqual(groups['blank'],groups['teacher_verdict'])
        self.assertEqual(len({m['submission_id'] for m in q['mistakes']}),4)

    def test_question_order_partial_credit_and_empty_teacher_verdict_are_honored(self):
        ids=[self.seed(i,'receive' if i<3 else 'recieve')[0] for i in range(4)]
        with self.conn() as conn,conn:
            qid=conn.execute("INSERT INTO questions(task_id,number,kind,answer,variants,points) VALUES(?,2,'word','home','[]',2)",(self.task['id'],)).lastrowid
            for sid in ids:
                conn.execute('INSERT INTO reviews(submission_id,question_id,transcript,auto_result) VALUES(?,?,?,?)',
                             (sid,qid,'house',json.dumps({'uncertain':False})))
        self.post(f'/api/submissions/{ids[0]}/reviews/{qid}',{'transcript':'house','confirmed':True,'score_override':1,'reason':'部分给分'})
        self.post(f'/api/submissions/{ids[1]}/reviews/{qid}',{'transcript':'home','confirmed':True})
        with self.conn() as conn,conn:
            conn.execute("UPDATE reviews SET transcript='' WHERE submission_id=? AND question_id=?",(ids[2],qid))
        self.post(f'/api/submissions/{ids[2]}/reviews/{qid}',{'quick_decision':'incorrect'})
        report=self.analysis()
        self.assertEqual([q['number'] for q in report['questions']],[2,1])
        q=report['questions'][0]
        self.assertEqual((q['wrong_count'],q['correct_count']),(3,1))
        self.assertEqual(next(m for m in q['mistakes'] if m['submission_id']==ids[0])['score'],1)
        self.assertEqual(next(m for m in q['mistakes'] if m['submission_id']==ids[2])['answer_status'],'teacher_verdict')
        html=self.client.get(f"/assignments/{self.assignment['id']}/analysis/download?view=students").get_data(as_text=True)
        self.assertIn('教师原图判错，未补录作答文字',html)

    def test_other_classes_and_pending_only_students_remain_separate(self):
        self.seed(0,'recieve',True)
        cid=self.post('/api/classes',{'name':'其他班'}).json['id']
        self.post(f'/api/classes/{cid}/members',{'student_code':'OTHER','name':'其他班同学'})
        other=self.post(f"/api/tasks/{self.task['id']}/assignments",{'class_ids':[cid]}).json['assignments'][0]
        report=self.analysis()
        self.assertEqual(report['counts']['wrong_answers'],0)
        self.assertEqual(report['counts']['pending_questions'],1)
        self.assertIsNone(report['questions'][0]['wrong_rate'])
        html=self.client.get(f"/assignments/{self.assignment['id']}/analysis/download?view=students").get_data(as_text=True)
        self.assertIn('以下题目由老师先核对',html)
        self.assertNotIn('data-correction-question=',html)
        self.assertNotIn('其他班同学',html)
        self.assertEqual(self.client.get(f"/assignments/{self.assignment['id']}/analysis/download?view=students&student={other['students'][0]['id']}").status_code,404)


if __name__=='__main__':unittest.main()
