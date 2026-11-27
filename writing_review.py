"""Audited writing suggestions; teachers alone confirm final scores."""
import hashlib
import json
import math
import threading
from uuid import uuid4
from flask import jsonify,request
import writing_grading as grading


def register(app,db,row,detail,stamp):
    from app import fail
    import settings
    # ponytail: process-local request keys suit the single local server.
    active_requests=set()
    request_lock=threading.Lock()

    def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)
    def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
    def input_hash(q,review):
        return digest(dict(question=q,transcript=review['transcript'],confirmed_blank=bool(review['confirmed_blank']),regions=review['regions']))

    def records(conn,sid,qid=None):
        sub=row(conn,'submissions',sid)
        question=row(conn,'questions',qid) if qid else None
        if question and (question['task_id']!=sub['task_id'] or question['kind'] not in grading.KINDS):
            fail('这不是当前答卷的写作题。',404)
        return sub,question

    def snapshot(conn,sid,qid):
        sub,q=records(conn,sid,qid);q=dict(q)
        q['variants']=json.loads(q['variants']);q['writing_config']=grading.config(q.get('writing_config'),q['kind'])
        review=dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone())
        value=dict(input_hash=input_hash(q,review),teacher_state={k:review[k] for k in
                   ('confirmed','score_override','reason','teacher_edited','teacher_decision')})
        return sub,q,review,digest(value)

    def latest(conn,sid,qid):
        record=conn.execute("""SELECT * FROM review_events WHERE submission_id=? AND question_id=?
            AND json_extract(snapshot,'$.action') IN ('writing_ai_request','writing_ai_result') ORDER BY id DESC LIMIT 1""",(sid,qid)).fetchone()
        return (dict(record),json.loads(record['snapshot'])) if record else (None,None)

    def dto(conn,sid):
        sub,_=records(conn,sid);questions=[]
        for entry in conn.execute('SELECT id FROM questions WHERE task_id=? ORDER BY number',(sub['task_id'],)):
            qid=entry[0];question=row(conn,'questions',qid)
            if question['kind'] not in grading.KINDS:continue
            _,q,review,revision=snapshot(conn,sid,qid)
            record,saved=latest(conn,sid,qid)
            state='not_run';result=None;error=''
            if saved:
                if saved['input_hash']!=input_hash(q,review):state='stale'
                elif saved['action']=='writing_ai_request':state='unknown';error='上次请求尚无完成记录，请等待正在运行的请求；中断后需明确重试。'
                elif saved.get('error'):state='failed';error=saved['error']
                else:state='completed';result=dict(saved['result'],event_id=record['id'])
            q.pop('id');q.update(question_id=qid,transcript=review['transcript'],confirmed=bool(review['confirmed']),
                confirmed_blank=bool(review['confirmed_blank']),score=review['score_override'] if review['confirmed'] else None,
                word_count=grading.word_count(review['transcript']),revision=revision,ai_state=state,ai_result=result,error=error,
                component_maximum={k:v/100 for k,v in grading.limits(q).items()})
            questions.append(q)
        return dict(questions=questions)

    def payload(allowed):
        value=request.get_json(silent=True)
        if not isinstance(value,dict) or set(value)-set(allowed):fail('写作操作格式无效。')
        return value

    def idle(conn,sub):
        import grading_jobs
        task_busy=False
        if 'task_id' in {c[1] for c in conn.execute('PRAGMA table_info(grading_jobs)')}:
            task_busy=conn.execute("SELECT 1 FROM grading_jobs WHERE state IN ('queued','running') AND task_id=?",(sub['task_id'],)).fetchone()
        if (sub['grading_state']=='running' or conn.execute("SELECT 1 FROM pages WHERE submission_id=? AND status='running'",(sub['id'],)).fetchone()
                or grading_jobs.active_submission_job(conn,sub['id']) or task_busy
                or conn.execute("SELECT 1 FROM grading_jobs WHERE state IN ('queued','running') AND assignment_id IN (SELECT assignment_id FROM assignment_students WHERE id=?)",
                                (sub.get('assignment_student_id'),)).fetchone()):
            fail('答卷正在处理，请完成后再操作写作评分。',409)

    def revision_check(expected,current):
        if not isinstance(expected,str) or expected!=current:fail('题目或转录已改变，请刷新后重新核对。',409)

    def event(conn,sid,qid,value):
        return conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                            (sid,qid,stamp(),encode(value))).lastrowid

    def pages(conn,sid):
        return [list(r) for r in conn.execute('''SELECT id,status,needs_review,error,
             (SELECT MAX(id) FROM ocr_runs WHERE page_id=pages.id) FROM pages WHERE submission_id=? ORDER BY id''',(sid,))]

    def response(conn,sid,undo=None):
        value=dict(submission=detail(conn,sid),writing=dto(conn,sid))
        if undo:value['undo']={'event_id':undo}
        return jsonify(value)

    @app.get('/api/submissions/<int:sid>/writing')
    def get_writing(sid):
        with db() as conn:return jsonify(dto(conn,sid))

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/writing-grade')
    def grade_writing(sid,qid):
        with request_lock:
            if (sid,qid) in active_requests:fail('本题的 AI 请求仍在运行，请等待，未重复提交。',409)
            active_requests.add((sid,qid))
        try:return run_grade(sid,qid)
        finally:
            with request_lock:active_requests.discard((sid,qid))

    def run_grade(sid,qid):
        data=payload(('expected_revision','retry'))
        if type(data.get('retry',False)) is not bool:fail('重试标记须为布尔值。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub,q,review,revision=snapshot(conn,sid,qid);idle(conn,sub)
            revision_check(data.get('expected_revision'),revision)
            fingerprint=input_hash(q,review)
            if review['confirmed']:fail('本题已有教师成绩，请先保存新转录或继续使用教师成绩。',409)
            if not review['teacher_edited'] or not review['transcript'].strip() or review['confirmed_blank']:
                fail('请先核对并保存英文转录；识别为空不代表未作答。',409)
            if q['kind'] in ('summary','summary_cloze') and not q['writing_config']['source_passage'].strip():
                fail('概要评分需要原始阅读材料，请补充材料后再生成建议。',409)
            if q['kind']=='summary_cloze' and not q['context'].strip():
                fail('概要填空需要保留原始填空模板，请补充题干后再评分。',409)
            record,saved=latest(conn,sid,qid)
            if saved and saved['input_hash']==fingerprint and not data.get('retry'):
                if saved['action']=='writing_ai_result' and not saved.get('error'):return response(conn,sid)
                fail('上次请求未成功或是否完成未知；本次未重复请求，请明确重试。',409)
            config=settings.read_ai_review(db)
            if not all(config.get(k) for k in ('base_url','model','api_key')):fail('请先配置当前账号的 AI 服务。')
            token=uuid4().hex
            event(conn,sid,qid,dict(action='writing_ai_request',input_hash=fingerprint,request_id=token,
                  model=config['model'],prompt_version=grading.PROMPT_VERSION))
        result=None;error=None
        try:
            result=grading.score_response(settings._chat(config,None,grading.prompt(q,review['transcript']),8192,timeout=90),q,review['transcript'])
        except Exception:
            error='写作建议未完成或未通过原文与分项校验，未生成成绩；可核对转录和服务后明确重试。'
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub,_,_,current=snapshot(conn,sid,qid);idle(conn,sub)
            record,saved=latest(conn,sid,qid)
            if not saved or saved.get('request_id')!=token:fail('写作请求已被新操作替代，请刷新。',409)
            event(conn,sid,qid,dict(action='writing_ai_result',input_hash=fingerprint,request_id=token,
                model=config['model'],prompt_version=grading.PROMPT_VERSION,result=result,error=error))
            if current!=revision:
                answer=response(conn,sid);answer.status_code=409
                value=answer.get_json();value['error']='转录或教师评分已改变，本次建议仅留作记录，请刷新核对。'
                return jsonify(value),409
            # Return failure normally: raising abort would roll back the audit result.
            answer=response(conn,sid)
            if error:
                value=answer.get_json();value['error']=error
                return jsonify(value),502
            return answer

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/writing-confirm')
    def confirm_writing(sid,qid):
        data=payload(('expected_revision','action','transcript','confirmed_blank','event_id','score','reason','regions'))
        action=data.get('action')
        if action not in ('transcript','adopt','manual'):fail('请选择保存转录、采纳建议或教师评分。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub,q,before,revision=snapshot(conn,sid,qid);idle(conn,sub)
            revision_check(data.get('expected_revision'),revision)
            score=None;reason='';adopted=None
            transcript=before['transcript'];blank=bool(before['confirmed_blank'])
            regions=json.loads(before['regions'])
            if action=='transcript':
                transcript=data.get('transcript');blank=data.get('confirmed_blank',False)
                if not isinstance(transcript,str) or len(transcript)>20000 or type(blank) is not bool or (blank and transcript.strip()):
                    fail('转录格式无效；确认未作答时必须清空文字。')
                if 'regions' in data:
                    regions=data['regions']
                    if not isinstance(regions,list) or len(regions)>100:fail('写作区域格式无效。')
                    for region in regions:
                        if not isinstance(region,dict) or set(region)!={'page_id','box','block_ids'} or type(region['page_id']) is not int:fail('写作区域格式无效。')
                        page=row(conn,'pages',region['page_id']);box=region['box']
                        if (page['submission_id']!=sid or not isinstance(box,list) or len(box)!=4
                                or any(type(n) not in (int,float) or not math.isfinite(n) for n in box)
                                or not 0<=box[0]<box[2]<=page['width'] or not 0<=box[1]<box[3]<=page['height']):
                            fail('写作区域不属于当前答卷或超出原图。')
                        ids=region['block_ids'];known={b['id'] for b in json.loads(page['blocks'])}
                        if not isinstance(ids,list) or any(type(n) is not int or n not in known for n in ids):fail('写作文字块无效。')
                if (before['teacher_edited'] and transcript==before['transcript'] and blank==bool(before['confirmed_blank'])
                        and regions==json.loads(before['regions'])):return response(conn,sid)
            elif action=='adopt':
                record,saved=latest(conn,sid,qid)
                if (type(data.get('event_id')) is not int or not record or record['id']!=data['event_id'] or saved['action']!='writing_ai_result'
                        or saved.get('error') or saved['input_hash']!=input_hash(q,before)):fail('写作建议已失效，请重新生成。',409)
                score=saved['result']['score'];adopted=record['id'];reason='教师核对原文后采纳写作建议。'
            else:
                try:score=grading.cents(data.get('score'))/100
                except ValueError as exc:fail(str(exc))
                reason=data.get('reason','')
                if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:fail('请填写教师评分理由。')
                if not transcript.strip() and not blank:fail('请先保存转录，或明确确认未作答。')
            if score is not None and (score>q['points'] or (blank and score!=0)):fail('教师评分超出范围，未作答只能为零分。')
            conn.execute('''UPDATE reviews SET transcript=?,confirmed_blank=?,score_override=?,confirmed=?,regions=?,
                teacher_edited=1,teacher_decision='',reason=? WHERE submission_id=? AND question_id=?''',
                (transcript,int(blank),score,int(action!='transcript'),encode(regions),reason,sid,qid))
            conn.execute('UPDATE submissions SET updated_at=? WHERE id=?',(stamp(),sid))
            if action!='transcript' and not conn.execute('SELECT 1 FROM reviews WHERE submission_id=? AND confirmed=0',(sid,)).fetchone():
                conn.execute("UPDATE submissions SET grading_state='final',grading_error='' WHERE id=?",(sid,))
            after=dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone())
            eid=event(conn,sid,qid,dict(action='writing_teacher_'+action,before=before,review_after=after,
                      pages=pages(conn,sid),ai_event_id=adopted,question=q))
            return response(conn,sid,eid)

    @app.post('/api/submissions/<int:sid>/reviews/<int:qid>/writing-undo')
    def undo_writing(sid,qid):
        data=payload(('event_id',));eid=data.get('event_id')
        if type(eid) is not int or eid<1:fail('撤销操作编号无效。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub,q,current,_=snapshot(conn,sid,qid);idle(conn,sub)
            record=conn.execute('SELECT * FROM review_events WHERE id=? AND submission_id=? AND question_id=?',(eid,sid,qid)).fetchone()
            if not record:fail('没有找到当前题目的记录。',404)
            saved=json.loads(record['snapshot'])
            last=conn.execute('SELECT MAX(id) FROM review_events WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone()[0]
            if (last!=eid or not saved.get('action','').startswith('writing_teacher_')
                    or saved.get('review_after')!=current or saved.get('pages')!=pages(conn,sid) or saved.get('question')!=q):
                fail('本题已有后续操作或照片变化，不能撤销旧操作。',409)
            before=saved['before'];columns=[k for k in before if k not in ('submission_id','question_id')]
            conn.execute('UPDATE reviews SET '+','.join(k+'=?' for k in columns)+' WHERE submission_id=? AND question_id=?',
                         [before[k] for k in columns]+[sid,qid])
            event(conn,sid,qid,dict(action='writing_undo',event_id=eid))
            return response(conn,sid)
