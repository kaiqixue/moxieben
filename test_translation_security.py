"""Translation security checks use two real teacher sessions and synthetic local data."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from app import create_app
import test_tenant_isolation
from test_translation_grading import RUBRIC, response


class TranslationSecurityTests(unittest.TestCase):
    # Reuse the existing real login/CSRF helpers without inheriting its unrelated tests.
    register = test_tenant_isolation.TenantIsolationTests.register
    token = test_tenant_isolation.TenantIsolationTests.token
    post = test_tenant_isolation.TenantIsolationTests.post

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'dictation.sqlite3'
        self.app = create_app({'DATA_DIR': temporary.name, 'TESTING': True,
                               'TEACHER_TENANT_MODE': True, 'GRADING_JOBS_AUTOSTART': False})
        self.a, self.b = self.app.test_client(), self.app.test_client()
        self.register(self.a, 'translation-a@example.test')
        self.register(self.b, 'translation-b@example.test')
        self.model = Mock(side_effect=lambda prompt: response('partial') if 'answer_snapshot' in prompt else RUBRIC)
        self.app.extensions['translation_model'] = self.model
        self.runner = self.app.extensions['grading_jobs']
        self.own = self.resources(self.a, '甲')
        self.foreign = self.resources(self.b, '乙')
        self.model.reset_mock()

    def resources(self, client, label):
        task = self.post(client, '/api/tasks', {'name': label, 'questions': [
            {'number': 1, 'kind': 'sentence', 'scoring_mode': 'translation',
             'source_text': '她回家。', 'answer': 'She goes home.', 'points': 3}]})
        self.assertEqual(task.status_code, 201, task.json)
        tid, qid = task.json['id'], task.json['questions'][0]['id']
        self.runner.drain()
        klass = self.post(client, '/api/classes', {'name': label + '班'})
        self.assertEqual(klass.status_code, 201, klass.json)
        cid = klass.json['id']
        member = self.post(client, f'/api/classes/{cid}/members', {'name': '合成学生', 'student_code': '001'})
        self.assertEqual(member.status_code, 201, member.json)
        assignment = self.post(client, f'/api/tasks/{tid}/assignments', {'class_ids': [cid]})
        self.assertEqual(assignment.status_code, 201, assignment.json)
        aid = assignment.json['assignments'][0]['id']
        ast = client.get(f'/api/assignments/{aid}').json['students'][0]['id']
        submission = self.post(client, f'/api/assignment-students/{ast}/submission', {})
        self.assertEqual(submission.status_code, 201, submission.json)
        sid = submission.json['id']
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE reviews SET transcript='She go home.',teacher_edited=1 WHERE submission_id=?", (sid,))
            conn.execute("INSERT INTO pages(submission_id,original,image,width,height,created_at) VALUES(?,'synthetic','synthetic',10,10,'now')", (sid,))
        queued = self.post(client, f'/api/submissions/{sid}/translation-jobs', {'request_key': label})
        self.assertEqual(queued.status_code, 202, queued.json)
        jid = queued.json['job']['id']
        self.runner.drain()
        dto = client.get(f'/api/submissions/{sid}/translation').json['questions'][0]
        self.assertEqual(dto['ai_result']['suggested_score'], 2.5, dto)
        return dict(tid=tid, qid=qid, cid=cid, aid=aid, sid=sid, jid=jid,
                    run_id=dto['ai_result']['id'], revision=dto['revision'])

    def test_other_teacher_cannot_read_or_write_translation_resources(self):
        f = self.foreign
        for path in [f"/api/questions/{f['qid']}/translation-rubric",
                     f"/api/submissions/{f['sid']}/translation",
                     f"/api/submissions/{f['sid']}/translation-history",
                     f"/api/assignments/{f['aid']}/translation",
                     f"/api/grading-jobs/{f['jid']}"]:
            with self.subTest(method='GET', path=path):
                self.assertEqual(self.a.get(path).status_code, 404)
        attempts = [
            (f"/api/questions/{f['qid']}/translation-rubrics", {'expected_version': 1, 'rubric': RUBRIC}),
            (f"/api/tasks/{f['tid']}/translation-rubric-jobs", {}),
            (f"/api/submissions/{f['sid']}/translation-jobs", {}),
            (f"/api/assignments/{f['aid']}/translation-jobs", {}),
            (f"/api/grading-jobs/{f['jid']}/resume", {}),
            (f"/api/submissions/{f['sid']}/reviews/{f['qid']}/translation-confirm",
             {'action': 'adopt', 'ai_run_id': f['run_id'], 'expected_revision': f['revision'], 'request_key': 'intrusion'}),
            (f"/api/submissions/{f['sid']}/reviews/{f['qid']}/translation-reopen",
             {'reason': 'unauthorized', 'expected_revision': f['revision'], 'request_key': 'intrusion-reopen'}),
        ]
        for path, payload in attempts:
            with self.subTest(method='POST', path=path):
                self.assertEqual(self.post(self.a, path, payload).status_code, 404)
        self.model.assert_not_called()
        foreign = self.b.get(f"/api/submissions/{f['sid']}/translation").json['questions'][0]
        self.assertEqual(foreign['revision'], f['revision'])
        self.assertIsNone(foreign['final_score'])
        history = self.b.get(f"/api/submissions/{f['sid']}/translation-history").json
        self.assertEqual(len(history['runs']), 1)

    def test_same_owner_mismatched_question_and_foreign_run_are_rejected(self):
        own, foreign = self.own, self.foreign
        other = self.resources(self.a, '甲的另一作业')
        self.model.reset_mock()
        sid = own['sid']
        for path, payload, expected in [
            (f"/api/submissions/{sid}/reviews/{other['qid']}/translation-confirm",
             {'action': 'adopt', 'ai_run_id': own['run_id'], 'expected_revision': own['revision'], 'request_key': 'wrong-question'}, 404),
            (f"/api/submissions/{sid}/translation-jobs", {'question_ids': [other['qid']]}, 400),
            (f"/api/submissions/{sid}/reviews/{own['qid']}/translation-confirm",
             {'action': 'adopt', 'ai_run_id': foreign['run_id'], 'expected_revision': own['revision'], 'request_key': 'wrong-run'}, 409),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.post(self.a, path, payload).status_code, expected)
        self.assertEqual(self.a.get(f"/api/submissions/{sid}/translation-history?question_id={other['qid']}").status_code, 404)
        dto = self.a.get(f'/api/submissions/{sid}/translation').json['questions'][0]
        self.assertIsNone(dto['final_score'])
        self.assertEqual(dto['revision'], own['revision'])
        self.model.assert_not_called()

    def test_archived_class_remains_readable_but_blocks_all_translation_writes(self):
        own = self.own
        archived = self.a.patch(f"/api/classes/{own['cid']}", json={'archived': True},
                                headers={'X-CSRF-Token': self.token(self.a)})
        self.assertEqual(archived.status_code, 200, archived.json)
        self.assertEqual(self.a.get(f"/api/submissions/{own['sid']}/translation").status_code, 200)
        for path, payload in [
            (f"/api/tasks/{own['tid']}/translation-rubric-jobs", {}),
            (f"/api/questions/{own['qid']}/translation-rubrics", {'expected_version': 1, 'rubric': RUBRIC}),
            (f"/api/submissions/{own['sid']}/translation-jobs", {}),
            (f"/api/assignments/{own['aid']}/translation-jobs", {}),
            (f"/api/submissions/{own['sid']}/reviews/{own['qid']}/translation-confirm",
             {'action': 'adopt', 'ai_run_id': own['run_id'], 'expected_revision': own['revision'], 'request_key': 'archived'}),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.post(self.a, path, payload).status_code, 409)
        self.model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
