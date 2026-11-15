"""Batch photo intake and teacher-confirmed identity linking."""
import json
import hashlib
import sqlite3
import re
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from flask import g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from ocr_engine import recognize_identity as recognize
from identity_matching import identity_evidence, primary_candidates, candidate_key, difference_text
import student_id_band
import name_memory
from teacher_workbench import processing_identity_photo_ids

IMAGE_LIMIT = 10 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def migrate(database):
    with closing(sqlite3.connect(database)) as conn, conn:
        name_memory.migrate(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS batch_photos (
            id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            original TEXT NOT NULL, image TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'uploaded', error TEXT NOT NULL DEFAULT '', blocks TEXT NOT NULL DEFAULT '[]',
            identified_text TEXT NOT NULL DEFAULT '', suggested_student_id INTEGER,
            candidates TEXT NOT NULL DEFAULT '[]',
            submission_id INTEGER REFERENCES submissions(id), page_id INTEGER REFERENCES pages(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(batch_photos)')}
        if 'page_id' not in columns:
            conn.execute('ALTER TABLE batch_photos ADD COLUMN page_id INTEGER REFERENCES pages(id)')
        if 'candidates' not in columns:
            conn.execute("ALTER TABLE batch_photos ADD COLUMN candidates TEXT NOT NULL DEFAULT '[]'")
        if 'match_details' not in columns:
            conn.execute("ALTER TABLE batch_photos ADD COLUMN match_details TEXT NOT NULL DEFAULT '{}'")
        if 'ocr_result' not in columns:
            conn.execute("ALTER TABLE batch_photos ADD COLUMN ocr_result TEXT NOT NULL DEFAULT '{}'")
        if 'excluded' not in columns:
            conn.execute('ALTER TABLE batch_photos ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0')
            conn.execute("ALTER TABLE batch_photos ADD COLUMN exclusion_history TEXT NOT NULL DEFAULT '[]'")
        if 'upload_id' not in columns:
            conn.execute('ALTER TABLE batch_photos ADD COLUMN upload_id TEXT')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS batch_photo_upload_receipt
                        ON batch_photos(assignment_id, upload_id) WHERE upload_id IS NOT NULL''')
        conn.execute("UPDATE batch_photos SET status='error', error='上次身份识别被中断，请重试。' WHERE status='identifying'")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def register_batch_routes(app, db, row, uploads, submission_detail, decode_photo, submission_summaries):
    from app import fail, integer, upload_answer_layout
    name_memory.register(app, db, row)
    tenant_mode = app.config.get('TEACHER_TENANT_MODE', False)

    def photo_dict(record):
        item = dict(record)
        if item.get('upload_id') is None:
            item.pop('upload_id', None)
        try:
            item['blocks'] = json.loads(item.get('blocks') or '[]')
        except json.JSONDecodeError:
            item['blocks'] = []
        try:
            item['candidates'] = json.loads(item.get('candidates') or '[]')
        except json.JSONDecodeError:
            item['candidates'] = []
        item['url'] = f"/api/batch-photos/{item['id']}/image"
        item['match_details'] = json.loads(item.get('match_details') or '{}') or None
        item['suggested_student_id'] = item.get('suggested_student_id')
        item['identity_names'] = identity_evidence(item['blocks'],[])['names']
        item.pop('original', None)
        item.pop('image', None)
        item.pop('ocr_result', None)
        return item

    def assignment_students(conn, aid):
        return [dict(item) for item in conn.execute(
            'SELECT id,student_id,student_code,name,exempt FROM assignment_students WHERE assignment_id=? ORDER BY student_code,id',
            (aid,)).fetchall()]

    def occupied_students(conn,aid):
        return {r[0] for r in conn.execute('''SELECT DISTINCT s.assignment_student_id FROM submissions s
            JOIN pages p ON p.submission_id=s.id JOIN assignment_students a ON a.id=s.assignment_student_id
            WHERE a.assignment_id=?''',(aid,))}

    def match_details(evidence,method,reason,candidates=None):
        details={'method':method,'reason':reason,'observed_name':evidence['name'],
                 'candidates':evidence['candidates'] if candidates is None else candidates}
        band=evidence.get('band')
        if band and band['state']!='absent':
            details['identity_band']=band
        return details

    def photo_evidence(blocks,students,assignment):
        snapshot=student_id_band.snapshot_of(assignment)
        band=student_id_band.read_band(blocks,format_snapshot=snapshot,students=students)
        return identity_evidence(blocks,students,band=band)

    def save_match(conn,pid,details,suggested=None):
        conn.execute('UPDATE batch_photos SET match_details=?,candidates=?,suggested_student_id=?,updated_at=? WHERE id=?',
            (_json(details),_json(details['candidates']),suggested,stamp(),pid))

    def match_response(conn,aid,linked=None,summary_only=False):
        photos=[]
        processing = processing_identity_photo_ids(conn, [aid])
        grading_fields=('score','total_points','is_final','grading_state','grading_source','uncertain_count','teacher_reviewed_count','recognition')
        records=conn.execute('SELECT * FROM batch_photos WHERE assignment_id=? ORDER BY id',(aid,)).fetchall()
        summaries=submission_summaries(conn,[r['submission_id'] for r in records if r['submission_id']])
        for record in records:
            item=photo_dict(record)
            item['processing_identity'] = record['id'] in processing
            if record['submission_id']:
                detail=summaries[record['submission_id']]
                item.update({key:detail.get(key) for key in grading_fields})
                if not (detail.get('machine_graded_count') or detail.get('teacher_reviewed_count')):
                    item['score'] = None
                if summary_only:
                    for key in ('blocks','identified_text','candidates','identity_names','exclusion_history'):
                        item.pop(key,None)
                    item['evidence_loaded']=False
            photos.append(item)
        used=occupied_students(conn,aid)
        eligible={s['id'] for s in assignment_students(conn,aid) if not s['exempt']}
        summary={'matched':len(used & eligible),'remaining_students':len(eligible-used),
                 'processing_photos':len(processing),
                 'pending':sum(not p['submission_id'] and not p['excluded'] for p in photos),
                 'excluded':sum(bool(p['excluded']) for p in photos)}
        for method in ('exact','band','memory','inferred','duplicate'):
            summary[method]=sum(not p['excluded'] and (p['match_details'] or {}).get('method')==method for p in photos)
        return {'photos':photos,'summary':summary,'linked_submission_ids':linked or []}

    def match_in_transaction(conn,aid):
        assignment=row(conn,'assignments',aid)
        if row(conn,'classes',assignment['class_id'])['archived']:
            fail('班级已归档，请恢复后再匹配。',409)
        if conn.execute("SELECT 1 FROM batch_photos WHERE assignment_id=? AND status='identifying'",(aid,)).fetchone():
            fail('照片身份仍在识别中，请全部识别完成后匹配。',409)
        students=[s for s in assignment_students(conn,aid) if not s['exempt']]
        photos=[dict(p) for p in conn.execute("SELECT * FROM batch_photos WHERE assignment_id=? AND status='identified' AND submission_id IS NULL AND excluded=0 ORDER BY id",(aid,))]
        evidence={p['id']:photo_evidence(json.loads(p['blocks']),students,assignment) for p in photos}
        memories=name_memory.list_memories(conn,assignment['class_id'])
        memory_targets={}
        for pid,e in evidence.items():
            hits=[m for m in memories if m['enabled'] and m['observed_name'] in e['names']]
            if not hits:
                continue
            # Memory conflicts must also block the ordinary one-character fallback.
            targets={m['student_id'] for m in hits}
            target=next((s for s in students if s['student_id'] in targets),None)
            exact=next((s for s in students if s['id']==e['exact_id']),None)
            reason=None
            if len(targets)>1 or any(m['conflict'] for m in hits):
                reason='教师纠错记忆冲突，请核对后在班级记忆管理中保留一个对应。'
            elif len(e['names'])!=1 or e['blocked']:
                reason=e['reason'] if e['blocked'] else '识别到多个姓名，不能使用纠错记忆。'
            elif target is None:
                reason='记忆对应学生不在本次有效应交名单中，请手动核对。'
            elif (e['code'] and e['code']!=target['student_code']) or (exact and exact['id']!=target['id']):
                reason='学号或精确姓名与教师纠错记忆冲突，请人工核对。'
            if reason:
                e.update(blocked=True,exact_id=None,reason=reason)
                continue
            if not exact:
                memory_targets[pid]=(target,hits[0])
                e['candidates']=[{k:target[k] for k in ('id','name','student_code')} | {
                    'observed_name':hits[0]['observed_name'],'differences':[], 'auto_eligible':False}]
                e['name']=hits[0]['observed_name']
        used=occupied_students(conn,aid);linked=[];resolved=set();reserved=set()
        exact_groups={}
        for pid,e in evidence.items():
            if e['exact_id'] and not e['blocked']:
                exact_groups.setdefault(e['exact_id'],[]).append(pid)
        for rid,ids in exact_groups.items():
            reserved.add(rid)
            if rid in used or len(ids)>1:
                for pid in ids:
                    save_match(conn,pid,match_details(evidence[pid],'duplicate',
                        '该学生已有照片，疑似重复；如为第二页，请明确手动追加。' if rid in used else '多张照片精确指向同一学生，请人工选择或明确追加。'),rid)
                    resolved.add(pid)
                continue
            pid=ids[0];e=evidence[pid]
            details=(match_details(e,'band',e['reason']) if e.get('method')=='band'
                     else match_details(e,'exact','学号或姓名唯一且无冲突，精确匹配。'))
            result=link_in_transaction(conn,pid,rid,'machine',details)
            linked.append(result['submission_id']);used.add(rid);resolved.add(pid)
        memory_groups={}
        for pid,(target,memory) in memory_targets.items():
            if pid not in resolved:
                memory_groups.setdefault(target['id'],[]).append(pid)
        for rid,ids in memory_groups.items():
            if rid in used or rid in reserved or len(ids)>1:
                for pid in ids:
                    save_match(conn,pid,match_details(evidence[pid],'duplicate','纠错记忆指向已有照片或被多张照片占用的学生，请核对是否重复。'),rid)
                    resolved.add(pid)
                reserved.add(rid)
                continue
            pid=ids[0]
            competing=any(other not in resolved and other!=pid and any(c['id']==rid for c in primary_candidates(e['candidates'])) for other,e in evidence.items())
            if competing:
                continue
            target,memory=memory_targets[pid]
            details=match_details(evidence[pid],'memory',f"依据教师纠错记忆：“{memory['observed_name']}” → “{target['name']}”。")
            details.update(memory_id=memory['id'],source_photo_ids=memory['source_photo_ids'])
            result=link_in_transaction(conn,pid,rid,'machine',details)
            linked.append(result['submission_id']);used.add(rid);resolved.add(pid)
        for comparison in ('suffix','edit'):
            while True:
                edges={pid:[c for c in primary_candidates(e['candidates']) if c['id'] not in used]
                       for pid,e in evidence.items() if pid not in resolved}
                competition={}
                for candidates in edges.values():
                    for c in candidates:
                        competition[c['id']]=competition.get(c['id'],0)+1
                ready=[(pid,cs[0]) for pid,cs in edges.items() if not evidence[pid]['blocked'] and len(cs)==1
                       and cs[0]['auto_eligible'] and cs[0].get('comparison')==comparison
                       and competition[cs[0]['id']]==1 and cs[0]['id'] not in reserved]
                if not ready:
                    break
                for pid,candidate in ready:
                    rid=candidate['id'];e=evidence[pid]
                    reason=(f"姓名推断：{candidate['observed_name']} → {candidate['name']}（{candidate['student_code']}）；"
                            + '；'.join(difference_text(d) for d in candidate['differences'])
                            + ('后姓名一致，' if comparison=='suffix' else '，姓名仅一处差异，')
                            + '剩余名单候选唯一，无照片竞争。')
                    details=match_details(e,'inferred',reason,[candidate])
                    result=link_in_transaction(conn,pid,rid,'machine',details)
                    linked.append(result['submission_id']);used.add(rid);resolved.add(pid)
        for pid,e in evidence.items():
            if pid in resolved:
                continue
            candidates=sorted([dict(c,already_matched=c['id'] in used) for c in e['candidates']],key=candidate_key)
            available=[c for c in primary_candidates(candidates) if not c['already_matched']]
            reason=e['reason']
            if not e['blocked'] and candidates:
                if any(c.get('ambiguous_name') for c in primary_candidates(candidates)):
                    reason='忽略末尾数字后名单存在同名学生，不能靠排除已交学生确定身份，请核对学号。'
                elif not available:
                    reason='主要相近候选已有照片，请核对是否重复；其余候选证据不足，仅供人工选择。'
                elif len(available)>1:
                    reason='剩余名单有多个相似候选，请核对后选择。'
                elif competition.get(available[0]['id'],0)>1:
                    reason='多张待处理照片竞争同一学生，请核对后选择。'
                elif len(e['names'])!=1:
                    reason='识别到多个姓名文字，不能按相近姓名自动归属，请核对候选。'
                elif available[0].get('comparison')=='suffix_edit':
                    reason='忽略末尾数字后仍有姓名差异，仅推荐候选，请对照原图确认。'
                elif available[0].get('distance',0)==2:
                    reason='姓名存在两处差异，仅推荐候选，请对照原图确认。'
                elif len(available[0]['observed_name'] or '')==2 or len(available[0]['name'])==2:
                    reason='二字姓名相差一字，仅推荐候选，请对照原图确认。'
                else:
                    reason='姓名证据不足以自动匹配，请核对候选或从完整名单选择。'
            band=e.get('band')
            if band and band['state']=='incomplete' and band['reason'] not in reason:
                reason=band['reason']+reason
            save_match(conn,pid,match_details(e,'pending',reason,candidates))
        return match_response(conn,aid,list(dict.fromkeys(linked)))

    @app.post('/api/assignments/<int:aid>/photos')
    def upload_batch_photos(aid):
        layout = upload_answer_layout()
        auto_process = request.form.get('auto_process', 'false')
        if auto_process not in ('true', 'false'):
            fail('自动处理设置无效。')
        incoming = request.files.getlist('image') or request.files.getlist('images')
        if not incoming:
            fail('请选择至少一张照片。')
        if len(incoming) > 100:
            fail('单次最多上传 100 张照片。')
        upload_id = request.form.get('upload_id')
        if upload_id is not None and (not re.fullmatch(r'[0-9a-f]{32}', upload_id)
                                      or len(incoming) != 1 or auto_process != 'false'):
            fail('上传标识仅适用于单张照片的独立上传。')
        with db() as conn:
            row(conn, 'assignments', aid)
        saved = []
        for item in incoming:
            raw = item.read(IMAGE_LIMIT + 1)
            image, fmt = decode_photo(raw, item.filename)
            key = uuid4().hex
            original = key + ('.jpg' if fmt == 'JPEG' else '.png')
            display = key + '-display.png'
            try:
                with db() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if upload_id is not None:
                        prior = conn.execute('SELECT * FROM batch_photos WHERE assignment_id=? AND upload_id=?',
                                             (aid, upload_id)).fetchone()
                        if prior:
                            if prior['answer_layout'] != layout or (uploads / prior['original']).read_bytes() != raw:
                                fail('此上传标识已用于另一张照片或作答形式。', 409)
                            photo = photo_dict(prior)
                            return jsonify(photos=[photo], **photo), 200
                    job = conn.execute('SELECT state FROM grading_jobs WHERE assignment_id=? ORDER BY id DESC LIMIT 1', (aid,)).fetchone()
                    if job and job['state'] in ('queued', 'running', 'interrupted', 'failed'):
                        fail('本班有未完成的后台任务，请先完成或继续该任务，再上传新照片。', 409)
                    (uploads / original).write_bytes(raw)
                    image.save(uploads / display, format='PNG')
                    values = (aid, original, display, image.width, image.height, stamp(), stamp(), layout, upload_id)
                    columns = 'assignment_id,original,image,width,height,created_at,updated_at,answer_layout,upload_id'
                    if tenant_mode:
                        columns += ',owner_id'
                        values += (g.teacher_id,)
                    pid = conn.execute(f'INSERT INTO batch_photos({columns}) VALUES({",".join("?" for _ in values)})',
                                       values).lastrowid
                    saved.append(photo_dict(conn.execute('SELECT * FROM batch_photos WHERE id=?', (pid,)).fetchone()))
            except Exception:
                (uploads / original).unlink(missing_ok=True)
                (uploads / display).unlink(missing_ok=True)
                raise
        # Keep a single-photo response convenient for the current UI while also
        # returning the complete queue for multi-file uploads.
        result = dict(photos=saved, **(saved[0] if len(saved) == 1 else {}))
        if auto_process == 'true':
            runner = app.extensions['grading_jobs']
            result['job'] = runner.enqueue(assignment_id=aid, photo_ids=[p['id'] for p in saved])
            runner.kick()
        return jsonify(result), 201

    @app.get('/api/assignments/<int:aid>/photos')
    def list_batch_photos(aid):
        with db() as conn:
            row(conn, 'assignments', aid)
            result=match_response(conn,aid,summary_only=request.args.get('summary')=='1')
        return jsonify(result)

    @app.get('/api/batch-photos/<int:pid>')
    def batch_photo_detail(pid):
        with db() as conn:
            return jsonify(dict(photo_dict(row(conn,'batch_photos',pid)),evidence_loaded=True))

    @app.post('/api/assignments/<int:aid>/photos/match')
    def match_batch_photos(aid):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = match_in_transaction(conn,aid)
        if (request.get_json(silent=True) or {}).get('auto_grade') is True:
            runner = app.extensions['grading_jobs']
            for sid in result['linked_submission_ids']:
                runner.enqueue(submission_id=sid,options={'reextract':True})
            runner.kick()
        return jsonify(result)

    @app.get('/api/batch-photos/<int:pid>/image')
    def batch_photo_image(pid):
        with db() as conn:
            item = row(conn, 'batch_photos', pid)
        return send_file(uploads / item['image'], mimetype='image/png')

    @app.post('/api/batch-photos/<int:pid>/exclude')
    def exclude_photo(pid):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or type(payload.get('excluded')) is not bool:
            fail('请明确选择排除或恢复照片。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            photo = row(conn, 'batch_photos', pid)
            assignment = row(conn, 'assignments', photo['assignment_id'])
            if row(conn, 'classes', assignment['class_id'])['archived']:
                fail('班级已归档，请恢复后操作。', 409)
            if photo['submission_id'] or photo['status'] == 'identifying' or conn.execute(
                "SELECT 1 FROM grading_jobs WHERE assignment_id=? AND state IN ('queued','running')", (photo['assignment_id'],)).fetchone():
                fail('照片已关联或正在处理中，不能排除；请先完成处理。', 409)
            excluded = payload['excluded']
            if bool(photo['excluded']) != excluded:
                history = json.loads(photo['exclusion_history']) + [{'excluded': excluded, 'created_at': stamp()}]
                conn.execute('UPDATE batch_photos SET excluded=?,exclusion_history=?,updated_at=? WHERE id=?',
                             (int(excluded), _json(history), stamp(), pid))
            return jsonify(photo_dict(row(conn, 'batch_photos', pid)))

    @app.post('/api/batch-photos/<int:pid>/identify')
    def identify_batch_photo(pid):
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict) or not isinstance(payload.get('defer_link',False),bool):
            fail('defer_link 必须是布尔值。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item = row(conn, 'batch_photos', pid)
            if item['excluded'] or item['submission_id'] or item['status'] == 'identifying':
                fail('照片已关联或正在识别，不能重复识别身份。', 409)
            path = uploads / item['image']
            assignment=row(conn,'assignments',item['assignment_id'])
            if row(conn,'classes',assignment['class_id'])['archived']:
                fail('班级已归档，请恢复后再识别。',409)
            students = [s for s in assignment_students(conn, item['assignment_id']) if not s['exempt']]
            conn.execute("UPDATE batch_photos SET status='identifying',error='',blocks='[]',ocr_result='{}',identified_text='',suggested_student_id=NULL,candidates='[]',updated_at=? WHERE id=?", (stamp(), pid))
        try:
            from settings import recognize_selected, RecognitionError
            result = recognize_selected(db, path, recognize)
            blocks = result.get('blocks') if isinstance(result, dict) else []
            if not isinstance(blocks, list):
                blocks = []
            evidence=photo_evidence(blocks,students,assignment)
            if isinstance(result,dict) and evidence.get('band'):
                result=dict(result,identity_band=evidence['band'])
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('''UPDATE batch_photos SET status=?,error='',blocks=?,ocr_result=?,identified_text=?,suggested_student_id=?,candidates=?,updated_at=? WHERE id=?''',
                             ('identified', _json(blocks), _json(result if isinstance(result, dict) else {}), evidence['text'], evidence['exact_id'], _json(evidence['candidates']), stamp(), pid))
                save_match(conn,pid,match_details(evidence,'pending','已识别文字，等待统一匹配。'),evidence['exact_id'])
            linked=[]
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if not payload.get('defer_link') and not conn.execute("SELECT 1 FROM batch_photos WHERE assignment_id=? AND status='identifying'",(item['assignment_id'],)).fetchone():
                    linked=match_in_transaction(conn,item['assignment_id'])['linked_submission_ids']
                saved = photo_dict(conn.execute('SELECT * FROM batch_photos WHERE id=?', (pid,)).fetchone())
            saved.update(code=evidence['code'],name=evidence['name'],linked_submission_ids=linked)
            return jsonify(saved)
        except HTTPException:
            raise
        except Exception as exc:
            message = ('身份识别失败：' + str(exc) + ' 原图已保留。' if isinstance(exc, RecognitionError)
                       else '身份识别失败，请检查网络、接口设置或重试；原图已保留。')
            app.logger.warning('Photo %s identity OCR failed: %s', pid, message)
            with db() as conn:
                conn.execute('UPDATE batch_photos SET status=?,error=?,updated_at=? WHERE id=?', ('error', message, stamp(), pid))
            return jsonify(error=message), 503

    @app.post('/api/batch-photos/<int:pid>/confirm')
    def confirm_batch_photo(pid):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload,dict) or payload.get('confirmed') is not True:
            fail('请对照原图确认身份后再关联。')
        if type(payload.get('auto_grade', False)) is not bool:
            fail('自动批改设置无效。')
        rid = integer(payload.get('assignment_student_id'), '应交名单 ID')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            photo = row(conn, 'batch_photos', pid)
            if pid in processing_identity_photo_ids(conn, [photo['assignment_id']]):
                fail('系统正在识别或统一匹配这张照片，请处理结束后再选择学生。', 409)
            if type(payload.get('append_page', False)) is not bool:
                fail('追加第二页须明确确认。')
            result=link_in_transaction(conn,pid,rid,'teacher',append_page=payload.get('append_page',False))
            photo=row(conn,'batch_photos',pid)
            roster=row(conn,'assignment_students',rid)
            assignment=row(conn,'assignments',photo['assignment_id'])
            name_memory.learn_confirmation(conn,photo,roster,assignment['class_id'],payload)
            result['photo']=photo_dict(row(conn,'batch_photos',pid))
        if payload.get('auto_grade'):
            runner = app.extensions['grading_jobs']
            result['job'] = runner.enqueue(submission_id=result['submission_id'])
            runner.kick()
        return jsonify(result)

    def link_in_transaction(conn,pid,rid,source,details=None,append_page=False):
        photo = row(conn, 'batch_photos', pid)
        assignment = row(conn, 'assignments', photo['assignment_id'])
        if photo['excluded'] or photo['status'] == 'identifying':
            fail('正在识别身份，请完成后再确认。', 409)
        klass = row(conn, 'classes', assignment['class_id'])
        if klass['archived']:
            fail('班级已归档，请恢复后再关联。', 409)
        roster = conn.execute('SELECT * FROM assignment_students WHERE id=? AND assignment_id=?', (rid, assignment['id'])).fetchone()
        if roster is None:
            fail('只能关联当前班级任务中的学生。')
        if roster['exempt']:
            fail('免交学生不能关联照片。', 409)
        if photo.get('submission_id'):
            linked = conn.execute('SELECT assignment_student_id FROM submissions WHERE id=?', (photo['submission_id'],)).fetchone()
            if linked and linked['assignment_student_id'] != rid:
                fail('这张照片已经确认归属其他学生，不能覆盖。', 409)
            return dict(submission_id=photo['submission_id'], page_id=photo['page_id'], photo=photo_dict(photo))
        submission = conn.execute('SELECT * FROM submissions WHERE assignment_student_id=?', (rid,)).fetchone()
        if submission is None:
            values = (assignment['task_id'], roster['student_code'], stamp(), stamp(), rid)
            columns = 'task_id,student_code,created_at,updated_at,assignment_student_id'
            if tenant_mode:
                columns += ',owner_id'
                values += (g.teacher_id,)
            submission_id = conn.execute(f'INSERT INTO submissions({columns}) VALUES({",".join("?" for _ in values)})',
                                         values).lastrowid
            conn.execute('INSERT INTO reviews(submission_id,question_id) SELECT ?,id FROM questions WHERE task_id=?', (submission_id, assignment['task_id']))
        else:
            submission_id = submission['id']
            from grading_jobs import active_submission_job
            if submission['grading_state'] == 'running' or active_submission_job(conn, submission_id):
                fail('答卷正在批改，请完成后再追加照片。', 409)
        pages = conn.execute('SELECT image FROM pages WHERE submission_id=?',(submission_id,)).fetchall()
        if pages:
            if source == 'machine' or not append_page:
                fail('该学生已有照片。重复照片请排除；确为第二页时请勾选追加确认。',409)
            digest = hashlib.sha256((uploads/photo['image']).read_bytes()).digest()
            if any(hashlib.sha256((uploads/p['image']).read_bytes()).digest() == digest for p in pages):
                fail('这张照片与已有页面内容完全相同，请排除重复照片，不要追加。',409)
        recognized = json.loads(photo.get('ocr_result') or '{}')
        has_ocr = bool(recognized.get('blocks'))
        page_id = conn.execute('''INSERT INTO pages(submission_id,original,image,width,height,status,error,blocks,created_at,needs_review,answer_layout)
            VALUES(?,?,?,?,?,?,'',?,?,?,?)''',
            (submission_id, photo['original'], photo['image'], photo['width'], photo['height'],
             'done' if has_ocr else 'uploaded', photo['blocks'], stamp(), int(not has_ocr), photo['answer_layout'])).lastrowid
        if recognized:
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                         (page_id, stamp(), _json({k: v for k, v in recognized.items() if k != 'identity_band'} | {'source': 'identity_ocr'})))
        conn.execute("UPDATE submissions SET grading_state='idle',grading_error='' WHERE id=?", (submission_id,))
        conn.execute('UPDATE batch_photos SET status=?,submission_id=?,page_id=?,identity_source=?,updated_at=? WHERE id=?', ('confirmed', submission_id, page_id, source, stamp(), pid))
        details=details or {'method':'manual','reason':'教师对照原图确认归属。',
            'observed_name':json.loads(photo.get('match_details') or '{}').get('observed_name'),
            'candidates':json.loads(photo['candidates'])}
        save_match(conn,pid,details,rid)
        for q in conn.execute('SELECT question_id FROM reviews WHERE submission_id=?',(submission_id,)).fetchall():
            conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                (submission_id,q['question_id'],stamp(),_json({'action':'identity_linked','photo_id':pid,
                    'identity_source':source,'assignment_student':dict(roster),'match_details':details})))
        saved = photo_dict(conn.execute('SELECT * FROM batch_photos WHERE id=?', (pid,)).fetchone())
        return dict(submission_id=submission_id, page_id=page_id, photo=saved,
                       submission=submission_detail(conn, submission_id))

    def teacher_work(conn,sid):
        recorded=conn.execute('''SELECT 1 FROM reviews WHERE submission_id=? AND (teacher_edited=1 OR confirmed=1
            OR confirmed_blank=1 OR score_override IS NOT NULL OR reason!=''
            OR (transcript!='' AND auto_result='{}'))''',(sid,)).fetchone() is not None
        return recorded or conn.execute("SELECT 1 FROM review_events WHERE submission_id=? AND json_extract(snapshot,'$.action')='page_manual_review'",(sid,)).fetchone() is not None

    @app.post('/api/batch-photos/<int:pid>/reassign')
    def reassign_photo(pid):
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict) or payload.get('confirmed') is not True:
            fail('请对照原图明确确认新的学生归属。')
        if type(payload.get('auto_grade', False)) is not bool:
            fail('自动批改设置无效。')
        rid=integer(payload.get('assignment_student_id'),'应交名单 ID')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            photo=row(conn,'batch_photos',pid)
            details=json.loads(photo.get('match_details') or '{}')
            if not photo['submission_id'] or details.get('method') not in ('inferred','memory','band','corrected'):
                fail('仅学号栏、姓名推断或纠错记忆关联的照片支持安全改选；原有人工归属不能覆盖。',409)
            original=row(conn,'submissions',photo['submission_id'])
            from grading_jobs import active_submission_job
            if active_submission_job(conn, original['id']):
                fail('答卷正在后台处理中，请结束后再改选。',409)
            if original['assignment_student_id']==rid:
                return jsonify(submission_id=original['id'],page_id=photo['page_id'],photo=photo_dict(photo),submission=submission_detail(conn,original['id']))
            if photo.get('identity_source')!='machine' or details.get('method') not in ('inferred','memory','band'):
                fail('照片已由教师更正归属，不能再次自动覆盖。',409)
            assignment=row(conn,'assignments',photo['assignment_id'])
            if row(conn,'classes',assignment['class_id'])['archived']:
                fail('班级已归档，请恢复后再改选。',409)
            roster=conn.execute('SELECT * FROM assignment_students WHERE assignment_id=? AND id=?',(photo['assignment_id'],rid)).fetchone()
            if roster is None or roster['exempt']:
                fail('只能选择本次任务中未免交的学生。')
            page=row(conn,'pages',photo['page_id'])
            if (page['submission_id']!=original['id'] or original['grading_state']=='running' or page['status']=='running'
                or teacher_work(conn,original['id'])
                or conn.execute('SELECT count(*) FROM pages WHERE submission_id=?',(original['id'],)).fetchone()[0]!=1):
                fail('原答卷已有人工记录、多页或正在批改，已阻止覆盖，请核对处理。',409)
            target=conn.execute('SELECT * FROM submissions WHERE assignment_student_id=?',(rid,)).fetchone()
            if target and (target['grading_state']=='running' or active_submission_job(conn,target['id']) or teacher_work(conn,target['id'])
                or conn.execute('SELECT 1 FROM pages WHERE submission_id=?',(target['id'],)).fetchone()):
                fail('目标学生已有照片、人工记录或正在批改，不能覆盖。',409)
            if target:
                target_id=target['id']
            else:
                values = (assignment['task_id'],roster['student_code'],stamp(),stamp(),rid)
                columns = 'task_id,student_code,created_at,updated_at,assignment_student_id'
                if tenant_mode:
                    columns += ',owner_id'
                    values += (g.teacher_id,)
                target_id=conn.execute(f'INSERT INTO submissions({columns}) VALUES({",".join("?" for _ in values)})',
                                       values).lastrowid
                conn.execute('INSERT INTO reviews(submission_id,question_id) SELECT ?,id FROM questions WHERE task_id=?',(target_id,assignment['task_id']))
            before=[dict(r) for r in conn.execute('SELECT * FROM reviews WHERE submission_id=?',(original['id'],))]
            new_details=dict(details,method='corrected',reason='教师撤回学号栏匹配并重新选择学生。' if details.get('method')=='band' else '教师撤回姓名推断并重新选择学生。',
                previous_submission_id=original['id'],previous_assignment_student_id=original['assignment_student_id'])
            for sid in (original['id'],target_id):
                for q in before:
                    conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                        (sid,q['question_id'],stamp(),_json({'action':'identity_reassigned','photo_id':pid,'before':q,
                            'from_submission_id':original['id'],'to_submission_id':target_id,'match_details':new_details,
                            'assignment_student':dict(roster)})))
            conn.execute('UPDATE pages SET submission_id=? WHERE id=?',(target_id,page['id']))
            conn.execute("UPDATE reviews SET machine_text='',transcript='',regions='[]',auto_result='{}',confirmed=0,confirmed_blank=0,score_override=NULL,reason='' WHERE submission_id=?",(original['id'],))
            conn.execute("UPDATE submissions SET grading_state='idle',grading_error='',updated_at=? WHERE id IN (?,?)",(stamp(),original['id'],target_id))
            conn.execute("UPDATE batch_photos SET submission_id=?,identity_source='teacher',updated_at=? WHERE id=?",(target_id,stamp(),pid))
            save_match(conn,pid,new_details,rid)
            if details.get('method')=='memory':
                memory=row(conn,'identity_memories',details['memory_id'])
                name_memory.set_enabled(conn,memory,False,'identity_corrected')
            name_memory.learn_confirmation(conn,row(conn,'batch_photos',pid),dict(roster),assignment['class_id'],payload)
            result = dict(submission_id=target_id,page_id=page['id'],photo=photo_dict(row(conn,'batch_photos',pid)),submission=submission_detail(conn,target_id))
        if payload.get('auto_grade'):
            runner = app.extensions['grading_jobs']
            result['job'] = runner.enqueue(submission_id=target_id)
            runner.kick()
        return jsonify(result)

