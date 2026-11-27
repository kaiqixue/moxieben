"""One synchronous, resumable grading path for individual and batch uploads."""
import json
import math
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from flask import jsonify, request
from PIL import Image

from answer_extraction import answer_diagnostics, missing_answer_crop, number_occurrences
from grading import normalize

PROCESSING_VERSION = 3


def migrate(database):
    additions = {
        'submissions': {'grading_state': "TEXT NOT NULL DEFAULT 'idle'", 'grading_error': "TEXT NOT NULL DEFAULT ''"},
        'reviews': {'auto_result': "TEXT NOT NULL DEFAULT '{}'", 'teacher_edited': 'INTEGER NOT NULL DEFAULT 0',
                    'teacher_decision': "TEXT NOT NULL DEFAULT ''"},
        'pages': {'auto_supplement_attempted': 'INTEGER NOT NULL DEFAULT 0',
                  'answer_layout': "TEXT NOT NULL DEFAULT 'inline'",
                  'region_supplement_attempted': 'INTEGER NOT NULL DEFAULT 0'},
        'batch_photos': {'identity_source': "TEXT NOT NULL DEFAULT ''", 'answer_layout': "TEXT NOT NULL DEFAULT 'inline'"},
    }
    with closing(sqlite3.connect(database)) as conn, conn:
        for table, columns in additions.items():
            existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        conn.execute('''UPDATE reviews SET teacher_edited=1 WHERE confirmed=1 OR confirmed_blank=1
            OR score_override IS NOT NULL OR reason!='' OR EXISTS(
            SELECT 1 FROM review_events e WHERE e.submission_id=reviews.submission_id
            AND e.question_id=reviews.question_id AND json_extract(e.snapshot,'$.action') IS NULL)''')
        conn.execute("UPDATE submissions SET grading_state='failed',grading_error='上次批改已中断，请重试。' WHERE grading_state='running'")
        conn.execute('UPDATE pages SET region_supplement_attempted=0 WHERE region_supplement_attempted=2')
        conn.execute("""UPDATE pages SET region_supplement_attempted=0 WHERE region_supplement_attempted=1 AND NOT EXISTS(
            SELECT 1 FROM ocr_runs o WHERE o.page_id=pages.id AND o.success=1
            AND json_extract(o.result,'$.source')='automatic_region_supplement')""")


def register(app, db, row, detail, suggestions, uploads, recognize, stamp, dumps, rules):
    from app import fail

    def candidates(value):
        # Re-extract machine evidence independently of saved drafts or reference answers.
        clean = dict(value, reviews=[dict(q, confirmed=False, transcript='', regions=[],
            confirmed_blank=False, score_override=None, reason='') for q in value['reviews']])
        return {q['question_id']: q for q in suggestions(clean)
                if q['transcript'].strip() and not re.fullmatch(r'[_＿\s]+',q['transcript'])
                and not re.search(r'无法辨认|无法识别|看不清|辨认不清|\[illegible\]|\[unreadable\]',q['transcript'],re.I)}

    def supplement_pages(value, unresolved):
        wanted={q['number'] for q in value['reviews'] if q['question_id'] in unresolved}
        per_page={p['id']:set(number_occurrences(p)) for p in value['pages']}
        located=set().union(*per_page.values()) & wanted
        # When a missing number has no location, no page can safely be excluded.
        return [p for p in value['pages'] if wanted-located or per_page[p['id']] & wanted]

    def persist_candidates(conn, sid, found, prior, supplement=False):
        for qid, item in found.items():
            old = dict(conn.execute('SELECT * FROM reviews WHERE submission_id=? AND question_id=?',(sid,qid)).fetchone())
            previous = prior.get(qid, {})
            source = item.get('source','existing_ocr') if not supplement else 'supplementary_ocr'
            evidence = list(previous.get('recognition_evidence', []))
            observation = {'transcript': item['transcript'], 'source': source}
            if observation not in evidence:
                evidence.append(observation)
            disagreement = len({normalize(e['transcript']) for e in evidence}) > 1
            uncertain = bool(item.get('uncertain')) or bool(previous.get('uncertain')) or disagreement
            decision = dict(item, uncertain=uncertain,
                processing_version=PROCESSING_VERSION,
                source=source, recognition_evidence=evidence, recognition_conflict=disagreement,
                reason=('整页或局部识别转录不同，采用补识别原文，已保留各次文字供核对。' if disagreement else
                        item.get('reason') or ('识别存在歧义，请对照原图核对。' if uncertain else '按明确题号提取并严格计分')))
            prior[qid] = decision
            if json.loads(old['auto_result']) == decision and (old['teacher_edited'] or (
                old['transcript'] == item['transcript'] and json.loads(old['regions']) == item['regions'])):
                continue
            if old['teacher_edited']:
                conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=? AND question_id=?',(dumps(decision),sid,qid))
            else:
                conn.execute('''UPDATE reviews SET auto_result=?,machine_text=?,transcript=?,regions=?
                    WHERE submission_id=? AND question_id=?''',
                    (dumps(decision),old['machine_text'] or item['transcript'],item['transcript'],dumps(item['regions']),sid,qid))
            conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                (sid,qid,stamp(),dumps({'action':'automatic_decision','before':old,'after':decision,
                    'question':row(conn,'questions',qid),'rules':rules})))

    def before_full_page_supplement(conn, value):
        """Recover this cycle's baseline so local re-extraction cannot erase disagreements."""
        pages, changed = [], False
        for page in value['pages']:
            baseline, supplemented = page, False
            for run in conn.execute("SELECT result FROM ocr_runs WHERE page_id=? AND success=1 AND COALESCE(json_extract(result,'$.source'),'')!='automatic_region_supplement' ORDER BY id DESC", (page['id'],)):
                saved = json.loads(run['result'])
                if saved.get('source') == 'automatic_supplement':
                    supplemented = True
                    continue
                if supplemented and isinstance(saved.get('blocks'), list):
                    baseline, changed = dict(page, blocks=saved['blocks']), True
                break  # A new primary run starts a fresh evidence cycle.
            pages.append(baseline)
        return candidates(dict(value, pages=pages)) if changed else {}

    def full_candidates(conn, value):
        found = candidates(value)
        sources = {}
        for page in value['pages']:
            run = conn.execute("SELECT result FROM ocr_runs WHERE page_id=? AND success=1 AND COALESCE(json_extract(result,'$.source'),'')!='automatic_region_supplement' ORDER BY id DESC LIMIT 1", (page['id'],)).fetchone()
            sources[page['id']] = json.loads(run['result']).get('source') if run else None
        for item in found.values():
            if any(sources.get(r['page_id']) == 'automatic_supplement' for r in item['regions']):
                item['source'] = 'supplementary_ocr'
        return found

    def ocr_page(pid, supplementary=False, local_recognize=None, manual=False):
        from settings import recognize_selected
        with db() as conn:
            page = row(conn,'pages',pid)
            conn.execute("UPDATE pages SET status='running',needs_review=1 WHERE id=?",(pid,))
        try:
            result = recognize_selected(db,uploads/page['image'],local_recognize or recognize)
            if not isinstance(result,dict) or not isinstance(result.get('blocks'),list):
                raise ValueError('invalid OCR result')
            with db() as conn:
                # Block ids belong to a single OCR run. Human text/scores survive reruns.
                for saved in conn.execute('SELECT * FROM reviews WHERE submission_id=?',(page['submission_id'],)).fetchall():
                    regions=[r for r in json.loads(saved['regions']) if r['page_id']!=pid]
                    if regions != json.loads(saved['regions']):
                        conn.execute("UPDATE reviews SET regions=?,confirmed=CASE WHEN teacher_edited=1 AND (auto_result!='{}' OR teacher_decision!='') THEN confirmed ELSE 0 END WHERE submission_id=? AND question_id=?",
                            (dumps(regions),page['submission_id'],saved['question_id']))
                        conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                            (page['submission_id'],saved['question_id'],stamp(),dumps({'action':'ocr_rerun_invalidated_regions',
                             'page_id':pid,'before_regions':json.loads(saved['regions']),'after_regions':regions})))
                conn.execute("UPDATE pages SET status='done',blocks=?,error='',needs_review=0 WHERE id=?",(dumps(result['blocks']),pid))
                if not supplementary:
                    conn.execute('UPDATE pages SET region_supplement_attempted=0 WHERE id=?',(pid,))
                    conn.execute('UPDATE pages SET auto_supplement_attempted=0 WHERE id=?',(pid,))
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                    (pid,stamp(),dumps(dict(result,source='automatic_supplement' if supplementary else 'manual_primary' if manual else 'automatic_primary'))))
            return True
        except Exception:
            # Provider exceptions can contain credentials. Never log their content or traceback.
            message='识别失败：识别服务未返回有效结果，请检查网络、接口额度和设置后重试。'
            (app.logger.error if manual else app.logger.warning)('OCR failed on page %s',pid)
            with db() as conn:
                if supplementary:
                    conn.execute("UPDATE pages SET status='done',error=?,needs_review=0 WHERE id=?",(message,pid))
                else:
                    conn.execute("UPDATE pages SET status='error',error=?,needs_review=1 WHERE id=?",(message,pid))
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,0,?)',
                    (pid,stamp(),dumps({'error':message,'source':'automatic_supplement' if supplementary else 'automatic_primary'})))
            return False

    app.extensions['page_ocr'] = ocr_page

    def region_ocr(page, box, questions):
        from settings import recognize_selected
        pid = page['id']
        with db() as conn:
            previous = conn.execute("SELECT result FROM ocr_runs WHERE page_id=? AND success=1 AND json_extract(result,'$.source')='automatic_region_supplement' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            if previous and json.loads(previous['result']).get('region_strategy', 1) < 3:
                # One explicit upgrade from the old layout-based retry, never an unlimited retry loop.
                conn.execute('UPDATE pages SET region_supplement_attempted=0 WHERE id=? AND region_supplement_attempted=1',(pid,))
            claimed = conn.execute('UPDATE pages SET region_supplement_attempted=2 WHERE id=? AND region_supplement_attempted=0', (pid,)).rowcount
            image_name = row(conn,'pages',pid)['image']
        if not claimed:
            return None
        try:
            rotation = page.get('display_rotation', 0)
            turns = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
            width, height = box[2]-box[0], box[3]-box[1]
            with tempfile.TemporaryDirectory(prefix='answer-region-') as folder:
                path = Path(folder)/'region.png'
                with Image.open(uploads/image_name) as image:
                    crop = image.crop(tuple(box))
                    if rotation in turns:
                        crop = crop.transpose(turns[rotation])
                    input_width, input_height = crop.size
                    crop.save(path, 'PNG')
                result = recognize_selected(db, path, recognize, layout_detection=False)
            if not isinstance(result, dict) or not isinstance(result.get('blocks'), list):
                raise ValueError('invalid OCR result')
            mapped = []
            for block in result['blocks']:
                local = block.get('box')
                if (not isinstance(block.get('text'), str) or not isinstance(local,list) or len(local)!=4
                    or not all(type(v) in (int,float) and math.isfinite(v) for v in local)
                    or not (0 <= local[0] < local[2] <= input_width and 0 <= local[1] < local[3] <= input_height)):
                    raise ValueError('invalid crop coordinates')
                if rotation == 90:
                    local = [width-local[3],local[0],width-local[1],local[2]]
                elif rotation == 180:
                    local = [width-local[2],height-local[3],width-local[0],height-local[1]]
                elif rotation == 270:
                    local = [local[1],height-local[2],local[3],height-local[0]]
                mapped.append(dict(block, id=len(mapped), box=[local[0]+box[0],local[1]+box[1],local[2]+box[0],local[3]+box[1]]))
            extracted = candidates(dict(pages=[dict(page,blocks=mapped)],reviews=questions))
            returned_numbers = {q['number'] for q in questions if q['question_id'] in extracted}
            if not returned_numbers:
                raise ValueError('局部识别未取得目标作答，不能作为有效补识别结果。')
            saved = dict(result, blocks=mapped, source='automatic_region_supplement', region_strategy=3,
                         crop_box=box, input_rotation=rotation, question_numbers=sorted(returned_numbers))
            with db() as conn:
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)', (pid,stamp(),dumps(saved)))
                conn.execute("UPDATE pages SET region_supplement_attempted=1,error='' WHERE id=?",(pid,))
            return saved
        except Exception:
            message = '局部补识别失败，原图与整页识别结果已保留，可再次点击重试。'
            app.logger.warning('Region OCR failed on page %s',pid)
            with db() as conn:
                conn.execute('UPDATE pages SET region_supplement_attempted=0,error=? WHERE id=?',(message,pid))
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,0,?)',
                             (pid,stamp(),dumps({'source':'automatic_region_supplement','error':message,'crop_box':box})))
            return None

    def stored_region_candidates(conn, value):
        """Supplement evidence stays separate: never replace a full-page transcription with a crop."""
        result = {}
        for page in value['pages']:
            run = conn.execute("SELECT id,result FROM ocr_runs WHERE page_id=? AND success=1 AND json_extract(result,'$.source')='automatic_region_supplement' ORDER BY id DESC LIMIT 1", (page['id'],)).fetchone()
            if not run:
                continue
            saved = json.loads(run['result'])
            if page['region_supplement_attempted'] != 1:
                # Recover crops cleared by the old fallback bug, never crops preceding a primary rerun.
                later = conn.execute('SELECT result FROM ocr_runs WHERE page_id=? AND success=1 AND id>? ORDER BY id',
                                     (page['id'],run['id'])).fetchall()
                if (page['region_supplement_attempted'] != 0 or saved.get('region_strategy',1) < 3 or not later
                    or any(json.loads(r['result']).get('source') != 'automatic_supplement' for r in later)):
                    continue
            cropped = dict(value, pages=[dict(page, blocks=saved['blocks'])])
            # A number on multiple original pages is ambiguous even if a crop can read it.
            for qid,item in candidates(cropped).items():
                q = next(q for q in value['reviews'] if q['question_id']==qid)
                occurrences = sum(number_occurrences(p).count(q['number']) for p in value['pages'])
                if occurrences!=1:
                    continue
                item.update(source='region_supplementary_ocr',reason='从题号所在局部区域补识别，按原词严格计分。')
                item['regions'] = [dict(r,block_ids=[]) for r in item['regions']]
                result[qid] = item
        return result

    @app.post('/api/submissions/<int:sid>/auto-grade')
    def auto_grade(sid):
        payload=request.get_json(silent=True) or {}
        if not isinstance(payload,dict) or any(not isinstance(payload.get(k,False),bool) for k in ('rerun','reextract','retry_missing')):
            fail('批改请求格式错误。')
        if sum(bool(payload.get(k)) for k in ('rerun','reextract','retry_missing'))>1:
            fail('请选择重新识别、重新分题或局部补识别中的一种操作。')
        local_only=payload.get('reextract',False)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current=detail(conn,sid)
            if current['grading_state']=='running' or any(p['status']=='running' for p in current['pages']):
                fail('答卷正在批改，请等待完成。',409)
            from settings import read_settings
            config=read_settings(db)
            refresh_pages=set()
            if config['provider']!='local':
                for page in current['pages']:
                    run=conn.execute("SELECT result FROM ocr_runs WHERE page_id=? AND success=1 AND COALESCE(json_extract(result,'$.source'),'')!='automatic_region_supplement' ORDER BY id DESC LIMIT 1",(page['id'],)).fetchone()
                    saved=json.loads(run['result']) if run else {}
                    model=config['paddle_model'] if config['provider']=='paddle_cloud' else config['model']
                    if saved.get('provider')!=config['provider'] or saved.get('model')!=model:
                        refresh_pages.add(page['id'])
            explicit=any(payload.get(k) for k in ('rerun','reextract','retry_missing'))
            old_decisions=any(not q['confirmed'] and (q['auto_result'] or {}).get('processing_version')!=PROCESSING_VERSION for q in current['reviews'])
            if current['grading_state'] == 'final' and not explicit:
                if all(q['confirmed'] for q in current['reviews']) or (not refresh_pages and not old_decisions):
                    return jsonify(current)
                # Recompute old decisions once from saved evidence; only a provider/model change needs OCR.
                local_only=not refresh_pages
            if not current['pages']:
                fail('请先上传作业照片。')
            if (local_only or payload.get('retry_missing')) and any(p['status']!='done' and not p['blocks'] for p in current['pages']):
                fail('请先完成整页识别，再重新分题或补识别。',409)
            conn.execute("UPDATE submissions SET grading_state='running',grading_error='' WHERE id=?",(sid,))
        try:
            for page in current['pages']:
                if not local_only and not payload.get('retry_missing') and (payload.get('rerun') or page['id'] in refresh_pages or page['status']=='error' or (page['status']!='done' and not page['blocks'])):
                    if not ocr_page(page['id']):
                        raise RuntimeError('primary OCR failed')
                elif page['status']!='done':
                    with db() as conn:
                        conn.execute("UPDATE pages SET status='done',needs_review=0,error='' WHERE id=?",(page['id'],))
                        conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                            (page['id'],stamp(),dumps({'source':'reused_existing_identity_ocr','blocks':page['blocks']})))
            with db() as conn:
                value=detail(conn,sid)
                prior={}
                baseline=before_full_page_supplement(conn,value)
                first=full_candidates(conn,value)
                for qid,item in baseline.items():
                    if qid not in first:
                        item.update(uncertain=True, regions=[], reason='补识别未取得有效对应作答，保留已有判读')
                persist_candidates(conn,sid,baseline,prior)
                persist_candidates(conn,sid,first,prior)
                existing=stored_region_candidates(conn,value)
                persist_candidates(conn,sid,existing,prior)
                unresolved={q['question_id'] for q in value['reviews'] if not q['teacher_edited']} - first.keys()
                unresolved-=existing.keys()
            if unresolved and not local_only:
                for page in supplement_pages(value,unresolved):
                    questions=[q for q in value['reviews'] if q['question_id'] in unresolved]
                    box=missing_answer_crop(page,questions)
                    if box:
                        supplement=region_ocr(page,box,questions)
                        if supplement and {q['number'] for q in questions} <= set(supplement['question_numbers']):
                            continue
                        # The cloud request can be HTTP-successful yet return unrelated text.
                        # A normal automatic pass gets one bounded full-page cloud fallback;
                        # explicit retry_missing remains a single, teacher-controlled attempt.
                        if payload.get('retry_missing'):
                            continue
                    if payload.get('retry_missing'):
                        continue
                    # ponytail: one full-page fallback when reliable local coordinates are unavailable.
                    with db() as conn:
                        claimed=conn.execute('UPDATE pages SET auto_supplement_attempted=1 WHERE id=? AND auto_supplement_attempted=0',(page['id'],)).rowcount
                    if claimed:
                        ocr_page(page['id'],supplementary=True)
                with db() as conn:
                    second=full_candidates(conn,detail(conn,sid))
                    persist_candidates(conn,sid,second,prior)
                    region_results=stored_region_candidates(conn,detail(conn,sid))
                    persist_candidates(conn,sid,region_results,prior)
                    second.update(region_results)
                    # Preserve a valid first reading when the second pass cannot locate it.
                    for qid,item in prior.items():
                        if qid not in second:
                            item['uncertain']=True
                            item['reason']='补识别未取得有效对应作答，保留已有判读'
                            item['regions']=[]
                            persist_candidates(conn,sid,{qid:item},prior)
            with db() as conn:
                value=detail(conn,sid)
                diagnostics=answer_diagnostics(value['reviews'],value['pages'])
                for q in value['reviews']:
                    if q['question_id'] in prior:
                        continue
                    decision={'transcript':'','regions':[],'uncertain':True,'source':'no_valid_answer','reason':'机器未取得有效作答',
                        'processing_version':PROCESSING_VERSION}
                    decision.update(diagnostics.get(q['question_id'],{'diagnostic':'answer_not_located',
                        'reason':'识别文字未能可靠关联本题，请核对题号和原图。'}))
                    if q['teacher_edited']:
                        conn.execute('UPDATE reviews SET auto_result=? WHERE submission_id=? AND question_id=?',(dumps(decision),sid,q['question_id']))
                    else:
                        conn.execute('UPDATE reviews SET auto_result=?,transcript=?,regions=? WHERE submission_id=? AND question_id=?',
                            (dumps(decision),'',dumps(decision['regions']),sid,q['question_id']))
                    conn.execute('INSERT INTO review_events(submission_id,question_id,created_at,snapshot) VALUES(?,?,?,?)',
                        (sid,q['question_id'],stamp(),dumps({'action':'automatic_decision','after':decision,
                            'question':row(conn,'questions',q['question_id']),'rules':rules})))
                conn.execute("UPDATE submissions SET grading_state='final',grading_error='',updated_at=? WHERE id=?",(stamp(),sid))
                return jsonify(detail(conn,sid))
        except Exception:
            message='批改失败：照片识别未完成，原图和已有记录已保留，请重试。'
            app.logger.warning('Automatic grading failed on submission %s',sid)
            with db() as conn:
                conn.execute("UPDATE submissions SET grading_state='failed',grading_error=? WHERE id=?",(message,sid))
            return jsonify(error=message),503
