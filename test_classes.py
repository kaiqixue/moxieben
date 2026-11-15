"""Class/roster acceptance checks: python -m unittest -v test_classes."""
from contextlib import closing
import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app


class ClassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'CSRF_TOKEN': 'test'})
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': 'test'}

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def patch(self, path, data):
        return self.client.patch(path, json=data, headers=self.headers)

    def klass(self, name='五年级一班'):
        response = self.post('/api/classes', {'name': name, 'term': '2026 秋'})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json['id']

    def task(self):
        return self.post('/api/tasks', {'name': 'Unit 1', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'receive', 'points': 2}]}).json['id']

    def excel_upload(self, cid, raw, **fields):
        response = self.client.post(f'/api/classes/{cid}/import', data={
            'file': (io.BytesIO(raw), '名单.xlsx'), **fields}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        return response

    def test_named_columns_auto_map_extra_class_column_and_reject_ambiguity(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(['班级', '姓名', '学号'])
        book.active.append(['高二1班', '测试同学甲', 268101])
        book.active.append(['高二1班', '测试同学乙', 268102])
        stream = io.BytesIO()
        book.save(stream)
        cid = self.klass('高二1班')
        preview = self.excel_upload(cid, stream.getvalue())
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json['errors'], [])
        self.assertEqual(preview.json['new_count'], 2)
        self.assertEqual((preview.json['code_column'], preview.json['name_column']), (2, 1))
        self.assertEqual(preview.json['column_detection'], 'auto')
        self.assertEqual(preview.json['ignored_columns'], ['班级'])
        self.assertEqual(preview.json['rows'][0]['student_code'], '268101')
        self.assertEqual(self.client.get(f'/api/classes/{cid}').json['members'], [])
        self.assertEqual(self.excel_upload(cid, stream.getvalue(), commit='true').status_code, 200)
        self.assertEqual(len(self.client.get(f'/api/classes/{cid}').json['members']), 2)
        path = f'/api/classes/{cid}/import'
        for header in ['编号,姓名,学号', '编号,姓名,学生姓名']:
            result = self.post(path, {'text': header+'\n009,同学,010', 'commit': False})
            self.assertTrue(result.json['errors'], 'Ambiguous header must require explicit mapping.')
        mapped = self.post(path, {'text': '编号,姓名,学号\n009,同学,010',
            'code_column': 2, 'name_column': 1, 'commit': False})
        self.assertEqual(mapped.json['errors'], [])
        self.assertEqual(mapped.json['rows'][0]['student_code'], '010')
        no_header = self.post(path, {'text': '高二1班,同学,009', 'commit': False})
        self.assertTrue(no_header.json['errors'], 'No silent guessing without a header.')

    def test_excel_preview_commit_sheets_and_leading_zero_codes(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.title = '说明'
        book.active.sheet_state = 'hidden'
        roster = book.create_sheet('一班')
        roster.append(['班内编号', '姓名'])
        roster.append(['001', '张同学'])
        roster.append([2, '李同学'])
        roster['A3'].number_format = '000'
        other = book.create_sheet('二班')
        other.append(['姓名', '备注', '编号'])
        other.append(['王同学', '测试', '007'])
        stream = io.BytesIO()
        book.save(stream)
        raw = stream.getvalue()
        cid = self.klass()
        preview = self.excel_upload(cid, raw)
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertEqual(preview.json['sheet_names'], ['一班', '二班'])
        self.assertEqual(preview.json['sheet_name'], '一班')
        self.assertEqual([r['student_code'] for r in preview.json['rows']], ['001', '002'])
        self.assertEqual(self.client.get(f'/api/classes/{cid}').json['members'], [])
        committed = self.excel_upload(cid, raw, commit='true')
        self.assertEqual(committed.json['new_count'], 2)
        self.assertEqual(self.excel_upload(cid, raw, commit='true').json['duplicate_count'], 2)
        mapped = self.excel_upload(cid, raw, sheet_name='二班', code_column='2', name_column='0')
        self.assertEqual(mapped.json['rows'][0]['student_code'], '007')
        self.assertEqual(mapped.json['rows'][0]['name'], '王同学')
        for sheet in ['不存在', '说明']:
            self.assertEqual(self.excel_upload(cid, raw, sheet_name=sheet).status_code, 400)

    def test_excel_errors_are_atomic_and_never_evaluate_formulas(self):
        from openpyxl import Workbook
        cid = self.klass()
        for bad_code, expected in [('=1+1', '公式'), (1234567890123456, '文本'), (True, '编号')]:
            book = Workbook()
            book.active.append(['编号', '姓名'])
            book.active.append(['001', '张同学'])
            book.active.append([bad_code, '李同学'])
            stream = io.BytesIO()
            book.save(stream)
            result = self.excel_upload(cid, stream.getvalue(), commit='true')
            self.assertEqual(result.status_code, 400)
            self.assertIn(expected, result.json['error'])
            self.assertEqual(self.client.get(f'/api/classes/{cid}').json['members'], [])
        book = Workbook()
        book.active.append(['编号', '姓名'])
        for n in range(1001):
            book.active.append([str(n), '同学'])
        stream = io.BytesIO()
        book.save(stream)
        self.assertEqual(self.excel_upload(cid, stream.getvalue()).status_code, 400)
        for raw in [b'not excel', b'x' * (2*1024*1024+1)]:
            self.assertEqual(self.excel_upload(cid, raw).status_code, 400)
        import zipfile
        expanded = io.BytesIO()
        with zipfile.ZipFile(expanded, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('oversized.xml', b'x' * (20*1024*1024+1))
        result = self.excel_upload(cid, expanded.getvalue(), commit='true')
        self.assertEqual(result.status_code, 400)
        self.assertIn('解压', result.json['error'])
        response = self.client.post(f'/api/classes/{cid}/import', data={
            'file': (io.BytesIO(b'legacy workbook'), '名单.xls')}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code, 400)
        self.assertIn('另存为 .xlsx', response.json['error'])

    def test_atomic_import_duplicate_conflict_and_identity_isolation(self):
        a, b = self.klass(), self.klass('五年级二班')
        path = f'/api/classes/{a}/import'
        content = '编号\t姓名\n001\t张同学\n002\t李同学'
        preview = self.post(path, {'text': content, 'commit': False})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json['new_count'], 2)
        self.assertEqual(self.client.get(f'/api/classes/{a}').json['members'], [])
        self.assertEqual(self.post(path, {'text': content, 'commit': True}).status_code, 200)
        self.assertEqual(self.post(path, {'text': content, 'commit': True}).json['duplicate_count'], 2)
        self.assertEqual(self.post(path, {'text': '003\t新同学\n001\t不同名字', 'commit': True}).status_code, 400)
        members = self.client.get(f'/api/classes/{a}').json['members']
        self.assertEqual(len(members), 2)
        self.assertEqual(members[0]['student_code'], '001')
        other = self.post(f'/api/classes/{b}/members', {'student_code': '001', 'name': '张同学'}).json
        self.assertNotEqual(other['student_id'], members[0]['student_id'])
        # CSV encodings and headers are read as text, preserving leading zeroes.
        response = self.client.post(f'/api/classes/{b}/import', data={
            'file': (io.BytesIO('编号,姓名\n009,王同学'.encode('gb18030')), '名单.csv'),
            'commit': 'true'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['rows'][0]['student_code'], '009')

    def test_assignment_snapshots_transfer_reuse_and_legacy_link(self):
        a, b, tid = self.klass(), self.klass('二班'), self.task()
        member = self.post(f'/api/classes/{a}/members', {'student_code': '01', 'name': '原名'}).json
        aid = self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [a]}).json['assignments'][0]['id']
        item = self.client.get(f'/api/assignments/{aid}').json['students'][0]
        first = self.post(f"/api/assignment-students/{item['id']}/submission", {}).json['id']
        self.assertEqual(self.post(f"/api/assignment-students/{item['id']}/submission", {}).json['id'], first)
        self.assertEqual(self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [a]}).json['assignments'][0]['id'], aid)
        self.assertEqual(self.post(f"/api/members/{member['id']}/transfer", {'class_id': b, 'student_code': '007'}).status_code, 200)
        self.assertEqual(self.patch(f"/api/students/{member['student_id']}", {'name': '新名'}).status_code, 200)
        old = self.client.get(f'/api/assignments/{aid}').json
        self.assertEqual(old['students'][0]['name'], '原名')
        self.assertEqual(old['students'][0]['student_code'], '01')
        self.assertEqual(old['counts']['not_submitted'], 1)
        history = self.client.get(f"/api/students/{member['student_id']}").json
        self.assertEqual(len(history['memberships']), 2)
        self.assertEqual(history['submissions'][0]['class_name'], '五年级一班')
        self.assertEqual(history['submissions'][0]['state'], 'not_submitted')
        legacy = self.post(f'/api/tasks/{tid}/submissions', {'student_code': 'legacy'}).json['id']
        self.assertEqual(self.post(f'/api/submissions/{legacy}/link', {'assignment_student_id': item['id']}).status_code, 409)

    def test_column_mapping_validation_and_transfer_rollback(self):
        a, b = self.klass(), self.klass('二班')
        path = f'/api/classes/{a}/import'
        preview = self.post(path, {'text': '姓名,备注,编号\n张同学,测试,001', 'code_column': 2, 'name_column': 0, 'commit': False})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json['rows'][0]['student_code'], '001')
        arbitrary = self.post(path, {'text': '显示名,其他,座号\n张同学,测试,001', 'code_column': 2, 'name_column': 0, 'has_header': True, 'commit': False})
        self.assertEqual(arbitrary.status_code, 200)
        self.assertEqual(arbitrary.json['new_count'], 1)
        self.assertEqual(arbitrary.json['rows'][0]['student_code'], '001')
        self.assertEqual(self.post(path, {'text': '001\t张同学\n002\t', 'commit': True}).status_code, 400)
        self.assertEqual(self.client.get(f'/api/classes/{a}').json['student_count'], 0)
        member = self.post(f'/api/classes/{a}/members', {'student_code': '001', 'name': '张同学'}).json
        self.post(f'/api/classes/{b}/members', {'student_code': '001', 'name': '其他同学'})
        self.assertEqual(self.post(f"/api/members/{member['id']}/transfer", {'class_id': b, 'student_code': '001'}).status_code, 409)
        self.assertEqual(self.client.get(f'/api/classes/{a}').json['members'][0]['active'], 1)
        for payload in [{'text': '001\t张同学', 'commit': 'yes'}, {'text': '001\t张同学', 'code_column': True}]:
            self.assertEqual(self.post(path, payload).status_code, 400)

    def test_states_csv_formula_safety_and_restart(self):
        cid, tid = self.klass('=班级'), self.task()
        for code, name in [('001', '=危险'), ('002', '未交'), ('003', '免交')]:
            self.post(f'/api/classes/{cid}/members', {'student_code': code, 'name': name})
        aid = self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [cid]}).json['assignments'][0]['id']
        students = self.client.get(f'/api/assignments/{aid}').json['students']
        self.assertEqual(self.patch(f"/api/assignment-students/{students[2]['id']}", {'exempt': True, 'reason': ''}).status_code, 400)
        self.patch(f"/api/assignment-students/{students[2]['id']}", {'exempt': True, 'reason': '病假'})
        sid = self.post(f"/api/assignment-students/{students[0]['id']}/submission", {}).json['id']
        path = Path(self.tmp.name) / 'dictation.sqlite3'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("INSERT INTO pages(submission_id,original,image,width,height,status,created_at) VALUES(?, 'x','x',10,10,'done','now')", (sid,))
            conn.execute("UPDATE reviews SET transcript='receive',confirmed=1 WHERE submission_id=?", (sid,))
        result = self.client.get(f'/api/assignments/{aid}').json
        self.assertEqual(result['counts']['submitted'], 1)
        self.assertEqual(result['counts']['expected'], 2)
        self.assertEqual(result['counts']['completed'], 1)
        self.assertEqual(result['final_average'], 2)
        self.patch(f"/api/assignment-students/{students[0]['id']}", {'exempt': True, 'reason': '补记免交'})
        exempted = self.client.get(f'/api/assignments/{aid}').json['students'][0]
        self.assertEqual(exempted['state'], 'exempt')
        self.assertIsNone(exempted['score'])
        self.assertEqual(exempted['submission_id'], sid)
        self.patch(f"/api/assignment-students/{students[0]['id']}", {'exempt': False})
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('UPDATE pages SET needs_review=1 WHERE submission_id=?', (sid,))
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['review_pending'], 1)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE pages SET status='error' WHERE submission_id=?", (sid,))
        self.assertTrue(self.client.get(f'/api/assignments/{aid}').json['students'][0]['has_ocr_error'])
        export = self.client.get(f'/assignments/{aid}/grades.csv').get_data(as_text=True)
        self.assertIn("'=危险", export)
        records = list(csv.DictReader(io.StringIO(export.lstrip('\ufeff'))))
        self.assertEqual(next(r for r in records if r['学生编号'] == '002')['得分'], '')
        other = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name}).test_client()
        self.assertEqual(len(other.get(f'/api/assignments/{aid}').json['students']), 3)

    def test_two_classes_late_enrollment_and_legacy_link_keep_snapshots(self):
        a, b, tid = self.klass(), self.klass('二班'), self.task()
        ma = self.post(f'/api/classes/{a}/members', {'student_code': '01', 'name': '同名'}).json
        mb = self.post(f'/api/classes/{b}/members', {'student_code': '01', 'name': '同名'}).json
        assigned = self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [a, b]}).json['assignments']
        aid, bid = assigned[0]['id'], assigned[1]['id']
        self.assertNotEqual(assigned[0]['students'][0]['student_id'], assigned[1]['students'][0]['student_id'])
        history = self.client.get(f"/api/students/{ma['student_id']}").json['submissions']
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]['id'])
        self.assertIsNone(history[0]['submission_id'])
        self.assertEqual(history[0]['assignment_id'], aid)
        self.assertEqual(history[0]['state'], 'not_submitted')
        rid_b = assigned[1]['students'][0]['id']
        self.patch(f'/api/assignment-students/{rid_b}', {'exempt': True, 'reason': '病假'})
        exempt_history = self.client.get(f"/api/students/{mb['student_id']}").json['submissions']
        self.assertEqual(exempt_history[0]['state'], 'exempt')
        self.assertIsNone(exempt_history[0]['score'])
        self.assertIsNone(exempt_history[0]['submission_id'])
        self.patch(f'/api/assignment-students/{rid_b}', {'exempt': False})
        late = self.post(f'/api/classes/{a}/members', {'student_code': '02', 'name': '新加入'}).json
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['expected'], 1)
        self.assertEqual(self.post(f'/api/assignments/{aid}/students', {'student_id': late['student_id']}).status_code, 200)
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['expected'], 2)
        self.assertEqual(self.client.get(f'/api/assignments/{bid}').json['counts']['expected'], 1)
        rid = assigned[0]['students'][0]['id']
        legacy = self.post(f'/api/tasks/{tid}/submissions', {'student_code': 'old'}).json['id']
        linked = self.post(f'/api/submissions/{legacy}/link', {'assignment_student_id': rid})
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(linked.json['student_code'], '01')
        self.assertEqual(self.post(f'/api/submissions/{legacy}/link', {'assignment_student_id': rid}).status_code, 200)
        self.patch(f'/api/classes/{a}', {'name': '新班名', 'term': '新学期'})
        self.patch(f"/api/members/{ma['id']}", {'student_code': '099'})
        old = self.client.get(f'/api/assignments/{aid}').json
        self.assertEqual(old['class_name'], '五年级一班')
        self.assertEqual(old['students'][0]['student_code'], '01')
        self.patch(f"/api/students/{ma['student_id']}", {'school_code': 'S001'})
        self.assertEqual(self.patch(f"/api/students/{mb['student_id']}", {'school_code': 'S001'}).status_code, 409)
        self.patch(f'/api/classes/{b}', {'archived': True})
        self.assertEqual(self.post(f'/api/classes/{b}/members', {'student_code': '03', 'name': '新学生'}).status_code, 409)
        self.assertEqual(self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [b]}).status_code, 409)

    def test_import_boundaries_and_explicit_existing_student(self):
        a, b = self.klass(), self.klass('二班')
        path = f'/api/classes/{a}/import'
        for content in ['', '编号,姓名', '001,', '001,张\n001,李']:
            self.assertEqual(self.post(path, {'text': content, 'commit': True}).status_code, 400)
        excessive = '\n'.join(f'{n:04d}\t同学' for n in range(1001))
        self.assertEqual(self.post(path, {'text': excessive, 'commit': False}).status_code, 400)
        for raw, filename in [(b'bad', 'x.xlsx'), (b'\xff', 'x.csv'), (b'x' * (2 * 1024 * 1024 + 1), 'x.csv')]:
            response = self.client.post(path, data={'file': (io.BytesIO(raw), filename)}, headers=self.headers)
            try:
                self.assertEqual(response.status_code, 400)
            finally:
                response.request.environ['wsgi.input'].close()
                response.close()
        member = self.post(f'/api/classes/{a}/members', {'student_code': '01', 'name': '同学'}).json
        existing = self.post(f'/api/classes/{b}/members', {'student_code': '09', 'student_id': member['student_id']})
        self.assertEqual(existing.status_code, 201)
        self.assertEqual(existing.json['student_id'], member['student_id'])
        self.assertEqual(len(self.client.get('/api/students?q=同学').json['students']), 1)
        self.assertEqual(self.post(f'/api/classes/{b}/members', {'student_code': '10', 'student_id': member['student_id']}).status_code, 409)

    def test_migration_backup_and_legacy_records(self):
        from class_management import migrate
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript('CREATE TABLE tasks(id INTEGER PRIMARY KEY); CREATE TABLE submissions(id INTEGER PRIMARY KEY, task_id INTEGER); CREATE TABLE pages(id INTEGER PRIMARY KEY); INSERT INTO submissions VALUES(7, 1);')
            migrate(path)
            backups = list(Path(folder).glob('*.bak*'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as conn, conn:
                self.assertNotIn('assignment_student_id', [r[1] for r in conn.execute('PRAGMA table_info(submissions)')])
            migrate(path)
            self.assertEqual(len(list(Path(folder).glob('*.bak*'))), 1)
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute('SELECT id FROM submissions').fetchone()[0], 7)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
