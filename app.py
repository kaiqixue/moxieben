"""Teacher-owned local dictation workflow. Start with: python app.py"""
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Flask, abort, g, jsonify, render_template, request, send_file, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException

from grading import compare, normalize
from ocr_engine import recognize, status as ocr_status
from class_management import migrate, register_class_routes
from batch_photos import migrate as migrate_batch_photos, register_batch_routes
from settings import migrate as migrate_settings, register_settings_routes
from word_import import migrate_word_fields, parse_answer_word, parse_word, WORD_LIMIT
from answer_extraction import grammar_candidates, card_candidates, number_occurrences, reading_order
import automatic_grading
import grading_jobs
import teacher_workbench
import task_deletion
import class_analysis
import student_id_band

ROOT = Path(__file__).resolve().parent
IMAGE_LIMIT = 10 * 1024 * 1024
PIXEL_LIMIT = 25_000_000
RULES = {'version': 'strict-v1', 'description': '仅合并连续空白；大小写、标点、拼写、词形和语序严格核对；每题全对得满分。'}
CLASS_WRITE_LOCKS = {}
SUBMISSION_WRITE_LOCKS = {}
NUMBER = re.compile(r'^\s*[（(]?\s*(\d{1,3})\s*[.．、)）:：]\s*(.*)$', re.DOTALL)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def fail(message, code=400):
    abort(code, description=message)


def number(value, minimum=0, maximum=1000):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail('分值必须是数字。')
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite or not minimum <= value <= maximum or abs(value * 100 - round(value * 100)) > 1e-7:
        fail('分值须在允许范围内，最多保留两位小数。')
    return round(value, 2)


def integer(value, label='编号'):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(f'{label}必须为正整数。')
    return value


def text(value, label, limit=4000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        fail(f'{label}不能为空且不能超过 {limit} 个字符。' if required else f'{label}格式错误或过长。')
    return value.strip() if required else value


def upload_answer_layout():
    layout = request.form.get('answer_layout', 'inline')
    if layout not in ('inline', 'separate'):
        fail('请选择原文作答或独立答题卡。')
    return layout


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATA_DIR=str(ROOT / 'data'), CSRF_TOKEN=secrets.token_hex(32),
                      MAX_CONTENT_LENGTH=IMAGE_LIMIT + 256 * 1024)
    if config:
        app.config.update(config)
    data_dir = Path(app.config['DATA_DIR'])
    uploads = data_dir / 'uploads'
    uploads.mkdir(parents=True, exist_ok=True)
    database = data_dir / 'dictation.sqlite3'

    @contextmanager
    def db():
        conn = sqlite3.connect(database, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, rules TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
            number INTEGER NOT NULL, kind TEXT NOT NULL, answer TEXT NOT NULL,
            variants TEXT NOT NULL, points REAL NOT NULL, UNIQUE(task_id, number));
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
            student_code TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL REFERENCES submissions(id),
            original TEXT NOT NULL, image TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'uploaded', error TEXT NOT NULL DEFAULT '',
            blocks TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ocr_runs (
            id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id),
            created_at TEXT NOT NULL, success INTEGER NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews (
            submission_id INTEGER NOT NULL REFERENCES submissions(id),
            question_id INTEGER NOT NULL REFERENCES questions(id), machine_text TEXT NOT NULL DEFAULT '',
            transcript TEXT NOT NULL DEFAULT '', regions TEXT NOT NULL DEFAULT '[]',
            confirmed INTEGER NOT NULL DEFAULT 0, confirmed_blank INTEGER NOT NULL DEFAULT 0,
            score_override REAL, reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(submission_id, question_id));
        CREATE TABLE IF NOT EXISTS review_events (
            id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL REFERENCES submissions(id),
            question_id INTEGER NOT NULL REFERENCES questions(id), created_at TEXT NOT NULL,
            snapshot TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS visual_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            revision TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS visual_suggestions_question
            ON visual_suggestions(submission_id, question_id, id);
        CREATE TABLE IF NOT EXISTS automatic_visual_attempts (
            submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            PRIMARY KEY(submission_id, question_id, page_id));
        ''')
        conn.execute("UPDATE pages SET status='error', error='上次识别被中断，请点击重新识别。' WHERE status='running'")

    migrate(database)
    student_id_band.migrate(database)
    migrate_word_fields(database)
    migrate_batch_photos(database)
    migrate_settings(database)
    automatic_grading.migrate(database)

    @app.before_request
    def local_only():
        if request.host.split(':')[0] not in ('localhost', '127.0.0.1'):
            fail('此网站仅供本机访问。', 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                fail('请求来源不匹配，请从本机网站操作。', 403)
            supplied = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(supplied, app.config['CSRF_TOKEN']):
                fail('页面验证已失效，请刷新页面后重试。', 403)

    @app.before_request
    def archived_assignment_read_only():
        """Resolve only assignment-owned writes, before uploads, OCR or queued work."""
        if request.method not in ('POST', 'PATCH', 'PUT', 'DELETE') or not request.url_rule:
            return
        rule = request.url_rule.rule
        args = request.view_args or {}
        sources = {
            '/api/assignments/': ('SELECT id FROM assignments WHERE id=?', 'aid'),
            '/api/assignment-students/': ('SELECT assignment_id FROM assignment_students WHERE id=?', 'rid'),
            '/api/submissions/': ('''SELECT a.assignment_id FROM submissions s
                JOIN assignment_students a ON a.id=s.assignment_student_id WHERE s.id=?''', 'sid'),
            '/api/pages/': ('''SELECT a.assignment_id FROM pages p JOIN submissions s ON s.id=p.submission_id
                JOIN assignment_students a ON a.id=s.assignment_student_id WHERE p.id=?''', 'pid'),
            '/api/batch-photos/': ('SELECT assignment_id FROM batch_photos WHERE id=?', 'pid'),
            '/api/grading-jobs/': ('''SELECT assignment_id FROM grading_jobs WHERE id=?
                UNION SELECT a.assignment_id FROM grading_jobs j JOIN submissions s ON
                (s.id=j.submission_id OR EXISTS(SELECT 1 FROM json_each(j.grade_ids) WHERE value=s.id))
                JOIN assignment_students a ON a.id=s.assignment_student_id WHERE j.id=?
                UNION SELECT p.assignment_id FROM grading_jobs j JOIN json_each(j.photo_ids) ids
                JOIN batch_photos p ON p.id=ids.value WHERE j.id=?''', 'jid'),
        }
        source = next((value for prefix, value in sources.items() if rule.startswith(prefix)), None)
        deleting_task = request.method == 'DELETE' and request.endpoint == 'delete_task'
        updating_class = request.endpoint == 'update_class'
        if source is None and not deleting_task and not updating_class:
            return
        g.archive_write_locks = []
        # Identity can change from anonymous to linked; lock the stable answer before resolving its class.
        sid = args.get('sid') if rule.startswith('/api/submissions/') else None
        if rule.startswith('/api/pages/'):
            with db() as conn:
                page = conn.execute('SELECT submission_id FROM pages WHERE id=?', (args['pid'],)).fetchone()
                sid = page[0] if page else None
        if sid:
            lock = SUBMISSION_WRITE_LOCKS.setdefault((str(database.resolve()), sid), threading.RLock())
            lock.acquire()
            g.archive_write_locks.append(lock)
        with db() as conn:
            conn.execute('BEGIN')
            aids = set()
            if source:
                query, key = source
                aids.update(r[0] for r in conn.execute(query, (args[key],) * query.count('?')) if r[0])
            if deleting_task:
                aids.update(r[0] for r in conn.execute('SELECT id FROM assignments WHERE task_id=?', (args['tid'],)))
            if request.endpoint in ('link_submission', 'confirm_batch_photo', 'reassign_photo'):
                payload = request.get_json(silent=True)
                rid = payload.get('assignment_student_id') if isinstance(payload, dict) else None
                if type(rid) is int and rid > 0:
                    aids.update(r[0] for r in conn.execute('SELECT assignment_id FROM assignment_students WHERE id=?', (rid,)))
            cids = {r[0] for r in conn.execute(f"SELECT class_id FROM assignments WHERE id IN ({','.join('?' for _ in aids)})", list(aids))} if aids else set()
            if updating_class:
                cids.add(args['cid'])
        # ponytail: per-class locks cover this local process; multiple server processes need database write guards.
        for cid in sorted(cids):
            lock = CLASS_WRITE_LOCKS.setdefault((str(database.resolve()), cid), threading.RLock())
            lock.acquire()
            g.archive_write_locks.append(lock)
        with db() as conn:
            if aids and conn.execute(f'''SELECT 1 FROM assignments a JOIN classes c ON c.id=a.class_id
                    WHERE a.id IN ({','.join('?' for _ in aids)}) AND c.archived=1 LIMIT 1''', list(aids)).fetchone():
                fail('班级已归档，请先恢复班级再操作。', 409)

    @app.teardown_request
    def release_archive_write_locks(error=None):
        for lock in reversed(g.pop('archive_write_locks', [])):
            lock.release()

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {404: '没有找到这条记录。', 413: '照片超过 10MB，请缩小后重新上传。',
                    415: '请使用 JSON 请求。', 400: '请求格式不正确。'}
        message = exc.description if exc.code not in (404, 413, 415) else messages[exc.code]
        if request.path.startswith('/api/'):
            return jsonify(error=message), exc.code
        return render_template('print.html', mode='error', error=message), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        app.logger.exception('Unexpected request error')
        if request.path.startswith('/api/'):
            return jsonify(error='操作失败，已有记录已保留。请重试；详情可查看终端日志。'), 500
        return render_template('print.html', mode='error', error='操作失败，请重试并查看终端日志。'), 500

    def body():
        payload = request.get_json()
        if not isinstance(payload, dict):
            fail('请求必须是 JSON 对象。')
        return payload

    def row(conn, table, record_id):
        # table names are internal constants, never request data.
        found = conn.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone()
        if found is None:
            fail('没有找到这条记录。', 404)
        return dict(found)

    def questions(conn, task_id):
        result = [dict(q) for q in conn.execute('SELECT * FROM questions WHERE task_id=? ORDER BY number', (task_id,))]
        for q in result:
            q['variants'] = json.loads(q['variants'])
        return result

    def page_dict(page, conn):
        result = dict(page)
        result['url'] = f"/api/pages/{result['id']}/image"
        result['blocks'] = json.loads(result['blocks'])
        result.update(teacher_workbench.preview_info(conn, result['id']))
        result.pop('original', None)
        result.pop('image', None)
        return result

    def submission_detail(conn, sid):
        result = row(conn, 'submissions', sid)
        task = row(conn, 'tasks', result['task_id'])
        result.update(task_name=task['name'], rules=json.loads(task['rules']))
        result['identity'] = None
        if result.get('assignment_student_id'):
            identity = conn.execute('''SELECT a.id AS assignment_id,a.class_id,a.class_name,a.term,c.archived AS class_archived,
                s.student_id,s.student_code,s.name,s.exempt FROM assignment_students s
                JOIN assignments a ON a.id=s.assignment_id JOIN classes c ON c.id=a.class_id WHERE s.id=?''', (result['assignment_student_id'],)).fetchone()
            result['identity'] = dict(identity)
            result['identity']['class_archived'] = bool(result['identity']['class_archived'])
        result['pages'] = [page_dict(p, conn) for p in conn.execute('SELECT * FROM pages WHERE submission_id=? ORDER BY id', (sid,))]
        saved = {r['question_id']: dict(r) for r in conn.execute('SELECT * FROM reviews WHERE submission_id=?', (sid,))}
        result['history_count'] = conn.execute('SELECT COUNT(*) FROM review_events WHERE submission_id=?', (sid,)).fetchone()[0]
        job = grading_jobs.active_submission_job(conn, sid, ('queued','running','interrupted'))
        result['processing_job'] = grading_jobs.GradingJobs.dto(job) if job else None
        result = finish_submission(result,saved,questions(conn,result['task_id']))
        records = conn.execute('''SELECT v.* FROM visual_suggestions v WHERE v.id IN (
            SELECT MAX(id) FROM visual_suggestions WHERE submission_id=? GROUP BY question_id)''', (sid,)).fetchall()
        if records:
            revisions = review_page_revision(conn, sid)
            result['visual_suggestions'] = {}
            for record in records:
                q = next(q for q in result['reviews'] if q['question_id'] == record['question_id'])
                if record['revision'] == visual_revision(result, q, revisions):
                    result['visual_suggestions'][q['question_id']] = visual_suggestion_dto(record)
        return result

    def finish_submission(result, saved, question_rows):
        """One scoring/state calculation shared by full reviews and list summaries."""
        reviews = []
        for question in question_rows:
            q=dict(question)
            review = dict(saved[q['id']])
            review['question_id'] = q.pop('id')
            review.update(q)
            review['regions'] = json.loads(review['regions'])
            review['confirmed'] = bool(review['confirmed'])
            review['confirmed_blank'] = bool(review['confirmed_blank'])
            review['auto_result'] = json.loads(review['auto_result']) or None
            review['teacher_edited'] = bool(review['teacher_edited'])
            review['graded'] = bool(review['auto_result']) or review['confirmed']
            review['uncertain'] = bool((review['auto_result'] or {}).get('uncertain')) and not review['confirmed']
            review.update(compare(review['transcript'], review['answer'], review['variants']))
            review['score'] = review['score_override'] if review['score_override'] is not None else (review['points'] if review['matched'] and not review['confirmed_blank'] else 0)
            review['needs_review'] = not review['graded'] or review['uncertain']
            reviews.append(review)
        result['reviews'] = reviews
        result['score'] = round(sum(r['score'] for r in reviews), 2)
        result['total_points'] = round(sum(r['points'] for r in reviews), 2)
        result['confirmed_count'] = sum(r['confirmed'] for r in reviews)
        result['question_count'] = len(reviews)
        manual_final = all(r['confirmed'] for r in reviews) and not any(p['needs_review'] for p in result['pages'])
        result['is_final'] = (result['grading_state'] == 'final' or (result['grading_state'] == 'idle' and manual_final))
        result['status'] = 'confirmed' if result['is_final'] else 'pending'
        result['machine_graded_count'] = sum(bool(r['auto_result']) for r in reviews)
        result['teacher_reviewed_count'] = result['confirmed_count']
        result['uncertain_count'] = sum(r['uncertain'] for r in reviews)
        result['correct_count'] = sum(r['graded'] and r['score'] == r['points'] for r in reviews)
        result['wrong_count'] = sum(r['graded'] and r['score'] < r['points'] for r in reviews)
        result['grading_source'] = ('机器批改／教师改判' if result['confirmed_count'] else '机器自动批改') if result['machine_graded_count'] else ('教师复核' if result['confirmed_count'] else '未批改')
        result['recognition'] = teacher_workbench.recognition_status(result)
        return result

    def submission_summaries(conn, ids):
        """Batch list data without OCR blocks, image orientation or recognition histories."""
        ids=list(dict.fromkeys(ids))
        if not ids:
            return {}
        marks=','.join('?' for _ in ids)
        results={r['id']:dict(r,identity=None,pages=[],processing_job=None) for r in conn.execute(f'SELECT * FROM submissions WHERE id IN ({marks})',ids)}
        for r in conn.execute(f'''SELECT sub.id,a.id AS assignment_id,a.class_id,a.class_name,a.term,c.archived AS class_archived,
                s.student_id,s.student_code,s.name,s.exempt FROM submissions sub
                JOIN assignment_students s ON s.id=sub.assignment_student_id JOIN assignments a ON a.id=s.assignment_id
                JOIN classes c ON c.id=a.class_id
                WHERE sub.id IN ({marks})''',ids):
            identity=dict(r);sid=identity.pop('id');results[sid]['identity']=identity
            identity['class_archived']=bool(identity['class_archived'])
        pages=[dict(r) for r in conn.execute(f'SELECT id,submission_id,status,error,needs_review FROM pages WHERE submission_id IN ({marks}) ORDER BY id',ids)]
        reviewed=teacher_workbench.reviewed_page_ids(conn,[p['id'] for p in pages if p['error']])
        for p in pages:
            p['manually_reviewed']=p['id'] in reviewed
            results[p['submission_id']]['pages'].append(p)
        saved={sid:{} for sid in ids}
        for r in conn.execute(f'''SELECT submission_id,question_id,transcript,confirmed,confirmed_blank,score_override,teacher_edited,teacher_decision,
                '[]' AS regions,CASE WHEN NOT json_valid(auto_result) THEN auto_result WHEN auto_result='{{}}' THEN '{{}}' ELSE json_object(
                'uncertain',COALESCE(json_extract(auto_result,'$.uncertain'),0),'reason',json_extract(auto_result,'$.reason')) END AS auto_result
                FROM reviews WHERE submission_id IN ({marks})''',ids):
            saved[r['submission_id']][r['question_id']]=dict(r)
        tids=list({r['task_id'] for r in results.values()})
        question_rows={tid:[] for tid in tids}
        for r in conn.execute(f"SELECT * FROM questions WHERE task_id IN ({','.join('?' for _ in tids)}) ORDER BY number",tids):
            q=dict(r);q['variants']=json.loads(q['variants']);question_rows[q['task_id']].append(q)
        for r in conn.execute(f'''SELECT s.id AS sid,j.* FROM submissions s JOIN grading_jobs j
                ON j.state IN ('queued','running','interrupted') AND (j.submission_id=s.id
                OR EXISTS(SELECT 1 FROM json_each(j.grade_ids) WHERE value=s.id)
                OR EXISTS(SELECT 1 FROM json_each(j.photo_ids) ids JOIN batch_photos p ON p.id=ids.value WHERE p.submission_id=s.id))
                WHERE s.id IN ({marks}) ORDER BY j.id DESC''',ids):
            if results[r['sid']]['processing_job'] is None:
                results[r['sid']]['processing_job']=grading_jobs.GradingJobs.dto(r)
        return {sid:finish_submission(result,saved[sid],question_rows[result['task_id']]) for sid,result in results.items()}

    def task_detail(conn, tid, include_submissions=True):
        task = row(conn, 'tasks', tid)
        task['rules'] = json.loads(task['rules'])
        task['questions'] = questions(conn, tid)
        task['total_points'] = round(sum(q['points'] for q in task['questions']), 2)
        task['assignments'] = [dict(a) for a in conn.execute('SELECT * FROM assignments WHERE task_id=? ORDER BY id DESC', (tid,))]
        task['submissions'] = []
        ids=[r[0] for r in conn.execute('SELECT id FROM submissions WHERE task_id=? ORDER BY id DESC',(tid,))] if include_submissions else []
        summaries=submission_summaries(conn,ids)
        for sid in ids:
            item = summaries[sid]
            task['submissions'].append({k: item[k] for k in ('id', 'student_code', 'identity', 'score', 'total_points', 'confirmed_count', 'question_count', 'status', 'created_at', 'is_final', 'grading_state', 'grading_error', 'grading_source', 'machine_graded_count', 'teacher_reviewed_count', 'uncertain_count', 'recognition')})
        task.update(teacher_workbench.task_counts(task['submissions']))
        task['pending_identity_photos'] = len(teacher_workbench.pending_identity_photos(conn, [a['id'] for a in task['assignments']]))
        task['background_jobs'] = conn.execute('''SELECT COUNT(*) FROM grading_jobs WHERE state IN ('queued','running')
            AND (assignment_id IN (SELECT id FROM assignments WHERE task_id=?)
            OR submission_id IN (SELECT id FROM submissions WHERE task_id=?))''', (tid,tid)).fetchone()[0]
        return task

    @app.get('/')
    @app.get('/tasks')
    @app.get('/tasks/new')
    @app.get('/tasks/<int:record_id>')
    @app.get('/submissions/<int:record_id>')
    @app.get('/classes')
    @app.get('/classes/<int:record_id>')
    @app.get('/assignments')
    @app.get('/assignments/<int:record_id>')
    @app.get('/assignments/<int:record_id>/analysis')
    @app.get('/settings')
    @app.get('/students/<int:record_id>')
    def index(record_id=None):
        return render_template('index.html', csrf_token=app.config['CSRF_TOKEN'])

    @app.get('/api/health')
    def health():
        return jsonify(ocr=ocr_status(), storage='sqlite', rules=RULES)

    @app.get('/api/tasks')
    def list_tasks():
        with db() as conn:
            result = []
            for record in conn.execute('SELECT id FROM tasks ORDER BY id DESC').fetchall():
                item = task_detail(conn, record['id'])
                result.append({'id': item['id'], 'name': item['name'], 'created_at': item['created_at'],
                    'question_count': len(item['questions']), 'total_points': item['total_points'],
                    'submission_count': len(item['submissions']),
                    'pending_count': sum(s['status'] == 'pending' for s in item['submissions']),
                    'pending_identity_photos': item['pending_identity_photos'],
                    'background_jobs': item['background_jobs'],
                    **teacher_workbench.task_counts(item['submissions'])})
        return jsonify(tasks=result)

    @app.post('/api/tasks/import-word')
    def import_word():
        incoming = request.files.get('file')
        if not incoming or Path(incoming.filename or '').suffix.lower() != '.docx':
            fail('请选择可编辑的 Word（.docx）试题，旧版 .doc 请先另存为 .docx。')
        try:
            parser = parse_answer_word if request.form.get('mode') == 'answers' else parse_word
            result = parser(incoming.read(WORD_LIMIT+1), Path(incoming.filename).name)
        except ValueError as exc:
            fail(str(exc))
        return jsonify(result)

    @app.post('/api/tasks')
    def new_task():
        payload = body()
        name = text(payload.get('name'), '任务名称', 120)
        passage = text(payload.get('passage', ''), '原文题干', 200000, required=False)
        instructions = text(payload.get('instructions', ''), '试题说明', 4000, required=False)
        source_name = text(payload.get('source_name', ''), '来源文件名', 240, required=False)
        items = payload.get('questions')
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            fail('每份任务须包含 1—200 道题。')
        validated, seen = [], set()
        for q in items:
            if not isinstance(q, dict):
                fail('题目格式不正确。')
            n = integer(q.get('number'), '题号')
            if n in seen or n > 999:
                fail('题号不能重复，且须在 1—999 之间。')
            seen.add(n)
            if q.get('kind') not in ('word', 'phrase', 'sentence', 'grammar'):
                fail('请选择单词、短语、句子或语法填空。')
            variants = q.get('variants', [])
            if not isinstance(variants, list) or len(variants) > 20:
                fail('每题最多设置 20 个可接受变体。')
            validated.append((n, q['kind'], text(q.get('answer'), '标准答案'),
                              dumps([text(v, '可接受变体') for v in variants]), number(q.get('points'), 0.01),
                              text(q.get('context', ''), '题目上下文', 10000, required=False),
                              text(q.get('hint', ''), '提示词', 200, required=False)))
        with db() as conn:
            tid = conn.execute('INSERT INTO tasks(name,created_at,rules,passage,instructions,source_name) VALUES(?,?,?,?,?,?)',
                               (name, now(), dumps(RULES), passage, instructions, source_name)).lastrowid
            conn.executemany('INSERT INTO questions(task_id,number,kind,answer,variants,points,context,hint) VALUES(?,?,?,?,?,?,?,?)', [(tid, *q) for q in validated])
            result = task_detail(conn, tid)
        return jsonify(result), 201

    @app.get('/api/tasks/<int:tid>')
    def get_task(tid):
        with db() as conn:
            return jsonify(task_detail(conn, tid))

    @app.post('/api/tasks/<int:tid>/submissions')
    def new_submission(tid):
        code = text(body().get('student_code'), '学生编号', 80)
        with db() as conn:
            row(conn, 'tasks', tid)
            stamp = now()
            sid = conn.execute('INSERT INTO submissions(task_id,student_code,created_at,updated_at) VALUES(?,?,?,?)', (tid, code, stamp, stamp)).lastrowid
            conn.execute('INSERT INTO reviews(submission_id,question_id) SELECT ?,id FROM questions WHERE task_id=?', (sid, tid))
        return jsonify(id=sid), 201

    @app.get('/api/submissions/<int:sid>')
    def get_submission(sid):
        with db() as conn:
            return jsonify(submission_detail(conn, sid))

    def decode_photo(raw, filename):
        if Path(filename or '').suffix.lower() not in ('.jpg', '.jpeg', '.png'):
            fail('仅支持 JPG、JPEG 和 PNG 照片。')
        if len(raw) > IMAGE_LIMIT:
            fail('照片超过 10MB。', 413)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    fmt = source.format
                    if fmt not in ('JPEG', 'PNG'):
                        fail('文件内容不是有效的 JPG 或 PNG。')
                    if source.width * source.height > PIXEL_LIMIT:
                        fail('图片超过 2500 万像素，请缩小后上传。')
                    source.verify()
                with Image.open(io.BytesIO(raw)) as source:
                    image = ImageOps.exif_transpose(source).convert('RGB')
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            fail('图片损坏或无法读取，请重新导出 JPG/PNG 后上传。')
        return image, fmt

    @app.post('/api/submissions/<int:sid>/pages')
    def upload(sid):
        layout = upload_answer_layout()
        auto_grade = request.form.get('auto_grade', 'false')
        if auto_grade not in ('true','false'):
            fail('自动批改设置无效。')
        with db() as conn:
            row(conn, 'submissions', sid)
        incoming = request.files.get('image')
        if not incoming:
            fail('请选择一张照片。')
        raw = incoming.read(IMAGE_LIMIT + 1)
        image, fmt = decode_photo(raw, incoming.filename)
        key = uuid4().hex
        original = key + ('.jpg' if fmt == 'JPEG' else '.png')
        image_name = key + '-display.png'
        try:
            (uploads / original).write_bytes(raw)
            image.save(uploads / image_name, format='PNG')
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if row(conn, 'submissions', sid)['grading_state'] == 'running' or grading_jobs.active_submission_job(conn, sid):
                    fail('答卷正在批改，请完成后再上传。', 409)
                pid = conn.execute('INSERT INTO pages(submission_id,original,image,width,height,created_at,needs_review,answer_layout) VALUES(?,?,?,?,?,?,1,?)', (sid, original, image_name, image.width, image.height, now(), layout)).lastrowid
                conn.execute("UPDATE submissions SET grading_state='idle',grading_error='' WHERE id=?", (sid,))
                result = page_dict(row(conn, 'pages', pid), conn)
        except Exception:
            (uploads / original).unlink(missing_ok=True)
            (uploads / image_name).unlink(missing_ok=True)
            raise
        if auto_grade == 'true':
            runner = app.extensions['grading_jobs']
            result['job'] = runner.enqueue(submission_id=sid)
            runner.kick()
        return jsonify(result), 201

    @app.post('/api/pages/<int:pid>/reviewed')
    def acknowledge_page(pid):
        if body().get('confirmed') is not True:
            fail('请明确确认已对照原图人工核对本页全部作答。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            page = row(conn, 'pages', pid)
            if page['status'] == 'running':
                fail('照片正在识别，请完成后再核对。', 409)
            conn.execute('UPDATE pages SET needs_review=0 WHERE id=?', (pid,))
            for review in conn.execute('SELECT question_id FROM reviews WHERE submission_id=?', (page['submission_id'],)).fetchall():
                conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                    (page['submission_id'], review['question_id'], now(), dumps({'action':'page_manual_review','page_id':pid})))
            return jsonify(submission_detail(conn, page['submission_id']))

    @app.get('/api/pages/<int:pid>/image')
    def get_image(pid):
        with db() as conn:
            page = row(conn, 'pages', pid)
        return send_file(uploads / page['image'], mimetype='image/png')

    @app.post('/api/pages/<int:pid>/ocr')
    def run_ocr(pid):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            page = row(conn, 'pages', pid)
            if row(conn, 'submissions', page['submission_id'])['grading_state'] == 'running' or grading_jobs.active_submission_job(conn, page['submission_id']):
                fail('答卷正在自动批改，请等待完成。', 409)
            if conn.execute("UPDATE pages SET status='running',error='',needs_review=1 WHERE id=? AND status!='running'", (pid,)).rowcount != 1:
                fail('这张照片正在识别，请等待。', 409)
            conn.execute("UPDATE submissions SET grading_state='idle',grading_error='' WHERE id=?", (page['submission_id'],))
        if not app.extensions['page_ocr'](pid, local_recognize=recognize, manual=True):
            return jsonify(error='识别失败，原图及复核记录已保留，请重试。'), 503
        with db() as conn:
            return jsonify(submission_detail(conn, page['submission_id']))

    def regions_and_text(conn, sid, regions, question):
        if not isinstance(regions, list) or len(regions) > 100:
            fail('作答区域格式不正确或数量过多。')
        validated, parts = [], []
        for region in regions:
            if not isinstance(region, dict):
                fail('作答区域格式不正确。')
            pid = integer(region.get('page_id'), '照片编号')
            page = conn.execute('SELECT * FROM pages WHERE id=? AND submission_id=?', (pid, sid)).fetchone()
            if page is None:
                fail('作答区域不属于这份作业。')
            box = region.get('box')
            def valid_coordinate(value):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return False
                try:
                    return math.isfinite(value)
                except (OverflowError, TypeError):
                    return False
            if (not isinstance(box, list) or len(box) != 4 or
                any(not valid_coordinate(v) for v in box)):
                fail('作答区域坐标无效。')
            x1, y1, x2, y2 = box
            if not (0 <= x1 < x2 <= page['width'] and 0 <= y1 < y2 <= page['height']):
                fail('作答区域超出图片范围。')
            ids = region.get('block_ids', [])
            if not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in ids) or len(ids) != len(set(ids)):
                fail('识别文字编号无效。')
            blocks = {b['id']: b for b in json.loads(page['blocks'])}
            if any(i not in blocks for i in ids):
                fail('识别结果已改变，请刷新后重新关联区域。')
            selected = [blocks[i] for i in ids]
            selected = reading_order(selected)
            part = '\n'.join(b['text'] for b in selected)
            match = NUMBER.match(part)
            if match and int(match[1]) == question['number']:
                part = match[2]
            parts.append(part)
            validated.append({'page_id': pid, 'box': box, 'block_ids': ids})
        machine = '\n'.join(p for p in parts if p)
        if validated:
            pages = []
            for pid in dict.fromkeys(r['page_id'] for r in validated):
                page = page_dict(row(conn, 'pages', pid), conn)
                ids = {i for r in validated if r['page_id'] == pid for i in r['block_ids']}
                page['blocks'] = [b for b in page['blocks'] if b['id'] in ids]
                pages.append(page)
            questions = [dict(question, question_id=question['id'])]
            candidates = card_candidates(questions, pages) + grammar_candidates(questions, pages)
            if candidates:
                machine = candidates[0]['transcript']
        return validated, machine

    def review_page_revision(conn, sid):
        return [dict(p) for p in conn.execute('''SELECT id,status,needs_review,error,
            COALESCE((SELECT MAX(id) FROM ocr_runs WHERE page_id=pages.id),0) AS ocr_run_id
            FROM pages WHERE submission_id=? ORDER BY id''',(sid,))]

    def visual_revision(detail, question, revisions):
        data = [detail['history_count'], detail['identity'], detail['grading_state'], detail['rules'], revisions, question]
        return hashlib.sha256(dumps(data).encode()).hexdigest()

    def visual_suggestion_dto(record):
        result = json.loads(record['result'])
        result.update(id=record['id'],
                      preview_url=f"/api/submissions/{record['submission_id']}/visual-suggestions/{record['id']}/image")
        return result

    def visual_crop_png(image_name, box, angle):
        turns = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                 270: Image.Transpose.ROTATE_270}
        buffer = io.BytesIO()
        with Image.open(uploads / image_name) as source:
            cropped = source.crop(tuple(box))
            if angle in turns:
                cropped = cropped.transpose(turns[angle])
            cropped.info.clear()
            cropped.save(buffer, 'PNG')
        return buffer.getvalue()

    def visual_overlaps(box, other):
        return (box[0] < other[2] and box[2] > other[0]
                and box[1] < other[3] and box[3] > other[1])

    def automatic_visual_crop(detail, question):
        evidence = question['auto_result'] or {}
        regions = evidence.get('regions', [])
        if (not question['uncertain'] or question['teacher_edited']
                or question['kind'] not in ('word', 'phrase', 'grammar')
                or evidence.get('source') != 'region_supplementary_ocr' or not regions):
            return None
        pid = regions[0]['page_id']
        page = next((p for p in detail['pages'] if p['id'] == pid), None)
        if not page or page['status'] != 'done' or any(r['page_id'] != pid for r in regions):
            return None
        box = [math.floor(min(r['box'][i] for r in regions)) if i < 2
               else math.ceil(max(r['box'][i] for r in regions)) for i in range(4)]
        x0, y0, x1, y1 = box
        width, height = page['width'], page['height']
        angle = page['display_rotation']
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            return None
        top, page_height = {0:(y0,height),90:(width-x1,width),180:(height-y1,height),
                            270:(x0,width)}[angle]
        area = (x1-x0)*(y1-y0)
        if top < page_height*.08 or area > min(2_000_000, width*height*.15):
            return None
        if any(r['page_id'] == pid and visual_overlaps(box, r['box'])
               for other in detail['reviews'] if other['question_id'] != question['question_id']
               for r in other['regions']):
            return None
        # Approximate OCR boxes containing identity text disqualify an otherwise usable crop.
        if any((re.search(r'学号|姓名|学生编号|班级|[\u3400-\u9fff]', b['text'])
                or re.fullmatch(r'\s*\d{5,20}\s*', b['text']))
               and visual_overlaps(box, b['box']) for b in page['blocks']):
            return None
        return {'page_id': pid, 'box': box, 'rotation': angle}

    def prepare_visual_suggestions(submission_ids):
        from settings import visual_review_config
        if os.environ.get('VLM_REVIEW_AUTO') != '1':
            return
        try:
            visual_review_config()
        except ValueError:
            return
        remaining = 8
        for sid in submission_ids:
            with db() as conn:
                if not conn.execute('SELECT 1 FROM submissions WHERE id=?', (sid,)).fetchone():
                    continue
                detail = submission_detail(conn, sid)
                attempted = {(r['question_id'], r['page_id']) for r in conn.execute(
                    'SELECT question_id,page_id FROM automatic_visual_attempts WHERE submission_id=?', (sid,))}
            candidates = sorted(detail['reviews'], key=lambda q: not (q['auto_result'] or {}).get('recognition_conflict'))
            for question in candidates:
                qid = question['question_id']
                crop = automatic_visual_crop(detail, question)
                if (not crop or (qid, crop['page_id']) in attempted
                        or qid in detail.get('visual_suggestions', {})):
                    continue
                remaining -= 1
                with app.test_client() as client:
                    client.post(f'/api/submissions/{sid}/reviews/{qid}/visual-reread',
                                json=crop, headers={'X-CSRF-Token': app.config['CSRF_TOKEN']},
                                environ_overrides={'moxieben.automatic_visual': True})
                if remaining == 0:
                    return

    app.extensions['prepare_visual_suggestions'] = prepare_visual_suggestions

    @app.get('/api/submissions/<int:sid>/visual-suggestions/<int:vid>/image')
    def visual_suggestion_image(sid, vid):
        with db() as conn:
            found = conn.execute('''SELECT v.result,p.image FROM visual_suggestions v
                JOIN pages p ON p.id=v.page_id WHERE v.id=? AND v.submission_id=?''', (vid,sid)).fetchone()
        if found is None:
            fail('没有找到这份复读图片。', 404)
        crop = json.loads(found['result'])['crop']
        try:
            png = visual_crop_png(found['image'], crop['box'], crop['rotation'])
        except (OSError, ValueError):
            fail('无法读取本题裁图。', 404)
        return send_file(io.BytesIO(png), mimetype='image/png')

    # ponytail: one in-flight manual reread for this local process. Use the job runner for batch rereads.
    visual_reread_lock = threading.Lock()

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/visual-reread')
    def visual_reread(sid, qid):
        from settings import reread_handwriting, visual_review_config

        automatic = request.environ.get('moxieben.automatic_visual') is True
        payload = body()
        if set(payload) - {'page_id', 'box', 'rotation'}:
            fail('只接受照片编号、原图框选坐标和旋转方向。')
        pid = integer(payload.get('page_id'), '照片编号')
        box = payload.get('box')
        if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
            fail('请填写四个原图像素坐标。')
        rotation = payload.get('rotation')
        if 'rotation' in payload and (type(rotation) is not int or rotation not in (0, 90, 180, 270)):
            fail('旋转方向须为 0、90、180 或 270 度。')

        def current(conn):
            detail = submission_detail(conn, sid)
            question = next((q for q in detail['reviews'] if q['question_id'] == qid), None)
            page = next((p for p in detail['pages'] if p['id'] == pid), None)
            if question is None or page is None:
                fail('题目或照片不属于这份答卷。', 404)
            if ((detail.get('identity') or {}).get('class_archived')
                    or (detail.get('identity') or {}).get('exempt')
                    or detail['grading_state'] != 'final' or page['status'] != 'done'
                    or (detail.get('processing_job') or {}).get('state') in ('running', 'queued')
                    or any(p['status'] == 'running' for p in detail['pages'])
                    or not question['uncertain'] or question['teacher_edited']
                    or question['kind'] not in ('word', 'phrase', 'grammar')):
                fail('只支持尚未核对的短答识别异常。', 409)
            revision = visual_revision(detail, question, review_page_revision(conn, sid))
            if automatic and automatic_visual_crop(detail, question) != payload:
                fail('题目区域有变化，请人工核对。', 409)
            return question, page, revision, detail['reviews']

        with db() as conn:
            conn.execute('BEGIN')
            question, page, revision, reviews = current(conn)
            image_name = row(conn, 'pages', pid)['image']
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= page['width'] and 0 <= y0 < y1 <= page['height']):
            fail('框选区域超出原图范围。')
        area = (x1 - x0) * (y1 - y0)
        if area > 2_000_000 or area * 3 > page['width'] * page['height']:
            fail('框选区域过大，请缩小到单题作答处。')
        try:
            config = visual_review_config()
        except ValueError as error:
            fail(str(error), 503)
        if not visual_reread_lock.acquire(blocking=False):
            fail('已有视觉复读正在进行，请稍后重试。', 409)
        try:
            angle = page['display_rotation'] if rotation is None else rotation
            try:
                png = visual_crop_png(image_name, box, angle)
            except (OSError, ValueError):
                fail('原图无法裁剪，请使用原有核对操作。', 409)
            if automatic:
                with db() as conn:
                    claimed = conn.execute('INSERT OR IGNORE INTO automatic_visual_attempts VALUES (?,?,?)',
                                           (sid,qid,pid)).rowcount
                if not claimed:
                    fail('本题原图已尝试自动复读。', 409)
            # The request only returns a suggestion. Unlock during the network call so manual review can continue.
            release_archive_write_locks()
            started = time.monotonic()
            try:
                result = reread_handwriting(config, png, question['number'])
            except (ValueError, OSError):
                if automatic:
                    app.logger.warning('Automatic visual reread failed for submission %s question %s', sid, qid)
                fail('视觉复读未取得有效结果，可继续人工核对。', 502)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            score = None
            if result['status'] == 'readable':
                match = compare(result['transcript'], question['answer'], question['variants'])
                score = question['points'] if match['matched'] else 0
            evidence = question['auto_result'] or {}
            same_reading = (score is not None and evidence.get('source') == 'region_supplementary_ocr'
                            and normalize(evidence['transcript']) == normalize(result['transcript']))
            regions = evidence.get('regions', [])
            # Broad boxes or another page cannot corroborate the teacher's selected crop.
            same_region = bool(regions) and all(
                r['page_id'] == pid and x0 <= r['box'][0] < r['box'][2] <= x1
                and y0 <= r['box'][1] < r['box'][3] <= y1 for r in regions)
            overlaps_neighbor = any(
                r['page_id'] == pid and x0 < r['box'][2] and x1 > r['box'][0]
                and y0 < r['box'][3] and y1 > r['box'][1]
                for other in reviews if other['question_id'] != qid for r in other['regions'])
            saved = dict(result, suggested_score=score, max_score=question['points'], elapsed_ms=elapsed_ms,
                         crop={'page_id': pid, 'box': box, 'rotation': angle},
                         can_adopt=bool(same_reading and same_region and not overlaps_neighbor),
                         trigger='automatic' if automatic else 'manual',
                         model=config['model'], rules_version=RULES['version'],
                         ocr_evidence=evidence.get('recognition_evidence', []),
                         machine_text=question['machine_text'])
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                _, _, latest_revision, _ = current(conn)
                if revision != latest_revision:
                    fail('答卷已更新，本次复读建议已失效。', 409)
                vid = conn.execute('''INSERT INTO visual_suggestions
                    (submission_id,question_id,page_id,revision,created_at,result) VALUES (?,?,?,?,?,?)''',
                    (sid,qid,pid,revision,now(),dumps(saved))).lastrowid
                return jsonify(visual_suggestion_dto(row(conn, 'visual_suggestions', vid)))
        finally:
            visual_reread_lock.release()

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>')
    def save_review(sid, qid):
        payload = body()
        quick = payload.get('quick_decision')
        visual_id = None
        if 'visual_suggestion_id' in payload:
            if set(payload) != {'visual_suggestion_id'}:
                fail('采纳复读只接受建议编号。')
            visual_id = integer(payload['visual_suggestion_id'], '建议编号')
        if 'quick_decision' in payload and (not isinstance(quick, str) or quick not in ('correct', 'incorrect', 'blank') or len(payload) != 1):
            fail('快捷判定仅接受正确、错误或未作答，请勿同时提交转录或分数。')
        transcript = text(payload.get('transcript', ''), '学生转录', 8000, required=False)
        reason = text(payload.get('reason', ''), '改分原因', 1000, required=False)
        confirmed = payload.get('confirmed', False)
        blank = payload.get('confirmed_blank', False)
        if not isinstance(confirmed, bool) or not isinstance(blank, bool):
            fail('确认状态须为布尔值。')
        if blank and normalize(transcript):
            fail('已确认未作答时，转录文字必须为空。')
        if confirmed and not normalize(transcript) and not blank:
            fail('识别为空不代表未作答，请确认空白或输入实际笔迹。')
        with db() as conn:
            # Serialize review writes so audit snapshots reflect the actual edit chain.
            conn.execute('BEGIN IMMEDIATE')
            submission = row(conn, 'submissions', sid)
            if submission['grading_state'] == 'running' or grading_jobs.active_submission_job(conn, sid) or conn.execute("SELECT 1 FROM pages WHERE submission_id=? AND status='running'", (sid,)).fetchone():
                fail('答卷正在批改，请完成后再改判。', 409)
            q = conn.execute('SELECT * FROM questions WHERE id=? AND task_id=?', (qid, submission['task_id'])).fetchone()
            if q is None:
                fail('这道题不属于当前任务。', 404)
            old = dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?', (sid, qid)).fetchone())
            if visual_id:
                detail = submission_detail(conn, sid)
                suggestion = (detail.get('visual_suggestions') or {}).get(qid)
                if (not suggestion or suggestion['id'] != visual_id or not suggestion['can_adopt']
                        or old['teacher_edited'] or old['confirmed']):
                    fail('这条复读建议已失效或需要人工判定。', 409)
                transcript = suggestion['transcript']
                box = suggestion['crop']
                regions = [{'page_id': box['page_id'], 'box': box['box'], 'block_ids': []}]
                machine = old['machine_text']
                override = None
                confirmed = True
                reason = '教师核对局部图后采纳视觉复读，按严格规则计分。'
            elif quick:
                if not conn.execute('SELECT 1 FROM pages WHERE submission_id=?', (sid,)).fetchone():
                    fail('请先上传作业原图，再进行教师判定。')
                transcript = '' if quick == 'blank' else old['transcript']
                blank = quick == 'blank'
                regions, machine = json.loads(old['regions']), old['machine_text']
                override = q['points'] if quick == 'correct' else 0
                reason = {'correct':'教师判定正确（已核对原图）', 'incorrect':'教师判定错误（已核对原图）', 'blank':'教师确认未作答（已核对原图）'}[quick]
                confirmed = True
            else:
                if old['auto_result'] != '{}' and not normalize(transcript) and not blank:
                    fail('修改机器结果时，请填写实际作答；确实空白时请明确确认未作答。')
                regions, machine = regions_and_text(conn, sid, payload.get('regions', []), dict(q))
                override = payload.get('score_override')
                if override is not None:
                    override = number(override, 0, q['points'])
                    if not reason.strip():
                        fail('手动调整分数时必须填写原因。')
                machine = old['machine_text'] or machine
                if old['confirmed'] and (transcript != old['transcript'] or regions != json.loads(old['regions']) or blank != bool(old['confirmed_blank']) or override != old['score_override']):
                    confirmed = False
                if old['auto_result'] != '{}' and (normalize(transcript) or blank):
                    confirmed = True
            conn.execute('UPDATE reviews SET teacher_edited=1,teacher_decision=?,machine_text=?,transcript=?,regions=?,confirmed=?,confirmed_blank=?,score_override=?,reason=? WHERE submission_id=? AND question_id=?',
                (quick or '', machine, transcript, dumps(regions), int(confirmed), int(blank), override, reason, sid, qid))
            stamp = now()
            conn.execute('UPDATE submissions SET updated_at=? WHERE id=?', (stamp, sid))
            snapshot = {'before': old, 'after': {'machine_text': machine, 'transcript': transcript, 'regions': regions,
                        'confirmed': confirmed, 'confirmed_blank': blank, 'score_override': override, 'reason': reason,
                        'teacher_decision': quick or ''},
                        'question': dict(q), 'rules': RULES}
            if quick or visual_id:
                snapshot['action'] = 'teacher_visual_adoption' if visual_id else 'teacher_quick_decision'
                if visual_id:
                    snapshot['visual_suggestion_id'] = visual_id
                    snapshot['score'] = suggestion['suggested_score']
                snapshot['review_after'] = dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone())
                snapshot['page_revision'] = review_page_revision(conn,sid)
            event_id = conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                         (sid, qid, stamp, dumps(snapshot))).lastrowid
            result = submission_detail(conn, sid)
            if quick or visual_id:
                result['undo'] = {'event_id':event_id,'question_id':qid,'number':q['number']}
            return jsonify(result)

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/undo')
    def undo_quick_review(sid, qid):
        payload=body()
        event_id=integer(payload.get('event_id'),'操作编号')
        if set(payload)!={'event_id'}:
            fail('撤销仅接受操作编号。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            submission=row(conn,'submissions',sid)
            event=conn.execute('SELECT * FROM review_events WHERE id=? AND submission_id=? AND question_id=?',(event_id,sid,qid)).fetchone()
            if event is None:
                fail('没有找到本题的操作记录。',404)
            if submission['grading_state']=='running' or grading_jobs.active_submission_job(conn,sid) or conn.execute("SELECT 1 FROM pages WHERE submission_id=? AND status='running'",(sid,)).fetchone():
                fail('答卷正在处理，请完成后再撤销。',409)
            snapshot=json.loads(event['snapshot'])
            latest=conn.execute('SELECT MAX(id) FROM review_events WHERE submission_id=?',(sid,)).fetchone()[0]
            current=dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone())
            if (snapshot.get('action') not in ('teacher_quick_decision', 'teacher_visual_adoption') or latest!=event_id
                    or snapshot.get('review_after')!=current or snapshot.get('page_revision')!=review_page_revision(conn,sid)):
                fail('这份答卷已有后续修改或识别，不能撤销旧操作。请打开本题核对后改判。',409)
            previous=snapshot['before']
            fields=[key for key in current if key not in ('submission_id','question_id')]
            conn.execute('UPDATE reviews SET '+','.join(f'{key}=?' for key in fields)+' WHERE submission_id=? AND question_id=?',
                (*[previous[key] for key in fields],sid,qid))
            stamp=now()
            conn.execute('UPDATE submissions SET updated_at=? WHERE id=?',(stamp,sid))
            conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                (sid,qid,stamp,dumps({'action':'undo_teacher_quick_decision','undone_event_id':event_id,'before':current,'after':previous,'rules':RULES})))
            return jsonify(submission_detail(conn,sid))

    @app.get('/api/submissions/<int:sid>/suggestions')
    def suggestions(sid):
        with db() as conn:
            detail = submission_detail(conn, sid)
        return jsonify(suggestions=suggestions_for(detail))

    def has_review_work(review):
        return (review['confirmed'] or review['regions'] or review['transcript'] or
                review['confirmed_blank'] or review['score_override'] is not None or review['reason'])

    def suggestions_for(detail):
        by_number = {r['number']: r for r in detail['reviews']}
        found = {}
        duplicates = set()
        for page in detail['pages']:
            if page['status'] in ('error', 'running') or page.get('answer_layout') == 'separate':
                continue
            active = None
            for block in reading_order(page['blocks']):
                match = NUMBER.match(block['text'])
                if match:
                    n = int(match[1])
                    active = None
                    if n not in by_number:
                        continue
                    if n in found:
                        duplicates.add(n)
                        continue
                    review = by_number[n]
                    if has_review_work(review):
                        continue
                    if review['kind'] == 'grammar' and re.search(r'[_＿]{3,}', review.get('context','')):
                        continue
                    # ponytail: grammar suggestions require a short standalone answer;
                    # inline article blanks need teacher mapping, not guessed extraction.
                    if review['kind'] == 'grammar' and (
                        len(match[2].split()) > 8 or re.search(r'[（(]\s*\d{1,3}\s*[)）]', match[2])):
                        continue
                    active = n
                    found[n] = {'question_id': review['question_id'], 'transcript': match[2],
                        'regions': [{'page_id': page['id'], 'box': block['box'], 'block_ids': [block['id']]}]}
                elif active is not None:
                    if by_number[active]['kind'] == 'grammar':
                        active = None
                        continue
                    item = found[active]
                    prev = item['regions'][-1]['box']
                    box = block['box']
                    # ponytail: nearby same-column continuation only; ambiguous layouts require manual mapping.
                    if abs(box[0] - prev[0]) < 100 and 0 <= box[1] - prev[3] <= max(80, 2 * (prev[3] - prev[1])):
                        item['transcript'] += '\n' + block['text']
                        item['regions'].append({'page_id': page['id'], 'box': box, 'block_ids': [block['id']]})
                    else:
                        active = None
        result = [found[n] for n in sorted(found) if n not in duplicates]
        untouched = [r for r in detail['reviews'] if not has_review_work(r)]
        result.extend(grammar_candidates(untouched, detail['pages']))
        result.extend(card_candidates(untouched, detail['pages']))
        # A repeated number across paper types/pages must not silently select the last candidate.
        all_numbers = [n for p in detail['pages'] if p['status'] not in ('error', 'running') for n in number_occurrences(p)]
        result = [item for item in result if sum(i['question_id'] == item['question_id'] for i in result) == 1
                  and all_numbers.count(next(r['number'] for r in detail['reviews'] if r['question_id'] == item['question_id'])) <= 1]
        return sorted(result, key=lambda item: next(r['number'] for r in detail['reviews'] if r['question_id'] == item['question_id']))

    @app.post('/api/submissions/<int:sid>/extract-answers')
    def extract_answers(sid):
        payload = body()
        pid = integer(payload['page_id'], '照片编号') if payload.get('page_id') is not None else None
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            detail = submission_detail(conn, sid)
            if pid and not any(p['id'] == pid for p in detail['pages']):
                fail('照片不属于这份答卷。')
            candidates = suggestions_for(detail)
            count = 0
            used_pages = set()
            for item in candidates:
                if pid and any(r['page_id'] != pid for r in item['regions']):
                    continue
                qid = item['question_id']
                q = dict(row(conn, 'questions', qid))
                old = dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?', (sid,qid)).fetchone())
                regions, machine = regions_and_text(conn, sid, item['regions'], q)
                conn.execute('UPDATE reviews SET machine_text=?,transcript=?,regions=? WHERE submission_id=? AND question_id=?',
                             (old['machine_text'] or machine,item['transcript'],dumps(regions),sid,qid))
                after = dict(old, machine_text=old['machine_text'] or machine,transcript=item['transcript'],regions=regions)
                conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                             (sid,qid,now(),dumps({'action':'extract_from_existing_ocr','before':old,'after':after,'question':q,'rules':RULES})))
                count += 1
                used_pages.update(r['page_id'] for r in regions)
            if count:
                conn.execute('UPDATE submissions SET updated_at=? WHERE id=?',(now(),sid))
                for page in detail['pages']:
                    if page['id'] in used_pages and page['status'] == 'uploaded' and page['blocks']:
                        conn.execute("UPDATE pages SET status='done',needs_review=0 WHERE id=?",(page['id'],))
                        conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                                     (page['id'],now(),dumps({'source':'reused_existing_identity_ocr','blocks':page['blocks']})))
            result = submission_detail(conn,sid)
            result['extracted_count'] = count
            return jsonify(result)

    @app.get('/tasks/<int:tid>/grades.csv')
    def grades(tid):
        with db() as conn:
            task = task_detail(conn, tid)
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['学生编号', '作业编号', '成绩状态', '得分', '满分', '教师已复核题数', '总题数', '姓名', '班级', '学年/学期', '批改来源', '机器批改题数', '不确定题数'])
        def safe(value):
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
        for s in task['submissions']:
            code = s['student_code']
            if code.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
                code = "'" + code
            identity = s['identity'] or {}
            writer.writerow([code, s['id'], '最终成绩' if s['is_final'] else ('批改失败' if s['grading_state'] == 'failed' else '暂定成绩'),
                             s['score'], s['total_points'], s['confirmed_count'], s['question_count'],
                             safe(identity.get('name', '')), safe(identity.get('class_name', '')), safe(identity.get('term', '')), s['grading_source'], s['machine_graded_count'], s['uncertain_count']])
        return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="task-{tid}-grades.csv"'})

    @app.get('/tasks/<int:tid>/sheet')
    def sheet(tid):
        with db() as conn:
            task = task_detail(conn, tid)
        return render_template('print.html', mode='sheet', task=task)

    @app.get('/tasks/<int:tid>/answer-card')
    @app.get('/tasks/<int:tid>/answer-card/download')
    @app.get('/assignments/<int:aid>/answer-card')
    @app.get('/assignments/<int:aid>/answer-card/download')
    def answer_card(tid=None, aid=None):
        from answer_card_export import paginate, pdf_card, word_card
        class_name = ''
        card_format = None
        with db() as conn:
            if aid is not None:
                assignment = row(conn, 'assignments', aid)
                tid = assignment['task_id']
                class_name = row(conn, 'classes', assignment['class_id'])['name']
                card_format = student_id_band.snapshot_of(assignment)
            task = task_detail(conn, tid)
        card_pages = paginate(task['questions'])
        card_label = student_id_band.field_label(card_format) if card_format else ''
        if request.path.endswith('/download'):
            kind = request.args.get('format', 'pdf')
            if kind not in ('pdf', 'docx'):
                fail('请选择 PDF 或 Word 格式下载答题卡。')
            renderer = pdf_card if kind == 'pdf' else word_card
            content = renderer(task['name'], card_pages, class_name, card_format, card_label)
            filename = f"answer-card-{'assignment-' + str(aid) if aid else 'task-' + str(task['id'])}.{kind}"
            mime = 'application/pdf' if kind == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            return send_file(io.BytesIO(content), mimetype=mime, as_attachment=True, download_name=filename)
        html = render_template('print.html', mode='answer_card', task=task,
                               card_pages=card_pages, class_name=class_name, assignment_id=aid,
                               card_format=card_format,
                               card_label=card_label)
        return html

    @app.get('/submissions/<int:sid>/report')
    def report(sid):
        with db() as conn:
            submission = submission_detail(conn, sid)
        return render_template('print.html', mode='report', submission=submission)

    @app.get('/api/submissions/<int:sid>/history')
    def history(sid):
        with db() as conn:
            row(conn, 'submissions', sid)
            events = [dict(r) for r in conn.execute('SELECT * FROM review_events WHERE submission_id=? ORDER BY id', (sid,))]
        for event in events:
            event['snapshot'] = json.loads(event['snapshot'])
        return jsonify(events=events)

    register_class_routes(app, db, body, row, submission_detail, task_detail, submission_summaries)
    register_batch_routes(app, db, row, uploads, submission_detail, decode_photo, submission_summaries)
    register_settings_routes(app, db)
    automatic_grading.register(app, db, row, submission_detail, suggestions_for, uploads, recognize, now, dumps, RULES)
    teacher_workbench.register(app, db, row, submission_detail, uploads, submission_summaries)
    class_analysis.register(app, db, row, submission_summaries)
    grading_jobs.register(app, db, row, now)
    task_deletion.register(app, db, row, database.resolve(), uploads)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    create_app().run(host='127.0.0.1', port=5000, debug=False, threaded=True, use_reloader=False)
