"""Translation workflow uses temporary databases and injected model responses only."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import Mock, patch
from test_translation_grading import RUBRIC, response


class TranslationMigrationTests(unittest.TestCase):
    def test_migration_preserves_strict_questions_and_is_repeatable(self):
        from translation_review import migrate
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite3'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript('''CREATE TABLE tasks(id INTEGER PRIMARY KEY);
                CREATE TABLE questions(id INTEGER PRIMARY KEY,task_id INTEGER,points REAL);
                CREATE TABLE submissions(id INTEGER PRIMARY KEY,task_id INTEGER);
                CREATE TABLE reviews(submission_id INTEGER,question_id INTEGER,PRIMARY KEY(submission_id,question_id));
                CREATE TABLE grading_jobs(id INTEGER PRIMARY KEY, submission_id INTEGER);
                INSERT INTO questions VALUES(1,1,3);''')
            migrate(path)
            migrate(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT scoring_mode FROM questions').fetchone()[0], 'strict')
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_failed_migration_rolls_back_columns_and_tables(self):
        from translation_review import migrate
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rollback.sqlite3'
            with closing(sqlite3.connect(path)) as conn,conn:
                conn.executescript('''CREATE TABLE tasks(id INTEGER PRIMARY KEY);
                    CREATE TABLE questions(id INTEGER PRIMARY KEY);
                    CREATE TABLE reviews(submission_id INTEGER,question_id INTEGER);
                    CREATE TABLE grading_jobs(id INTEGER PRIMARY KEY);
                    CREATE TABLE translation_ai_runs(id INTEGER PRIMARY KEY);''')
            with self.assertRaises(sqlite3.OperationalError):
                migrate(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertNotIn('scoring_mode',{r[1] for r in conn.execute('PRAGMA table_info(questions)')})
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='translation_rubrics'").fetchone())


class TranslationWorkflowTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'dictation.sqlite3'
        self.app = create_app({'DATA_DIR': self.tmp.name, 'TESTING': True, 'CSRF_TOKEN': 'test'})
        self.client = self.app.test_client()
        self.model = Mock(return_value=RUBRIC)
        self.app.extensions['translation_model'] = self.model
        task = self.post('/api/tasks', {'name':'翻译','questions':[{'number':1,'kind':'sentence',
             'scoring_mode':'translation','source_text':'她回家。','answer':'She goes home.','points':3}]})
        self.assertEqual(task.status_code,201,task.json)
        self.tid, self.qid = task.json['id'],task.json['questions'][0]['id']
        self.runner = self.app.extensions['grading_jobs']
        self.runner.drain()
        sub = self.post(f'/api/tasks/{self.tid}/submissions', {'student_code':'001'})
        self.assertEqual(sub.status_code,201,sub.json)
        self.sid = sub.json['id']
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE reviews SET transcript='She go home.',teacher_edited=1 WHERE submission_id=?",(self.sid,))
            conn.execute("INSERT INTO pages(submission_id,original,image,width,height,created_at) VALUES(?,'x','x',10,10,'now')",(self.sid,))
        self.model.reset_mock()
        self.model.return_value=response('partial')

    def post(self,path,value):
        return self.client.post(path,json=value,headers={'X-CSRF-Token':'test'})

    def dto(self):
        value=self.client.get(f'/api/submissions/{self.sid}/translation')
        self.assertEqual(value.status_code,200,value.json)
        return value.json['questions'][0]

    def analyze(self,scope='missing',key='job1'):
        result=self.post(f'/api/submissions/{self.sid}/translation-jobs',{'scope':scope,'request_key':key})
        self.assertEqual(result.status_code,202,result.json)
        self.runner.drain()
        return self.dto()

    def test_ai_is_pending_until_teacher_adopts_and_replay_is_safe(self):
        q=self.analyze()
        self.assertEqual(q['ai_result']['suggested_score'],2.5)
        self.assertIsNone(q['final_score'])
        self.assertTrue(q['ai_result']['can_adopt'])
        revision=q['revision']
        payload={'action':'adopt','ai_run_id':q['ai_result']['id'],'expected_revision':revision,'request_key':'confirm1'}
        result=self.post(f'/api/submissions/{self.sid}/reviews/{self.qid}/translation-confirm',payload)
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['score'],2.5)
        self.assertEqual(self.dto()['revision'],revision+1)
        self.assertEqual(self.post(f'/api/submissions/{self.sid}/reviews/{self.qid}/translation-confirm',payload).status_code,200)
        self.assertEqual(self.dto()['revision'],revision+1)
        self.analyze('regrade_unconfirmed','job2')
        self.assertEqual(self.model.call_count,1)

    def test_edit_invalidates_old_result_and_revision_guards_other_tab(self):
        q=self.analyze()
        changed=self.post(f'/api/submissions/{self.sid}/reviews/{self.qid}/translation-confirm',
            {'action':'transcript','transcript':'She returns home.','expected_revision':q['revision'],'request_key':'edit1'})
        self.assertEqual(changed.status_code,200,changed.json)
        self.assertFalse(self.dto()['ai_result']['can_adopt'])
        result=self.post(f'/api/submissions/{self.sid}/reviews/{self.qid}/translation-confirm',
            {'action':'adopt','ai_run_id':q['ai_result']['id'],'expected_revision':q['revision'],'request_key':'stale'})
        self.assertEqual(result.status_code,409)

    def test_failure_is_history_and_never_zero(self):
        self.model.return_value='broken JSON'
        q=self.analyze()
        self.assertIsNone(q['final_score'])
        self.assertEqual(q['latest_attempt']['state'],'failed')
        self.assertIsNone(q['ai_result'])

    def test_manual_accounting_and_explicit_reopen(self):
        q=self.dto()
        base={'action':'manual','request_key':'manual1','expected_revision':q['revision'],
              'score':2.75,'components':{'semantic':2,'completeness':0,'grammar':.75,'expression':0},
              'criteria':[{'criterion_id':'meaning','awarded_points_cents':200},
                          {'criterion_id':'verb','awarded_points_cents':75}],
              'errors':[],'feedback':'教师复核','reason':'谓语形式部分正确，教师裁量。'}
        path=f'/api/submissions/{self.sid}/reviews/{self.qid}'
        bad=dict(base,score=3)
        self.assertEqual(self.post(path+'/translation-confirm',bad).status_code,400)
        result=self.post(path+'/translation-confirm',base)
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['score'],2.75)
        changed=self.post(path+'/translation-confirm',dict(base,request_key='cannot-overwrite',expected_revision=q['revision']+1))
        self.assertEqual(changed.status_code,409)
        reopened=self.post(path+'/translation-reopen',{'request_key':'reopen1','expected_revision':q['revision']+1,'reason':'重新检查依据'})
        self.assertEqual(reopened.status_code,200,reopened.json)
        self.assertIsNone(reopened.json['score'])

    def test_same_job_key_replays_after_completion_and_changed_payload_conflicts(self):
        self.analyze()
        replay=self.post(f'/api/submissions/{self.sid}/translation-jobs',{'scope':'missing','request_key':'job1'})
        self.assertEqual(replay.status_code,202,replay.json)
        changed=self.post(f'/api/submissions/{self.sid}/translation-jobs',{'scope':'regrade_unconfirmed','request_key':'job1'})
        self.assertEqual(changed.status_code,409)
        self.assertEqual(self.model.call_count,1)

    def test_unfinished_attempt_becomes_unknown_without_a_second_provider_call(self):
        self.analyze()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('''INSERT INTO translation_ai_runs(job_id,submission_id,question_id,rubric_id,attempt_no,
                  request_key,input_hash,answer_snapshot,input_snapshot,state,created_at)
                SELECT NULL,submission_id,question_id,rubric_id,attempt_no+1,'lost',input_hash,
                  answer_snapshot,input_snapshot,'requesting',created_at FROM translation_ai_runs LIMIT 1''')
        self.analyze('regrade_unconfirmed','new-intent')
        self.assertEqual(self.model.call_count,1)
        self.assertEqual(self.dto()['latest_attempt']['state'],'unknown')

    def test_read_only_endpoints_do_not_call_provider_and_csrf_is_required(self):
        self.dto()
        self.client.get(f'/api/questions/{self.qid}/translation-rubric')
        self.client.get(f'/api/submissions/{self.sid}/translation-history')
        self.model.assert_not_called()
        self.assertEqual(self.client.post(f'/api/submissions/{self.sid}/translation-jobs',json={}).status_code,403)

    def test_class_batch_counts_units_and_retry_only_calls_failed_unit(self):
        cid=self.post('/api/classes',{'name':'翻译班','term':'秋'}).json['id']
        for code in ('001','002'):
            self.post(f'/api/classes/{cid}/members',{'student_code':code,'name':'同学'+code})
        aid=self.post(f'/api/tasks/{self.tid}/assignments',{'class_ids':[cid]}).json['assignments'][0]['id']
        sid2=self.post(f'/api/tasks/{self.tid}/submissions',{'student_code':'002'}).json['id']
        with closing(sqlite3.connect(self.path)) as conn,conn:
            for sid,code in ((self.sid,'001'),(sid2,'002')):
                rid=conn.execute('SELECT id FROM assignment_students WHERE assignment_id=? AND student_code=?',(aid,code)).fetchone()[0]
                conn.execute('UPDATE submissions SET assignment_student_id=? WHERE id=?',(rid,sid))
                conn.execute("UPDATE reviews SET transcript='She go home.',teacher_edited=1 WHERE submission_id=?",(sid,))
        self.model.side_effect=[response('partial'),'bad json']
        started=self.post(f'/api/assignments/{aid}/translation-jobs',{'request_key':'batch1'})
        self.assertEqual(started.status_code,202,started.json)
        self.runner.drain()
        overview=self.client.get(f'/api/assignments/{aid}/translation').json
        self.assertEqual(overview['summary']['total'],2)
        self.assertEqual(overview['summary']['submitted'],2)
        self.assertEqual(overview['summary']['ai_completed'],1)
        self.assertEqual(overview['summary']['failed'],1)
        self.assertTrue(all(s['final_score'] is None for s in overview['students']))
        self.model.side_effect=None
        self.model.return_value=response('partial')
        retried=self.post(f'/api/assignments/{aid}/translation-jobs',{'request_key':'batch2','scope':'retry_failed'})
        self.assertEqual(retried.status_code,202,retried.json)
        self.runner.drain()
        self.assertEqual(self.model.call_count,3)
        history=self.client.get(f'/api/submissions/{sid2}/translation-history').json
        self.assertEqual([r['state'] for r in history['runs']],['succeeded','failed'])

    def test_normal_grading_queue_continues_into_translation(self):
        started=self.post(f'/api/submissions/{self.sid}/grading-jobs',{})
        self.assertEqual(started.status_code,202,started.json)
        with patch.object(self.runner,'post',return_value=Mock(status_code=200)) as auto_grade:
            self.runner.drain()
        auto_grade.assert_called_once()
        self.assertEqual(self.model.call_count,1)
        self.assertEqual(self.dto()['ai_result']['suggested_score'],2.5)
        self.assertIsNone(self.dto()['final_score'])


if __name__ == '__main__':
    unittest.main()
