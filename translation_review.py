"""Versioned translation suggestions. Only teachers write final grades."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from uuid import uuid4

from flask import abort, g, jsonify, request


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def migrate(database):
    """Explicit additive migration; caller must decide whether this database may change."""
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        def execute_script(script):
            # sqlite3.executescript commits an open transaction; execute complete statements instead.
            pending=''
            for line in script.splitlines(keepends=True):
                pending+=line
                if sqlite3.complete_statement(pending):
                    conn.execute(pending)
                    pending=''
            if pending.strip():
                raise ValueError('Incomplete migration statement')
        for table, columns in {
            'questions': [('scoring_mode', "TEXT NOT NULL DEFAULT 'strict'"), ('source_text', "TEXT NOT NULL DEFAULT ''"), ('translation_requirements', "TEXT NOT NULL DEFAULT ''")],
            'reviews': [('translation_meta', "TEXT NOT NULL DEFAULT '{}'"), ('translation_revision', 'INTEGER NOT NULL DEFAULT 0')],
            'grading_jobs': [('task_id', 'INTEGER REFERENCES tasks(id)')],
        }.items():
            existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name, definition in columns:
                if name not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        execute_script('''
        CREATE TABLE IF NOT EXISTS translation_rubrics (
          id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL REFERENCES questions(id),
          version INTEGER NOT NULL, question_hash TEXT NOT NULL, question_snapshot TEXT NOT NULL CHECK(json_valid(question_snapshot)),
          criteria_json TEXT NOT NULL CHECK(json_valid(criteria_json)), max_points_cents INTEGER NOT NULL CHECK(max_points_cents>0),
          origin TEXT NOT NULL, model_name TEXT NOT NULL DEFAULT 'unreported', model_version TEXT NOT NULL DEFAULT 'unreported',
          prompt_version TEXT NOT NULL DEFAULT 'translation-rubric-v1', created_by INTEGER, created_at TEXT NOT NULL,
          UNIQUE(question_id,version), UNIQUE(id,question_id));
        CREATE TABLE IF NOT EXISTS translation_ai_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES grading_jobs(id), submission_id INTEGER NOT NULL,
          question_id INTEGER NOT NULL, rubric_id INTEGER NOT NULL, attempt_no INTEGER NOT NULL,
          request_key TEXT NOT NULL, input_hash TEXT NOT NULL, answer_snapshot TEXT NOT NULL,
          input_snapshot TEXT NOT NULL CHECK(json_valid(input_snapshot)), state TEXT NOT NULL
            CHECK(state IN ('queued','requesting','succeeded','failed','unknown')),
          result_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result_json)), provider_metadata TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(provider_metadata)),
          error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, duration_ms INTEGER,
          FOREIGN KEY(submission_id,question_id) REFERENCES reviews(submission_id,question_id),
          FOREIGN KEY(rubric_id,question_id) REFERENCES translation_rubrics(id,question_id));
        CREATE INDEX IF NOT EXISTS translation_runs_question ON translation_ai_runs(submission_id,question_id,id);
        CREATE INDEX IF NOT EXISTS translation_runs_job ON translation_ai_runs(job_id,state);
        CREATE TRIGGER IF NOT EXISTS translation_rubric_immutable BEFORE UPDATE ON translation_rubrics
          BEGIN SELECT RAISE(ABORT,'published rubric is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS translation_run_final BEFORE UPDATE ON translation_ai_runs
          WHEN OLD.state IN ('succeeded','failed','unknown')
          BEGIN SELECT RAISE(ABORT,'translation attempt is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS translation_run_task BEFORE INSERT ON translation_ai_runs
          WHEN (SELECT task_id FROM submissions WHERE id=NEW.submission_id) !=
               (SELECT task_id FROM questions WHERE id=NEW.question_id)
          BEGIN SELECT RAISE(ABORT,'translation task mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS translation_run_identity BEFORE UPDATE ON translation_ai_runs
          WHEN NEW.submission_id!=OLD.submission_id OR NEW.question_id!=OLD.question_id OR NEW.rubric_id!=OLD.rubric_id
          BEGIN SELECT RAISE(ABORT,'translation identity is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS translation_job_task_insert BEFORE INSERT ON grading_jobs
          WHEN NEW.task_id IS NOT NULL AND NEW.submission_id IS NOT NULL AND NEW.task_id !=
               (SELECT task_id FROM submissions WHERE id=NEW.submission_id)
          BEGIN SELECT RAISE(ABORT,'translation job task mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS translation_job_task_update BEFORE UPDATE ON grading_jobs
          WHEN NEW.task_id IS NOT NULL AND NEW.submission_id IS NOT NULL AND NEW.task_id !=
               (SELECT task_id FROM submissions WHERE id=NEW.submission_id)
          BEGIN SELECT RAISE(ABORT,'translation job task mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS translation_job_assignment_insert BEFORE INSERT ON grading_jobs
          WHEN NEW.task_id IS NOT NULL AND NEW.assignment_id IS NOT NULL AND NEW.task_id !=
               (SELECT task_id FROM assignments WHERE id=NEW.assignment_id)
          BEGIN SELECT RAISE(ABORT,'translation job assignment mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS translation_job_assignment_update BEFORE UPDATE ON grading_jobs
          WHEN NEW.task_id IS NOT NULL AND NEW.assignment_id IS NOT NULL AND NEW.task_id !=
               (SELECT task_id FROM assignments WHERE id=NEW.assignment_id)
          BEGIN SELECT RAISE(ABORT,'translation job assignment mismatch'); END;
        ''')
        review_columns = {r[1] for r in conn.execute('PRAGMA table_info(reviews)')}
        if {'transcript','regions','machine_text','auto_result'} <= review_columns:
            execute_script('''CREATE TRIGGER IF NOT EXISTS translation_revision_on_input AFTER UPDATE OF
                transcript,regions,machine_text,auto_result ON reviews
                WHEN NEW.translation_revision=OLD.translation_revision AND
                  (SELECT scoring_mode FROM questions WHERE id=NEW.question_id)='translation' AND
                  (NEW.transcript IS NOT OLD.transcript OR NEW.regions IS NOT OLD.regions OR
                   NEW.machine_text IS NOT OLD.machine_text OR NEW.auto_result IS NOT OLD.auto_result)
                BEGIN UPDATE reviews SET translation_revision=translation_revision+1
                    WHERE submission_id=NEW.submission_id AND question_id=NEW.question_id; END;''')
        if 'owner_id' in {r[1] for r in conn.execute('PRAGMA table_info(grading_jobs)')}:
            execute_script('''
            CREATE TRIGGER IF NOT EXISTS translation_job_owner_insert BEFORE INSERT ON grading_jobs
              WHEN NEW.task_id IS NOT NULL AND (NEW.owner_id IS NULL OR NEW.owner_id IS NOT
                   (SELECT owner_id FROM tasks WHERE id=NEW.task_id))
              BEGIN SELECT RAISE(ABORT,'translation job owner mismatch'); END;
            CREATE TRIGGER IF NOT EXISTS translation_job_owner_update BEFORE UPDATE ON grading_jobs
              WHEN NEW.task_id IS NOT NULL AND (NEW.owner_id IS NULL OR NEW.owner_id IS NOT
                   (SELECT owner_id FROM tasks WHERE id=NEW.task_id))
              BEGIN SELECT RAISE(ABORT,'translation job owner mismatch'); END;
            ''')


def register(app, db, row, stamp, detail):
    from translation_grading import validate_rubric, score_response, rubric_prompt, grading_prompt
    endpoints_before = set(app.view_functions)

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400, description='请求必须是 JSON 对象。')
        return value

    def question(conn, qid):
        q = row(conn, 'questions', qid)
        row(conn, 'tasks', q['task_id'])
        if q['scoring_mode'] != 'translation':
            abort(400, description='这不是翻译评分题。')
        return q

    def writable(conn, task_id=None, sid=None, aid=None):
        if sid:
            sub = row(conn, 'submissions', sid)
            task_id = sub['task_id']
            if sub.get('assignment_student_id'):
                aid = row(conn, 'assignment_students', sub['assignment_student_id'])['assignment_id']
        if aid:
            assignment = row(conn, 'assignments', aid)
            if row(conn, 'classes', assignment['class_id'])['archived']:
                abort(409, description='班级已归档，请恢复后操作。')
        if task_id:
            row(conn, 'tasks', task_id)
            if conn.execute('''SELECT 1 FROM assignments a JOIN classes c ON c.id=a.class_id
                    WHERE a.task_id=? AND c.archived=1 LIMIT 1''', (task_id,)).fetchone():
                abort(409, description='题目关联归档班级，请恢复后操作。')

    def qsnapshot(q):
        return {key: q.get(key) for key in ('id','task_id','number','source_text','answer','translation_requirements','points')}

    def latest_rubric(conn, qid):
        record = conn.execute('SELECT * FROM translation_rubrics WHERE question_id=? ORDER BY version DESC LIMIT 1', (qid,)).fetchone()
        return dict(record) if record else None

    def rubric_dto(record):
        if not record:
            return None
        return dict(id=record['id'], version=record['version'], question_id=record['question_id'],
                    rubric=json.loads(record['criteria_json']), criteria=json.loads(record['criteria_json'])['criteria'],
                    origin=record['origin'], created_at=record['created_at'])

    def publish(conn, q, value, origin):
        rubric = validate_rubric(value, q['points'])
        current = latest_rubric(conn, q['id'])
        rid = conn.execute('''INSERT INTO translation_rubrics(question_id,version,question_hash,question_snapshot,
            criteria_json,max_points_cents,origin,created_by,created_at,model_name) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (q['id'], (current['version'] if current else 0)+1, digest(qsnapshot(q)), dumps(qsnapshot(q)),
             dumps(rubric), rubric['max_points_cents'], origin, getattr(g, 'teacher_id', None), stamp(),model_name() if origin=='ai' else 'teacher')).lastrowid
        return dict(conn.execute('SELECT * FROM translation_rubrics WHERE id=?', (rid,)).fetchone())

    def unit(conn, sid, qid):
        sub, q = row(conn, 'submissions', sid), question(conn, qid)
        if sub['task_id'] != q['task_id']:
            abort(404)
        review = conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?', (sid,qid)).fetchone()
        if review is None:
            abort(409, description='请先完成答卷分题。')
        return sub, q, dict(review)

    def input_snapshot(sub, q, review, rubric):
        return dict(question=qsnapshot(q), rubric_id=rubric['id'], answer=review['transcript'],
                    revision=review['translation_revision'], regions=review['regions'],
                    confirmed_blank=review['confirmed_blank'], assignment_student_id=sub.get('assignment_student_id'),
                    owner_id=sub.get('owner_id'), ocr_status=sub.get('grading_state'),
                    ocr_result=review.get('auto_result','{}'))

    def ocr_blocked(review):
        auto = json.loads(review.get('auto_result') or '{}')
        return not review['transcript'].strip() or (auto.get('uncertain') and not review['teacher_edited'])

    def run_dto(conn, record, sub, q, review, rubric):
        if not record:
            return None
        result = json.loads(record['result_json'])
        metadata=json.loads(record['provider_metadata'])
        for key in ('model_name','model_version','prompt_version','rubric_version'):
            result.setdefault(key,metadata.get(key,'unreported'))
        result.setdefault('suggested_score',None)
        result.setdefault('max_score',q['points'])
        result.setdefault('grading_status',record['state'])
        for dimension in ('semantic','completeness','grammar','expression'):
            result.setdefault(dimension+'_score',None)
        fresh = bool(rubric and record['rubric_id'] == rubric['id'] and
                     record['input_hash'] == digest(input_snapshot(sub,q,review,rubric)))
        result.update(id=record['id'], ai_run_id=record['id'], state=record['state'], error_code=record['error_code'],
                      answer_snapshot=record['answer_snapshot'], rubric_id=record['rubric_id'],
                      created_at=record['created_at'], stale=not fresh,question_id=q['id'],submission_id=sub['id'])
        result['can_adopt'] = bool(result.get('can_adopt') and fresh and not ocr_blocked(review))
        return result

    def submission_dto(conn, sid):
        sub = row(conn,'submissions',sid)
        output = []
        for record in conn.execute("SELECT * FROM questions WHERE task_id=? AND scoring_mode='translation' ORDER BY number", (sub['task_id'],)):
            q = dict(record)
            review = conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,q['id'])).fetchone()
            if not review:
                continue
            review = dict(review)
            rubric = latest_rubric(conn,q['id'])
            valid = conn.execute("SELECT * FROM translation_ai_runs WHERE submission_id=? AND question_id=? AND state='succeeded' ORDER BY id DESC LIMIT 1", (sid,q['id'])).fetchone()
            last = conn.execute('SELECT * FROM translation_ai_runs WHERE submission_id=? AND question_id=? ORDER BY id DESC LIMIT 1',(sid,q['id'])).fetchone()
            ai = run_dto(conn,valid,sub,q,review,rubric)
            status = ('confirmed' if review['confirmed'] else 'blocked_ocr' if ocr_blocked(review) else
                      'failed' if last and last['state'] in ('failed','unknown') else
                      ('awaiting_confirmation' if ai.get('can_adopt') else 'ai_anomaly') if ai and not ai['stale'] else 'waiting')
            output.append(dict(question_id=q['id'], number=q['number'], source_text=q['source_text'],
                reference_answer=q['answer'], requirements=q['translation_requirements'], points=q['points'],
                student_answer=review['transcript'], confirmed_blank=bool(review['confirmed_blank']), revision=review['translation_revision'],
                rubric=rubric_dto(rubric), ai_result=ai, latest_attempt=run_dto(conn,last,sub,q,review,rubric),
                teacher_confirmation=json.loads(review['translation_meta']) if review['confirmed'] else None,
                final_score=review['score_override'] if review['confirmed'] else None, status=status))
        return dict(submission_id=sid, student_code=sub['student_code'], questions=output,
                    pages=[dict(id=p['id'],url=f"/api/pages/{p['id']}/image") for p in conn.execute('SELECT id FROM pages WHERE submission_id=?',(sid,))])

    @app.get('/api/questions/<int:qid>/translation-rubric')
    def translation_rubric(qid):
        with db() as conn:
            question(conn,qid)
            return jsonify(rubric=rubric_dto(latest_rubric(conn,qid)))

    @app.post('/api/questions/<int:qid>/translation-rubrics')
    def translation_publish(qid):
        value = body()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            q = question(conn,qid)
            writable(conn,task_id=q['task_id'])
            current = latest_rubric(conn,qid)
            if type(value.get('expected_version')) is not int or value['expected_version'] != (current['version'] if current else 0):
                abort(409, description='细则版本已经变化，请刷新。')
            if conn.execute("SELECT 1 FROM grading_jobs WHERE state IN ('queued','running','interrupted') AND (task_id=? OR assignment_id IN (SELECT id FROM assignments WHERE task_id=?))",(q['task_id'],q['task_id'])).fetchone():
                abort(409,description='请等待相关后台任务结束后发布细则。')
            try:
                record = publish(conn,q,value.get('rubric'),'teacher')
            except ValueError:
                abort(400, description='细则结构或分值预算无效。')
            return jsonify(rubric=rubric_dto(record)),201

    @app.get('/api/submissions/<int:sid>/translation')
    def translation_submission(sid):
        with db() as conn:
            return jsonify(submission_dto(conn,sid))

    @app.get('/api/submissions/<int:sid>/translation-history')
    def translation_history(sid):
        with db() as conn:
            row(conn,'submissions',sid)
            qid = request.args.get('question_id',type=int)
            if qid:
                unit(conn,sid,qid)
            limit = min(100,max(1,request.args.get('limit',30,type=int)))
            cursor = request.args.get('cursor',2**63-1,type=int)
            records = conn.execute('''SELECT id,question_id,rubric_id,attempt_no,state,error_code,created_at,result_json
                 FROM translation_ai_runs WHERE submission_id=? AND (? IS NULL OR question_id=?) AND id<? ORDER BY id DESC LIMIT ?''', (sid,qid,qid,cursor,limit)).fetchall()
            events = [dict(e) for e in conn.execute('SELECT * FROM review_events WHERE submission_id=? AND (? IS NULL OR question_id=?) ORDER BY id DESC LIMIT ?', (sid,qid,qid,limit))]
            for event in events:
                event['snapshot'] = json.loads(event['snapshot'])
            return jsonify(runs=[dict(dict(r),result=json.loads(r['result_json'])) for r in records],events=events,
                           next_cursor=records[-1]['id'] if len(records)==limit else None)

    @app.get('/api/assignments/<int:aid>/translation')
    def translation_assignment(aid):
        with db() as conn:
            conn.execute('BEGIN')
            assignment = row(conn,'assignments',aid)
            students=[]
            submitted=set()
            for sub in conn.execute('''SELECT s.id,ast.name,ast.id AS roster_id FROM submissions s JOIN assignment_students ast
                  ON ast.id=s.assignment_student_id WHERE ast.assignment_id=? AND ast.exempt=0 ORDER BY ast.student_code,s.id''',(aid,)):
                value=submission_dto(conn,sub['id'])
                submitted.add(sub['roster_id'])
                for q in value['questions']:
                    students.append(dict(q,submission_id=sub['id'],student_code=value['student_code'],name=sub['name'],
                        suggested_score=(q['ai_result'] or {}).get('suggested_score')))
            jobs=[dict(j) for j in conn.execute("SELECT * FROM grading_jobs WHERE state IN ('queued','running') AND (assignment_id=? OR submission_id IN (SELECT s.id FROM submissions s JOIN assignment_students ast ON ast.id=s.assignment_student_id WHERE ast.assignment_id=?))",(aid,aid))]
            active_ids={sid for j in jobs for sid in json.loads(j['grade_ids'])}
            for student in students:
                if student['submission_id'] in active_ids and student['status']=='waiting':
                    student['status']='processing'
            summary={key:sum(s['status']==key for s in students) for key in ('confirmed','blocked_ocr','failed','waiting','awaiting_confirmation','ai_anomaly','processing')}
            summary['total']=len(students)
            summary.update(submitted=len(submitted),ai_completed=sum(bool(s['ai_result'] and not s['ai_result']['stale']) for s in students),needs_review=len(students)-summary['confirmed'])
            summary['ai_succeeded']=summary['ai_completed']
            summary.update(queued=sum(j['state']=='queued' for j in jobs),running=sum(j['state']=='running' for j in jobs))
            priority={'blocked_ocr':0,'failed':1,'ai_anomaly':2,'waiting':3,'processing':3,'awaiting_confirmation':4,'confirmed':5}
            students.sort(key=lambda s:((priority[s['status']] if request.args.get('t.order')!='number' else 0),s['student_code'],s['submission_id'],s['number']))
            query=request.args.get('t.q','').casefold()
            status=request.args.get('t.status','')
            qfilter=request.args.get('t.question',type=int)
            filtered=[s for s in students if (not query or query in (s['student_code']+' '+s['name']).casefold()) and
                      (not status or status==s['status']) and (not qfilter or s['question_id']==qfilter)]
            limit=min(100,max(1,request.args.get('limit',50,type=int)))
            cursor=max(0,request.args.get('cursor',0,type=int))
            return jsonify(summary=summary,jobs=[app.extensions['grading_jobs'].dto(j) for j in jobs],processing_job=app.extensions['grading_jobs'].dto(jobs[-1]) if jobs else None,students=filtered[cursor:cursor+limit],filtered_total=len(filtered),
                next_cursor=cursor+limit if cursor+limit<len(filtered) else None,snapshot_at=stamp(),
                questions=[dict(q) for q in conn.execute("SELECT id AS question_id,number,source_text,points FROM questions WHERE task_id=? AND scoring_mode='translation' ORDER BY number",(assignment['task_id'],))])

    def enqueue(task_id=None, aid=None, sid=None):
        value=body()
        request_key=value.get('request_key') or uuid4().hex
        if not isinstance(request_key,str) or not 1<=len(request_key)<=160:
            abort(400,description='请求编号无效。')
        if value.get('scope','missing') not in ('missing','retry_failed','regrade_unconfirmed'):
            abort(400,description='评分范围无效。')
        with db() as conn:
            writable(conn,task_id=task_id,sid=sid,aid=aid)
            if sid:
                task_id=row(conn,'submissions',sid)['task_id']
            if aid:
                task_id=row(conn,'assignments',aid)['task_id']
            qs=[r[0] for r in conn.execute("SELECT id FROM questions WHERE task_id=? AND scoring_mode='translation' ORDER BY number",(task_id,))]
            selected=value.get('question_ids',qs)
            if not isinstance(selected,list) or not selected or any(type(q) is not int or q not in qs for q in selected) or len(selected)!=len(set(selected)):
                abort(400,description='请选择本作业的翻译题。')
            payload_hash=digest(dict(value,question_ids=selected,task_id=task_id,aid=aid,sid=sid))
            for existing in conn.execute('SELECT * FROM grading_jobs WHERE task_id=? OR assignment_id=? OR submission_id=? ORDER BY id DESC',(task_id,aid,sid)):
                opts=json.loads(existing['options'])
                if opts.get('request_key')==request_key:
                    row(conn,'grading_jobs',existing['id'])
                    if opts.get('payload_hash')!=payload_hash:
                        abort(409,description='同一请求编号不能用于不同内容。')
                    return jsonify(job=app.extensions['grading_jobs'].dto(existing)),202
            from grading_jobs import active_submission_job
            active = (active_submission_job(conn,sid,('queued','running','interrupted')) if sid else
                      conn.execute("SELECT id FROM grading_jobs WHERE (task_id=? OR assignment_id=?) AND state IN ('queued','running','interrupted') LIMIT 1",(task_id,aid)).fetchone())
            if active:
                abort(409,description='已有后台任务，请等待结束或继续原任务。')
            units=([[None,qid] for qid in selected] if not aid and not sid else
                   [[s,qid] for s in ([sid] if sid else [r[0] for r in conn.execute('''SELECT s.id FROM submissions s
                       JOIN assignment_students ast ON ast.id=s.assignment_student_id
                       WHERE ast.assignment_id=? AND ast.exempt=0 ORDER BY s.id''',(aid,))]) for qid in selected])
            options=dict(question_ids=selected,units=units,request_key=request_key,payload_hash=payload_hash,
                         scope=value.get('scope','missing'),rubric_ids={str(q): (latest_rubric(conn,q) or {}).get('id') for q in selected},
                         expected_versions=value.get('expected_versions',{}))
            options['translation_only' if sid or aid else 'rubric_only']=True
        runner=app.extensions['grading_jobs']
        result=runner.enqueue(task_id=task_id if not aid and not sid else None,assignment_id=aid,submission_id=sid,options=options)
        with db() as conn:
            returned=json.loads(row(conn,'grading_jobs',result['id'])['options'])
            if returned.get('payload_hash')!=payload_hash or returned.get('request_key')!=request_key:
                abort(409,description='已有另一项后台任务，请等待结束后重试。')
        runner.kick()
        return jsonify(job=result),202

    @app.post('/api/tasks/<int:tid>/translation-rubric-jobs')
    def translation_rubric_job(tid):
        return enqueue(task_id=tid)

    @app.post('/api/assignments/<int:aid>/translation-jobs')
    def translation_assignment_job(aid):
        return enqueue(aid=aid)

    @app.post('/api/submissions/<int:sid>/translation-jobs')
    def translation_submission_job(sid):
        return enqueue(sid=sid)

    def manual_result(value,rubric,answer,source):
        from decimal import Decimal, InvalidOperation
        def cents(v):
            if type(v) not in (int,float):
                raise ValueError()
            try:
                n=Decimal(str(v))*100
                if not n.is_finite() or n!=n.to_integral_value():
                    raise ValueError()
                return int(n)
            except InvalidOperation:
                raise ValueError() from None
        criteria=value.get('criteria',value.get('final_criteria'))
        components=value.get('components',value.get('final_components'))
        score=value.get('score',value.get('final_score'))
        if not isinstance(criteria,list) or not isinstance(components,dict):
            raise ValueError()
        by_id={c['criterion_id']:c for c in criteria}
        if len(by_id)!=len(criteria) or set(by_id)!={c['id'] for c in rubric['criteria']}:
            raise ValueError()
        totals=dict.fromkeys(('semantic','completeness','grammar','expression'),0)
        normalized=[]
        override=False
        for c in rubric['criteria']:
            award=by_id[c['id']].get('awarded_points_cents')
            if type(award) is not int or not 0<=award<=c['max_points_cents']:
                raise ValueError()
            override |= award not in c['allowed_levels'].values()
            totals[c['dimension']]+=award
            normalized.append(dict(criterion_id=c['id'],awarded_points_cents=award))
        if set(components)!=set(totals) or any(cents(components[k])!=v for k,v in totals.items()) or cents(score)!=sum(totals.values()):
            raise ValueError()
        if override and not str(value.get('reason','')).strip():
            raise ValueError()
        errors=value.get('errors',value.get('final_errors',[]))
        if not isinstance(errors,list) or len(errors)>100 or not isinstance(value.get('feedback',''),str):
            raise ValueError()
        normalized_errors=[]
        seen=set()
        for error in errors:
            if not isinstance(error,dict) or not isinstance(error.get('explanation'),str) or len(error['explanation'])>4000:
                raise ValueError()
            error=dict(error)
            cid=error.get('affected_criterion')
            if cid is not None and (not isinstance(cid,str) or cid not in by_id):
                raise ValueError()
            locations=error.get('segments',[error] if 'start' in error else [])
            if not isinstance(locations,list) or len(locations)>30:
                raise ValueError()
            for location in locations:
                from translation_grading import _span
                _span(location,answer,source,error.get('error_type') in ('semantic','mistranslation','omission','completeness','logic'))
            deficit=next((c['max_points_cents']-by_id[c['id']]['awarded_points_cents'] for c in rubric['criteria'] if c['id']==cid),0)
            error['deduction']=deficit/100 if cid not in seen else 0
            if deficit and not locations:
                raise ValueError()
            if cid:
                seen.add(cid)
            normalized_errors.append(error)
        if any(by_id[c['id']]['awarded_points_cents']<c['max_points_cents'] and c['id'] not in seen for c in rubric['criteria']) and not str(value.get('reason','')).strip():
            raise ValueError()
        return dict(suggested_score=score,criteria_results=normalized,errors=normalized_errors,feedback=value.get('feedback',''),
                    teacher_override=override,**{k+'_score':v/100 for k,v in totals.items()})

    def confirm(sid,qid,reopen=False):
        value=body()
        action='reopen' if reopen else value.get('action')
        if not isinstance(action,str) or action not in ('adopt','manual','transcript','reopen'):
            abort(400,description='确认操作无效。')
        key=value.get('request_key')
        if not isinstance(key,str) or not 1<=len(key)<=160:
            abort(400,description='请提供请求编号。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub,q,review=unit(conn,sid,qid)
            writable(conn,sid=sid)
            payload_hash=digest(value)
            for event in conn.execute('SELECT snapshot FROM review_events WHERE submission_id=? AND question_id=? ORDER BY id DESC',(sid,qid)):
                old=json.loads(event['snapshot'])
                if old.get('request_key')==key:
                    if old.get('payload_hash')!=payload_hash:
                        abort(409,description='同一请求编号不能用于不同内容。')
                    return jsonify(detail(conn,sid))
            if type(value.get('expected_revision')) is not int or value['expected_revision']!=review['translation_revision']:
                abort(409,description='答案已被修改，请刷新后操作。')
            if review['confirmed'] and action!='reopen':
                abort(409,description='本题已确认，请明确重新开启后修改。')
            from grading_jobs import active_submission_job
            if active_submission_job(conn,sid) or conn.execute("SELECT 1 FROM pages WHERE submission_id=? AND status='running'",(sid,)).fetchone():
                abort(409,description='本答卷仍有后台任务，请结束后再修改。')
            meta={}
            score=None
            confirmed=False
            transcript=review['transcript']
            blank=review['confirmed_blank']
            reason=value.get('reason','')
            if not isinstance(reason,str) or len(reason)>2000:
                abort(400,description='理由无效。')
            if action=='transcript':
                transcript=value.get('transcript')
                if not isinstance(transcript,str) or len(transcript)>20000 or type(value.get('confirmed_blank',False)) is not bool:
                    abort(400,description='转录内容无效。')
                blank=int(value.get('confirmed_blank',False))
                if blank and transcript.strip():
                    abort(400,description='已确认空白不能同时填写答案。')
            elif action=='reopen':
                if not reason.strip():
                    abort(400,description='重新开启请填写理由。')
            else:
                rubric=latest_rubric(conn,qid)
                if not rubric or rubric['question_hash']!=digest(qsnapshot(q)):
                    abort(409,description='请先发布当前题目的有效细则。')
                if action=='adopt':
                    ai=conn.execute('SELECT * FROM translation_ai_runs WHERE id=? AND submission_id=? AND question_id=?',
                                    (value.get('ai_run_id'),sid,qid)).fetchone()
                    result=run_dto(conn,ai,sub,q,review,rubric)
                    if not result or not result.get('can_adopt'):
                        abort(409,description='建议已过期或需人工核查。')
                    saved_result=json.loads(ai['result_json'])
                    core_input={k:saved_result[k] for k in ('criteria_results','errors','feedback','confidence_level')}
                    result=score_response(core_input,json.loads(rubric['criteria_json']),review['transcript'],q['source_text'])
                    source_ai=ai['id']
                else:
                    if not review['transcript'].strip() and not review['confirmed_blank']:
                        abort(409,description='请先明确确认本题为空白，再人工评分。')
                    try:
                        result=manual_result(value,json.loads(rubric['criteria_json']),review['transcript'],q['source_text'])
                    except (ValueError,KeyError,TypeError):
                        abort(400,description='请核对每项预算、四维合计与总分；裁量分须填写理由。')
                    source_ai=None
                score=result['suggested_score']
                confirmed=True
                meta=dict(final_score=score,final_components={k:result[k+'_score'] for k in ('semantic','completeness','grammar','expression')},
                          final_criteria=result['criteria_results'],final_errors=result['errors'],feedback=result['feedback'],
                          source_ai_run_id=source_ai,rubric_id=rubric['id'],actor_id=getattr(g,'teacher_id',None),
                          confirmed_at=stamp(),answer_hash=digest(review['transcript']),teacher_override=result.get('teacher_override',False))
            conn.execute('''UPDATE reviews SET transcript=?,confirmed_blank=?,confirmed=?,teacher_edited=1,score_override=?,
                translation_meta=?,translation_revision=translation_revision+1,reason=? WHERE submission_id=? AND question_id=?''',
                (transcript,blank,int(confirmed),score,dumps(meta),reason,sid,qid))
            after=dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone())
            conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                (sid,qid,stamp(),dumps(dict(action='translation_'+action,before=review,after=after,actor=getattr(g,'teacher_id',None),
                                         request_key=key,payload_hash=payload_hash))))
            conn.execute('UPDATE submissions SET updated_at=? WHERE id=?',(stamp(),sid))
            return jsonify(detail(conn,sid))

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/translation-confirm')
    def translation_confirm(sid,qid):
        return confirm(sid,qid)

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/translation-reopen')
    def translation_reopen(sid,qid):
        return confirm(sid,qid,True)

    def model(prompt):
        import settings
        config=settings.read_ai_review(db)
        if not all(config.get(key) for key in ('base_url','model','api_key')):
            config=settings.visual_review_config()
        return settings._chat(config,None,prompt,timeout=60)

    def model_name():
        import settings
        config=settings.read_ai_review(db)
        if config.get('model'):
            return config['model']
        try:
            return settings.visual_review_config().get('model') or 'unreported'
        except ValueError:
            return 'unreported'

    app.extensions.setdefault('translation_model',model)

    def step(runner,job):
        options=json.loads(job['options'])
        if 'units' not in options:
            with db() as conn:
                if options.get('rubric_only'):
                    qs=options.get('question_ids') or [q[0] for q in conn.execute("SELECT id FROM questions WHERE task_id=? AND scoring_mode='translation' ORDER BY number",(job['task_id'],))]
                    options['units']=[[None,qid] for qid in qs]
                else:
                    options['units']=[]
                    for sid in json.loads(job['grade_ids']):
                        sub=row(conn,'submissions',sid)
                        options['units'] += [[sid,q[0]] for q in conn.execute("SELECT id FROM questions WHERE task_id=? AND scoring_mode='translation' ORDER BY number",(sub['task_id'],)) if not options.get('question_ids') or q[0] in options['question_ids']]
                options['rubric_ids']={str(qid):(latest_rubric(conn,qid) or {}).get('id') for _,qid in options['units']}
                options.setdefault('request_key',uuid4().hex)
            runner.save(job,options=dumps(options),total=len(options['units']),grade_cursor=0,completed=0)
        cursor=job['grade_cursor']
        if cursor>=len(options['units']):
            runner.save(job,state='done',message='翻译处理已结束；建议分需教师确认。')
            return
        sid,qid=options['units'][cursor]
        with db() as conn:
            q=question(conn,qid)
            writable(conn,task_id=q['task_id'] if sid is None else None,sid=sid)
            rubric=latest_rubric(conn,qid)
            pinned=options.get('rubric_ids',{}).get(str(qid))
            if pinned and (not rubric or rubric['id']!=pinned):
                raise ValueError('rubric changed')
            expected=options.get('expected_versions',{}).get(str(qid))
            if expected is not None and expected!=(rubric['version'] if rubric else 0):
                raise ValueError('rubric changed')
        # Persist the billable boundary before calling the provider. Resumption never repeats an uncertain request.
        if not rubric or (sid is None and (options.get('scope')=='regrade_unconfirmed' or str(qid) in options.get('expected_versions',{}))):
            if options.get('rubric_requesting'):
                runner.error(job,'细则请求结果未知，请明确重试。',question_id=qid)
                options.pop('rubric_requesting',None)
                runner.save(job,options=dumps(options),grade_cursor=cursor+1,completed=cursor+1,errors=job['errors'])
                return
            options['rubric_requesting']=qid
            runner.save(job,options=dumps(options))
            try:
                raw=app.extensions['translation_model'](rubric_prompt(q))
                value=validate_rubric(raw,q['points'])
                with db() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    current=question(conn,qid)
                    writable(conn,task_id=q['task_id'])
                    if digest(qsnapshot(current))!=digest(qsnapshot(q)):
                        raise ValueError('question changed')
                    rubric=publish(conn,current,value,'ai')
                    options.pop('rubric_requesting',None)
                    options.setdefault('rubric_ids',{})[str(qid)]=rubric['id']
                    conn.execute('UPDATE grading_jobs SET options=?,updated_at=? WHERE id=?',(dumps(options),stamp(),job['id']))
            except Exception:
                runner.error(job,'细则生成失败，请检查配置或手动发布细则。',question_id=qid)
                options.pop('rubric_requesting',None)
                runner.save(job,options=dumps(options),grade_cursor=cursor+1,completed=cursor+1,errors=job['errors'])
                return
            options.pop('rubric_requesting',None)
            options.setdefault('rubric_ids',{})[str(qid)]=rubric['id']
            runner.save(job,options=dumps(options))
        if sid is not None:
            grade_unit(runner,job,options,sid,qid,rubric)
        runner.save(job,grade_cursor=cursor+1,completed=cursor+1,errors=job['errors'])

    def grade_unit(runner,job,options,sid,qid,rubric):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub,q,review=unit(conn,sid,qid)
            if review['confirmed']:
                return
            snapshot=input_snapshot(sub,q,review,rubric)
            input_hash=digest(snapshot)
            prior=conn.execute('SELECT * FROM translation_ai_runs WHERE submission_id=? AND question_id=? AND input_hash=? ORDER BY id DESC LIMIT 1',(sid,qid,input_hash)).fetchone()
            if options.get('scope','missing') in ('missing','retry_failed') and conn.execute(
                "SELECT 1 FROM translation_ai_runs WHERE submission_id=? AND question_id=? AND input_hash=? AND state='succeeded' LIMIT 1",(sid,qid,input_hash)).fetchone():
                return
            if prior:
                if prior['state']=='requesting':
                    conn.execute("UPDATE translation_ai_runs SET state='unknown',error_code='interrupted',finished_at=? WHERE id=?",(stamp(),prior['id']))
                    runner.error(job,'上次调用结果未知，需要明确重试。',submission_id=sid,question_id=qid)
                    return
                if prior['job_id']==job['id'] or (options.get('scope','missing')=='missing' and prior['state']=='succeeded'):
                    return
            if ocr_blocked(review):
                runner.error(job,'请先核对本题转录；OCR空白或不确定不计零分。',submission_id=sid,question_id=qid)
                return
            attempt=conn.execute('SELECT COUNT(*) FROM translation_ai_runs WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone()[0]+1
            run_id=conn.execute('''INSERT INTO translation_ai_runs(job_id,submission_id,question_id,rubric_id,attempt_no,
                request_key,input_hash,answer_snapshot,input_snapshot,state,created_at,started_at,provider_metadata)
                VALUES(?,?,?,?,?,?,?,?,?,'requesting',?,?,?)''',
                (job['id'],sid,qid,rubric['id'],attempt,options['request_key'],input_hash,review['transcript'],dumps(snapshot),stamp(),stamp(),
                 dumps(dict(model_name=model_name(),model_version='unreported',prompt_version='translation-grade-v1',rubric_version=rubric['version'],tokens=None,cost=None)))).lastrowid
        started=time.monotonic()
        try:
            raw=app.extensions['translation_model'](grading_prompt(q,json.loads(rubric['criteria_json']),review['transcript']))
        except Exception:
            result={}
            state,code='unknown','provider_unconfirmed'
            runner.error(job,'本题服务调用结果未知，后续任务已暂停；明确重试可能产生新的调用费用。',submission_id=sid,question_id=qid)
            runner.save(job,state='failed')
        else:
            try:
                result=score_response(raw,json.loads(rubric['criteria_json']),review['transcript'],q['source_text'])
                state,code='succeeded',''
            except Exception:
                result={}
                state,code='failed','invalid_response'
                runner.error(job,'本题分析格式无效，未写入最终成绩。',submission_id=sid,question_id=qid)
        if result:
            with db() as conn:
                student=conn.execute('SELECT student_id FROM assignment_students WHERE id=?',(sub.get('assignment_student_id'),)).fetchone()
            result.update(question_id=qid,submission_id=sid,student_id=student[0] if student else None,
                model_name=model_name(),model_version='unreported',prompt_version='translation-grade-v1',
                rubric_version=rubric['version'],rubric_id=rubric['id'],created_at=stamp(),input_hash=input_hash,
                answer_snapshot=review['transcript'])
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current_sub,current_q,current_review=unit(conn,sid,qid)
            try:
                writable(conn,sid=sid)
                fresh=digest(input_snapshot(current_sub,current_q,current_review,rubric))==input_hash
            except Exception:
                fresh=False
            if result and not fresh:
                result.update(can_adopt=False,risk_flags=result.get('risk_flags',[])+['input_changed'])
            conn.execute('''UPDATE translation_ai_runs SET state=?,result_json=?,error_code=?,finished_at=?,duration_ms=?
                    WHERE id=? AND state='requesting' ''',(state,dumps(result),code,stamp(),int((time.monotonic()-started)*1000),run_id))

    app.extensions['translation_job_step']=step
    app.extensions['translation_endpoints']=set(app.view_functions)-endpoints_before
