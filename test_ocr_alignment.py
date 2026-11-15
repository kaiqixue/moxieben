"""Reading order, missing-answer diagnostics and bounded region OCR regression."""
import base64
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import test_auto_grade
from answer_extraction import reading_order


class OCRAlignmentTests(unittest.TestCase):
    setUpClass = test_auto_grade.AutoGradeTests.__dict__['setUpClass']
    setUp = test_auto_grade.AutoGradeTests.setUp
    post = test_auto_grade.AutoGradeTests.post
    submission = test_auto_grade.AutoGradeTests.submission
    image = test_auto_grade.AutoGradeTests.image
    seed = test_auto_grade.AutoGradeTests.seed

    def test_visual_reread_crops_only_one_question_and_keeps_score_unchanged(self):
        sid, page, _ = self.setup_grammar(missing=True)
        detail = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
        q = detail['reviews'][0]
        self.assertTrue(q['uncertain'])
        route = f"/api/submissions/{sid}/reviews/{q['question_id']}/visual-reread"
        crop = {'page_id': page['id'], 'box': [200, 170, 420, 290]}
        config = {'VLM_REVIEW_BASE_URL': 'https://vision.example.test/v1',
                  'VLM_REVIEW_MODEL': 'vision', 'VLM_REVIEW_API_KEY': 'test-vision-key'}
        with patch.dict(os.environ, config), patch('settings._open') as network:
            upstream = network.return_value.__enter__.return_value
            def reply(content):
                upstream.read.return_value = json.dumps({'choices': [
                    {'message': {'content': json.dumps(content)}}]}).encode()

            reply({'status': 'readable', 'transcript': 'Why'})
            response = self.post(route, crop)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['transcript'], 'Why')
            self.assertEqual(response.json['suggested_score'], 0)
            self.assertFalse(response.json['can_adopt'])
            preview = self.client.get(response.json['preview_url'])
            self.assertEqual(preview.mimetype, 'image/png')
            preview.close()
            sent = json.loads(network.call_args.args[0].data)
            self.assertEqual(sent['model'], 'vision')
            prompt, image = sent['messages'][0]['content']
            self.assertNotIn('why', prompt['text'].lower())
            url = image['image_url']['url']
            with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as decoded:
                self.assertEqual(decoded.size, (220, 120))

            reply({'status': 'readable', 'transcript': 'why'})
            self.assertEqual(self.post(route, crop).json['suggested_score'], 1)
            reply({'status': 'blank', 'transcript': ''})
            self.assertIsNone(self.post(route, crop).json['suggested_score'])
            reply({'status': 'readable', 'transcript': 'secret', 'score': 1})
            rejected = self.post(route, crop)
            self.assertEqual(rejected.status_code, 502)
            self.assertNotIn('secret', rejected.get_data(as_text=True))
            self.assertEqual(self.post(route, dict(crop, box=[0, 0, 1000, 1000])).status_code, 400)
            self.assertEqual(self.post(route, dict(crop, page_id=99999)).status_code, 404)
            normal = detail['reviews'][1]['question_id']
            self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{normal}/visual-reread', crop).status_code, 409)
            self.assertEqual(network.call_count, 4)
            network.side_effect = TimeoutError('test-vision-key')
            failed = self.post(route, crop)
            self.assertEqual(failed.status_code, 502)
            self.assertNotIn('test-vision-key', failed.get_data(as_text=True))
        after = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(after.pop('visual_suggestions')[str(q['question_id'])]['status'], 'blank')
        self.assertEqual(after, detail)

    def test_visual_adoption_is_versioned_uses_strict_score_and_can_be_undone(self):
        import sqlite3
        from contextlib import closing
        sid, page, _ = self.setup_grammar()
        crop = {'source': 'automatic_region_supplement', 'region_strategy': 3, 'question_numbers': [1],
                'blocks': [{'id': 0, 'text': '(1) what Western societies are known for this.',
                            'box': [200, 100, 400, 200], 'approximate': True}]}
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn, conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?', (page['id'],))
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                         (page['id'], 'now', json.dumps(crop)))
        detail = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
        q = detail['reviews'][0]
        self.assertTrue(q['uncertain'])
        route = f"/api/submissions/{sid}/reviews/{q['question_id']}"
        runner = self.app.extensions['grading_jobs']
        runner.enqueue(submission_id=sid)
        with patch('settings.visual_review_config', return_value={'model': 'test'}), \
                patch.dict(os.environ, {'VLM_REVIEW_AUTO': '1'}), \
                patch('settings.reread_handwriting', return_value={'status':'readable','transcript':'what'}) as model:
            runner.drain()
            refreshed = self.client.get(f'/api/submissions/{sid}').json
            self.assertEqual(refreshed['score'], detail['score'])
            self.assertTrue(refreshed['reviews'][0]['uncertain'])
            suggestion = refreshed.get('visual_suggestions', {}).get(str(q['question_id']))
            self.assertIsNotNone(suggestion)
            self.assertEqual(suggestion['trigger'], 'automatic')
            runner.enqueue(submission_id=sid)
            runner.drain()
            model.assert_called_once()
        self.assertTrue(suggestion['can_adopt'])
        accepted = self.post(route, {'visual_suggestion_id': suggestion['id']})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json['reviews'][0]['transcript'], 'what')
        self.assertEqual(accepted.json['reviews'][0]['machine_text'], q['machine_text'])
        self.assertEqual(accepted.json['reviews'][0]['score'], 0)
        self.assertFalse(accepted.json['reviews'][0]['uncertain'])
        undone = self.post(route+'/undo', {'event_id':accepted.json['undo']['event_id']})
        self.assertTrue(undone.json['reviews'][0]['uncertain'])
        self.assertEqual(self.post(route, {'visual_suggestion_id': suggestion['id']}).status_code, 409)

    def setup_grammar(self, missing=False):
        task = self.post('/api/tasks', {'name': 'Reading order', 'questions': [
            {'number': 1, 'kind': 'grammar', 'answer': 'why', 'points': 1,
             'context': 'This is probably (1) ____ Western societies are known for this.'},
            {'number': 2, 'kind': 'grammar', 'answer': 'to demand', 'points': 1,
             'context': 'They (2) ____ that special rights be given today.'}]}).json
        sid = self.submission(task)
        buf = io.BytesIO(); Image.new('RGB', (1000, 1000), 'white').save(buf, 'PNG')
        page = self.image(sid, content=buf.getvalue()).json
        blocks = [
            {'id': 0, 'text': 'This is probably (1)', 'box': [200, 100, 700, 200], 'approximate': True},
            {'id': 1, 'text': ('' if missing else 'why ') + 'Western societies are known for this.', 'box': [200, 200, 700, 320], 'approximate': True},
            {'id': 2, 'text': 'They (2) demand that special rights be given today.', 'box': [200, 400, 700, 500], 'approximate': True}]
        self.seed(sid, page['id'], blocks, 'done')
        return sid, page, blocks

    def test_rotated_approximate_blocks_keep_provider_reading_order(self):
        sid, page, blocks = self.setup_grammar()
        blocks[1]['box'] = [600, 20, 800, 700]  # rotated continuation sorts before its question by y
        self.seed(sid, page['id'], blocks, 'done')
        result = self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions']
        self.assertEqual([q['transcript'] for q in result], ['why', 'demand'])
        self.assertEqual(result[0]['regions'][0]['block_ids'], [0])
        self.assertEqual(result[0]['regions'][1]['block_ids'], [1])

    def test_reextract_final_uses_saved_text_and_preserves_teacher(self):
        sid, page, _ = self.setup_grammar()
        with patch('settings.recognize_selected') as cloud:
            self.post(f'/api/submissions/{sid}/auto-grade', {})
            sub = self.client.get(f'/api/submissions/{sid}').json
            q = sub['reviews'][1]
            self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}", {'transcript': 'teacher text', 'confirmed': True})
            # Simulate the old extraction failure while retaining valid saved OCR.
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as c, c:
                c.execute("UPDATE reviews SET transcript='',auto_result=? WHERE submission_id=? AND question_id=?",
                          (json.dumps({'transcript': '', 'source': 'no_valid_answer', 'uncertain': True}), sid, sub['reviews'][0]['question_id']))
            repaired = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
            cloud.assert_not_called()
        self.assertEqual(repaired['reviews'][0]['transcript'], 'why')
        self.assertEqual(repaired['reviews'][1]['transcript'], 'teacher text')
        self.assertTrue(repaired['is_final'])

    def test_default_grading_refreshes_old_final_decisions_without_cloud(self):
        import sqlite3
        from contextlib import closing
        sid, page, _ = self.setup_grammar()
        initial = self.post(f'/api/submissions/{sid}/auto-grade', {}).json
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute("UPDATE reviews SET transcript='',auto_result=? WHERE submission_id=? AND question_id=?",
                (json.dumps({'uncertain':True,'source':'no_valid_answer'}),sid,initial['reviews'][0]['question_id']))
        with patch('settings.recognize_selected') as cloud:
            repaired=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
            self.assertEqual(repaired['reviews'][0]['transcript'],'why')
            self.assertFalse(repaired['reviews'][0]['uncertain'])
            repeated=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
            self.assertEqual(repeated['history_count'],repaired['history_count'])
            cloud.assert_not_called()

    def test_rotated_crop_is_sent_upright_and_maps_back_without_changing_preview(self):
        import sqlite3
        from contextlib import closing
        from answer_extraction import missing_answer_crop
        sid,page,blocks=self.setup_grammar(missing=True)
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                (page['id'],'now',json.dumps({'source':'automatic_primary','blocks':blocks,
                    'raw':{'pruned_result':{'doc_preprocessor_res':{'angle':270}}}})))
        sub=self.client.get(f'/api/submissions/{sid}').json
        box=missing_answer_crop(sub['pages'][0],[sub['reviews'][0]])
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn:
            image_name=conn.execute('SELECT image FROM pages WHERE id=?',(page['id'],)).fetchone()[0]
        image_path=Path(self.tmp.name)/'uploads'/image_name
        with Image.open(image_path) as original:
            original.putpixel((box[0],box[3]-1),(255,0,0))
            original.save(image_path)
        def upright(db,path,local,**options):
            with Image.open(path) as im:
                self.assertEqual(im.size,(box[3]-box[1],box[2]-box[0]))
                self.assertEqual(im.getpixel((0,0)),(255,0,0),'Rotation must turn the crop in the correct direction')
            return {'blocks':[{'id':0,'text':'(1) why Western societies are known for this.',
                'box':[5,7,150,200],'approximate':True}],
                'raw':{'pruned_result':{'doc_preprocessor_res':{'angle':0}}}}
        with patch('settings.recognize_selected',side_effect=upright) as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'retry_missing':True}).json
        self.assertEqual(cloud.call_count,1)
        self.assertEqual(result['reviews'][0]['transcript'],'why')
        self.assertEqual(result['pages'][0]['display_rotation'],270)
        self.assertEqual(result['reviews'][0]['regions'][0]['box'],
            [box[0]+7,box[3]-150,box[0]+200,box[3]-5])

    def test_target_marker_without_extracted_answer_is_not_a_successful_crop(self):
        sid,page,_=self.setup_grammar(missing=True)
        blank={'blocks':[{'id':0,'text':'(1) ___ Western societies are known for this.',
            'box':[5,5,300,150],'approximate':True}]}
        with patch('settings.recognize_selected',return_value=blank):
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'retry_missing':True}).json
        self.assertTrue(result['reviews'][0]['uncertain'])
        self.assertFalse(result['pages'][0]['region_supplement_attempted'])

    def test_partial_crop_survives_full_page_fallback_and_local_reextract(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        blocks[2]['text'] = 'They (2) ___ that special rights be given today.'
        self.seed(sid, page['id'], blocks, 'done')
        crop = {'blocks':[{'id':0,'text':'(1) why Western societies are known for this.',
                           'box':[5,5,300,150],'approximate':True}]}
        with patch('settings.recognize_selected',side_effect=[crop,{'blocks':blocks}]) as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade',{}).json
        self.assertEqual(cloud.call_count,2)
        self.assertEqual(result['reviews'][0]['transcript'],'why')
        self.assertEqual(result['reviews'][0]['auto_result']['source'],'region_supplementary_ocr')
        self.assertTrue(result['pages'][0]['region_supplement_attempted'])
        self.assertEqual(result['pages'][0]['blocks'],blocks)
        with patch('settings.recognize_selected') as cloud:
            repaired = self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(repaired['reviews'][0]['transcript'],'why')
        self.assertTrue(repaired['reviews'][1]['uncertain'])

    def test_old_crop_cleared_by_fallback_is_recovered_but_primary_rerun_invalidates_it(self):
        import sqlite3
        from contextlib import closing
        sid, page, blocks = self.setup_grammar(missing=True)
        crop = {'source':'automatic_region_supplement','region_strategy':3,'question_numbers':[1],
                'blocks':[{'id':0,'text':'(1) why Western societies are known for this.',
                           'box':[5,5,300,150],'approximate':True}]}
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            for result in (crop,{'source':'automatic_supplement','blocks':blocks}):
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                             (page['id'],'now',json.dumps(result)))
        with patch('settings.recognize_selected') as cloud:
            restored = self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(restored['reviews'][0]['transcript'],'why')
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            for source in ('automatic_primary','automatic_supplement'):
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                             (page['id'],'now',json.dumps({'source':source,'blocks':blocks})))
        with patch('settings.recognize_selected') as cloud:
            invalidated = self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(invalidated['reviews'][0]['transcript'],'')

    def test_parenthesized_ocr_answer_is_retained_without_stripping_punctuation(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        blocks[0]['text'] = 'This is probably (1) ___(why)____Western societies are known for this.'
        blocks[1]['text'] = ''
        self.seed(sid,page['id'],blocks,'done')
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(result['reviews'][0]['transcript'],'(why)')
        self.assertTrue(result['reviews'][0]['uncertain'])
        self.assertEqual(result['reviews'][0]['score'],0)

    def test_printed_hint_comes_from_context_even_when_hint_field_is_empty(self):
        from answer_extraction import grammar_candidates
        questions = [{'question_id':3,'number':3,'kind':'grammar','hint':'','answer':'is favored',
                      'context':'The group (3) ____ (favor) over the individual as a result of many rules.'}]
        def extract(text):
            return grammar_candidates(questions,[{'id':1,'status':'done','blocks':[
                {'id':0,'text':text,'box':[0,0,900,100]}]}])
        self.assertEqual(extract('The group (3) ___(favor) over the individual as a result of many rules.'),[])
        self.assertEqual(extract('The group (3) favors (favor) over the individual as a result of many rules.')[0]['transcript'],'favors')
        self.assertEqual(extract('The group (3) favors (favour) over the individual as a result of many rules.')[0]['transcript'],'favors')

    def test_full_page_and_saved_crop_disagreement_remains_uncertain(self):
        import sqlite3
        from contextlib import closing
        sid,page,_ = self.setup_grammar()
        crop = {'source':'automatic_region_supplement','region_strategy':3,'question_numbers':[1],
                'blocks':[{'id':0,'text':'(1) what Western societies are known for this.',
                           'box':[5,5,300,150],'approximate':True}]}
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?',(page['id'],))
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                         (page['id'],'now',json.dumps(crop)))
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(result['reviews'][0]['transcript'],'what')
        self.assertEqual(result['reviews'][0]['score'],0)
        self.assertTrue(result['reviews'][0]['uncertain'])
        self.assertIn('不同',result['reviews'][0]['auto_result']['reason'])

    def test_crop_also_checks_readable_neighbor_not_just_requested_missing_question(self):
        import sqlite3
        from contextlib import closing
        sid,page,blocks = self.setup_grammar(missing=True)
        blocks[2]['text'] = 'They (2) a kind of code that special rights be given today.'
        self.seed(sid,page['id'],blocks,'done')
        # Only Q1 was requested, but the valid crop also contains a different reading of Q2.
        crop = {'source':'automatic_region_supplement','region_strategy':3,'question_numbers':[1],
                'blocks':[{'id':0,'text':'(1) why Western societies are known for this. They (2) demand that special rights be given today.',
                           'box':[5,5,700,500],'approximate':True}]}
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?',(page['id'],))
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                         (page['id'],'now',json.dumps(crop)))
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        q = result['reviews'][1]
        self.assertEqual(q['transcript'],'demand')
        self.assertEqual(q['score'],0, 'Keep literal OCR; do not complete it to the answer to demand')
        self.assertTrue(q['uncertain'])
        self.assertTrue(q['auto_result']['recognition_conflict'])
        evidence=q['auto_result']['recognition_evidence']
        self.assertEqual({e['transcript'] for e in evidence},{'a kind of code','demand'})
        self.assertEqual({e['source'] for e in evidence},{'existing_ocr','region_supplementary_ocr'})
        # Teacher action resolves the warning, survives re-extraction, and still retains machine evidence.
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'quick_decision':'incorrect'})
        with patch('settings.recognize_selected') as cloud:
            repeated=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(repeated['reviews'][1]['teacher_decision'],'incorrect')
        self.assertFalse(repeated['reviews'][1]['uncertain'])
        self.assertEqual(repeated['reviews'][1]['auto_result']['recognition_evidence'],evidence)

    def test_full_page_supplement_conflict_survives_local_reextract(self):
        import sqlite3
        from contextlib import closing
        sid,page,primary=self.setup_grammar()
        later=[dict(b) for b in primary]
        later[0]['text']='This is probably (1) what'
        later[1]['text']='Western societies are known for this.'
        self.seed(sid,page['id'],later,'done')
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            for source,blocks in [('automatic_primary',primary),('automatic_supplement',later)]:
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                             (page['id'],'now',json.dumps({'source':source,'blocks':blocks})))
        with patch('settings.recognize_selected') as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        q=result['reviews'][0]
        self.assertEqual(q['transcript'],'what')
        self.assertTrue(q['uncertain'])
        self.assertEqual({e['transcript'] for e in q['auto_result']['recognition_evidence']},{'why','what'})

    def test_new_primary_does_not_reuse_previous_cycle_conflicts(self):
        import sqlite3
        from contextlib import closing
        sid,page,primary=self.setup_grammar()
        wrong=[dict(b) for b in primary];wrong[0]['text']='This is probably (1) what'
        wrong[1]['text']='Western societies are known for this.'
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            for source,blocks in [('automatic_primary',primary),('automatic_supplement',wrong),('automatic_primary',primary)]:
                conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                             (page['id'],'now',json.dumps({'source':source,'blocks':blocks})))
        with patch('settings.recognize_selected') as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(result['reviews'][0]['transcript'],'why')
        self.assertFalse(result['reviews'][0]['uncertain'])

    def test_saved_crop_cannot_assign_a_number_duplicated_across_original_pages(self):
        import sqlite3
        from contextlib import closing
        sid,page,blocks=self.setup_grammar()
        second=self.image(sid).json
        self.seed(sid,second['id'],[{'id':0,'text':blocks[2]['text'],'box':[0,0,600,100]}],'done')
        crop={'source':'automatic_region_supplement','region_strategy':3,'question_numbers':[1],
              'blocks':blocks}
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?',(page['id'],))
            conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                         (page['id'],'now',json.dumps(crop)))
        with patch('settings.recognize_selected') as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            cloud.assert_not_called()
        self.assertEqual(result['reviews'][1]['transcript'],'')
        self.assertTrue(result['reviews'][1]['uncertain'])

    def test_missing_answer_has_specific_reason_and_evidence_without_cloud(self):
        sid, page, _ = self.setup_grammar(missing=True)
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
            cloud.assert_not_called()
        q = result['reviews'][0]
        self.assertEqual(q['auto_result']['diagnostic'], 'ocr_no_answer')
        self.assertIn('未返回作答文字', q['auto_result']['reason'])
        self.assertTrue(q['regions'])
        self.assertFalse(q['confirmed_blank'])

    def test_bad_anchor_is_association_failure_not_claimed_ocr_omission(self):
        sid, page, blocks = self.setup_grammar()
        blocks[1]['text'] = 'why totally unrelated content'
        self.seed(sid, page['id'], blocks, 'done')
        with patch('settings.recognize_selected'):
            result = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
        q = result['reviews'][0]
        self.assertEqual(q['auto_result']['diagnostic'], 'answer_not_located')
        self.assertIn('未能可靠关联', q['auto_result']['reason'])

    def test_crop_retry_maps_to_original_keeps_full_ocr_and_does_not_repeat(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        with patch('settings.recognize_selected'):
            self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True})
        sizes = []
        def recognize(db, path, local, **options):
            self.assertFalse(options['layout_detection'])
            with Image.open(path) as im: sizes.append(im.size)
            return {'blocks': [{'id': 0, 'text': 'This is probably (1) why Western societies are known for this.',
                                'box': [5, 5, 300, 150], 'approximate': True}], 'provider': 'test'}
        with patch('settings.recognize_selected', side_effect=recognize) as cloud:
            response = self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True})
            self.assertEqual(response.status_code, 200, response.json)
            result = response.json
            self.assertEqual(cloud.call_count, 1)
            self.assertLess(sizes[0][0]*sizes[0][1], 1000*1000)
            self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True})
            self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True})
            self.assertEqual(cloud.call_count, 1)
        self.assertEqual(result['reviews'][0]['transcript'], 'why')
        self.assertEqual(result['reviews'][0]['auto_result']['source'], 'region_supplementary_ocr')
        region = result['reviews'][0]['regions'][0]
        self.assertGreater(region['box'][0], 5)
        self.assertEqual(region['block_ids'], [])
        self.assertEqual(result['pages'][0]['blocks'], blocks)
        self.assertEqual(result['reviews'][1]['transcript'], 'demand', 'Normal wrong answer must stay unchanged')
        self.assertEqual(result['uncertain_count'], 0)

    def test_crop_failure_keeps_original_and_allows_explicit_retry(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        with patch('settings.recognize_selected'):
            self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True})
        with patch('settings.recognize_selected', side_effect=RuntimeError('private-token-not-for-response')):
            failed = self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True})
        self.assertEqual(failed.status_code, 200)
        self.assertNotIn('private-token', failed.get_data(as_text=True))
        self.assertEqual(failed.json['pages'][0]['blocks'], blocks)
        self.assertFalse(failed.json['pages'][0]['region_supplement_attempted'])
        self.assertIn('局部补识别失败', failed.json['pages'][0]['error'])
        self.assertTrue(failed.json['reviews'][0]['uncertain'])

    def test_interrupted_crop_does_not_reuse_old_result_after_full_rerun(self):
        sid, page, _ = self.setup_grammar(missing=True)
        with patch('settings.recognize_selected', return_value={'blocks': [
            {'id': 0, 'text': 'This is probably (1) why Western societies are known for this.',
             'box': [5, 5, 300, 150], 'approximate': True}]}):
            self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True})
        import sqlite3
        from contextlib import closing
        from automatic_grading import migrate
        database = Path(self.tmp.name)/'dictation.sqlite3'
        with closing(sqlite3.connect(database)) as c, c:
            # A later full-page run invalidates the old crop; a new region call is interrupted.
            c.execute('UPDATE pages SET region_supplement_attempted=2 WHERE id=?',(page['id'],))
        migrate(database)
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True}).json
            cloud.assert_not_called()
        self.assertEqual(result['reviews'][0]['transcript'], '')
        self.assertFalse(result['pages'][0]['region_supplement_attempted'])

    def test_missing_retry_never_falls_back_to_full_page_and_teacher_is_preserved(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        for block in blocks:
            block['box'] = [0, 0, 1000, 1000]
        self.seed(sid, page['id'], blocks, 'done')
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True}).json
            cloud.assert_not_called()
            q = result['reviews'][0]
            self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}", {'transcript': 'teacher answer', 'confirmed': True})
            result = self.post(f'/api/submissions/{sid}/auto-grade', {'retry_missing': True}).json
            cloud.assert_not_called()
        self.assertEqual(result['reviews'][0]['transcript'], 'teacher answer')
        self.assertTrue(result['reviews'][0]['confirmed'])

    def test_explicit_reading_order_and_local_coordinate_order(self):
        blocks = [{'id': 0, 'box': [0, 100, 20, 120]}, {'id': 1, 'box': [0, 0, 20, 20]}]
        self.assertEqual([b['id'] for b in reading_order(blocks)], [1, 0])
        blocks = [dict(b, reading_order=i, approximate=True) for i,b in enumerate(blocks)]
        self.assertEqual([b['id'] for b in reading_order(blocks)], [0, 1])

    def test_old_failed_extraction_can_use_new_region_strategy_once(self):
        sid, page, _ = self.setup_grammar(missing=True)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as c, c:
            c.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?',(page['id'],))
            c.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                (page['id'],'2026-10-25',json.dumps({'source':'automatic_region_supplement','blocks':[],
                                                   'question_numbers':[1],'crop_box':[0,0,900,900]})))
        with patch('settings.recognize_selected',return_value={'blocks':[{'id':0,
            'text':'This is probably (1) why Western societies are known for this.',
            'box':[5,5,300,150],'approximate':True}]}) as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'retry_missing':True}).json
            self.assertEqual(result['reviews'][0]['transcript'],'why')
            self.post(f'/api/submissions/{sid}/auto-grade',{'retry_missing':True})
            self.assertEqual(cloud.call_count,1)
            self.assertFalse(cloud.call_args.kwargs['layout_detection'])

    def test_real_cloud_region_fixture_extracts_why_without_answer_key(self):
        from answer_extraction import grammar_candidates
        from paddle_cloud import _parse_result
        path = Path(__file__).parent/'tests/fixtures/grammar_region_upright.json'
        if not path.is_file():
            self.skipTest('真实 OCR 样本仅保存在本机')
        fixture = json.loads(path.read_text('utf-8'))
        raw={'result':{'layoutParsingResults':[{'prunedResult':{
            'doc_preprocessor_res':{'angle':fixture['angle']},
            'parsing_res_list':[{'block_content':fixture['text'],'block_bbox':fixture['box']}]}}]}}
        parsed=_parse_result(json.dumps(raw).encode(),fixture['input_width'],fixture['input_height'])
        # Deliberately wrong reference answer: extraction must read the handwriting, not guess the key.
        questions=[{'question_id':99,'number':4,'kind':'grammar','answer':'not-the-answer',
                    'context':'This is probably (4) ____ Western societies, most of which are multi-cultural, are known for being plain-spoken.'}]
        found=grammar_candidates(questions,[{'id':5,'status':'done','blocks':parsed['blocks']}])
        self.assertEqual(found[0]['transcript'],'why')
        self.assertEqual(found[0]['regions'][0]['box'],[0,0,523,829])

    def test_duplicate_printed_number_uses_contextual_candidate(self):
        from answer_extraction import grammar_candidates
        questions = [{'question_id': 10, 'number': 10, 'kind': 'grammar', 'answer': 'What',
                      'context': '(10) ____ is important is that we tolerate these differences and remember that, in this big country and big world, we all belong.'}]
        blocks = [{'id': 0, 'text': 'Eccentric (9) ___ in (10) ___ (inporate) into Chinese culture.', 'box': [0, 0, 900, 100]},
                  {'id': 1, 'text': 'Being different is not bad. (10) What is important is that we tolerate these differences and remember that, in this big country and big world, we all belong.', 'box': [0, 100, 900, 200]}]
        result = grammar_candidates(questions, [{'id': 1, 'status': 'done', 'blocks': blocks}])
        self.assertEqual(result[0]['transcript'], 'What')

    def test_handwriting_on_same_line_precedes_printed_continuation(self):
        blocks = [
            {'id':15,'text':'This is probably (4)','box':[116,518,897,560]},
            {'id':16,'text':'why','box':[133,560,177,588]},
            {'id':17,'text':'Western societies, most of which are multi-cultural.','box':[224,549,811,586]},
            {'id':18,'text':'Next paragraph starts here.','box':[140,567,858,624]}]
        self.assertEqual([b['id'] for b in reading_order(blocks)], [15,16,17,18])
        self.assertEqual([b['id'] for b in blocks], [15,16,17,18], 'Never mutate the OCR evidence')

    def test_printed_context_typos_do_not_lose_or_correct_student_answer(self):
        from answer_extraction import grammar_candidates
        q={'question_id':4,'number':4,'kind':'grammar','answer':'unused-key',
           'context':'This is probably (4) ____ Western societies, most of which are multi-cultural, are known for this.'}
        original=[{'id':0,'text':'This is probably (4)','box':[116,518,897,560]},
                  {'id':1,'text':'whyy','box':[133,560,177,588]},
                  {'id':2,'text':'Westem societies, mosi of which are multi-cultural, are known for this.','box':[224,549,811,586]}]
        result=grammar_candidates([q],[{'id':1,'status':'done','blocks':original}])
        self.assertEqual([r['transcript'] for r in result],['whyy'])
        self.assertEqual(original[2]['text'].split()[0],'Westem')
        self.assertEqual({r['block_ids'][0] for r in result[0]['regions']},{0,1})

    def test_context_fallback_rejects_ambiguous_short_or_unrelated_prefix(self):
        from answer_extraction import grammar_candidates
        q={'question_id':4,'number':4,'kind':'grammar',
           'context':'(4) ____ Western societies, most of which are multi-cultural, are known for this.'}
        def extract(text,question=q):
            return grammar_candidates([question],[{'id':1,'status':'done','blocks':[
                {'id':0,'text':text,'box':[0,0,900,100]}]}])
        for text in ('(4) why Westem societies, mosi of which are multi-cultural. Westem societies, mosi of which are multi-cultural.',
                     '(4) why completely unrelated material without the printed question context.',
                     '(4) Westem societies, mosi of which are multi-cultural.'):
            self.assertEqual(extract(text),[],text)
        self.assertEqual(extract('(4) why Westem societies.',dict(q,context='(4) ____ Western societies.')),[])

    def test_repaired_context_counts_without_cloud_or_overwriting_teacher(self):
        sid,page,blocks=self.setup_grammar()
        blocks[1]['text']='why Westem societies are known for this.'
        blocks[0]['text']='This is probably (1)'
        self.seed(sid,page['id'],blocks,'done')
        q=self.client.get(f'/api/submissions/{sid}').json['reviews'][1]
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':'teacher literal','confirmed':True})
        with patch('settings.recognize_selected') as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
            self.assertEqual(result['reviews'][0]['transcript'],'why')
            self.assertEqual(result['reviews'][0]['score'],1)
            self.assertEqual(result['reviews'][1]['transcript'],'teacher literal')
            self.assertTrue(result['reviews'][1]['confirmed'])
            repeated=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
            self.assertEqual(repeated['history_count'],result['history_count'])
            cloud.assert_not_called()

    def test_cloud_wavy_underline_is_formatting_not_student_answer(self):
        from answer_extraction import grammar_candidates, plain_ocr
        text=r'The group (3) $ \uwave{\text{favered}} $ (favor) over the individual as a result of many rules.'
        question={'question_id':3,'number':3,'kind':'grammar','answer':'is favored',
                  'context':'The group (3) ____ (favor) over the individual as a result of many rules.'}
        found=grammar_candidates([question],[{'id':1,'status':'done','blocks':[
            {'id':0,'text':text,'box':[0,0,900,100],'approximate':True}]}])
        self.assertEqual([q['transcript'] for q in found],['favered'])
        self.assertEqual(plain_ocr(r'$ \uwave{\text{(why)}} $'),'(why)')
        self.assertIn(r'\uwave',text,'Raw cloud result remains unchanged')

    def test_unbalanced_hint_fragments_remain_visible_but_uncertain(self):
        from answer_extraction import grammar_candidates
        q={'question_id':6,'number':6,'kind':'grammar',
           'context':'They (6) ____ (demand) that special rights be given today.'}
        found=grammar_candidates([q],[{'id':1,'status':'done','blocks':[
            {'id':0,'text':'They (6) enhdefahd) that special rights be given today.','box':[0,0,900,100]}]}])
        self.assertEqual(found[0]['transcript'],'enhdefahd)')
        self.assertTrue(found[0]['uncertain'])

    def test_invalid_region_output_is_not_recorded_as_success(self):
        sid, page, blocks = self.setup_grammar(missing=True)
        with patch('settings.recognize_selected'):
            self.post(f'/api/submissions/{sid}/auto-grade', {'reextract': True})
        def useless_region(db, path, local, **options):
            if 'layout_detection' in options:
                self.assertFalse(options['layout_detection'])
            return {'blocks': [{'id': 0, 'text': 'unrelated repeated text', 'box': [0, 0, 300, 100]}]}
        with patch('settings.recognize_selected', side_effect=useless_region) as cloud:
            result = self.post(f'/api/submissions/{sid}/auto-grade', {'rerun': True}).json
        self.assertEqual(cloud.call_count, 2, 'Invalid local supplement falls back to one bounded full-page cloud retry')
        self.assertTrue(result['reviews'][0]['uncertain'])
        self.assertFalse(result['pages'][0]['region_supplement_attempted'])


if __name__ == '__main__':
    unittest.main()
