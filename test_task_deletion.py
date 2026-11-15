"""Task deletion checks use temporary data only; no cloud requests."""
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_batch
import test_classes


class TaskDeletionTests(unittest.TestCase):
    setUp = test_classes.ClassTests.setUp
    post = test_classes.ClassTests.post
    task = test_classes.ClassTests.task
    klass = test_classes.ClassTests.klass
    setup_assignment = test_batch.BatchTests.setup_assignment
    upload_batch = test_batch.BatchTests.upload_batch

    def connect(self):
        return closing(sqlite3.connect(Path(self.tmp.name) / 'dictation.sqlite3'))

    def preview(self, tid):
        response = self.client.get(f'/api/tasks/{tid}/deletion-preview')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def delete(self, tid, preview=None):
        return self.client.delete(f'/api/tasks/{tid}', json={
            'confirmed': True, 'expected_counts': (preview or self.preview(tid))['counts']}, headers=self.headers)

    def test_confirmation_preview_and_empty_task_backup(self):
        tid, other = self.task(), self.task()
        preview = self.preview(tid)
        self.assertEqual(preview['counts']['photos'], 0)
        self.assertEqual(self.client.delete(f'/api/tasks/{tid}', json={}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.delete(f'/api/tasks/{tid}', json={'confirmed': True}).status_code, 403)
        result = self.delete(tid, preview)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.client.get(f'/api/tasks/{tid}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/tasks/{other}').status_code, 200)
        backups = list((Path(self.tmp.name) / 'backups').glob('task-delete-*/*sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM tasks WHERE id=?', (tid,)).fetchone())
        self.assertEqual(self.client.get('/api/tasks/999/deletion-preview').status_code, 404)

    def test_linked_and_anonymous_data_deleted_but_roster_and_memory_retained(self):
        aid, students = self.setup_assignment()
        with self.connect() as conn:
            tid, cid = conn.execute('SELECT task_id,class_id FROM assignments WHERE id=?', (aid,)).fetchone()
        photo = self.upload_batch(aid)
        linked = self.post(f"/api/batch-photos/{photo['id']}/confirm", {
            'assignment_student_id': students[0]['id'], 'confirmed': True}).json
        self.post(f'/api/tasks/{tid}/submissions', {'student_code': 'anonymous'})
        sub = self.client.get(f"/api/submissions/{linked['submission_id']}").json
        qid = sub['reviews'][0]['question_id']
        self.post(f"/api/submissions/{sub['id']}/reviews/{qid}", {'transcript': 'receive', 'confirmed': True, 'regions': []})
        with self.connect() as conn, conn:
            from name_memory import remember
            student_id = conn.execute('SELECT student_id FROM assignment_students WHERE id=?', (students[0]['id'],)).fetchone()[0]
            remember(conn, cid, student_id, '张同雪', photo['id'])
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)', (linked['page_id'], 'now', '{}'))
            files = set(conn.execute('SELECT original,image FROM pages').fetchone())
        preview = self.preview(tid)
        self.assertEqual(preview['counts']['photos'], 1)
        self.assertEqual(preview['counts']['submissions'], 2)
        result = self.delete(tid, preview)
        self.assertEqual(result.status_code, 200, result.json)
        with self.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            for table in ['tasks','questions','assignments','assignment_students','submissions','pages','ocr_runs','reviews','review_events','batch_photos']:
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)
            self.assertEqual(conn.execute('SELECT count(*) FROM students').fetchone()[0], 3)
            memory = conn.execute('SELECT enabled,source_photo_ids,history FROM identity_memories').fetchone()
            self.assertEqual(memory[:2], (1, '[]'))
            self.assertEqual(json.loads(memory[2])[-1]['action'], 'source_task_deleted')
        for name in files:
            self.assertFalse((Path(self.tmp.name) / 'uploads' / name).exists())
            self.assertTrue(list((Path(self.tmp.name) / 'backups').glob(f'task-delete-*/uploads/{name}')))

    def test_changed_counts_and_active_processing_block_deletion(self):
        aid, students = self.setup_assignment()
        with self.connect() as conn:
            tid = conn.execute('SELECT task_id FROM assignments WHERE id=?', (aid,)).fetchone()[0]
        old = self.preview(tid)
        photo = self.upload_batch(aid)
        self.assertEqual(self.delete(tid, old).status_code, 409)
        linked = self.post(f"/api/batch-photos/{photo['id']}/confirm", {
            'assignment_student_id': students[0]['id'], 'confirmed': True}).json
        for table, column, ident, status, reset in [
            ('batch_photos','status',photo['id'],'identifying','linked'),
            ('pages','status',linked['page_id'],'running','uploaded'),
            ('submissions','grading_state',linked['submission_id'],'running','idle')]:
            with self.subTest(table=table):
                with self.connect() as conn, conn:
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (status, ident))
                self.assertFalse(self.preview(tid)['can_delete'])
                self.assertEqual(self.delete(tid).status_code, 409)
                with self.connect() as conn, conn:
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (reset, ident))
        self.assertFalse((Path(self.tmp.name) / 'backups').exists())

    def test_backup_failure_and_unsafe_path_preserve_data(self):
        aid, _ = self.setup_assignment()
        photo = self.upload_batch(aid)
        with self.connect() as conn:
            tid = conn.execute('SELECT task_id FROM assignments WHERE id=?', (aid,)).fetchone()[0]
        with patch('task_deletion.shutil.copy2', side_effect=OSError('copy failed')):
            self.assertEqual(self.delete(tid).status_code, 503)
        self.assertEqual(self.client.get(f'/api/tasks/{tid}').status_code, 200)
        outside = Path(self.tmp.name) / 'keep.txt'
        outside.write_text('keep')
        with self.connect() as conn, conn:
            conn.execute('UPDATE batch_photos SET original=? WHERE id=?', ('../keep.txt', photo['id']))
        self.assertEqual(self.delete(tid).status_code, 409)
        self.assertEqual(outside.read_text(), 'keep')

    def test_active_job_blocks_and_shared_images_are_preserved(self):
        aid, _ = self.setup_assignment()
        photo = self.upload_batch(aid)
        with self.connect() as conn, conn:
            tid = conn.execute('SELECT task_id FROM assignments WHERE id=?', (aid,)).fetchone()[0]
            conn.execute('''INSERT INTO grading_jobs(assignment_id,state,stage,photo_ids,created_at,updated_at)
                VALUES(?,'queued','identifying','[]','now','now')''', (aid,))
        self.assertEqual(self.delete(tid).status_code, 409)
        with self.connect() as conn, conn:
            conn.execute("UPDATE grading_jobs SET state='done'")
        other = self.task()
        sid = self.post(f'/api/tasks/{other}/submissions', {'student_code': 'other'}).json['id']
        with self.connect() as conn, conn:
            original, image = conn.execute('SELECT original,image FROM batch_photos WHERE id=?', (photo['id'],)).fetchone()
            conn.execute('''INSERT INTO pages(submission_id,original,image,width,height,created_at)
                VALUES(?,?,?,640,240,'now')''', (sid, original, image))
        self.assertEqual(self.delete(tid).status_code, 200)
        for name in {original, image}:
            self.assertTrue((Path(self.tmp.name) / 'uploads' / name).exists())
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM grading_jobs').fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
