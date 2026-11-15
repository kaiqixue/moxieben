"""Read-only teacher attention queue and lossless, on-demand image previews."""
import io
import json
from pathlib import Path

from flask import abort, jsonify, request, send_file
from PIL import Image


def submission_attention(detail):
    """Completion describes grading; attention describes evidence still worth checking."""
    pages = detail['pages']
    if not pages or detail['grading_state'] == 'running' or (detail.get('processing_job') or {}).get('state') in ('queued','running') or any(p['status']=='running' for p in pages):
        return {'failed':False, 'ungraded':False, 'uncertain':0, 'attention':False}
    uncertain = sum(q['uncertain'] for q in detail['reviews'])
    confirmed = all(q['confirmed'] for q in detail['reviews'])
    failed = detail['grading_state'] == 'failed' or any(
        p['status'] == 'error' or (p.get('error') and not (
            p['status'] == 'done' and confirmed and p.get('manually_reviewed') and not p['needs_review']))
        for p in pages)
    return {'failed': bool(failed), 'ungraded': bool(pages) and not detail['is_final'] and not failed,
            'uncertain': uncertain, 'attention': bool(pages) and (not detail['is_final'] or failed or bool(uncertain))}


def recognition_status(detail):
    attention = submission_attention(detail)
    processing = detail['grading_state']=='running' or (detail.get('processing_job') or {}).get('state') in ('queued','running') or any(p['status']=='running' for p in detail['pages'])
    state = ('not_uploaded' if not detail['pages'] else 'processing' if processing else
             'failed' if attention['failed'] else 'uncertain' if attention['uncertain'] else
             'completed' if detail['is_final'] else 'ungraded')
    return {'state':state, 'needs_attention':attention['attention'], 'uncertain_count':attention['uncertain'],
            'failed':attention['failed'], 'first_question_id': next((q['question_id'] for q in detail['reviews']
                if q['uncertain']),None) if attention['uncertain'] else None}


def task_counts(submissions):
    active = [s for s in submissions if not (s.get('identity') or {}).get('exempt')]
    return {'uploaded_count':sum(s['recognition']['state']!='not_uploaded' for s in active),
            'completed_count':sum(s['is_final'] and s['recognition']['state']!='not_uploaded' for s in active),
            'attention_count':sum(s['recognition']['needs_attention'] for s in active),
            'uncertain_submissions':sum(bool(s['recognition']['uncertain_count']) for s in active),
            'uncertain_questions':sum(s['recognition']['uncertain_count'] for s in active),
            'failed_count':sum(s['recognition']['failed'] for s in active),
            'processing_count':sum(s['recognition']['state']=='processing' for s in active)}


def processing_identity_photo_ids(conn, assignment_ids):
    """Only an active job's identity/matching snapshot is still system-owned."""
    if not assignment_ids:
        return set()
    return {r[0] for r in conn.execute(f'''SELECT DISTINCT p.id FROM batch_photos p
        JOIN grading_jobs j ON j.assignment_id=p.assignment_id
        JOIN json_each(j.photo_ids) ids ON ids.value=p.id
        LEFT JOIN assignment_students s ON s.id=p.suggested_student_id
        WHERE p.assignment_id IN ({','.join('?' for _ in assignment_ids)})
        AND j.state IN ('queued','running') AND j.stage IN ('identifying','matching')
        AND p.submission_id IS NULL AND p.excluded=0 AND COALESCE(s.exempt,0)=0''', assignment_ids)}


def pending_identity_photos(conn, assignment_ids):
    """Use the same population for dashboard counts and actionable identity items."""
    result = []
    processing = processing_identity_photo_ids(conn, assignment_ids)
    for aid in assignment_ids:
        result.extend(dict(p) for p in conn.execute('''SELECT p.* FROM batch_photos p
            LEFT JOIN assignment_students s ON s.id=p.suggested_student_id
            WHERE p.assignment_id=? AND p.submission_id IS NULL AND p.excluded=0
            AND p.status NOT IN ('confirmed','identifying') AND COALESCE(s.exempt,0)=0 ORDER BY p.id''', (aid,)) if p['id'] not in processing)
    return result


def attention_snapshot(conn, assignment_ids, submissions, include_items=False):
    """One read-only evaluator for queue items and grouped dashboard counts."""
    keys = ('identity_photos', 'processing_photos', 'failed_submissions', 'ungraded_submissions',
            'uncertain_questions', 'uncertain_submissions', 'attention_count')
    summary = dict.fromkeys(keys, 0)
    grouped = {aid: dict.fromkeys((*keys, 'attention_submissions', 'processing_submissions'), 0) | {'first_uncertain': None}
               for aid in assignment_ids}
    items = []

    def add(aid, key, amount=1):
        if key in summary:
            summary[key] += amount
        if aid in grouped:
            grouped[aid][key] += amount

    def item(kind, identity, sid=None, question=None, reason='', photo_id=None, aid=None):
        question = question or {}
        qid = question.get('question_id')
        items.append({'id': f'{kind}:{photo_id or sid}' + (f':{qid}' if qid else ''),
            'kind': kind, 'student_name': identity.get('name') or '',
            'student_code': identity.get('student_code') or '', 'submission_id': sid,
            'question_id': qid, 'number': question.get('number'), 'reason': reason,
            'photo_id': photo_id, 'assignment_id': aid or identity.get('assignment_id')})

    if assignment_ids:
        processing = processing_identity_photo_ids(conn, assignment_ids)
        fields = ',p.error,p.match_details,s.name,s.student_code' if include_items else ''
        for photo in conn.execute(f'''SELECT p.id,p.assignment_id,p.status{fields} FROM batch_photos p
                LEFT JOIN assignment_students s ON s.id=p.suggested_student_id
                WHERE p.assignment_id IN ({','.join('?' for _ in assignment_ids)})
                AND p.submission_id IS NULL AND p.excluded=0 AND COALESCE(s.exempt,0)=0
                ORDER BY p.id''', assignment_ids):
            aid = photo['assignment_id']
            if photo['id'] in processing:
                add(aid, 'processing_photos')
            elif photo['status'] not in ('confirmed', 'identifying'):
                add(aid, 'identity_photos')
                add(aid, 'attention_count')
                if include_items:
                    details = json.loads(photo['match_details'] or '{}')
                    item('identity', dict(photo), reason=photo['error'] or details.get('reason') or
                         '照片身份尚未确定，请对照姓名和名单。', photo_id=photo['id'], aid=aid)
    for detail in submissions:
        identity = detail.get('identity') or {'student_code': detail['student_code']}
        if identity.get('exempt'):
            continue
        aid = identity.get('assignment_id')
        attention = submission_attention(detail)
        add(aid, 'attention_submissions', int(attention['attention']))
        add(aid, 'processing_submissions', int(detail['recognition']['state'] == 'processing'))
        add(aid, 'failed_submissions', int(attention['failed']))
        add(aid, 'ungraded_submissions', int(attention['ungraded']))
        add(aid, 'uncertain_questions', attention['uncertain'])
        add(aid, 'uncertain_submissions', int(bool(attention['uncertain'])))
        add(aid, 'attention_count', int(attention['failed']) + int(attention['ungraded']) + attention['uncertain'])
        if attention['uncertain'] and aid in grouped and grouped[aid]['first_uncertain'] is None:
            grouped[aid]['first_uncertain'] = {'submission_id': detail['id'],
                                               'question_id': detail['recognition']['first_question_id']}
        if include_items:
            sid = detail['id']
            if attention['failed']:
                item('grading_failed', identity, sid, reason=detail['grading_error'] or '照片识别或批改失败，请重试。')
            elif attention['ungraded']:
                item('ungraded', identity, sid, reason='答卷尚未完成计分。')
            if attention['uncertain']:
                for question in detail['reviews']:
                    if question['uncertain']:
                        item('question', identity, sid, question,
                             (question['auto_result'] or {}).get('reason') or '作答识别存在不确定，请对照原图。')
    if include_items:
        priority = {'grading_failed': 0, 'question': 1, 'identity': 2, 'ungraded': 3}
        items.sort(key=lambda i: (priority[i['kind']], i['student_code'] or '', i['submission_id'] or 0,
                                  i['number'] or 0, i['photo_id'] or 0))
    return {'summary': summary, 'assignments': grouped, 'items': items}


def reviewed_page_ids(conn, page_ids):
    if not page_ids:
        return set()
    return {r[0] for r in conn.execute(f'''SELECT p.id FROM pages p WHERE p.id IN ({','.join('?' for _ in page_ids)})
        AND EXISTS(SELECT 1 FROM review_events e WHERE e.submission_id=p.submission_id
        AND json_extract(e.snapshot,'$.action')='page_manual_review' AND json_extract(e.snapshot,'$.page_id')=p.id
        AND e.id>COALESCE((SELECT MAX(id) FROM review_events WHERE submission_id=e.submission_id
            AND json_extract(snapshot,'$.action')='automatic_decision'),0)
        AND e.created_at>=COALESCE((SELECT MAX(created_at) FROM ocr_runs WHERE page_id=p.id),'') LIMIT 1)''',page_ids)}


def preview_info(conn, pid):
    page = conn.execute('SELECT submission_id,region_supplement_attempted FROM pages WHERE id=?', (pid,)).fetchone()
    if page is None:
        abort(404)
    reviewed = pid in reviewed_page_ids(conn,[pid])
    runs = []
    for run in conn.execute('SELECT id,success,result FROM ocr_runs WHERE page_id=? ORDER BY id DESC', (pid,)):
        try:
            result = json.loads(run['result'])
        except (ValueError, TypeError):
            continue
        if isinstance(result, dict):
            runs.append((run['id'], run['success'], result))
    latest_full = next((rid for rid, _, r in runs if r.get('source') != 'automatic_region_supplement'), 0)
    candidates = [(rid, r) for rid, success, r in runs if success and (
        r.get('source') != 'automatic_region_supplement' or (page['region_supplement_attempted'] == 1 and rid > latest_full))]
    for _, result in candidates:
        if result.get('source') == 'automatic_region_supplement' and result.get('input_rotation'):
            continue  # An upright crop's orientation does not describe the original full page.
        raw = result.get('raw') or {}
        pruned = raw.get('pruned_result', raw) if isinstance(raw, dict) else {}
        preprocessor = pruned.get('doc_preprocessor_res') if isinstance(pruned, dict) else None
        angle = preprocessor.get('angle') if isinstance(preprocessor, dict) else None
        if type(angle) is int and angle in (0, 90, 180, 270):
            return {'display_rotation': angle, 'manually_reviewed': reviewed}
        # A newer full-page result without orientation must not inherit an old angle.
        if result.get('source') != 'automatic_region_supplement':
            break
    return {'display_rotation': 0, 'manually_reviewed': reviewed}


def register(app, db, row, submission_detail, uploads, submission_summaries):
    def attention_response(conn, assignment_ids, submissions):
        snapshot = attention_snapshot(conn, assignment_ids, submission_summaries(conn, submissions).values(), True)
        return jsonify(summary=snapshot['summary'], items=snapshot['items'])

    @app.get('/api/workbench/assignments')
    def workbench_assignments():
        with db() as conn:
            conn.execute('BEGIN')
            assignments = [dict(r) for r in conn.execute('''SELECT a.id,a.class_id,a.class_name,
                c.archived AS class_archived,a.archived,a.task_id,t.name AS task_name,a.term,a.created_at
                FROM assignments a JOIN classes c ON c.id=a.class_id JOIN tasks t ON t.id=a.task_id
                ORDER BY a.created_at DESC,a.id DESC''')]
            aids = [a['id'] for a in assignments]
            roster_counts = {r['assignment_id']: r['expected'] for r in conn.execute('''SELECT assignment_id,
                SUM(CASE WHEN exempt=0 THEN 1 ELSE 0 END) AS expected FROM assignment_students GROUP BY assignment_id''')}
            ids = {aid: [] for aid in aids}
            for r in conn.execute('''SELECT s.id,a.assignment_id FROM submissions s
                    JOIN assignment_students a ON a.id=s.assignment_student_id WHERE a.exempt=0'''):
                ids[r['assignment_id']].append(r['id'])
            unavailable = set()
            try:
                summaries = submission_summaries(conn, [sid for group in ids.values() for sid in group])
            except (ValueError, TypeError, KeyError):
                # A damaged record must not hide healthy assignments or invent zero counts.
                summaries = {}
                for aid, group in ids.items():
                    try:
                        summaries.update(submission_summaries(conn, group))
                    except (ValueError, TypeError, KeyError):
                        unavailable.add(aid)
            snapshot = attention_snapshot(conn, aids, summaries.values())['assignments']
            totals = {aid: {'submitted': 0, 'completed': 0} for aid in aids}
            for detail in summaries.values():
                aid = detail['identity']['assignment_id']
                totals[aid]['submitted'] += bool(detail['pages'])
                totals[aid]['completed'] += bool(detail['pages']) and detail['is_final']
            for assignment in assignments:
                aid = assignment['id']
                counts = snapshot[aid]
                assignment['class_archived'] = bool(assignment['class_archived'])
                assignment['archived'] = bool(assignment['archived'])
                assignment.update(expected=roster_counts.get(aid, 0), **totals[aid],
                    attention_items=counts['attention_count'], attention_submissions=counts['attention_submissions'],
                    pending_identity_photos=counts['identity_photos'],
                    processing=counts['processing_submissions'] + counts['processing_photos'],
                    failed=counts['failed_submissions'], ungraded=counts['ungraded_submissions'],
                    uncertain_questions=counts['uncertain_questions'], first_uncertain=counts['first_uncertain'], status='ok')
                if aid in unavailable:
                    assignment.update({key: None for key in ('expected', 'submitted', 'completed', 'attention_items',
                        'attention_submissions', 'pending_identity_photos', 'processing', 'failed', 'ungraded', 'uncertain_questions', 'first_uncertain')})
                    assignment['status'] = 'unavailable'
            return jsonify(assignments=assignments)

    @app.get('/api/assignments/<int:aid>/attention')
    def assignment_attention(aid):
        with db() as conn:
            row(conn,'assignments',aid)
            sids = [r['id'] for r in conn.execute('SELECT s.id FROM submissions s JOIN assignment_students a ON a.id=s.assignment_student_id WHERE a.assignment_id=? AND a.exempt=0',(aid,))]
            return attention_response(conn,[aid],sids)

    @app.get('/api/tasks/<int:tid>/attention')
    def task_attention(tid):
        with db() as conn:
            row(conn,'tasks',tid)
            aids = [r['id'] for r in conn.execute('SELECT id FROM assignments WHERE task_id=?',(tid,))]
            sids = [r['id'] for r in conn.execute('SELECT id FROM submissions WHERE task_id=?',(tid,))]
            return attention_response(conn,aids,sids)

    @app.get('/api/pages/<int:pid>/preview')
    def page_preview(pid):
        rotation = request.args.get('rotation')
        if rotation is not None and rotation not in ('0', '90', '180', '270'):
            abort(400, description='旋转角度须为 0、90、180 或 270。')
        with db() as conn:
            page = row(conn, 'pages', pid)
            angle = int(rotation) if rotation is not None else preview_info(conn, pid)['display_rotation']
        output = io.BytesIO()
        with Image.open(Path(uploads) / page['image']) as image:
            if angle:
                image = image.transpose({90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}[angle])
            image.save(output, format='PNG')
        output.seek(0)
        return send_file(output, mimetype='image/png')
