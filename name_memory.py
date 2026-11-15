"""Local, class-scoped teacher corrections. No OCR calls or model training."""
import json
import re
from datetime import datetime, timezone

from flask import jsonify, request

from identity_matching import identity_evidence


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS identity_memories (
        id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL REFERENCES classes(id),
        observed_name TEXT NOT NULL, student_id INTEGER NOT NULL REFERENCES students(id),
        source_photo_ids TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]',
        UNIQUE(class_id, observed_name, student_id))''')


def valid_name(value):
    # Teacher-entered evidence may be a Chinese name, an English name or a numeric code.
    if not isinstance(value, str) or not value.strip():
        return False
    return re.fullmatch(r'[\u4e00-\u9fff·A-Za-z0-9０-９]{1,40}', value.strip()) is not None


def observed_name(photo):
    details = json.loads(photo.get('match_details') or '{}')
    if details.get('teacher_observed_name'):
        return details['teacher_observed_name']
    names = identity_evidence(json.loads(photo['blocks']), [])['names']
    return names[0] if len(names) == 1 and valid_name(names[0]) else None


def list_memories(conn, cid):
    items = [dict(r) for r in conn.execute('''SELECT m.*,s.name FROM identity_memories m
        JOIN students s ON s.id=m.student_id WHERE m.class_id=? ORDER BY m.id''', (cid,))]
    active = {}
    for item in items:
        if item['enabled']:
            active.setdefault(item['observed_name'], set()).add(item['student_id'])
    for item in items:
        item['enabled'] = bool(item['enabled'])
        item['conflict'] = len(active.get(item['observed_name'], set())) > 1
        item['source_photo_ids'] = json.loads(item['source_photo_ids'])
        item['history'] = json.loads(item['history'])
    return items


def set_enabled(conn, memory, enabled, action):
    history = json.loads(memory['history']) if isinstance(memory['history'], str) else list(memory['history'])
    if bool(memory['enabled']) == enabled:
        return
    history.append({'action': action, 'enabled': enabled, 'created_at': stamp()})
    conn.execute('UPDATE identity_memories SET enabled=?,updated_at=?,history=? WHERE id=?',
                 (int(enabled), stamp(), json.dumps(history, ensure_ascii=False), memory['id']))


def remember(conn, cid, student_id, name, pid):
    old = conn.execute('SELECT * FROM identity_memories WHERE class_id=? AND observed_name=? AND student_id=?',
                       (cid, name, student_id)).fetchone()
    if old:
        sources = json.loads(old['source_photo_ids'])
        if pid in sources:
            return old['id']  # Repeated confirmation must not reactivate a disabled memory.
        sources.append(pid)
        history = json.loads(old['history']) + [{'action': 'teacher_confirmed', 'photo_id': pid, 'created_at': stamp()}]
        conn.execute('UPDATE identity_memories SET source_photo_ids=?,enabled=1,updated_at=?,history=? WHERE id=?',
                     (json.dumps(sources), stamp(), json.dumps(history, ensure_ascii=False), old['id']))
        return old['id']
    return conn.execute('''INSERT INTO identity_memories
        (class_id,observed_name,student_id,source_photo_ids,created_at,updated_at,history) VALUES(?,?,?,?,?,?,?)''',
        (cid, name, student_id, json.dumps([pid]), stamp(), stamp(),
         json.dumps([{'action': 'teacher_confirmed', 'photo_id': pid, 'created_at': stamp()}]))).lastrowid


def learn_confirmation(conn, photo, roster, cid, payload):
    from app import fail
    enabled = payload.get('remember_identity', False)  # Legacy API callers retain their prior behavior.
    if not isinstance(enabled, bool):
        fail('remember_identity 必须是布尔值。')
    if not enabled:
        return None
    name = payload.get('observed_name')
    if isinstance(name, str):
        name = name.strip()
    if not name:
        # A blank field means the teacher chose not to record evidence:
        # confirmation must still succeed, only the memory is skipped.
        if name is None:  # Legacy callers omit the field; fall back to photo evidence.
            name = observed_name(photo)
        if not name:
            return None
    if not valid_name(name):
        fail('要记住的识别姓名限 1–40 字，可用中文、英文字母、数字或「·」，或取消记住。')
    if name == roster['name']:
        return None
    details = json.loads(photo.get('match_details') or '{}')
    details['teacher_observed_name'] = name
    conn.execute('UPDATE batch_photos SET match_details=? WHERE id=?', (json.dumps(details, ensure_ascii=False), photo['id']))
    return remember(conn, cid, roster['student_id'], name, photo['id'])


def register(app, db, row):
    from app import fail, integer

    def editable(conn, cid):
        if row(conn, 'classes', cid)['archived']:
            fail('班级已归档，请恢复后再修改纠错记忆。', 409)

    def source(conn, cid, pid):
        photo = row(conn, 'batch_photos', pid)
        assignment = row(conn, 'assignments', photo['assignment_id'])
        if assignment['class_id'] != cid or photo.get('identity_source') != 'teacher' or not photo['submission_id']:
            fail('只能从本班教师确认的照片启用记忆。', 409)
        submission = row(conn, 'submissions', photo['submission_id'])
        return photo, row(conn, 'assignment_students', submission['assignment_student_id'])

    @app.get('/api/classes/<int:cid>/identity-memories')
    def memories(cid):
        with db() as conn:
            klass = row(conn, 'classes', cid)
            return jsonify(memories=list_memories(conn, cid), archived=bool(klass['archived']))

    @app.get('/api/classes/<int:cid>/identity-memories/candidates')
    def candidates(cid):
        with db() as conn:
            row(conn, 'classes', cid)
            existing = {(m['observed_name'], m['student_id']) for m in list_memories(conn, cid)}
            result = []
            for item in conn.execute('''SELECT p.* FROM batch_photos p JOIN assignments a ON a.id=p.assignment_id
                WHERE a.class_id=? AND p.identity_source='teacher' AND p.submission_id IS NOT NULL ORDER BY p.id''', (cid,)):
                photo, roster = source(conn, cid, item['id'])
                name = observed_name(photo)
                if name and name != roster['name'] and (name, roster['student_id']) not in existing:
                    result.append({'photo_id': photo['id'], 'observed_name': name, 'student_id': roster['student_id'],
                                   'name': roster['name'], 'student_code': roster['student_code']})
            return jsonify(candidates=result)

    @app.post('/api/classes/<int:cid>/identity-memories')
    def import_memory(cid):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            fail('请先预览并选择历史照片。')
        pid = integer(payload.get('photo_id'), '照片 ID')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            editable(conn, cid)
            photo, roster = source(conn, cid, pid)
            name = observed_name(photo)
            if not name or name == roster['name']:
                fail('历史照片没有明确的姓名纠错证据，不能启用。')
            remember(conn, cid, roster['student_id'], name, pid)
            return jsonify(memories=list_memories(conn, cid))

    @app.patch('/api/classes/<int:cid>/identity-memories/<int:mid>')
    def update_memory(cid, mid):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('enabled'), bool) or not isinstance(payload.get('resolve_conflict', False), bool):
            fail('enabled 和 resolve_conflict 必须是布尔值。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            editable(conn, cid)
            memory = row(conn, 'identity_memories', mid)
            if memory['class_id'] != cid:
                fail('记忆不属于本班。', 404)
            if payload.get('resolve_conflict') and payload['enabled']:
                for other in conn.execute('SELECT * FROM identity_memories WHERE class_id=? AND observed_name=? AND id!=?',
                                           (cid, memory['observed_name'], mid)).fetchall():
                    set_enabled(conn, dict(other), False, 'conflict_resolved')
            set_enabled(conn, memory, payload['enabled'], 'teacher_enabled' if payload['enabled'] else 'teacher_disabled')
            return jsonify(memories=list_memories(conn, cid))
