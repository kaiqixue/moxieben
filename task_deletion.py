"""Delete a task's owned records only, with a local recovery backup first."""
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from flask import abort, jsonify, request


SUBMISSIONS = 'SELECT id FROM submissions WHERE task_id=?'
ASSIGNMENTS = 'SELECT id FROM assignments WHERE task_id=?'
PAGES = f'SELECT id FROM pages WHERE submission_id IN ({SUBMISSIONS})'
JOBS = f'(assignment_id IN ({ASSIGNMENTS}) OR submission_id IN ({SUBMISSIONS}))'


def preview(conn, task):
    tid = task['id']
    count = lambda sql: conn.execute(sql, (tid,)).fetchone()[0]
    counts = {
        'questions': count('SELECT count(*) FROM questions WHERE task_id=?'),
        'assignments': count('SELECT count(*) FROM assignments WHERE task_id=?'),
        'submissions': count('SELECT count(*) FROM submissions WHERE task_id=?'),
        'uploaded_submissions': count(f'SELECT count(DISTINCT submission_id) FROM pages WHERE submission_id IN ({SUBMISSIONS})'),
    }
    # A linked batch photo and its page are one photograph.
    counts['photos'] = count(f'SELECT count(*) FROM pages WHERE submission_id IN ({SUBMISSIONS})') + count(
        f'SELECT count(*) FROM batch_photos WHERE assignment_id IN ({ASSIGNMENTS}) AND page_id IS NULL')
    busy = any(conn.execute(sql, (tid,) * sql.count('?')).fetchone() for sql in (
        f"SELECT 1 FROM grading_jobs WHERE {JOBS} AND state IN ('queued','running')",
        "SELECT 1 FROM submissions WHERE task_id=? AND grading_state='running'",
        f"SELECT 1 FROM pages WHERE submission_id IN ({SUBMISSIONS}) AND status='running'",
        f"SELECT 1 FROM batch_photos WHERE assignment_id IN ({ASSIGNMENTS}) AND status='identifying'"))
    return {'id': tid, 'name': task['name'], 'counts': counts, 'can_delete': not busy,
            'blocked_reason': '正在识别或批改，请等待处理结束后再删除。' if busy else ''}


def register(app, db, row, database, uploads):
    @app.get('/api/tasks/<int:tid>/deletion-preview')
    def deletion_preview(tid):
        with db() as conn:
            return jsonify(preview(conn, row(conn, 'tasks', tid)))

    @app.delete('/api/tasks/<int:tid>')
    def delete_task(tid):
        if app.config.get('PRODUCTION_MODE'):
            # Local deletion snapshots the entire DB, unsuitable for untrusted tenants.
            abort(403, description='服务器版暂未开放永久删除，请先归档；数据不会被删除。')
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('confirmed') is not True or not isinstance(payload.get('expected_counts'), dict):
            abort(400, description='请先查看删除范围并明确确认删除。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            info = preview(conn, row(conn, 'tasks', tid))
            if not info['can_delete']:
                abort(409, description=info['blocked_reason'])
            if info['counts'] != payload['expected_counts']:
                abort(409, description='任务内容已变化，请重新查看删除范围后确认。')
            files = set()
            for sql in (f'SELECT original,image FROM pages WHERE submission_id IN ({SUBMISSIONS})',
                        f'SELECT original,image FROM batch_photos WHERE assignment_id IN ({ASSIGNMENTS})'):
                for record in conn.execute(sql, (tid,)):
                    files.update(record)
            root = uploads.resolve()
            for name in files:
                if not isinstance(name, str) or not name or '/' in name or '\\' in name or (root / name).resolve().parent != root:
                    abort(409, description='照片存储路径异常，已停止删除，请检查数据。')
            folder = database.parent / 'backups' / f"task-delete-{tid}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
            try:
                (folder / 'uploads').mkdir(parents=True)
                # A separate reader can snapshot the committed DB while this transaction blocks writers.
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(folder / 'before-deletion.sqlite3')) as target:
                    source.backup(target)
                for name in files:
                    shutil.copy2(root / name, folder / 'uploads' / name)
                (folder / 'task.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, sqlite3.Error):
                abort(503, description='删除前备份未完成，任务未删除；请检查磁盘空间和照片文件后重试。')
            photo_ids = {r[0] for r in conn.execute(f'SELECT id FROM batch_photos WHERE assignment_id IN ({ASSIGNMENTS})', (tid,))}
            for memory in conn.execute('SELECT id,source_photo_ids,history FROM identity_memories').fetchall():
                sources = json.loads(memory['source_photo_ids'])
                removed = [pid for pid in sources if pid in photo_ids]
                if removed:
                    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                    history = json.loads(memory['history']) + [{'action': 'source_task_deleted', 'task_id': tid, 'photo_ids': removed, 'created_at': stamp}]
                    conn.execute('UPDATE identity_memories SET source_photo_ids=?,history=?,updated_at=? WHERE id=?',
                                 (json.dumps([pid for pid in sources if pid not in photo_ids]), json.dumps(history, ensure_ascii=False), stamp, memory['id']))
            for sql in (
                f'DELETE FROM grading_jobs WHERE {JOBS}',
                f'DELETE FROM batch_photos WHERE assignment_id IN ({ASSIGNMENTS})',
                f'DELETE FROM review_events WHERE submission_id IN ({SUBMISSIONS})',
                f'DELETE FROM reviews WHERE submission_id IN ({SUBMISSIONS})',
                f'DELETE FROM ocr_runs WHERE page_id IN ({PAGES})',
                f'DELETE FROM pages WHERE submission_id IN ({SUBMISSIONS})',
                'DELETE FROM submissions WHERE task_id=?',
                f'DELETE FROM assignment_students WHERE assignment_id IN ({ASSIGNMENTS})',
                'DELETE FROM assignments WHERE task_id=?',
                'DELETE FROM questions WHERE task_id=?', 'DELETE FROM tasks WHERE id=?'):
                conn.execute(sql, (tid,) * sql.count('?'))
            remaining_files = {name for table in ('pages', 'batch_photos') for record in conn.execute(f'SELECT original,image FROM {table}') for name in record}
        # Commit before file cleanup; a failed cleanup must never roll back into missing images.
        failed = 0
        for name in files - remaining_files:
            try:
                (root / name).unlink(missing_ok=True)
            except OSError:
                failed += 1
        return jsonify(deleted=True, backup=folder.name,
                       message='任务已删除，已保存本机备份。' + (f'有 {failed} 个照片文件未清理，备份已保留。' if failed else ''))
