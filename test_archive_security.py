"""Archive authorization regressions; temporary tenant databases only."""
import io
import sqlite3
import unittest
from contextlib import closing

from PIL import Image
import test_tenant_isolation as fixtures


class ArchiveSecurityTests(unittest.TestCase):
    setUp = fixtures.TenantIsolationTests.setUp
    register = fixtures.TenantIsolationTests.register
    token = fixtures.TenantIsolationTests.token
    post = fixtures.TenantIsolationTests.post

    def resources(self, client):
        cid = self.post(client, '/api/classes', {'name': '测试班'}).json['id']
        self.post(client, f'/api/classes/{cid}/members', {'name': '测试学生', 'student_code': '1001'})
        tid = self.post(client, '/api/tasks', {'name': '测试作业', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'book', 'points': 1}]}).json['id']
        assignment = self.post(client, f'/api/tasks/{tid}/assignments', {'class_ids': [cid]}).json['assignments'][0]
        aid, rid = assignment['id'], assignment['students'][0]['id']
        sid = self.post(client, f'/api/assignment-students/{rid}/submission', {}).json['id']
        image = io.BytesIO()
        Image.new('RGB', (10, 10), 'white').save(image, 'PNG')
        page = client.post(f'/api/submissions/{sid}/pages',
            data={'image': (io.BytesIO(image.getvalue()), 'test.png')},
            headers={'X-CSRF-Token': self.token(client)})
        self.assertEqual(page.status_code, 201)
        with closing(sqlite3.connect(f'{self.data_dir}/dictation.sqlite3')) as conn, conn:
            owner = conn.execute('SELECT owner_id FROM assignments WHERE id=?', (aid,)).fetchone()[0]
            photo = conn.execute('''INSERT INTO batch_photos
                (assignment_id,original,image,width,height,status,created_at,updated_at,owner_id)
                VALUES(?,'test','test',10,10,'identified','now','now',?)''', (aid, owner)).lastrowid
            job = conn.execute('''INSERT INTO grading_jobs
                (submission_id,state,stage,photo_ids,created_at,updated_at,owner_id)
                VALUES(?,'interrupted','grading','[]','now','now',?)''', (sid, owner)).lastrowid
        return dict(cid=cid, tid=tid, aid=aid, rid=rid, sid=sid, pid=page.json['id'], photo=photo, jid=job)

    def archive(self, client, resources, archived):
        response = client.patch(f'/api/classes/{resources["cid"]}', json={'archived': archived},
                                headers={'X-CSRF-Token': self.token(client)})
        self.assertEqual(response.status_code, 200, response.json)

    def writes(self, r):
        return [
            ('POST', f'/api/assignments/{r["aid"]}/answer-card-format', {'clear': True}),
            ('PATCH', f'/api/assignment-students/{r["rid"]}', {'exempt': True, 'reason': '测试免交'}),
            ('POST', f'/api/submissions/{r["sid"]}/link', {'assignment_student_id': r['rid']}),
            ('POST', f'/api/pages/{r["pid"]}/reviewed', {'confirmed': True}),
            ('POST', f'/api/batch-photos/{r["photo"]}/exclude', {'excluded': True}),
            ('POST', f'/api/grading-jobs/{r["jid"]}/resume', {}),
            ('DELETE', f'/api/tasks/{r["tid"]}', {'confirmed': True, 'expected_counts': {}}),
        ]

    def test_foreign_active_archived_and_missing_resources_have_identical_404(self):
        resources = self.resources(self.a)
        missing = {key: value + 10000 for key, value in resources.items()}
        headers = {'X-CSRF-Token': self.token(self.b)}
        for archived in (False, True):
            self.archive(self.a, resources, archived)
            for existing, absent in zip(self.writes(resources), self.writes(missing)):
                method, path, payload = existing
                with self.subTest(archived=archived, path=path):
                    response = self.b.open(path, method=method, json=payload, headers=headers)
                    unknown = self.b.open(absent[1], method=method, json=absent[2], headers=headers)
                    self.assertEqual(unknown.status_code, 404, unknown.json)
                    self.assertEqual(response.status_code, 404, response.json)
                    self.assertEqual(response.json, unknown.json)

    def test_owner_writes_succeed_until_class_is_archived(self):
        resources = self.resources(self.a)
        headers = {'X-CSRF-Token': self.token(self.a)}
        path = f'/api/assignments/{resources["aid"]}/answer-card-format'
        self.assertEqual(self.a.post(path, json={'clear': True}, headers=headers).status_code, 200)
        self.archive(self.a, resources, True)
        for method, path, payload in self.writes(resources):
            with self.subTest(path=path):
                response = self.a.open(path, method=method, json=payload, headers=headers)
                self.assertEqual(response.status_code, 409, response.json)
                self.assertIn('班级已归档', response.json['error'])
        self.archive(self.a, resources, False)
        self.assertEqual(self.a.post(f'/api/assignments/{resources["aid"]}/answer-card-format',
                                    json={'clear': True}, headers=headers).status_code, 200)

    def test_link_targets_are_authorized_before_either_class_archive_check(self):
        own, other = self.resources(self.a), self.resources(self.b)
        headers = {'X-CSRF-Token': self.token(self.a)}
        for own_archived in (False, True):
            self.archive(self.a, own, own_archived)
            for other_archived in (False, True):
                self.archive(self.b, other, other_archived)
                for path in (f'/api/submissions/{own["sid"]}/link',
                             f'/api/batch-photos/{own["photo"]}/confirm',
                             f'/api/batch-photos/{own["photo"]}/reassign'):
                    with self.subTest(own=own_archived, other=other_archived, path=path):
                        responses = [self.a.post(path, json={'confirmed': True, 'assignment_student_id': rid},
                                                headers=headers) for rid in (other['rid'], other['rid'] + 10000)]
                        self.assertEqual([r.status_code for r in responses], [404, 404])
                        self.assertEqual(responses[0].json, responses[1].json)

    def test_foreign_class_updates_match_missing_classes(self):
        resources = self.resources(self.a)
        headers = {'X-CSRF-Token': self.token(self.b)}
        for archived in (False, True):
            self.archive(self.a, resources, archived)
            responses = [self.b.patch(f'/api/classes/{cid}', json={'archived': False}, headers=headers)
                         for cid in (resources['cid'], resources['cid'] + 10000)]
            self.assertEqual([r.status_code for r in responses], [404, 404])
            self.assertEqual(responses[0].json, responses[1].json)


if __name__ == '__main__':
    unittest.main()
