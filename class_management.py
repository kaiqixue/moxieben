"""Local class rosters and immutable assignment identity snapshots."""
import csv
import io
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import Response, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from openpyxl import load_workbook
from openpyxl import Workbook
from openpyxl.styles import Font
from teacher_workbench import submission_attention, attention_snapshot
import json
import student_id_band


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def migrate(database: Path):
    """Back up a pre-class database once, then migrate it transactionally."""
    database = Path(database)
    with closing(sqlite3.connect(database)) as conn, conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in conn.execute('PRAGMA table_info(submissions)')}
        page_columns = {r[1] for r in conn.execute('PRAGMA table_info(pages)')}
        if 'assignment_students' in tables and 'assignment_student_id' in columns and 'needs_review' in page_columns:
            conn.execute('BEGIN IMMEDIATE')
            migrate_assignment_archive(conn)
            return
        if 'submissions' in tables:
            backup = database.with_name(database.name + '.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            with closing(sqlite3.connect(backup)) as destination:
                conn.backup(destination)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        statements = [
            '''CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, term TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, school_code TEXT UNIQUE,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS class_memberships (
                id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL REFERENCES classes(id),
                student_id INTEGER NOT NULL REFERENCES students(id), student_code TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, joined_at TEXT NOT NULL, left_at TEXT)''',
            '''CREATE UNIQUE INDEX IF NOT EXISTS active_class_code ON class_memberships(class_id,student_code) WHERE active=1''',
            '''CREATE UNIQUE INDEX IF NOT EXISTS active_class_student ON class_memberships(class_id,student_id) WHERE active=1''',
            '''CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                class_id INTEGER NOT NULL REFERENCES classes(id), class_name TEXT NOT NULL,
                term TEXT NOT NULL, created_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, UNIQUE(task_id,class_id))''',
            '''CREATE TABLE IF NOT EXISTS assignment_students (
                id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES assignments(id),
                student_id INTEGER NOT NULL REFERENCES students(id), student_code TEXT NOT NULL,
                name TEXT NOT NULL, exempt INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, UNIQUE(assignment_id,student_id))''',
        ]
        for statement in statements:
            conn.execute(statement)
        migrate_assignment_archive(conn)
        if 'assignment_student_id' not in columns:
            conn.execute('ALTER TABLE submissions ADD COLUMN assignment_student_id INTEGER REFERENCES assignment_students(id)')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_counted_submission ON submissions(assignment_student_id) WHERE assignment_student_id IS NOT NULL')
        if 'needs_review' not in page_columns:
            conn.execute('ALTER TABLE pages ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0')


def migrate_assignment_archive(conn):
    if 'archived' not in {r[1] for r in conn.execute('PRAGMA table_info(assignments)')}:
        conn.execute('ALTER TABLE assignments ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')


def register_class_routes(app, db, body, row, submission_detail, task_detail, submission_summaries):
    from app import fail, integer, text
    tenant_mode = app.config.get('TEACHER_TENANT_MODE', False)

    def boolean(payload, key, default=False):
        value = payload.get(key, default)
        if not isinstance(value, bool):
            fail(f'{key} 必须为布尔值。')
        return value

    def editable_class(conn, cid):
        item = row(conn, 'classes', cid)
        if item['archived']:
            fail('班级已归档，请先恢复班级再操作。', 409)
        return item

    def add_member(conn, cid, payload):
        editable_class(conn, cid)
        code = text(payload.get('student_code'), '班内编号', 80)
        if conn.execute('SELECT id FROM class_memberships WHERE class_id=? AND student_code=? AND active=1', (cid, code)).fetchone():
            fail('班内编号已被使用，请检查名单。', 409)
        sid = payload.get('student_id')
        if sid is not None:
            student = row(conn, 'students', integer(sid, '学生 ID'))
            if not student['active']:
                fail('学生已停用，请先恢复学生档案。', 409)
            if conn.execute('SELECT id FROM class_memberships WHERE class_id=? AND student_id=? AND active=1', (cid, sid)).fetchone():
                fail('该学生已在班级名单中。', 409)
        else:
            name = text(payload.get('name'), '姓名', 120)
            if tenant_mode:
                sid = conn.execute('INSERT INTO students(name,created_at,owner_id) VALUES(?,?,?)',
                                   (name, stamp(), g.teacher_id)).lastrowid
            else:
                sid = conn.execute('INSERT INTO students(name,created_at) VALUES(?,?)',
                                   (name, stamp())).lastrowid
        if tenant_mode:
            mid = conn.execute('INSERT INTO class_memberships(class_id,student_id,student_code,joined_at,owner_id) VALUES(?,?,?,?,?)',
                               (cid, sid, code, stamp(), g.teacher_id)).lastrowid
        else:
            mid = conn.execute('INSERT INTO class_memberships(class_id,student_id,student_code,joined_at) VALUES(?,?,?,?)',
                               (cid, sid, code, stamp())).lastrowid
        return dict(conn.execute('SELECT m.*,s.name FROM class_memberships m JOIN students s ON s.id=m.student_id WHERE m.id=?', (mid,)).fetchone())

    def assignment_detail(conn, aid):
        result = row(conn, 'assignments', aid)
        result['archived'] = bool(result['archived'])
        result['class_archived'] = bool(row(conn, 'classes', result['class_id'])['archived'])
        task = task_detail(conn, result['task_id'],include_submissions=False)
        result.update(task_name=task['name'], total_points=task['total_points'],
                      has_translation=any(q.get('scoring_mode') == 'translation' for q in task['questions']))
        items = []
        counts = {k: 0 for k in ('expected', 'submitted', 'not_submitted', 'ocr_pending', 'review_pending', 'completed', 'exempt',
                               'attention_submissions', 'uncertain_questions', 'teacher_reviewed_questions',
                               'failed_submissions', 'uncertain_submissions', 'processing_submissions', 'ungraded_submissions')}
        final_scores = []
        roster=conn.execute('SELECT a.*,s.id AS submission_id FROM assignment_students a LEFT JOIN submissions s ON s.assignment_student_id=a.id WHERE a.assignment_id=? ORDER BY a.student_code,a.id',(aid,)).fetchall()
        summaries=submission_summaries(conn,[r['submission_id'] for r in roster if r['submission_id']])
        for item in roster:
            item = dict(item)
            item.update(score=None, total_points=task['total_points'], confirmed_count=0, question_count=len(task['questions']), status='pending')
            detail = summaries.get(item['submission_id'])
            pages = detail['pages'] if detail else []
            item['has_ocr_error'] = any(p['status'] == 'error' for p in pages)
            if detail:
                item.update({k: detail[k] for k in ('score', 'confirmed_count', 'question_count', 'status', 'is_final', 'grading_state', 'grading_error', 'grading_source', 'machine_graded_count', 'teacher_reviewed_count', 'uncertain_count', 'recognition')})
            if item['exempt']:
                state = 'exempt'
                item['score'] = None
            elif not pages:
                state = 'not_submitted'
                item['score'] = None
            elif detail['is_final']:
                state = 'completed'
                final_scores.append(detail['score'])
            elif any(p['status'] != 'done' and p.get('needs_review') for p in pages):
                state = 'ocr_pending'
            else:
                state = 'review_pending'
            item['state'] = state
            item['status'] = 'confirmed' if state == 'completed' else 'pending'
            item['exempt'] = bool(item['exempt'])
            counts[state] += 1
            if not item['exempt']:
                counts['expected'] += 1
                counts['submitted'] += bool(pages)
                if detail:
                    attention = submission_attention(detail)
                    counts['attention_submissions'] += attention['attention']
                    counts['uncertain_questions'] += attention['uncertain']
                    counts['teacher_reviewed_questions'] += detail['teacher_reviewed_count']
                    recognition = detail['recognition']
                    counts['failed_submissions'] += recognition['failed']
                    counts['uncertain_submissions'] += recognition['uncertain_count'] > 0
                    counts['processing_submissions'] += recognition['state'] == 'processing'
                    counts['ungraded_submissions'] += recognition['state'] == 'ungraded'
            items.append(item)
        result.update(students=items, counts=counts,
                      final_average=round(sum(final_scores) / len(final_scores), 2) if final_scores else None)
        counts['attention_items'] = attention_snapshot(conn, [aid], summaries.values())['summary']['attention_count']
        result['answer_card'] = answer_card_state(conn, result)
        result.pop('answer_card_identity', None)
        return result

    def answer_card_state(conn, assignment):
        """Teacher-facing card format: saved snapshot, roster suggestion and whether it may change."""
        codes = [r[0] for r in conn.execute('SELECT student_code FROM assignment_students WHERE assignment_id=? ORDER BY id', (assignment['id'],))]
        suggested, problem = student_id_band.suggestion(codes)
        snapshot = student_id_band.snapshot_of(assignment)
        return {'format': snapshot, 'description': student_id_band.describe(snapshot) if snapshot else '',
                'suggestion': suggested, 'problem': problem, 'supported_modes': list(student_id_band.ENTRY_MODES),
                'editable': not assignment.get('class_archived') and not answer_card_used(conn, assignment['id'])}

    def answer_card_used(conn, aid):
        # Any uploaded photo may be printed on the saved format, so the format is frozen from then on.
        return bool(conn.execute('SELECT 1 FROM batch_photos WHERE assignment_id=? LIMIT 1', (aid,)).fetchone()
                    or conn.execute('''SELECT 1 FROM pages p JOIN submissions s ON s.id=p.submission_id
                        JOIN assignment_students a ON a.id=s.assignment_student_id WHERE a.assignment_id=? LIMIT 1''', (aid,)).fetchone())

    @app.post('/api/assignments/<int:aid>/answer-card-format')
    def save_answer_card_format(aid):
        payload = body()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            assignment = row(conn, 'assignments', aid)
            editable_class(conn, assignment['class_id'])
            current = student_id_band.snapshot_of(assignment)
            if payload.get('clear') is True:
                if current and answer_card_used(conn, aid):
                    fail('本次作业已有上传照片，答题卡格式已固定，不能清除；如需新格式请另开作业。', 409)
                conn.execute("UPDATE assignments SET answer_card_identity='{}' WHERE id=?", (aid,))
                return jsonify(answer_card_state(conn, row(conn, 'assignments', aid)))
            codes = [r[0] for r in conn.execute('SELECT student_code FROM assignment_students WHERE assignment_id=?', (aid,))]
            try:
                mode, length, prefix = student_id_band.validate(payload, codes)
            except ValueError as exc:
                fail(str(exc))
            snapshot = student_id_band.build_snapshot(aid, mode, length, prefix)
            if current == snapshot:
                return jsonify(answer_card_state(conn, assignment))
            if current and answer_card_used(conn, aid):
                fail('本次作业已有上传照片，答题卡格式已固定，避免已打印的纸张失配；如需新格式请另开作业。', 409)
            conn.execute('UPDATE assignments SET answer_card_identity=? WHERE id=?',
                         (json.dumps(snapshot, ensure_ascii=False), aid))
            return jsonify(answer_card_state(conn, row(conn, 'assignments', aid)))

    def class_detail(conn, cid):
        result = row(conn, 'classes', cid)
        result['archived'] = bool(result['archived'])
        result['members'] = [dict(m) for m in conn.execute('SELECT m.*,s.name,s.active AS student_active FROM class_memberships m JOIN students s ON s.id=m.student_id WHERE m.class_id=? ORDER BY m.active DESC,m.student_code,m.id', (cid,))]
        result['student_count'] = sum(bool(m['active'] and m['student_active']) for m in result['members'])
        result['assignments'] = []
        for record in conn.execute('SELECT id FROM assignments WHERE class_id=? ORDER BY id DESC', (cid,)).fetchall():
            assignment = assignment_detail(conn, record['id'])
            assignment.pop('students')
            result['assignments'].append(assignment)
        result['pending_count'] = sum(a['counts']['ocr_pending'] + a['counts']['review_pending'] for a in result['assignments'])
        return result

    @app.get('/api/classes')
    def list_classes():
        with db() as conn:
            items = []
            records = (conn.execute('SELECT id FROM classes WHERE owner_id=? ORDER BY archived,id DESC', (g.teacher_id,))
                       if tenant_mode else conn.execute('SELECT id FROM classes ORDER BY archived,id DESC'))
            for record in records.fetchall():
                item = class_detail(conn, record['id'])
                items.append({k: item[k] for k in ('id', 'name', 'term', 'archived', 'student_count', 'pending_count')})
            return jsonify(classes=items)

    @app.post('/api/classes')
    def create_class():
        payload = body()
        name = text(payload.get('name'), '班级名称', 120)
        term = text(payload.get('term', ''), '学年/学期', 120, required=False).strip()
        with db() as conn:
            if tenant_mode:
                cid = conn.execute('INSERT INTO classes(name,term,created_at,owner_id) VALUES(?,?,?,?)',
                                   (name, term, stamp(), g.teacher_id)).lastrowid
            else:
                cid = conn.execute('INSERT INTO classes(name,term,created_at) VALUES(?,?,?)',
                                   (name, term, stamp())).lastrowid
            return jsonify(class_detail(conn, cid)), 201

    @app.get('/api/classes/<int:cid>')
    def get_class(cid):
        with db() as conn:
            return jsonify(class_detail(conn, cid))

    @app.patch('/api/classes/<int:cid>')
    def update_class(cid):
        payload = body()
        with db() as conn:
            item = row(conn, 'classes', cid)
            name = text(payload.get('name', item['name']), '班级名称', 120)
            term = text(payload.get('term', item['term']), '学年/学期', 120, required=False).strip()
            archived = boolean(payload, 'archived', bool(item['archived']))
            if archived and not item['archived']:
                active = conn.execute('''SELECT 1 FROM grading_jobs j WHERE j.state IN ('queued','running') AND
                    (j.assignment_id IN (SELECT id FROM assignments WHERE class_id=?) OR
                     j.submission_id IN (SELECT sub.id FROM submissions sub JOIN assignment_students s
                         ON s.id=sub.assignment_student_id JOIN assignments a ON a.id=s.assignment_id WHERE a.class_id=?))
                    UNION ALL SELECT 1 FROM submissions sub JOIN assignment_students s ON s.id=sub.assignment_student_id
                        JOIN assignments a ON a.id=s.assignment_id LEFT JOIN pages p ON p.submission_id=sub.id
                        WHERE a.class_id=? AND (sub.grading_state='running' OR p.status='running')
                    UNION ALL SELECT 1 FROM batch_photos p JOIN assignments a ON a.id=p.assignment_id
                        WHERE a.class_id=? AND p.status='identifying' LIMIT 1''', (cid,cid,cid,cid)).fetchone()
                if active:
                    fail('班级仍有批改或识别任务处理中，请等待完成后再归档。', 409)
            conn.execute('UPDATE classes SET name=?,term=?,archived=? WHERE id=?', (name, term, archived, cid))
            return jsonify(class_detail(conn, cid))

    @app.post('/api/classes/<int:cid>/members')
    def create_member(cid):
        payload = body()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return jsonify(add_member(conn, cid, payload)), 201

    @app.patch('/api/members/<int:mid>')
    def update_member(mid):
        payload = body()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            member = row(conn, 'class_memberships', mid)
            editable_class(conn, member['class_id'])
            code = text(payload.get('student_code', member['student_code']), '班内编号', 80)
            active = boolean(payload, 'active', bool(member['active']))
            if active and conn.execute('SELECT id FROM class_memberships WHERE class_id=? AND active=1 AND id!=? AND (student_code=? OR student_id=?)', (member['class_id'], mid, code, member['student_id'])).fetchone():
                fail('班内编号或学生已在有效名单中。', 409)
            conn.execute('UPDATE class_memberships SET student_code=?,active=?,left_at=? WHERE id=?', (code, active, None if active else stamp(), mid))
            return jsonify(row(conn, 'class_memberships', mid))

    @app.post('/api/members/<int:mid>/transfer')
    def transfer_member(mid):
        payload = body()
        cid = integer(payload.get('class_id'), '目标班级')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            member = row(conn, 'class_memberships', mid)
            editable_class(conn, member['class_id'])
            if not member['active'] or cid == member['class_id']:
                fail('请选择有效成员及不同的目标班级。')
            result = add_member(conn, cid, {'student_id': member['student_id'], 'student_code': payload.get('student_code')})
            conn.execute('UPDATE class_memberships SET active=0,left_at=? WHERE id=?', (stamp(), mid))
            return jsonify(result)

    def excel_content(raw, selected_sheet):
        # Bound expanded XML too: an XLSX is a ZIP, so upload size alone is insufficient.
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 1000 or sum(item.file_size for item in entries) > 20 * 1024 * 1024:
                    fail('Excel 解压后内容过大，请只保留名单工作表后重试。')
            book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        except HTTPException:
            raise
        except Exception:
            fail('无法读取 Excel：文件可能损坏、加密或并非真正的 .xlsx，请用 Excel 另存为 .xlsx 后重试。')
        try:
            names = [sheet.title for sheet in book.worksheets if sheet.sheet_state == 'visible']
            if not names or (selected_sheet and selected_sheet not in names):
                fail('请选择文件中存在的可见工作表。')
            selected_sheet = selected_sheet or names[0]
            sheet = book[selected_sheet]
            sheet.reset_dimensions()
            stream = io.StringIO()
            writer = csv.writer(stream)
            for line, cells in enumerate(sheet.iter_rows(max_col=51), 1):
                if line > 10002:
                    fail('Excel 行数过多，请去掉多余空行并分批导入。')
                values = []
                for cell in cells:
                    value = cell.value
                    if cell.data_type in ('f', 'e'):
                        fail(f'第 {line} 行：名单含公式或错误单元格，请粘贴为值后重试。')
                    if isinstance(value, bool):
                        fail(f'第 {line} 行：编号或姓名不能是布尔值。')
                    if isinstance(value, (int, float)):
                        if value != int(value) or abs(value) >= 10**15:
                            fail(f'第 {line} 行：长编号或小数请在 Excel 中按文本填写，以免丢失精度。')
                        value = str(int(value))
                        if re.fullmatch(r'0{1,80}', cell.number_format or ''):
                            value = value.zfill(len(cell.number_format))
                    values.append('' if value is None else str(value))
                while values and not values[-1]:
                    values.pop()
                if len(values) > 50:
                    fail('Excel 最多读取 50 列，请只保留名单所需列。')
                writer.writerow(values)
            return stream.getvalue(), {'sheet_names': names, 'sheet_name': selected_sheet}
        except HTTPException:
            raise
        except Exception:
            fail('Excel 工作表无法解析，请另存为 .xlsx 后重试。')
        finally:
            book.close()

    @app.post('/api/classes/<int:cid>/import')
    def import_members(cid):
        sheet_info = {}
        if request.mimetype == 'multipart/form-data':
            incoming = request.files.get('file')
            suffix = Path(incoming.filename or '').suffix.lower() if incoming else ''
            if suffix == '.xls':
                fail('旧版 .xls 请在 Excel 中另存为 .xlsx 后上传。')
            if suffix not in ('.csv', '.xlsx'):
                fail('请上传 Excel（.xlsx）或 CSV 文件，或直接粘贴两列名单。')
            raw = incoming.read(2 * 1024 * 1024 + 1)
            flag = request.form.get('commit', 'false').lower()
            if flag not in ('true', 'false'):
                fail('commit 必须为 true 或 false。')
            commit = flag == 'true'
            header_flag = request.form.get('has_header')
            if header_flag is not None and header_flag.lower() not in ('true', 'false'):
                fail('has_header 必须为 true 或 false。')
            has_header = header_flag.lower() == 'true' if header_flag is not None else None
            mapping = {key: request.form[key] for key in ('code_column', 'name_column') if key in request.form}
            for key in mapping:
                try:
                    mapping[key] = int(mapping[key])
                except ValueError:
                    fail('列编号必须为整数。')
            if len(raw) > 2 * 1024 * 1024:
                fail('名单超过 2MB，请分批导入。')
            if suffix == '.xlsx':
                content, sheet_info = excel_content(raw, request.form.get('sheet_name', ''))
            else:
                content = None
                for encoding in ('utf-8-sig', 'gb18030'):
                    try:
                        content = raw.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                if content is None:
                    fail('名单编码无法读取，请另存为 CSV UTF-8。')
        else:
            payload = body()
            content = text(payload.get('text'), '名单', 2 * 1024 * 1024)
            commit = boolean(payload, 'commit')
            has_header = boolean(payload, 'has_header') if 'has_header' in payload else None
            mapping = {key: payload[key] for key in ('code_column', 'name_column') if key in payload}
            if len(content.encode('utf-8')) > 2 * 1024 * 1024:
                fail('名单超过 2MB，请分批导入。')
        if '\x00' in content:
            fail('名单包含无效字符，请另存为 CSV UTF-8。')
        for value in mapping.values():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 50:
                fail('列编号须为 0—49 的整数。')
        code_col, name_col = mapping.get('code_column', 0), mapping.get('name_column', 1)
        if code_col == name_col:
            fail('编号与姓名不能选择同一列。')
        delimiter = '\t' if not sheet_info and content and '\t' in content.splitlines()[0] else ','
        parsed, errors, column_labels = [], [], []
        columns_resolved = bool(mapping)
        column_detection = 'manual' if mapping else 'default'
        header_detected = False
        try:
            reader = csv.reader(io.StringIO(content), delimiter=delimiter, strict=True)
            first = True
            for cells in reader:
                if not cells or not any(c.strip() for c in cells):
                    continue
                if first:
                    headers = [c.strip().lstrip('\ufeff') for c in cells]
                    column_labels = headers
                    first = False
                    codes = ('编号', '班内编号', '学生编号', '学号', 'student_code')
                    names = ('姓名', '学生姓名', 'name')
                    code_matches = [i for i,c in enumerate(headers) if c in codes]
                    name_matches = [i for i,c in enumerate(headers) if c in names]
                    known_header = bool(code_matches and name_matches)
                    if has_header is True or (has_header is None and known_header):
                        header_detected = True
                        if not mapping and known_header:
                            if len(code_matches) == len(name_matches) == 1:
                                code_col, name_col = code_matches[0], name_matches[0]
                                columns_resolved = True
                                column_detection = 'auto'
                            else:
                                column_detection = 'ambiguous'
                                errors.append(f'第 {reader.line_num} 行：存在多个编号或姓名列，请勾选“手动指定列”后重新预览。')
                        continue
                if len(parsed) >= 1000:
                    fail('每批最多导入 1000 行，请分批处理。')
                line = reader.line_num
                if len(cells) <= max(code_col, name_col) or (not columns_resolved and len(cells) != 2):
                    errors.append(f'第 {line} 行：需要编号、姓名两列；多列名单请指定对应列。')
                    parsed.append({'student_code': '', 'name': '', 'status': 'conflict', 'line': line})
                    continue
                code, name = cells[code_col].strip(), cells[name_col].strip()
                if not code or not name or len(code) > 80 or len(name) > 120 or any(ord(c) < 32 for c in code + name):
                    errors.append(f'第 {line} 行：编号或姓名为空、过长或包含控制字符。')
                parsed.append({'student_code': code, 'name': name, 'status': 'new', 'line': line})
        except csv.Error:
            fail('CSV 格式有误，请检查引号和列分隔符。')
        if not parsed:
            fail('名单中没有学生，请填写编号和姓名。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            editable_class(conn, cid)
            seen = {m['student_code']: m['name'] for m in conn.execute('SELECT m.student_code,s.name FROM class_memberships m JOIN students s ON s.id=m.student_id WHERE m.class_id=? AND m.active=1', (cid,))}
            for item in parsed:
                code, name = item['student_code'], item['name']
                if any(e.startswith(f"第 {item['line']} 行：") for e in errors):
                    item['status'] = 'conflict'
                elif code in seen:
                    item['status'] = 'duplicate' if seen[code] == name else 'conflict'
                    if item['status'] == 'conflict':
                        errors.append(f"第 {item['line']} 行：编号 {code} 已对应其他姓名，未覆盖。")
                else:
                    seen[code] = name
            result = {**sheet_info, 'rows': parsed, 'errors': errors,
                      'columns': column_labels, 'code_column': code_col, 'name_column': name_col,
                      'column_detection': column_detection, 'header_detected': header_detected,
                      'ignored_columns': [label or f'第 {i+1} 列' for i,label in enumerate(column_labels)
                                          if header_detected and columns_resolved and i not in (code_col, name_col)],
                      'new_count': sum(r['status'] == 'new' for r in parsed),
                      'duplicate_count': sum(r['status'] == 'duplicate' for r in parsed)}
            if commit and errors:
                return jsonify(**result, error='名单有冲突，未保存任何学生，请修改后重试。'), 400
            if commit:
                for item in parsed:
                    if item['status'] == 'new':
                        add_member(conn, cid, item)
            result['committed'] = commit
            return jsonify(result)

    @app.get('/api/students')
    def search_students():
        query = text(request.args.get('q', ''), '搜索条件', 120, required=False).strip()
        # Literal substring search: percent/underscore in names are not SQL wildcards.
        with db() as conn:
            if tenant_mode:
                records = conn.execute('''SELECT * FROM students WHERE owner_id=? AND
                    (instr(name,?)>0 OR instr(COALESCE(school_code,''),?)>0 OR id IN
                    (SELECT student_id FROM class_memberships WHERE instr(student_code,?)>0))
                    ORDER BY active DESC,name,id LIMIT 100''', (g.teacher_id, query, query, query))
            else:
                records = conn.execute('SELECT * FROM students WHERE instr(name,?)>0 OR instr(COALESCE(school_code,\'\'),?)>0 OR id IN (SELECT student_id FROM class_memberships WHERE instr(student_code,?)>0) ORDER BY active DESC,name,id LIMIT 100', (query, query, query))
            students = [dict(s) for s in records]
            return jsonify(students=students)

    @app.get('/api/students/<int:sid>')
    def get_student(sid):
        with db() as conn:
            result = row(conn, 'students', sid)
            result['memberships'] = [dict(m) for m in conn.execute('SELECT m.*,c.name AS class_name,c.term,c.archived FROM class_memberships m JOIN classes c ON c.id=m.class_id WHERE m.student_id=? ORDER BY m.id DESC', (sid,))]
            result['submissions'] = []
            records = conn.execute('''SELECT s.id,s.id AS submission_id,roster.id AS assignment_student_id,
                a.id AS assignment_id,a.task_id,a.class_id,a.class_name,a.term,a.created_at,
                roster.name AS student_name,roster.student_code AS snapshot_code
                FROM assignment_students roster JOIN assignments a ON a.id=roster.assignment_id
                LEFT JOIN submissions s ON s.assignment_student_id=roster.id
                WHERE roster.student_id=? ORDER BY a.id DESC''', (sid,)).fetchall()
            assignments = {}
            for record in records:
                aid = record['assignment_id']
                if aid not in assignments:
                    assignments[aid] = assignment_detail(conn, aid)
                assignment = assignments[aid]
                roster = next(s for s in assignment['students'] if s['student_id'] == sid)
                item = submission_detail(conn, record['id']) if record['id'] is not None else {}
                item.update(dict(record))
                item.update(task_name=assignment['task_name'], student_code=record['snapshot_code'])
                item.update({key: roster[key] for key in ('state', 'score', 'status', 'has_ocr_error',
                                                         'total_points', 'confirmed_count', 'question_count', 'exempt', 'reason')})
                result['submissions'].append(item)
            return jsonify(result)

    @app.patch('/api/students/<int:sid>')
    def update_student(sid):
        payload = body()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item = row(conn, 'students', sid)
            name = text(payload.get('name', item['name']), '姓名', 120)
            code = payload.get('school_code', item['school_code'])
            if code is not None:
                code = text(code, '校级学号', 80, required=False).strip() or None
            active = boolean(payload, 'active', bool(item['active']))
            duplicate = (conn.execute('SELECT id FROM students WHERE owner_id=? AND school_code=? AND id!=?',
                                      (g.teacher_id, code, sid)) if tenant_mode and code else
                         conn.execute('SELECT id FROM students WHERE school_code=? AND id!=?', (code, sid)) if code else None)
            if duplicate and duplicate.fetchone():
                fail('校级学号已对应其他学生，请检查后再修改。', 409)
            conn.execute('UPDATE students SET name=?,school_code=?,active=? WHERE id=?', (name, code, active, sid))
            return jsonify(row(conn, 'students', sid))

    def add_assignment_student(conn, aid, student):
        if tenant_mode:
            conn.execute('INSERT OR IGNORE INTO assignment_students(assignment_id,student_id,student_code,name,created_at,owner_id) VALUES(?,?,?,?,?,?)',
                         (aid, student['student_id'], student['student_code'], student['name'], stamp(), g.teacher_id))
        else:
            conn.execute('INSERT OR IGNORE INTO assignment_students(assignment_id,student_id,student_code,name,created_at) VALUES(?,?,?,?,?)',
                         (aid, student['student_id'], student['student_code'], student['name'], stamp()))

    @app.post('/api/tasks/<int:tid>/assignments')
    def create_assignments(tid):
        ids = body().get('class_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            fail('请选择 1—100 个班级。')
        ids = list(dict.fromkeys(integer(cid, '班级 ID') for cid in ids))
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row(conn, 'tasks', tid)
            classes = [editable_class(conn, cid) for cid in ids]
            result = []
            for klass in classes:
                existing = conn.execute('SELECT id FROM assignments WHERE task_id=? AND class_id=?', (tid, klass['id'])).fetchone()
                if existing:
                    aid = existing['id']
                else:
                    if tenant_mode:
                        aid = conn.execute('INSERT INTO assignments(task_id,class_id,class_name,term,created_at,owner_id) VALUES(?,?,?,?,?,?)',
                                           (tid, klass['id'], klass['name'], klass['term'], stamp(), g.teacher_id)).lastrowid
                    else:
                        aid = conn.execute('INSERT INTO assignments(task_id,class_id,class_name,term,created_at) VALUES(?,?,?,?,?)',
                                           (tid, klass['id'], klass['name'], klass['term'], stamp())).lastrowid
                    for student in conn.execute('SELECT m.student_id,m.student_code,s.name FROM class_memberships m JOIN students s ON s.id=m.student_id WHERE m.class_id=? AND m.active=1 AND s.active=1', (klass['id'],)).fetchall():
                        add_assignment_student(conn, aid, student)
                result.append(assignment_detail(conn, aid))
            return jsonify(assignments=result), 201

    @app.get('/api/assignments/<int:aid>')
    def get_assignment(aid):
        with db() as conn:
            return jsonify(assignment_detail(conn, aid))

    @app.patch('/api/assignments/<int:aid>')
    def set_assignment_archive(aid):
        payload = body()
        if 'archived' not in payload:
            fail('请选择归档或恢复作业。')
        archived = boolean(payload, 'archived')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            assignment = row(conn, 'assignments', aid)
            editable_class(conn, assignment['class_id'])
            conn.execute('UPDATE assignments SET archived=? WHERE id=?', (archived, aid))
            return jsonify(id=aid, archived=archived)

    @app.post('/api/assignments/<int:aid>/students')
    def append_assignment_student(aid):
        sid = integer(body().get('student_id'), '学生 ID')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            assignment = row(conn, 'assignments', aid)
            editable_class(conn, assignment['class_id'])
            student = conn.execute('SELECT m.student_id,m.student_code,s.name FROM class_memberships m JOIN students s ON s.id=m.student_id WHERE m.class_id=? AND m.student_id=? AND m.active=1 AND s.active=1', (assignment['class_id'], sid)).fetchone()
            if student is None:
                fail('请先将学生加入本班有效名册。')
            snapshot = student_id_band.snapshot_of(assignment)
            if snapshot and not student_id_band.compatible(snapshot, student['student_code']):
                fail(f"学号 {student['student_code']} 与本次答题卡格式（{student_id_band.describe(snapshot)}）不符，"
                     '不能补入；请另开作业，或由老师人工归属该生照片。', 409)
            add_assignment_student(conn, aid, student)
            return jsonify(assignment_detail(conn, aid))

    @app.patch('/api/assignment-students/<int:rid>')
    def exempt_student(rid):
        payload = body()
        exempt = boolean(payload, 'exempt')
        reason = text(payload.get('reason', ''), '免交原因', 1000, required=False).strip()
        if exempt and not reason:
            fail('标记免交时必须填写原因。')
        with db() as conn:
            row(conn, 'assignment_students', rid)
            conn.execute('UPDATE assignment_students SET exempt=?,reason=? WHERE id=?', (exempt, reason, rid))
            return jsonify(row(conn, 'assignment_students', rid))

    @app.post('/api/assignment-students/<int:rid>/submission')
    def roster_submission(rid):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            roster = row(conn, 'assignment_students', rid)
            found = conn.execute('SELECT id FROM submissions WHERE assignment_student_id=?', (rid,)).fetchone()
            if found:
                return jsonify(id=found['id'])
            if roster['exempt']:
                fail('该学生已免交，请取消免交后再建立答卷。', 409)
            assignment = row(conn, 'assignments', roster['assignment_id'])
            editable_class(conn, assignment['class_id'])
            timestamp = stamp()
            if tenant_mode:
                sid = conn.execute('INSERT INTO submissions(task_id,student_code,created_at,updated_at,assignment_student_id,owner_id) VALUES(?,?,?,?,?,?)',
                                   (assignment['task_id'], roster['student_code'], timestamp, timestamp, rid, g.teacher_id)).lastrowid
            else:
                sid = conn.execute('INSERT INTO submissions(task_id,student_code,created_at,updated_at,assignment_student_id) VALUES(?,?,?,?,?)',
                                   (assignment['task_id'], roster['student_code'], timestamp, timestamp, rid)).lastrowid
            conn.execute('INSERT INTO reviews(submission_id,question_id) SELECT ?,id FROM questions WHERE task_id=?', (sid, assignment['task_id']))
            return jsonify(id=sid), 201

    @app.post('/api/submissions/<int:sid>/link')
    def link_submission(sid):
        rid = integer(body().get('assignment_student_id'), '应交名单 ID')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            submission = row(conn, 'submissions', sid)
            roster = row(conn, 'assignment_students', rid)
            assignment = row(conn, 'assignments', roster['assignment_id'])
            if submission['task_id'] != assignment['task_id']:
                fail('只能关联同一默写任务的答卷。')
            if submission['assignment_student_id'] not in (None, rid):
                fail('答卷已经关联学生，不能覆盖原身份。', 409)
            if conn.execute('SELECT id FROM submissions WHERE assignment_student_id=? AND id!=?', (rid, sid)).fetchone():
                fail('目标学生已有答卷，不能覆盖。', 409)
            conn.execute('UPDATE submissions SET assignment_student_id=?,student_code=?,updated_at=? WHERE id=?', (rid, roster['student_code'], stamp(), sid))
            return jsonify(submission_detail(conn, sid))

    grade_headers = ['学年/学期', '班级', '学生编号', '姓名', '作业状态', '成绩状态',
                     '作业编号', '得分', '满分', '教师已复核题数', '总题数', '免交原因', '批改来源', '机器批改题数', '不确定题数']

    def grade_rows(assignment):
        labels = {'not_submitted': '未交', 'ocr_pending': '待识别', 'review_pending': '待复核',
                  'completed': '已完成', 'exempt': '免交'}
        for item in assignment['students']:
            yield [assignment['term'], assignment['class_name'], item['student_code'], item['name'], labels[item['state']],
                   '最终成绩' if item['state'] == 'completed' else ('暂定成绩' if item['score'] is not None else ''),
                   item['submission_id'], item['score'], item['total_points'], item['confirmed_count'],
                   item['question_count'], item['reason'], item.get('grading_source',''), item.get('machine_graded_count',0), item.get('uncertain_count',0)]

    @app.get('/assignments/<int:aid>/grades.csv')
    def assignment_grades(aid):
        with db() as conn:
            assignment = assignment_detail(conn, aid)
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(grade_headers)
        def safe(value):
            return "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
        for values in grade_rows(assignment):
            writer.writerow([safe(v) for v in values])
        return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="assignment-{aid}-grades.csv"'})

    @app.get('/assignments/<int:aid>/grades.xlsx')
    def assignment_grades_xlsx(aid):
        """Export the same roster rows as CSV, with roster codes stored as text."""
        with db() as conn:
            assignment = assignment_detail(conn, aid)
        book = Workbook()
        sheet = book.active
        sheet.title = '成绩'
        sheet.append(grade_headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for values in grade_rows(assignment):
            sheet.append(values)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        sheet.freeze_panes = 'A2'
        for cell in sheet['C'][1:]:
            cell.number_format = '@'
        for column in sheet.columns:
            letter = column[0].column_letter
            width = min(32, max(10, max(len(str(cell.value or '')) for cell in column) + 2))
            sheet.column_dimensions[letter].width = width
        output = io.BytesIO()
        book.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f'assignment-{aid}-grades.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
