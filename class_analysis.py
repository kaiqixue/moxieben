"""Read-only teaching summaries; scoring remains owned by submission_summaries."""
from datetime import datetime
from pathlib import Path
import json

from flask import Response, abort, jsonify, render_template, request

from grading import normalize
from teacher_workbench import pending_identity_photos


def register(app, db, row, submission_summaries):
    def build(conn, aid):
        assignment=row(conn,'assignments',aid)
        assignment['class_archived']=bool(row(conn,'classes',assignment['class_id'])['archived'])
        assignment['task_name']=row(conn,'tasks',assignment['task_id'])['name']
        roster=[dict(r) for r in conn.execute('''SELECT a.*,s.id AS submission_id FROM assignment_students a
            LEFT JOIN submissions s ON s.assignment_student_id=a.id WHERE a.assignment_id=? ORDER BY a.student_code,a.id''',(aid,))]
        submissions=submission_summaries(conn,[s['submission_id'] for s in roster if s['submission_id']])
        questions={}
        for record in conn.execute('SELECT * FROM questions WHERE task_id=? ORDER BY number',(assignment['task_id'],)):
            q=dict(record);q['variants']=json.loads(q['variants'])
            q.update(wrong_count=0,correct_count=0,pending_count=0,mistakes=[])
            questions[q['id']]=q
        counts=dict.fromkeys(('expected','final','wrong_students','wrong_answers','pending_questions','processing','failed','ungraded','not_uploaded','exempt'),0)
        students=[]
        for student in roster:
            detail=submissions.get(student['submission_id'])
            state=detail['recognition']['state'] if detail else 'not_uploaded'
            student.update(errors=[],pending=[],state=state)
            students.append(student)
            if student['exempt']:
                counts['exempt']+=1
                student['state']='exempt'
                continue
            counts['expected']+=1
            translation = bool(detail and any(q.get('scoring_mode') == 'translation' or q['kind'] in ('summary_cloze','summary','essay') for q in detail['reviews']))
            if not detail or state not in ('completed','uncertain','ungraded') or (
                    not detail['is_final'] and detail['grading_state'] != 'final' and not translation):
                counts[state if state in ('not_uploaded','processing','failed') else 'ungraded']+=1
                continue
            counts['ungraded']+=int(not detail['is_final'])
            counts['final']+=int(detail['is_final'])
            for review in detail['reviews']:
                q=questions[review['question_id']]
                item={k:review[k] for k in ('question_id','number','kind','context','answer','variants','points','score','transcript')}
                item['source']='教师已核对' if review['confirmed'] else '机器批改'
                item['answer_status']=('blank' if review['confirmed_blank'] else 'text' if normalize(review['transcript']) else
                                       'teacher_verdict' if review['confirmed'] else 'missing')
                if review['uncertain'] or not review['graded'] or review['score'] is None:
                    q['pending_count']+=1
                    item['reason']=(review['auto_result'] or {}).get('reason') or '尚未取得明确识别结果'
                    student['pending'].append(item)
                elif review['score']<review['points']:
                    q['wrong_count']+=1
                    student['errors'].append(item)
                    q['mistakes'].append(dict(item,submission_id=detail['id'],student_code=student['student_code'],student_name=student['name']))
                else:
                    q['correct_count']+=1
            counts['wrong_students']+=bool(student['errors'])
            counts['wrong_answers']+=len(student['errors'])
            counts['pending_questions']+=len(student['pending'])
        for q in questions.values():
            q['valid_count']=q['wrong_count']+q['correct_count']
            q['wrong_rate']=round(q['wrong_count']*100/q['valid_count'],1) if q['valid_count'] else None
            groups={}
            for mistake in q['mistakes']:
                key=(mistake['answer_status'],normalize(mistake['transcript']))
                group=groups.setdefault(key,{'id':len(groups),'answer_status':key[0],'text':key[1],'count':0})
                group['count']+=1
                mistake['answer_group']=group['id']
            q['common_answers']=sorted(groups.values(),key=lambda g:(-g['count'],g['answer_status'],g['text']))
        counts['identity_photos']=len(pending_identity_photos(conn,[aid]))
        return {'assignment':assignment,'counts':counts,'students':students,
                'questions':sorted(questions.values(),key=lambda q:(-q['wrong_count'],q['number'])),
                'generated_at':datetime.now().astimezone().isoformat(timespec='seconds')}

    @app.get('/api/assignments/<int:aid>/analysis')
    def class_analysis_json(aid):
        with db() as conn:
            conn.execute('BEGIN')
            return jsonify(build(conn,aid))

    @app.get('/assignments/<int:aid>/analysis/download')
    def class_analysis_page(aid):
        view=request.args.get('view','class')
        if view not in ('class','students'):
            abort(400,description='请选择全班讲评或学生订正。')
        selected=request.args.get('student','')
        if selected and (view!='students' or len(selected)>19 or not selected.isascii() or not selected.isdigit() or int(selected)<1):
            abort(400,description='学生编号格式错误，请从本次名单选择。')
        if selected:
            selected=str(int(selected))
        with db() as conn:
            conn.execute('BEGIN')
            report=build(conn,aid)
        if selected and not any(s['id']==int(selected) for s in report['students']):
            abort(404,description='该学生不在本次应交名单。')
        selected_students=[s for s in report['students'] if s['id']==int(selected)] if selected else [s for s in report['students'] if s['errors'] or s['pending']]
        download=request.path.endswith('/download')
        html=render_template('class_analysis.html',report=report,view=view,selected=selected,
                             selected_students=selected_students,download=download,base=request.url_root.rstrip('/'),
                             shared_css='\n'.join((Path(app.static_folder)/name).read_text(encoding='utf-8')
                                                  for name in ('theme.css','style.css')) if download else '')
        if download:
            return Response(html,mimetype='text/html',headers={'Content-Disposition':f'attachment; filename="assignment-{aid}-{view}.html"'})
        return html
