"""Local durable batch progress; existing routes own OCR, matching and scoring."""
import json
import threading

from flask import abort, g, jsonify, request
from teacher_auth import internal_post


def ai_review_summary(options):
    stats = options.get('ai_metrics', {})
    used, resolved = options.get('ai_used', 0), options.get('ai_resolved', 0)
    suggestions = stats.get('suggestions', 0)
    reused = stats.get('reused_suggestions')
    if reused is None and stats.get('requests') == 0:
        reused = suggestions
    counts = (f'新生成建议 {suggestions-reused} 题，复用已有建议 {reused} 题' if reused is not None else
              f'可用建议 {suggestions} 题（历史记录未区分新增与复用）')
    requests = stats.get('requests', '未记录')
    return (f"AI 复核结束：覆盖 {used} 题，图片请求 {requests} 次，有效返回 {stats.get('valid',0)} 次，"
            f"{counts}，自动解决 {resolved} 题，本次仍待确认 {stats.get('pending',max(0,used-resolved))} 题。")


def active_submission_job(conn, sid, states=('queued', 'running')):
    return conn.execute(f'''SELECT j.* FROM grading_jobs j WHERE j.state IN ({','.join('?' for _ in states)})
        AND (j.submission_id=? OR EXISTS(SELECT 1 FROM json_each(j.grade_ids) WHERE value=?)
        OR EXISTS(SELECT 1 FROM json_each(j.photo_ids) ids JOIN batch_photos p ON p.id=ids.value
                  WHERE p.submission_id=?)) ORDER BY j.id DESC LIMIT 1''', (*states, sid, sid, sid)).fetchone()


def register(app, db, row, stamp):
    runner = GradingJobs(app, db, row, stamp)
    app.extensions['grading_jobs'] = runner

    def payload():
        if not isinstance(request.get_json(silent=True), dict):
            abort(400, description='任务请求必须是 JSON 对象。')

    @app.get('/api/grading-jobs/<int:jid>')
    def get_grading_job(jid):
        with db() as conn:
            return jsonify(job=runner.dto(row(conn, 'grading_jobs', jid)))

    @app.get('/api/assignments/<int:aid>/grading-jobs')
    def latest_grading_job(aid):
        with db() as conn:
            row(conn, 'assignments', aid)
            job = conn.execute('SELECT * FROM grading_jobs WHERE assignment_id=? ORDER BY id DESC LIMIT 1', (aid,)).fetchone()
        return jsonify(job=runner.dto(job) if job else None)

    @app.post('/api/assignments/<int:aid>/grading-jobs')
    def start_grading_job(aid):
        payload()
        with db() as conn:
            row(conn, 'assignments', aid)
        result = runner.enqueue(assignment_id=aid)
        runner.kick()
        return jsonify(job=result), 202

    @app.post('/api/assignments/<int:aid>/ai-review-jobs')
    def start_ai_review_job(aid):
        payload()
        with db() as conn:
            row(conn, 'assignments', aid)
        if not app.extensions['read_ai_review']()['enabled']:
            abort(409, description='请先配置并启用 AI 自动复核。')
        result = runner.enqueue(assignment_id=aid, options={'ai_review_only': True})
        runner.kick()
        return jsonify(job=result), 202

    @app.post('/api/assignments/<int:aid>/identity-review-jobs')
    def start_identity_review_job(aid):
        payload()
        if request.get_json():
            abort(400, description='身份复核只接受空请求对象。')
        config = app.extensions['read_ai_review']()
        if not all(config.get(k) for k in ('base_url', 'model', 'api_key')):
            abort(409, description='请先保存 AI 复核接口配置。')
        with db() as conn:
            row(conn, 'assignments', aid)
            photos=conn.execute("SELECT id,ocr_result FROM batch_photos WHERE assignment_id=? AND status='identified' AND submission_id IS NULL AND excluded=0 ORDER BY id", (aid,)).fetchall()
            ids = [p['id'] for p in photos]
            retries={str(p['id']):r.get('request_id',r.get('created_at')) for p in photos
                     if (r:=json.loads(p['ocr_result'] or '{}').get('identity_review')) and r.get('status')!='requesting'}
        if not ids:
            abort(409, description='没有已完成 OCR 的待归属照片。')
        result = runner.enqueue(assignment_id=aid, photo_ids=ids, options={'identity_review_only': True,'identity_retry_requests':retries})
        runner.kick()
        return jsonify(job=result), 202

    @app.post('/api/batch-photos/<int:pid>/process')
    def process_photo(pid):
        payload()
        with db() as conn:
            photo = row(conn, 'batch_photos', pid)
            if photo['excluded']:
                abort(409, description='照片已排除，请恢复后再处理。')
        result = runner.enqueue(assignment_id=photo['assignment_id'], photo_ids=[pid])
        runner.kick()
        return jsonify(job=result), 202

    @app.get('/api/submissions/<int:sid>/grading-jobs')
    def submission_job(sid):
        with db() as conn:
            row(conn, 'submissions', sid)
            job = active_submission_job(conn, sid, ('queued','running','interrupted','failed','done'))
            return jsonify(job=runner.dto(job) if job else None)

    @app.post('/api/submissions/<int:sid>/grading-jobs')
    def start_submission_job(sid):
        payload()
        options = request.get_json()
        if set(options) - {'rerun','reextract','retry_missing'} or any(type(v) is not bool for v in options.values()) or sum(options.values()) > 1:
            abort(400, description='请选择一种批改操作。')
        result = runner.enqueue(submission_id=sid, options=options)
        runner.kick()
        return jsonify(job=result), 202

    @app.post('/api/grading-jobs/<int:jid>/resume')
    def resume_grading_job(jid):
        payload()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = row(conn, 'grading_jobs', jid)
            assignment = row(conn, 'assignments', job['assignment_id']) if job['assignment_id'] else None
            if not assignment and job['submission_id']:
                assignment = conn.execute('''SELECT a.* FROM assignments a JOIN assignment_students s ON s.assignment_id=a.id
                    JOIN submissions sub ON sub.assignment_student_id=s.id WHERE sub.id=?''', (job['submission_id'],)).fetchone()
            if assignment and row(conn, 'classes', assignment['class_id'])['archived']:
                abort(409, description='班级已归档，请恢复后再继续。')
            if job['state'] in ('interrupted', 'failed'):
                active = (active_submission_job(conn, job['submission_id']) if job['submission_id'] else
                    conn.execute("SELECT id FROM grading_jobs WHERE assignment_id=? AND state IN ('queued','running') AND id!=?", (job['assignment_id'], jid)).fetchone())
                if active:
                    abort(409, description='本班已有后台任务，请等待完成。')
                conn.execute("UPDATE grading_jobs SET state='queued',message='等待继续后台任务。',updated_at=? WHERE id=?", (stamp(), jid))
                job = row(conn, 'grading_jobs', jid)
            result = runner.dto(job)
        runner.kick()
        return jsonify(job=result), 202


class GradingJobs:
    def __init__(self, app, db, row, stamp):
        self.app, self.db, self.row, self.stamp = app, db, row, stamp
        self.tenant_mode = app.config.get('TEACHER_TENANT_MODE', False)
        self.lock = threading.Lock()
        self.thread_lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        with db() as conn:
            definition = '''CREATE TABLE IF NOT EXISTS grading_jobs (
                id INTEGER PRIMARY KEY, assignment_id INTEGER REFERENCES assignments(id),
                submission_id INTEGER REFERENCES submissions(id), options TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                photo_ids TEXT NOT NULL, identify_cursor INTEGER NOT NULL DEFAULT 0,
                grade_ids TEXT NOT NULL DEFAULT '[]', grade_cursor INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                errors TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)'''
            columns = conn.execute('PRAGMA table_info(grading_jobs)').fetchall()
            if columns and 'submission_id' not in {c['name'] for c in columns}:
                conn.execute('ALTER TABLE grading_jobs RENAME TO grading_jobs_legacy')
                conn.execute(definition)
                names = ','.join(c['name'] for c in columns)
                conn.execute(f'INSERT INTO grading_jobs({names}) SELECT {names} FROM grading_jobs_legacy')
                conn.execute('DROP TABLE grading_jobs_legacy')
            else:
                conn.execute(definition)
            if self.tenant_mode:
                if 'owner_id' not in {c['name'] for c in conn.execute('PRAGMA table_info(grading_jobs)')}:
                    if conn.execute('SELECT 1 FROM grading_jobs LIMIT 1').fetchone():
                        raise ValueError('后台任务已有记录，需要先核对教师归属。')
                    conn.execute('ALTER TABLE grading_jobs ADD COLUMN owner_id INTEGER REFERENCES users(id)')
                conn.executescript('''
                    CREATE INDEX IF NOT EXISTS grading_jobs_owner ON grading_jobs(owner_id,id);
                    CREATE TRIGGER IF NOT EXISTS grading_jobs_owned_insert BEFORE INSERT ON grading_jobs
                    WHEN NEW.owner_id IS NULL OR
                        (NEW.assignment_id IS NOT NULL AND NEW.owner_id !=
                         (SELECT owner_id FROM assignments WHERE id=NEW.assignment_id)) OR
                        (NEW.submission_id IS NOT NULL AND NEW.owner_id !=
                         (SELECT owner_id FROM submissions WHERE id=NEW.submission_id))
                    BEGIN SELECT RAISE(ABORT, 'job owner mismatch'); END;
                    CREATE TRIGGER IF NOT EXISTS grading_jobs_owned_update BEFORE UPDATE ON grading_jobs
                    WHEN NEW.owner_id IS NULL OR NEW.owner_id != OLD.owner_id OR
                        (NEW.assignment_id IS NOT NULL AND NEW.owner_id !=
                         (SELECT owner_id FROM assignments WHERE id=NEW.assignment_id)) OR
                        (NEW.submission_id IS NOT NULL AND NEW.owner_id !=
                         (SELECT owner_id FROM submissions WHERE id=NEW.submission_id))
                    BEGIN SELECT RAISE(ABORT, 'job owner mismatch'); END;
                ''')
            conn.execute("UPDATE grading_jobs SET state='interrupted',message='服务已重启，请点击继续；已完成记录会保留。',updated_at=? WHERE state IN ('queued','running')", (stamp(),))
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_grading_job ON grading_jobs(assignment_id) WHERE state IN ('queued','running')")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_submission_job ON grading_jobs(submission_id) WHERE state IN ('queued','running')")

    def enqueue(self, assignment_id=None, photo_ids=None, submission_id=None, options=None, task_id=None):
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            translation = bool((options or {}).get('translation_only') or (options or {}).get('rubric_only'))
            if task_id:
                self.row(conn, 'tasks', task_id)
                job = conn.execute("SELECT * FROM grading_jobs WHERE task_id=? AND state IN ('queued','running','interrupted') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
                stage, ids, grades = 'translation', [], []
            elif submission_id:
                submission = self.row(conn, 'submissions', submission_id)
                if not conn.execute('SELECT 1 FROM pages WHERE submission_id=?', (submission_id,)).fetchone():
                    abort(400, description='请先上传作业照片。')
                identity = conn.execute('SELECT a.class_id FROM assignments a JOIN assignment_students s ON s.assignment_id=a.id WHERE s.id=?', (submission['assignment_student_id'],)).fetchone()
                if identity and self.row(conn, 'classes', identity['class_id'])['archived']:
                    abort(409, description='班级已归档，请恢复后再批改。')
                job = active_submission_job(conn, submission_id, ('queued','running','interrupted'))
                stage, ids, grades = ('translation' if translation else 'grading'), [], [submission_id]
            else:
                assignment = self.row(conn, 'assignments', assignment_id)
                if self.row(conn, 'classes', assignment['class_id'])['archived']:
                    abort(409, description='班级已归档，请恢复后再批改。')
                job = conn.execute("SELECT * FROM grading_jobs WHERE assignment_id=? AND state IN ('queued','running','interrupted') ORDER BY id DESC LIMIT 1", (assignment_id,)).fetchone()
                ids = ([] if ((options or {}).get('ai_review_only') or translation) else photo_ids if photo_ids is not None else
                       [p[0] for p in conn.execute('SELECT id FROM batch_photos WHERE assignment_id=? AND excluded=0 ORDER BY id', (assignment_id,))])
                if not ids and not ((options or {}).get('ai_review_only') or translation):
                    abort(400, description='请先上传未排除的作业照片。')
                if self.tenant_mode and any(self.row(conn, 'batch_photos', pid)['assignment_id'] != assignment_id for pid in ids):
                    abort(404, description='照片不属于本次作业。')
                if job and not set(ids) <= set(json.loads(job['photo_ids'])):
                    abort(409, description='本班已有后台任务，请等待完成后再处理这些照片。')
                stage, grades = ('matching' if (options or {}).get('identity_review_only') else 'identifying'), []
                if ((options or {}).get('ai_review_only') or translation):
                    if job:
                        return self.dto(job)
                    stage, ids = ('translation' if translation else 'ai_review'), []
                    grades = [r[0] for r in conn.execute('''SELECT s.id FROM submissions s
                        JOIN assignment_students ast ON ast.id=s.assignment_student_id
                        WHERE ast.assignment_id=? ORDER BY s.id''', (assignment_id,))]
                    if any(active_submission_job(conn, sid) for sid in grades):
                        abort(409, description='本作业仍有后台任务，请完成后重试。')
            if not job:
                created = self.stamp()
                values = (assignment_id,submission_id,json.dumps(options or {}),stage,json.dumps(ids),json.dumps(grades),len(grades or ids),created,created)
                columns = 'assignment_id,submission_id,options,stage,photo_ids,grade_ids,total,created_at,updated_at'
                if task_id:
                    columns += ',task_id'
                    values += (task_id,)
                if self.tenant_mode:
                    columns += ',owner_id'
                    values += (g.teacher_id,)
                jid = conn.execute(f"INSERT INTO grading_jobs({columns},state,message) VALUES({','.join('?' for _ in values)},'queued','已交给后台，可离开页面。')", values).lastrowid
                job = self.row(conn, 'grading_jobs', jid)
            elif self.tenant_mode:
                job = self.row(conn, 'grading_jobs', job['id'])
            return self.dto(job)

    @staticmethod
    def dto(job):
        value = dict(job)
        options=json.loads(value.get('options') or '{}')
        stats=options.get('ai_metrics',{})
        # Correct historical display from stored metrics without rewriting jobs or grades.
        if value['message'].startswith('AI 复核结束：'):
            value['message'] = ai_review_summary(options) + value['message'].partition('。')[2]
        return {'ai_incomplete':bool(stats.get('failed_pages') or stats.get('stopped'))} | {key: json.loads(value[key]) if key == 'errors' else value[key]
                 for key in ('id', 'assignment_id', 'submission_id', 'state', 'stage', 'message', 'completed',
                            'total', 'errors', 'created_at', 'updated_at')}

    def kick(self):
        if not self.app.config.get('GRADING_JOBS_AUTOSTART', not self.app.testing):
            return
        with self.thread_lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self.worker, name='grading-jobs', daemon=True)
                self.thread.start()
            self.wake.set()

    def worker(self):
        # ponytail: one local worker; use an external queue only for multiple server processes.
        while True:
            self.wake.wait()
            self.wake.clear()
            self.drain()

    def drain(self):
        while self.run_next():
            pass

    def save(self, job, **changes):
        changes['updated_at'] = self.stamp()
        with self.db() as conn:
            conn.execute('UPDATE grading_jobs SET '+','.join(f'{key}=?' for key in changes)+' WHERE id=?',
                         (*changes.values(), job['id']))
        job.update(changes)

    def post(self, path, payload, *, environ=None):
        return internal_post(self.app, path, payload,
                             owner_id=g.teacher_id if self.tenant_mode else None, environ=environ)

    def error(self, job, message, **reference):
        errors = json.loads(job['errors'])
        errors.append(dict(stage=job['stage'], message=message, **reference))
        job['errors'] = json.dumps(errors, ensure_ascii=False)

    def run_next(self):
        """Run one durable unit, allowing deterministic restart checks without threads."""
        if not self.lock.acquire(blocking=False):
            return False
        job = None
        try:
            with self.app.app_context():
                with self.db() as conn:
                    found = conn.execute('''SELECT j.* FROM grading_jobs j WHERE j.state IN ('queued','running')
                    AND NOT EXISTS(SELECT 1 FROM assignments a JOIN classes c ON c.id=a.class_id
                        WHERE c.archived=1 AND (a.id=j.assignment_id
                        OR EXISTS(SELECT 1 FROM assignment_students ast JOIN submissions sub ON sub.assignment_student_id=ast.id
                            WHERE ast.assignment_id=a.id AND (sub.id=j.submission_id
                                OR EXISTS(SELECT 1 FROM json_each(j.grade_ids) WHERE value=sub.id)))
                        OR EXISTS(SELECT 1 FROM json_each(j.photo_ids) ids JOIN batch_photos p ON p.id=ids.value
                            WHERE p.assignment_id=a.id))) ORDER BY j.id LIMIT 1''').fetchone()
                    if not found:
                        return False
                    job = dict(found)
                    if self.tenant_mode:
                        g.teacher_id = job['owner_id']
                        if not conn.execute('SELECT 1 FROM users WHERE id=? AND role=? AND disabled_at IS NULL',
                                            (g.teacher_id, 'teacher')).fetchone():
                            raise ValueError('job owner unavailable')
                        self.row(conn, 'grading_jobs', job['id'])
                        if job.get('task_id'):
                            self.row(conn, 'tasks', job['task_id'])
                        if job['assignment_id']:
                            self.row(conn, 'assignments', job['assignment_id'])
                        if job['submission_id']:
                            self.row(conn, 'submissions', job['submission_id'])
                        for pid in json.loads(job['photo_ids']):
                            photo = self.row(conn, 'batch_photos', pid)
                            if photo['assignment_id'] != job['assignment_id']:
                                raise ValueError('job photo assignment mismatch')
                        for sid in json.loads(job['grade_ids']):
                            self.row(conn, 'submissions', sid)
                self.save(job, state='running')
                if job['stage'] == 'translation':
                    self.app.extensions['translation_job_step'](self, job)
                elif job['stage'] == 'identifying':
                    self.identify(job)
                elif job['stage'] == 'identity_review':
                    self.identity_review(job)
                elif job['stage'] == 'matching':
                    self.match(job)
                elif job['stage'] == 'ai_review':
                    self.ai_review(job)
                else:
                    self.grade(job)
            return True
        except Exception:
            # Provider exceptions may contain credentials: never expose the original exception.
            if job:
                self.error(job, '后台任务中断，已有记录已保留，请检查后继续。')
                self.save(job, state='failed', message='后台任务失败，请检查后继续。', errors=job['errors'])
            return True
        finally:
            self.lock.release()

    def identify(self, job):
        ids = json.loads(job['photo_ids'])
        cursor = job['identify_cursor']
        if cursor == len(ids):
            self.save(job, stage='matching', completed=0, total=1, message='全部身份识别结束，正在统一匹配。')
            return
        pid = ids[cursor]
        with self.db() as conn:
            photo = self.row(conn, 'batch_photos', pid)
        if not photo['excluded'] and not photo['submission_id'] and photo['status'] != 'identified':
            response = self.post(f'/api/batch-photos/{pid}/identify', {'defer_link': True})
            if response.status_code != 200:
                with self.db() as conn:
                    message = self.row(conn, 'batch_photos', pid)['error']
                self.error(job, message or '身份识别失败，原图已保留，请教师核对。', photo_id=pid)
        self.save(job, identify_cursor=cursor+1, completed=cursor+1, total=len(ids),
                  errors=job['errors'], message=f'已处理 {cursor+1}/{len(ids)} 张照片身份。')

    def match(self, job):
        # A single-photo retry can finish identity OCR while another photo from the
        # same assignment is already identified. Snapshot both before matching so a
        # restart cannot leave the newly linked answer ungraded.
        if job['assignment_id']:
            with self.db() as conn:
                extra=[r[0] for r in conn.execute("""SELECT id FROM batch_photos
                    WHERE assignment_id=? AND status='identified' AND submission_id IS NULL
                    AND excluded=0 ORDER BY id""",(job['assignment_id'],))]
            ids= list(dict.fromkeys(json.loads(job['photo_ids']) + extra))
            if ids != json.loads(job['photo_ids']):
                self.save(job,photo_ids=json.dumps(ids),message='已补入同班已识别照片，正在统一匹配。')
        response = self.post(f"/api/assignments/{job['assignment_id']}/photos/match", {})
        if response.status_code != 200:
            self.error(job, '身份匹配未完成，请检查班级状态后继续。')
            self.save(job, state='failed', errors=job['errors'], message='统一匹配失败，请检查后继续。')
            return
        options = json.loads(job['options'])
        if not options.get('identity_review_done') and (options.get('identity_review_only') or self.app.extensions['read_ai_review']()['enabled']):
            with self.db() as conn:
                pending = [p['id'] for p in conn.execute("SELECT id,match_details FROM batch_photos WHERE assignment_id=? AND status='identified' AND submission_id IS NULL AND excluded=0 ORDER BY id", (job['assignment_id'],))
                           if json.loads(p['match_details'] or '{}').get('method') != 'duplicate']
            if pending:
                options.update(identity_review_ids=pending, identity_review_cursor=0)
                self.save(job, stage='identity_review', options=json.dumps(options), completed=0,
                          total=len(pending), message=f'正在 AI 复核 {len(pending)} 张未确定归属的照片。')
                return
        ids = set(json.loads(job['photo_ids']))
        grade_ids = []
        with self.db() as conn:
            for photo in conn.execute('SELECT id,submission_id FROM batch_photos WHERE assignment_id=? ORDER BY id', (job['assignment_id'],)):
                if photo['id'] in ids and photo['submission_id'] and photo['submission_id'] not in grade_ids:
                    grade_ids.append(photo['submission_id'])
        self.save(job, stage='grading', grade_ids=json.dumps(grade_ids), completed=0,
                  total=len(grade_ids), message=f'身份匹配结束，等待批改 {len(grade_ids)} 份答卷。')

    def identity_review(self, job):
        options = json.loads(job['options'])
        ids = options.get('identity_review_ids', [])
        cursor = options.get('identity_review_cursor', 0)
        if cursor >= len(ids) or not (options.get('identity_review_only') or self.app.extensions['read_ai_review']()['enabled']):
            options['identity_review_done'] = True
            self.save(job, stage='matching', options=json.dumps(options), completed=0, total=1,
                      message='AI 身份复核已结束，正在按名单统一核对归属。')
            return
        pid = ids[cursor]
        with self.db() as conn:
            prior=json.loads(self.row(conn,'batch_photos',pid)['ocr_result'] or '{}').get('identity_review') or {}
        retry=(str(pid) in options.get('identity_retry_requests',{}) and
               options['identity_retry_requests'][str(pid)]==prior.get('request_id',prior.get('created_at')))
        response = self.post(f"/api/batch-photos/{pid}/identity-review{'/retry' if retry else ''}", {},
                             environ={'moxieben.identity_review_job': job['id']})
        review_status = ((response.json or {}).get('identity_review') or {}).get('status')
        review = (response.json or {}).get('identity_review') or {}
        verification=review.get('verification') or {}
        verification_failed=verification.get('status')=='error'
        error_code=review.get('error_code') or verification.get('error_code')
        if (response.status_code != 200 or review_status == 'error' or verification_failed) and error_code not in ('output_truncated','response_format','empty_response','image_too_large'):
            self.error(job, 'AI 身份复核未完成，请查看照片上的失败原因；原识别已保留。', photo_id=pid)
            # One provider/configuration failure pauses the batch; never fan out retries.
            options['identity_review_done'] = True
            self.save(job,stage='matching',options=json.dumps(options),completed=cursor+1,total=len(ids),errors=job['errors'],
                      message=f'AI 身份复核在第 {cursor+1}/{len(ids)} 张停止；剩余照片未请求模型，请检查接口后明确重试。')
            return
        if review_status == 'error' or verification_failed:
            self.error(job,'本张 AI 身份复核失败，已保留原识别并继续其余照片。',photo_id=pid)
        options['identity_review_cursor'] = cursor + 1
        self.save(job, options=json.dumps(options), completed=cursor+1, total=len(ids), errors=job['errors'],
                  message=f'已处理 {cursor+1}/{len(ids)} 张 AI 身份复核；仍有歧义的照片交给教师。')

    def start_translation(self, job):
        if not self.app.extensions.get('translation_enabled'):
            return False
        ids = json.loads(job['grade_ids'])
        if not ids:
            return False
        with self.db() as conn:
            found = conn.execute("SELECT 1 FROM questions q JOIN submissions s ON s.task_id=q.task_id WHERE s.id IN (" + ','.join('?' for _ in ids) + ") AND q.scoring_mode='translation' LIMIT 1", ids).fetchone()
        if not found:
            return False
        options = json.loads(job['options'])
        options.update(translation_only=True)
        self.save(job, stage='translation', grade_cursor=0, completed=0,
                  options=json.dumps(options), message='正在分析翻译答案，最终成绩需教师确认。')
        return True

    def grade(self, job):
        ids = json.loads(job['grade_ids'])
        cursor = job['grade_cursor']
        if cursor == len(ids):
            if self.app.extensions['read_ai_review']()['enabled']:
                self.save(job, stage='ai_review', completed=0, total=len(ids),
                          message='正在 AI 复核不确定题，完成后再交给教师。')
                return
            if self.start_translation(job):
                return
            self.save(job, state='done', completed=len(ids), message='后台批改完成，请核对异常。' if json.loads(job['errors']) else '后台批改完成。')
            prepare = self.app.extensions.get('prepare_visual_suggestions')
            if prepare:
                try:
                    prepare(ids)
                except Exception:
                    # Suggestions are optional. A reread failure must not reopen completed grading.
                    self.app.logger.warning('Visual suggestions did not finish for grading job %s', job['id'])
            return
        sid = ids[cursor]
        response = self.post(f'/api/submissions/{sid}/auto-grade', json.loads(job['options']))
        if response.status_code != 200:
            self.error(job, '答卷批改失败，已有记录已保留，请教师核对或重试。', submission_id=sid)
        self.save(job, grade_cursor=cursor+1, completed=cursor+1, total=len(ids), errors=job['errors'],
                  message=f'已处理 {cursor+1}/{len(ids)} 份答卷。')

    def ai_review(self, job):
        options = json.loads(job['options'])
        ids = json.loads(job['grade_ids'])
        cursor = options.get('ai_cursor', 0)
        config = self.app.extensions['read_ai_review']()
        used, resolved = options.get('ai_used', 0), options.get('ai_resolved', 0)
        if options.get('ai_reserved'):
            self.save(job, state='done', message='上次 AI 请求中断，已停止自动重试以避免重复计费；剩余题请教师核对。')
            return
        if cursor >= len(ids) or not config['enabled'] or options.get('ai_metrics',{}).get('stopped'):
            reasons = '；'.join(f'{reason} {count} 题' for reason,count in options.get('ai_skipped', {}).items())
            stats = options.get('ai_metrics', {})
            message = (ai_review_summary(options) if used else
                       '未调用 AI：没有符合安全复核条件的题目。')
            if not config['enabled']:
                message = 'AI 自动复核已关闭。' + (f'此前已覆盖 {used} 题。' if used else '')
            if stats.get('stopped'):
                message += '接口失败，已暂停后续 AI 请求；请排查原因后再重试。'
            if self.start_translation(job):
                return
            self.save(job, state='done', message=message + (f'未自动解决：{reasons}。' if reasons else '') + '剩余异常请对照原图核对。')
            return
        # Reserve this request before calling a paid provider; interruption never repeats a charge.
        self.save(job, options=json.dumps(dict(options, ai_reserved=True)),
                  message=f'正在整批 AI 复核：第 {cursor+1}/{len(ids)} 份答卷，无需逐题打开。')
        count, accepted, skipped, metrics = self.app.extensions['automatic_ai_review'](ids[cursor], job['id'])
        stats = options.setdefault('ai_metrics', {})
        for key, amount in metrics.items():
            stats[key] = stats.get(key,0) + amount
        totals = options.setdefault('ai_skipped', {})
        for reason, amount in skipped.items():
            totals[reason] = totals.get(reason, 0) + amount
        options.update(ai_cursor=cursor+1, ai_used=used+count, ai_resolved=resolved+accepted)
        self.save(job, options=json.dumps(options), completed=cursor+1, total=len(ids),
                  message=f'AI 已覆盖 {used+count} 题，自动解决 {resolved+accepted} 题。')
