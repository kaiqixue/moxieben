"""Navigation snapshots and archive protection use temporary SQLite only."""
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_teacher_workbench as fixtures


class TeacherNavigationTests(unittest.TestCase):
    setUp = fixtures.TeacherWorkbenchTests.setUp
    post = fixtures.TeacherWorkbenchTests.post
    conn = fixtures.TeacherWorkbenchTests.conn
    page = fixtures.TeacherWorkbenchTests.page

    def listing(self):
        response = self.client.get('/api/workbench/assignments')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return {a['id']: a for a in response.json['assignments']}

    def photo(self, status='identified', **values):
        with self.conn() as conn, conn:
            return conn.execute('''INSERT INTO batch_photos
                (assignment_id,original,image,width,height,status,created_at,updated_at,
                 suggested_student_id,match_details) VALUES(?, 'x','x',1,1,?,'now','now',?,?)''',
                (values.get('assignment_id', self.assignment['id']), status, values.get('suggested_student_id'),
                 json.dumps({'reason': 'private reason', 'raw': 'large OCR payload'}))).lastrowid

    def archive(self):
        response = self.client.patch(f"/api/classes/{self.assignment['class_id']}",
            json={'archived': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def test_single_assignment_archive_preserves_records_and_other_classes(self):
        aid = self.assignment['id']
        sid, page_id = self.page()
        cid = self.post('/api/classes', {'name': '另一个班'}).json['id']
        sibling = self.post(f"/api/tasks/{self.task['id']}/assignments", {'class_ids': [cid]}).json['assignments'][0]
        endpoint = f'/api/assignments/{aid}'
        self.assertEqual(self.client.patch(endpoint, json={'archived': 'true'}, headers=self.headers).status_code, 400)
        detail_paths = [f'{endpoint}/analysis', f'/api/submissions/{sid}', f'/api/submissions/{sid}/history']
        before = {path: self.client.get(path).json for path in detail_paths}
        self.assertTrue(all(value is not None for value in before.values()))
        before[detail_paths[0]]['assignment'].pop('archived')
        before[detail_paths[0]].pop('generated_at')
        for archived in (True, True, False):
            response = self.client.patch(endpoint, json={'archived': archived}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertIs(response.json['archived'], archived)
            listing = self.listing()
            self.assertIs(listing[aid]['archived'], archived)
            self.assertFalse(listing[aid]['class_archived'])
            self.assertFalse(listing[sibling['id']]['archived'])
            grades = self.client.get(endpoint).json
            self.assertIs(grades['archived'], archived)
            self.assertEqual(grades['students'][0]['submission_id'], sid)
            for path, original in before.items():
                current = self.client.get(path).json
                if path.endswith('/analysis'):
                    self.assertEqual(current['assignment'].pop('archived'), int(archived))
                    current.pop('generated_at')
                self.assertEqual(current, original)
            self.assertEqual(self.client.get(f'/api/pages/{page_id}/preview').status_code, 200)

    def test_grouped_summary_matches_queue_with_processing_exempt_and_multiple_assignments(self):
        failed, pid = self.page(0)
        pending, _ = self.page(1)
        processing, _ = self.page(2)
        exempt, _ = self.page(3)
        for _ in range(2):
            self.photo()
        self.photo(suggested_student_id=self.assignment['students'][3]['id'])
        processing_photo = self.photo()
        with self.conn() as conn, conn:
            conn.execute("UPDATE pages SET status='done',needs_review=0")
            conn.execute("UPDATE submissions SET grading_state='failed' WHERE id=?", (failed,))
            conn.execute("UPDATE submissions SET grading_state='running' WHERE id=?", (processing,))
            conn.execute('UPDATE assignment_students SET exempt=1 WHERE id=?', (self.assignment['students'][3]['id'],))
            conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id IN (?,?)',
                (json.dumps({'uncertain': True}), failed, processing))
            conn.execute('''INSERT INTO grading_jobs(assignment_id,state,stage,photo_ids,created_at,updated_at)
                VALUES(?,'queued','identifying',?,'now','now')''',
                (self.assignment['id'], json.dumps([processing_photo])))
        other_class = self.post('/api/classes', {'name': '二班'}).json['id']
        self.post(f'/api/classes/{other_class}/members', {'student_code': '01', 'name': '另一位'})
        other = self.post(f"/api/tasks/{self.task['id']}/assignments", {'class_ids': [other_class]}).json['assignments'][0]
        result = self.listing()
        summary = result[self.assignment['id']]
        queue = self.client.get(f"/api/assignments/{self.assignment['id']}/attention").json
        self.assertEqual(summary['attention_items'], len(queue['items']))
        self.assertEqual(summary['attention_items'], 5)
        self.assertEqual(summary['uncertain_questions'], queue['summary']['uncertain_questions'])
        self.assertIn(summary['first_uncertain'], [{k:i[k] for k in ('submission_id','question_id')} for i in queue['items'] if i['kind']=='question'])
        self.assertEqual((summary['expected'], summary['submitted'], summary['completed']), (3, 3, 0))
        self.assertEqual((summary['attention_submissions'], summary['pending_identity_photos']), (2, 2))
        self.assertEqual((summary['processing'], summary['failed'], summary['ungraded']), (2, 1, 1))
        self.assertEqual(summary['status'], 'ok')
        self.assertEqual(result[other['id']]['expected'], 1)
        self.assertEqual(result[other['id']]['attention_items'], 0)
        detail = self.client.get(f"/api/assignments/{self.assignment['id']}").json
        self.assertEqual(detail['counts']['attention_items'], summary['attention_items'])

    def test_summary_is_slim_batched_and_read_only(self):
        self.page()
        self.photo()
        cid = self.post('/api/classes', {'name': '另一个班'}).json['id']
        self.post(f'/api/classes/{cid}/members', {'student_code': '01', 'name': '另一位'})
        other = self.post(f"/api/tasks/{self.task['id']}/assignments", {'class_ids': [cid]}).json['assignments'][0]
        self.post(f"/api/assignment-students/{other['students'][0]['id']}/submission", {})
        with self.conn() as conn:
            before = list(conn.iterdump())
        queries = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(queries.append)
            return conn
        with patch('settings.recognize_selected') as ocr, patch('sqlite3.connect', side_effect=traced):
            summary = self.listing()[self.assignment['id']]
            ocr.assert_not_called()
        sql = [' '.join(q.split()).lower() for q in queries]
        self.assertEqual(sum(q.startswith('select * from submissions where id in') for q in sql), 1)
        self.assertFalse(any(q.startswith(('insert ', 'update ', 'delete ', 'replace ')) for q in sql))
        self.assertFalse(any('select p.*' in q or 'select * from assignment_students' in q or
                             'select id,success,result from ocr_runs' in q or 'p.match_details' in q for q in sql))
        self.assertEqual(set(summary), {'id','class_id','class_name','class_archived','archived','task_id','task_name','term',
            'created_at','expected','submitted','completed','attention_items','attention_submissions',
            'pending_identity_photos','processing','failed','ungraded','uncertain_questions','first_uncertain','status'})
        with self.conn() as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_bad_assignment_is_explicitly_unavailable_and_database_failure_is_http_error(self):
        other_class = self.post('/api/classes', {'name': 'healthy class'}).json['id']
        other = self.post(f"/api/tasks/{self.task['id']}/assignments", {'class_ids': [other_class]}).json['assignments'][0]
        sid, _ = self.page()
        with self.conn() as conn, conn:
            conn.execute("UPDATE reviews SET auto_result='not json' WHERE submission_id=?", (sid,))
        listing = self.listing()
        result = listing[self.assignment['id']]
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['attention_items'])
        self.assertEqual(listing[other['id']]['status'], 'ok')
        with patch('sqlite3.connect', side_effect=sqlite3.OperationalError('database unavailable')), self.assertLogs(self.app.logger, level='ERROR'):
            self.assertEqual(self.client.get('/api/workbench/assignments').status_code, 500)

    def test_archived_sources_reject_all_existing_mutation_routes_before_ocr_or_enqueue(self):
        sid, pid = self.page()
        photo = self.photo()
        qid = self.task['questions'][0]['id']
        rid = self.assignment['students'][0]['id']
        with self.conn() as conn, conn:
            jid = conn.execute('''INSERT INTO grading_jobs(submission_id,state,stage,photo_ids,created_at,updated_at)
                VALUES(?,'interrupted','grading','[]','now','now')''', (sid,)).lastrowid
        anonymous = self.post(f"/api/tasks/{self.task['id']}/submissions", {'student_code': 'anonymous'}).json['id']
        self.archive()
        with self.conn() as conn:
            before = list(conn.iterdump())
        routes = [
            ('POST', f'/api/submissions/{sid}/pages', {}),
            ('POST', f'/api/pages/{pid}/reviewed', {'confirmed': True}),
            ('POST', f'/api/pages/{pid}/ocr', {}),
            ('POST', f'/api/submissions/{sid}/reviews/{qid}', {'transcript': 'receive', 'confirmed': True}),
            ('POST', f'/api/submissions/{sid}/reviews/{qid}/undo', {}),
            ('POST', f'/api/submissions/{sid}/extract-answers', {}),
            ('POST', f'/api/submissions/{sid}/auto-grade', {'reextract': True}),
            ('POST', f'/api/submissions/{sid}/auto-grade', {'retry_missing': True}),
            ('POST', f'/api/submissions/{sid}/link', {'assignment_student_id': rid}),
            ('POST', f'/api/submissions/{anonymous}/link', {'assignment_student_id': rid}),
            ('POST', f'/api/assignments/{self.assignment["id"]}/students', {'student_id': 1}),
            ('PATCH', f'/api/assignment-students/{rid}', {'exempt': True, 'reason': 'leave'}),
            ('POST', f'/api/assignment-students/{rid}/submission', {}),
            ('POST', f'/api/assignments/{self.assignment["id"]}/photos', {}),
            ('POST', f'/api/assignments/{self.assignment["id"]}/photos/match', {}),
            ('POST', f'/api/batch-photos/{photo}/exclude', {'excluded': True}),
            ('POST', f'/api/batch-photos/{photo}/identify', {}),
            ('POST', f'/api/batch-photos/{photo}/confirm', {'confirmed': True, 'assignment_student_id': rid}),
            ('POST', f'/api/batch-photos/{photo}/reassign', {'confirmed': True, 'assignment_student_id': rid}),
            ('POST', f'/api/batch-photos/{photo}/process', {}),
            ('POST', f'/api/assignments/{self.assignment["id"]}/grading-jobs', {}),
            ('POST', f'/api/submissions/{sid}/grading-jobs', {}),
            ('POST', f'/api/grading-jobs/{jid}/resume', {}),
            ('DELETE', f'/api/tasks/{self.task["id"]}', {'confirmed': True}),
        ]
        runner = self.app.extensions['grading_jobs']
        with patch('settings.recognize_selected') as ocr, patch.object(runner, 'enqueue') as enqueue, patch.object(runner, 'kick') as kick:
            for method, path, payload in routes:
                with self.subTest(path=path):
                    response = self.client.open(path, method=method, json=payload, headers=self.headers)
                    self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
            ocr.assert_not_called()
            enqueue.assert_not_called()
            kick.assert_not_called()
        with self.conn() as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertTrue(self.listing()[self.assignment['id']]['class_archived'])
        self.assertTrue(self.client.get(f'/api/submissions/{sid}').json['identity']['class_archived'])
        self.assertTrue(self.client.get(f"/api/assignments/{self.assignment['id']}").json['class_archived'])
        self.assertTrue(self.client.get(f"/api/assignments/{self.assignment['id']}/analysis").json['assignment']['class_archived'])
        self.assertEqual(self.post(f"/api/tasks/{self.task['id']}/submissions", {'student_code': 'allowed anonymous'}).status_code, 201)
        self.assertEqual(self.client.patch(f"/api/classes/{self.assignment['class_id']}",
            json={'archived': False}, headers=self.headers).status_code, 200)

    def test_online_pages_use_spa_and_download_keeps_selfcontained_report(self):
        aid = self.assignment['id']
        index = self.client.get('/').data
        self.assertEqual(self.client.get('/tasks').data, index)
        self.assertEqual(self.client.get(f'/assignments/{aid}/analysis').data, index)
        download = self.client.get(f'/assignments/{aid}/analysis/download')
        self.assertEqual(download.status_code, 200)
        self.assertIn('attachment;', download.headers['Content-Disposition'])
        self.assertIn('离线快照', download.get_data(as_text=True))
        self.assertEqual(self.client.get(f'/api/assignments/{aid}/analysis').status_code, 200)

    def test_archive_and_assignment_write_cannot_cross_the_guard_transaction_window(self):
        _, pid = self.page()
        entered, release, archived = threading.Event(), threading.Event(), threading.Event()
        responses = {}
        view = self.app.view_functions['acknowledge_page']
        def paused_view(**args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test write was not released')
            return view(**args)
        def write():
            with self.app.test_client() as client:
                responses['write'] = client.post(f'/api/pages/{pid}/reviewed',
                    json={'confirmed': True}, headers=self.headers)
        def archive():
            try:
                with self.app.test_client() as client:
                    responses['archive'] = client.patch(f"/api/classes/{self.assignment['class_id']}",
                        json={'archived': True}, headers=self.headers)
            finally:
                archived.set()
        writer, archiver = threading.Thread(target=write), threading.Thread(target=archive)
        with patch.dict(self.app.view_functions, acknowledge_page=paused_view):
            writer.start()
            self.assertTrue(entered.wait(2))
            archiver.start()
            try:
                self.assertFalse(archived.wait(.15), 'Archive must wait until the checked write finishes')
            finally:
                release.set()
                writer.join(4)
                archiver.join(4)
        self.assertEqual(responses['write'].status_code, 200)
        self.assertEqual(responses['archive'].status_code, 200)
        self.assertFalse(responses['write'].json['identity']['class_archived'])
        self.assertEqual(self.post(f'/api/pages/{pid}/reviewed', {'confirmed': True}).status_code, 409)

    def test_archive_waits_for_background_and_direct_processing_to_finish(self):
        sid, pid = self.page()
        photo = self.photo()
        runner = self.app.extensions['grading_jobs']
        job = runner.enqueue(submission_id=sid)
        url = f"/api/classes/{self.assignment['class_id']}"
        with patch('settings.recognize_selected') as ocr:
            for state in ('queued', 'running'):
                with self.conn() as conn, conn:
                    conn.execute('UPDATE grading_jobs SET state=? WHERE id=?', (state, job['id']))
                response = self.client.patch(url, json={'archived': True}, headers=self.headers)
                self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
            with self.conn() as conn, conn:
                conn.execute("UPDATE grading_jobs SET state='done'")
            for table, field, value, rid in (('submissions', 'grading_state', 'running', sid),
                    ('pages', 'status', 'running', pid), ('batch_photos', 'status', 'identifying', photo)):
                with self.conn() as conn, conn:
                    previous = conn.execute(f'SELECT {field} FROM {table} WHERE id=?', (rid,)).fetchone()[0]
                    conn.execute(f'UPDATE {table} SET {field}=? WHERE id=?', (value, rid))
                response = self.client.patch(url, json={'archived': True}, headers=self.headers)
                self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
                with self.conn() as conn, conn:
                    conn.execute(f'UPDATE {table} SET {field}=? WHERE id=?', (previous, rid))
            self.assertEqual(self.client.patch(url, json={'archived': True}, headers=self.headers).status_code, 200)
            self.assertFalse(runner.run_next())
            ocr.assert_not_called()

    def test_worker_skips_legacy_archived_jobs_without_writes_and_runs_other_classes(self):
        sid, _ = self.page()
        runner = self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        self.photo()
        runner.enqueue(assignment_id=self.assignment['id'])
        with self.conn() as conn, conn:
            # Simulate a queued job persisted by an older version that allowed archiving it.
            conn.execute('UPDATE classes SET archived=1 WHERE id=?', (self.assignment['class_id'],))
            before = list(conn.iterdump())
        with patch('settings.recognize_selected') as ocr, patch.object(runner, 'grade') as grade, patch.object(runner, 'identify') as identify:
            self.assertFalse(runner.run_next())
            grade.assert_not_called()
            identify.assert_not_called()
            ocr.assert_not_called()
        with self.conn() as conn:
            self.assertEqual(list(conn.iterdump()), before)
        cid = self.post('/api/classes', {'name': 'active class'}).json['id']
        other = self.post(f"/api/tasks/{self.task['id']}/assignments", {'class_ids': [cid]}).json['assignments'][0]
        self.photo(assignment_id=other['id'])
        runner.enqueue(assignment_id=other['id'])
        with patch.object(runner, 'identify') as identify:
            self.assertTrue(runner.run_next())
            self.assertEqual(identify.call_args[0][0]['assignment_id'], other['id'])

    def test_anonymous_page_write_serializes_with_identity_link_before_archive(self):
        sid, pid = self.page()
        rid = self.assignment['students'][0]['id']
        with self.conn() as conn, conn:
            conn.execute('UPDATE submissions SET assignment_student_id=NULL WHERE id=?', (sid,))
        entered, release, linked = threading.Event(), threading.Event(), threading.Event()
        responses = {}
        view = self.app.view_functions['acknowledge_page']
        def paused(**args):
            entered.set()
            release.wait(3)
            return view(**args)
        def write():
            with self.app.test_client() as client:
                responses['write'] = client.post(f'/api/pages/{pid}/reviewed', json={'confirmed': True}, headers=self.headers)
        def link_and_archive():
            with self.app.test_client() as client:
                responses['link'] = client.post(f'/api/submissions/{sid}/link', json={'assignment_student_id': rid}, headers=self.headers)
                linked.set()
                responses['archive'] = client.patch(f"/api/classes/{self.assignment['class_id']}", json={'archived': True}, headers=self.headers)
        writer, linker = threading.Thread(target=write), threading.Thread(target=link_and_archive)
        with patch.dict(self.app.view_functions, acknowledge_page=paused):
            writer.start()
            self.assertTrue(entered.wait(2))
            linker.start()
            try:
                self.assertFalse(linked.wait(.15), 'Link must wait for the anonymous answer write')
            finally:
                release.set()
                writer.join(4)
                linker.join(4)
        self.assertEqual(responses['write'].status_code, 200)
        self.assertIsNone(responses['write'].json['identity'])
        self.assertEqual(responses['link'].status_code, 200)
        self.assertEqual(responses['archive'].status_code, 200)


if __name__ == '__main__':
    unittest.main()

