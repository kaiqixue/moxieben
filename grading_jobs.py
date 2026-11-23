"""Local durable batch progress; existing routes own OCR, matching and scoring."""
import json
import threading

from flask import abort, g, jsonify, request
from teacher_auth import internal_post


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

    def enqueue(self, assignment_id=None, photo_ids=None, submission_id=None, options=None):
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if submission_id:
                submission = self.row(conn, 'submissions', submission_id)
                if not conn.execute('SELECT 1 FROM pages WHERE submission_id=?', (submission_id,)).fetchone():
                    abort(400, description='请先上传作业照片。')
                identity = conn.execute('SELECT a.class_id FROM assignments a JOIN assignment_students s ON s.assignment_id=a.id WHERE s.id=?', (submission['assignment_student_id'],)).fetchone()
                if identity and self.row(conn, 'classes', identity['class_id'])['archived']:
                    abort(409, description='班级已归档，请恢复后再批改。')
                job = active_submission_job(conn, submission_id, ('queued','running','interrupted'))
                stage, ids, grades = 'grading', [], [submission_id]
            else:
                assignment = self.row(conn, 'assignments', assignment_id)
                if self.row(conn, 'classes', assignment['class_id'])['archived']:
                    abort(409, description='班级已归档，请恢复后再批改。')
                job = conn.execute("SELECT * FROM grading_jobs WHERE assignment_id=? AND state IN ('queued','running','interrupted') ORDER BY id DESC LIMIT 1", (assignment_id,)).fetchone()
                ids = ([] if (options or {}).get('ai_review_only') else photo_ids if photo_ids is not None else
                       [p[0] for p in conn.execute('SELECT id FROM batch_photos WHERE assignment_id=? AND excluded=0 ORDER BY id', (assignment_id,))])
                if not ids and not (options or {}).get('ai_review_only'):
                    abort(400, description='请先上传未排除的作业照片。')
                if self.tenant_mode and any(self.row(conn, 'batch_photos', pid)['assignment_id'] != assignment_id for pid in ids):
                    abort(404, description='照片不属于本次作业。')
                if job and not set(ids) <= set(json.loads(job['photo_ids'])):
                    abort(409, description='本班已有后台任务，请等待完成后再处理这些照片。')
                stage, grades = 'identifying', []
                if (options or {}).get('ai_review_only'):
                    if job:
                        return self.dto(job)
                    stage, ids = 'ai_review', []
                    grades = [r[0] for r in conn.execute('''SELECT s.id FROM submissions s
                        JOIN assignment_students ast ON ast.id=s.assignment_student_id
                        WHERE ast.assignment_id=? ORDER BY s.id''', (assignment_id,))]
                    if any(active_submission_job(conn, sid) for sid in grades):
                        abort(409, description='本作业仍有后台任务，请完成后重试。')
            if not job:
                created = self.stamp()
                values = (assignment_id,submission_id,json.dumps(options or {}),stage,json.dumps(ids),json.dumps(grades),len(grades or ids),created,created)
                columns = 'assignment_id,submission_id,options,stage,photo_ids,grade_ids,total,created_at,updated_at'
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
        return {key: json.loads(value[key]) if key == 'errors' else value[key]
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

    def post(self, path, payload):
        return internal_post(self.app, path, payload,
                             owner_id=g.teacher_id if self.tenant_mode else None)

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
                if job['stage'] == 'identifying':
                    self.identify(job)
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
        ids = set(json.loads(job['photo_ids']))
        grade_ids = []
        with self.db() as conn:
            for photo in conn.execute('SELECT id,submission_id FROM batch_photos WHERE assignment_id=? ORDER BY id', (job['assignment_id'],)):
                if photo['id'] in ids and photo['submission_id'] and photo['submission_id'] not in grade_ids:
                    grade_ids.append(photo['submission_id'])
        self.save(job, stage='grading', grade_ids=json.dumps(grade_ids), completed=0,
                  total=len(grade_ids), message=f'身份匹配结束，等待批改 {len(grade_ids)} 份答卷。')

    def grade(self, job):
        ids = json.loads(job['grade_ids'])
        cursor = job['grade_cursor']
        if cursor == len(ids):
            if self.app.extensions['read_ai_review']()['enabled']:
                self.save(job, stage='ai_review', completed=0, total=len(ids),
                          message='正在 AI 复核不确定题，完成后再交给教师。')
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
        limit = options.setdefault('ai_limit', config['limit'])
        used, resolved = options.get('ai_used', 0), options.get('ai_resolved', 0)
        if options.get('ai_reserved'):
            self.save(job, state='done', message='上次 AI 请求中断，已停止自动重试以避免重复计费；剩余题请教师核对。')
            return
        if cursor >= len(ids) or not config['enabled'] or used >= limit:
            reasons = '；'.join(f'{reason} {count} 题' for reason,count in options.get('ai_skipped', {}).items())
            stats = options.get('ai_metrics', {})
            message = (f"AI 复核结束：尝试复读 {used} 题，图片请求 {stats.get('requests',used)} 次，有效返回 {stats.get('valid',0)} 次，生成建议 {stats.get('suggestions',0)} 题，自动解决 {resolved} 题。" if used else
                       '未调用 AI：没有符合安全复核条件的题目。')
            if not config['enabled']:
                message = 'AI 自动复核已关闭。' + (f'此前已调用 {used} 题。' if used else '')
            self.save(job, state='done', message=message + (f'未自动解决：{reasons}。' if reasons else '') + '剩余异常请对照原图核对。')
            return
        # Reserve the budget before calling a paid provider; interruption never repeats a charge.
        self.save(job, options=json.dumps(dict(options, ai_reserved=True)),
                  message=f'正在本机补定位并复核第 {cursor+1}/{len(ids)} 份答卷；首次加载定位模型可能较慢。')
        count, accepted, skipped, metrics = self.app.extensions['automatic_ai_review'](ids[cursor], job['id'], limit - used)
        stats = options.setdefault('ai_metrics', {})
        for key, amount in metrics.items():
            stats[key] = stats.get(key,0) + amount
        totals = options.setdefault('ai_skipped', {})
        for reason, amount in skipped.items():
            totals[reason] = totals.get(reason, 0) + amount
        options.update(ai_cursor=cursor+1, ai_used=used+count, ai_resolved=resolved+accepted)
        self.save(job, options=json.dumps(options), completed=cursor+1, total=len(ids),
                  message=f'AI 已尝试 {used+count} 题，自动解决 {resolved+accepted} 题。')
