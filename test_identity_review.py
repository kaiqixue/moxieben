"""Identity assistance uses fake providers and temporary databases only."""
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_batch


class IdentityReviewTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_batch.BatchTests('test_batch_identity_confirmation_idempotence_and_existing_review_preserved')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.client, self.post = self.fx.client, self.fx.post
        self.aid, self.students = self.fx.setup_assignment()
        self.db_path = self.fx.tmp.name + '/dictation.sqlite3'
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('ai_review', json.dumps(dict(enabled=False,base_url='https://fake.invalid',model='fake-vision',api_key='secret'))))

    def photo(self, text='无法读取'):
        photo = self.fx.upload_batch(self.aid)
        blocks = [{'id':0,'text':text,'confidence':.9,'box':[0,0,300,50]}]
        with patch('batch_photos.recognize',return_value={'blocks':blocks,'model':'original-model'}):
            self.assertEqual(self.post(f"/api/batch-photos/{photo['id']}/identify", {'defer_link':True}).status_code,200)
        return photo['id']

    def review(self, pid, **fields):
        result = dict(name='',student_code='001',class_name='',confidence='high') | fields
        with patch('settings._chat',return_value=json.dumps(result)) as provider:
            response = self.post(f'/api/batch-photos/{pid}/identity-review', {})
        return response, provider

    def test_unique_code_preserves_original_and_is_idempotent(self):
        pid = self.photo()
        response, provider = self.review(pid)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['submission_id'])
        self.assertEqual(response.json['identity_review']['read_code'],'001')
        self.assertEqual(response.json['match_details']['method'],'ai_identity')
        self.assertNotIn('张同学',provider.call_args.args[2])
        with sqlite3.connect(self.db_path) as conn:
            original = json.loads(conn.execute('SELECT ocr_result FROM batch_photos WHERE id=?',(pid,)).fetchone()[0])
        self.assertEqual(original['model'],'original-model')
        response, provider = self.review(pid)
        self.assertEqual(response.status_code,200,response.json)
        provider.assert_not_called()

    def test_conflicting_original_code_cannot_auto_assign(self):
        pid = self.photo('学号：002')
        response, _ = self.review(pid)
        self.assertEqual(response.status_code,200,response.json)
        self.assertIsNone(response.json['submission_id'])
        self.assertIn('冲突',response.json['match_details']['reason'])

    def test_fuzzy_name_and_low_confidence_never_auto_assign(self):
        pid = self.photo()
        response, _ = self.review(pid,name='张同字',student_code='',confidence='low')
        self.assertIsNone(response.json['submission_id'])
        self.assertTrue(response.json['candidates'])
        self.assertTrue(all(not c['auto_eligible'] for c in response.json['candidates']))

    def test_unreadable_is_durable_and_not_rebilled(self):
        pid = self.photo()
        response, _ = self.review(pid,student_code='',confidence='unreadable')
        self.assertEqual(response.json['identity_review']['status'],'inconclusive')
        response, provider = self.review(pid)
        self.assertEqual(response.status_code,200)
        provider.assert_not_called()

    def test_provider_error_is_safe_precise_and_durable(self):
        pid = self.photo()
        with patch('settings._chat',side_effect=ValueError('接口返回 HTTP 429：secret')):
            response = self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,503,response.json)
        self.assertEqual(response.json['identity_review']['status'],'error')
        self.assertNotIn('secret',str(response.json))
        self.assertEqual(response.json['identity_review']['error_code'],'rate_limit')
        _, provider = self.review(pid)
        provider.assert_not_called()

    def test_archived_and_payload_rejected_without_provider(self):
        pid = self.photo()
        self.assertEqual(self.post(f'/api/batch-photos/{pid}/identity-review',{'roster':['bad']}).status_code,400)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE classes SET archived=1')
        response, provider = self.review(pid)
        self.assertEqual(response.status_code,409)
        provider.assert_not_called()

    def test_ai_duplicate_competition_is_resolved_after_all_evidence(self):
        ids=[self.photo(),self.photo()]
        runner=self.fx.app.extensions['grading_jobs']
        job=runner.enqueue(assignment_id=self.aid,photo_ids=ids,options={'identity_review_only':True})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE grading_jobs SET state='running',stage='identity_review' WHERE id=?",(job['id'],))
        for pid in ids:
            with patch('settings._chat',return_value=json.dumps(dict(name='',student_code='001',class_name='',confidence='high'))):
                response=self.client.post(f'/api/batch-photos/{pid}/identity-review',json={},headers=self.fx.headers,
                                          environ_overrides={'moxieben.identity_review_job':job['id']})
            self.assertEqual(response.status_code,200,response.json)
            self.assertIsNone(response.json['submission_id'])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE grading_jobs SET state='done' WHERE id=?",(job['id'],))
        photos=self.post(f'/api/assignments/{self.aid}/photos/match',{}).json['photos']
        self.assertTrue(all(p['submission_id'] is None for p in photos))
        self.assertTrue(all(p['match_details']['method']=='duplicate' for p in photos))

    def test_teacher_confirmation_during_request_keeps_teacher_identity(self):
        pid=self.photo()
        def finish(*args,**kwargs):
            response=self.post(f'/api/batch-photos/{pid}/confirm',dict(confirmed=True,assignment_student_id=self.students[1]['id']))
            self.assertEqual(response.status_code,200,response.json)
            return json.dumps(dict(name='',student_code='001',class_name='',confidence='high'))
        with patch('settings._chat',side_effect=finish):
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['suggested_student_id'],self.students[1]['id'])
        self.assertEqual(response.json['identity_source'],'teacher')
        self.assertEqual(response.json['identity_review']['read_code'],'001')

    def test_input_changed_during_request_never_applies_stale_read(self):
        pid=self.photo()
        def finish(*args,**kwargs):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('UPDATE batch_photos SET blocks=? WHERE id=?',(json.dumps([{'text':'new OCR'}]),pid))
            return json.dumps(dict(name='',student_code='001',class_name='',confidence='high'))
        with patch('settings._chat',side_effect=finish):
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,409,response.json)
        self.assertIsNone(self.client.get(f'/api/batch-photos/{pid}').json['submission_id'])

    def test_explicit_retry_keeps_previous_attempt_and_original_ocr(self):
        pid=self.photo()
        with patch('settings._chat',side_effect=TimeoutError()):
            self.assertEqual(self.post(f'/api/batch-photos/{pid}/identity-review',{}).status_code,503)
        with patch('settings._chat',return_value=json.dumps(dict(name='',student_code='',class_name='',confidence='unreadable'))) as model:
            response=self.post(f'/api/batch-photos/{pid}/identity-review/retry',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(model.call_count,2)
        review=response.json['identity_review']
        self.assertEqual(review['history'][0]['status'],'error')
        self.assertEqual(response.json['identified_text'],'无法读取')
        self.assertIsNone(response.json['submission_id'])

    def test_restart_marks_unknown_request_and_requires_explicit_retry(self):
        pid=self.photo()
        with patch('settings._chat',side_effect=SystemExit()):
            with self.assertRaises(SystemExit):self.post(f'/api/batch-photos/{pid}/identity-review',{})
        from app import create_app
        restarted=create_app({'TESTING':True,'DATA_DIR':self.fx.tmp.name,'CSRF_TOKEN':'test'})
        client=restarted.test_client()
        photo=client.get(f'/api/batch-photos/{pid}').json
        self.assertEqual(photo['identity_review']['status'],'unknown')
        with patch('settings._chat') as model:
            self.assertEqual(client.post(f'/api/batch-photos/{pid}/identity-review',json={},headers=self.fx.headers).status_code,200)
        model.assert_not_called()

    def test_no_roster_match_has_specific_reason(self):
        response,_=self.review(self.photo(),student_code='999')
        self.assertIsNone(response.json['submission_id'])
        self.assertIn('不在本次',response.json['match_details']['reason'])

    def test_duplicate_json_keys_rejected(self):
        with patch('settings._chat',return_value='{"name":"","student_code":"999","student_code":"001","class_name":"","confidence":"high"}'):
            response=self.post(f'/api/batch-photos/{self.photo()}/identity-review',{})
        self.assertEqual(response.status_code,503,response.json)
        self.assertEqual(response.json['identity_review']['error_code'],'response_format')

    def test_fenced_identity_json_is_parsed_without_weakening_validation(self):
        pid=self.photo()
        result=dict(name='',student_code='',class_name='',confidence='unreadable')
        with patch('settings._chat',return_value='```json'+chr(10)+json.dumps(result)+chr(10)+'```'):
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['identity_review']['status'],'inconclusive')
        self.assertIsNone(response.json['submission_id'])

    def test_identity_crop_preview_matches_sent_region(self):
        pid=self.photo('姓名：看不清')
        response,_=self.review(pid,student_code='',confidence='unreadable')
        from PIL import Image
        import io
        crop=response.json['identity_review']['crop']
        self.assertIn('url',crop)
        preview=self.client.get(crop['url'])
        self.assertEqual(preview.status_code,200)
        self.assertEqual(list(Image.open(io.BytesIO(preview.data)).size),[crop['box'][2]-crop['box'][0],crop['box'][3]-crop['box'][1]])

    def test_unlabelled_signature_fallback_keeps_name_candidate_without_auto_linking(self):
        import io
        from PIL import Image
        pid=self.photo()
        empty=dict(name='',student_code='',class_name='',confidence='unreadable')
        signature=dict(name='张同学',student_code='',class_name='',confidence='low')
        with patch('settings._chat',side_effect=[json.dumps(empty),json.dumps(signature)]) as provider:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(provider.call_count,2)
        self.assertEqual(response.status_code,200,response.json)
        review=response.json['identity_review']
        self.assertEqual(review['read_name'],'张同学')
        self.assertEqual(review['crop']['kind'],'signature_header')
        self.assertEqual(len(review['views']),2)
        self.assertIsNone(response.json['submission_id'])
        self.assertTrue(any(c['name']=='张同学' for c in response.json['candidates']))
        preview=self.client.get(review['crop']['url'])
        self.assertEqual(Image.open(io.BytesIO(preview.data)).size,Image.open(io.BytesIO(provider.call_args.args[1])).size)

    def test_uncertain_whole_page_name_gets_detail_read_without_discarding_evidence(self):
        pid=self.photo()
        whole=dict(name='张同字',student_code='',class_name='',confidence='low')
        detail=dict(name='张同学',student_code='',class_name='',confidence='high')
        with patch('settings._chat',side_effect=[json.dumps(whole),json.dumps(detail)]) as provider:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(provider.call_count,2)
        review=response.json['identity_review']
        self.assertEqual(review['read_name'],'张同学')
        self.assertEqual(review['confidence'],'low')
        self.assertEqual(review['views'][0]['result']['name'],'张同字')
        self.assertIsNone(response.json['submission_id'])

    def test_sideways_photo_uses_ocr_rotation_for_both_views_and_preview(self):
        import io
        from pathlib import Path
        from PIL import Image, ImageDraw
        pid=self.photo()
        upright=Image.new('RGB',(240,640),'white')
        ImageDraw.Draw(upright).rectangle((0,0,80,40),fill='red')
        with sqlite3.connect(self.db_path) as conn:
            filename,original=conn.execute('SELECT image,ocr_result FROM batch_photos WHERE id=?',(pid,)).fetchone()
            upright.transpose(Image.Transpose.ROTATE_90).save(Path(self.fx.tmp.name,'uploads',filename))
            original=json.loads(original)
            original['raw']={'pruned_result':{'doc_preprocessor_res':{'angle':270}}}
            conn.execute('UPDATE batch_photos SET ocr_result=? WHERE id=?',(json.dumps(original),pid))
        empty=dict(name='',student_code='',class_name='',confidence='unreadable')
        named=dict(name='张同学',student_code='',class_name='',confidence='low')
        with patch('settings._chat',side_effect=[json.dumps(empty),json.dumps(named)]) as provider:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,200,response.json)
        whole=Image.open(io.BytesIO(provider.call_args_list[0].args[1]))
        detail=Image.open(io.BytesIO(provider.call_args_list[1].args[1]))
        self.assertEqual(whole.size,upright.size)
        self.assertEqual(whole.getpixel((10,10)),(255,0,0))
        self.assertEqual(detail.getpixel((10,10)),(255,0,0))
        review=response.json['identity_review']
        self.assertEqual(review['crop']['rotation'],270)
        preview=self.client.get(review['crop']['url'])
        self.assertEqual(preview.data,provider.call_args_list[1].args[1])
        self.assertIsNone(response.json['submission_id'])

    def test_rotated_whole_page_and_label_previews_match_sent_image(self):
        for text in ('无法读取','姓名：看不清'):
            with self.subTest(text=text):
                pid=self.photo(text)
                with sqlite3.connect(self.db_path) as conn:
                    original=json.loads(conn.execute('SELECT ocr_result FROM batch_photos WHERE id=?',(pid,)).fetchone()[0])
                    original['raw']={'doc_preprocessor_res':{'angle':90}}
                    conn.execute('UPDATE batch_photos SET ocr_result=? WHERE id=?',(json.dumps(original),pid))
                response,provider=self.review(pid,name='张同学',student_code='',confidence='high')
                crop=response.json['identity_review']['crop']
                self.assertIn('url',crop)
                self.assertEqual(self.client.get(crop['url']).data,provider.call_args.args[1])

    def test_high_confidence_unique_name_links_and_queues_grading_without_code(self):
        pid=self.photo()
        response,provider=self.review(pid,name='张同学',student_code='',confidence='high')
        self.assertEqual(response.status_code,200,response.json)
        self.assertIsNotNone(response.json['submission_id'])
        self.assertEqual(response.json['match_details']['method'],'ai_identity')
        self.assertIn('姓名',response.json['match_details']['reason'])
        provider.assert_called_once()
        with sqlite3.connect(self.db_path) as conn:
            jobs=conn.execute('SELECT submission_id,state FROM grading_jobs').fetchall()
        self.assertEqual(jobs,[(response.json['submission_id'],'queued')])
        with patch('settings._chat') as model:
            again=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        model.assert_not_called()
        self.assertEqual(again.json['submission_id'],response.json['submission_id'])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grading_jobs').fetchone()[0],1)

    def test_unique_one_character_candidate_requires_distinct_visual_verification(self):
        pid=self.photo('张同字')
        free=dict(name='张同字',student_code='',class_name='',confidence='low')
        verified=dict(name='张同学',decision='match',confidence='high',evidence='最后一字可辨认为学，与签名字形一致')
        with patch('settings._chat',side_effect=[json.dumps(free),json.dumps(free),json.dumps(verified)]) as model:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(model.call_count,3)
        self.assertIsNotNone(response.json['submission_id'])
        review=response.json['identity_review']
        self.assertEqual(review['read_name'],'张同字','keep free transcription separate')
        self.assertEqual(review['verification']['result']['name'],'张同学')
        self.assertIn('张同学',model.call_args.args[2])
        self.assertNotIn('001',model.call_args.args[2])
        self.assertNotIn('张同学',model.call_args_list[0].args[2])
        self.assertIn('签名',response.json['match_details']['reason'])

    def test_candidate_verification_uncertain_or_none_never_auto_links(self):
        for decision in ('uncertain','none'):
            with self.subTest(decision=decision):
                pid=self.photo('张同字')
                free=dict(name='张同字',student_code='',class_name='',confidence='low')
                result=dict(name='',decision=decision,confidence='low',evidence='字迹不足以确认')
                with patch('settings._chat',side_effect=[json.dumps(free),json.dumps(free),json.dumps(result)]):
                    response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
                self.assertEqual(response.status_code,200,response.json)
                self.assertIsNone(response.json['submission_id'])
                self.assertEqual(response.json['identity_review']['verification']['result']['decision'],decision)

    def test_candidate_verification_failure_keeps_free_reading_and_candidates(self):
        pid=self.photo('张同字')
        free=dict(name='张同字',student_code='',class_name='',confidence='low')
        with patch('settings._chat',side_effect=[json.dumps(free),json.dumps(free),TimeoutError()]):
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['identity_review']['read_name'],'张同字')
        self.assertEqual(response.json['identity_review']['verification']['status'],'error')
        self.assertIsNone(response.json['submission_id'])
        self.assertTrue(response.json['candidates'])

    def test_weak_supplementary_candidates_do_not_claim_multiple_best_matches(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE assignment_students SET name=? WHERE id=?',('李别字',self.students[1]['id']))
        pid=self.photo('张同字')
        free=dict(name='张同字',student_code='',class_name='',confidence='low')
        no_match=dict(name='',decision='uncertain',confidence='low',evidence='无法确认')
        with patch('settings._chat',side_effect=[json.dumps(free),json.dumps(free),json.dumps(no_match)]):
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertIn('唯一最佳候选',response.json['match_details']['reason'])
        self.assertNotIn('对应多个候选',response.json['match_details']['reason'])

    def test_high_name_cannot_win_over_another_photo_competing_for_same_student(self):
        first=self.photo('张同字')
        second=self.photo('张同字')
        response,_=self.review(first,name='张同学',student_code='',confidence='high')
        self.assertIsNone(response.json['submission_id'])
        self.assertIn('竞争',response.json['match_details']['reason'])
        self.assertIsNone(self.client.get(f'/api/batch-photos/{second}').json['submission_id'])

    def test_duplicate_name_and_explicit_other_name_are_not_auto_assigned(self):
        for original,name in [('未知','李同学'),('姓名：张同学','李同学')]:
            with self.subTest(original=original):
                pid=self.photo(original)
                response,_=self.review(pid,name=name,student_code='',confidence='high')
                self.assertIsNone(response.json['submission_id'])

    def test_candidate_verification_rejects_foreign_name_extra_keys_and_duplicates(self):
        from identity_review import verify_candidate
        from settings import VisualReviewError
        valid=dict(name='张同学',decision='match',confidence='high',evidence='姓名笔画清晰')
        invalid=[json.dumps(valid|{'name':'其他人'}),json.dumps(valid|{'student_id':12}),
                 json.dumps(valid|{'evidence':''}),
                 '{"name":"张同学","name":"张同学","decision":"match","confidence":"high","evidence":"笔画清楚"}']
        for content in invalid:
            with self.subTest(content=content),patch('settings._chat',return_value=content):
                with self.assertRaises(VisualReviewError):verify_candidate({},b'fake','张同学')

    def test_repeated_low_name_without_visual_match_never_auto_links(self):
        pid=self.photo('未知')
        response,model=self.review(pid,name='张同学',student_code='',confidence='low')
        self.assertIsNone(response.json['submission_id'])
        self.assertEqual(model.call_count,2)
        self.assertNotIn('verification',response.json['identity_review'])

    def test_candidate_verification_cannot_override_teacher_confirmation(self):
        pid=self.photo('张同字')
        free=dict(name='张同字',student_code='',class_name='',confidence='low')
        def verify(*args,**kwargs):
            result=self.post(f'/api/batch-photos/{pid}/confirm',dict(confirmed=True,assignment_student_id=self.students[1]['id']))
            self.assertEqual(result.status_code,200,result.json)
            return json.dumps(dict(name='张同学',decision='match',confidence='high',evidence='可辨姓名'))
        calls=iter([json.dumps(free),json.dumps(free)])
        def model(*args,**kwargs):
            try:return next(calls)
            except StopIteration:return verify()
        with patch('settings._chat',side_effect=model):
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.json['identity_source'],'teacher')
        self.assertEqual(response.json['suggested_student_id'],self.students[1]['id'])

    def test_class_conflict_in_any_view_prevents_candidate_verification(self):
        pid=self.photo('张同字')
        whole=dict(name='张同字',student_code='',class_name='其他班',confidence='low')
        detail=dict(whole,class_name='')
        with patch('settings._chat',side_effect=[json.dumps(whole),json.dumps(detail)]) as model:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertIsNone(response.json['submission_id'])
        self.assertEqual(model.call_count,2)
        self.assertTrue(response.json['identity_review']['class_conflict'])

    def test_conflicting_class_in_first_view_cannot_be_hidden_by_second_view(self):
        pid=self.photo('张同字')
        with sqlite3.connect(self.db_path) as conn:
            name=conn.execute('SELECT c.name FROM classes c JOIN assignments a ON a.class_id=c.id WHERE a.id=?',(self.aid,)).fetchone()[0]
        first=dict(name='张同字',student_code='',class_name='其他班',confidence='low')
        second=dict(first,class_name=name)
        with patch('settings._chat',side_effect=[json.dumps(first),json.dumps(second)]) as model:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertIsNone(response.json['submission_id'])
        self.assertEqual(model.call_count,2)
        self.assertTrue(response.json['identity_review']['class_conflict'])

    def test_two_equal_best_candidates_are_never_narrowed_by_model_guess(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE assignment_students SET name=? WHERE id=?',('张同宇',self.students[1]['id']))
        pid=self.photo('张同字')
        response,model=self.review(pid,name='张同字',student_code='',confidence='low')
        self.assertIsNone(response.json['submission_id'])
        self.assertEqual(model.call_count,2)
        self.assertNotIn('verification',response.json['identity_review'])

    def test_standalone_auto_link_drains_existing_queue_without_repeating_cloud_ocr(self):
        pid=self.photo('未识别身份')
        response,_=self.review(pid,name='张同学',student_code='',confidence='high')
        sid=response.json['submission_id']
        self.assertIsNotNone(sid)
        with patch('settings.recognize_selected') as ocr,patch('settings._chat') as model:
            self.fx.app.extensions['grading_jobs'].drain()
        ocr.assert_not_called();model.assert_not_called()
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute('SELECT grading_state FROM submissions WHERE id=?',(sid,)).fetchone()[0],'final')

    def test_disagreeing_free_views_never_trigger_candidate_led_confirmation(self):
        pid=self.photo('未知')
        first=dict(name='王同字',student_code='',class_name='',confidence='low')
        second=dict(first,name='张同字')
        with patch('settings._chat',side_effect=[json.dumps(first),json.dumps(second)]) as model:
            response=self.post(f'/api/batch-photos/{pid}/identity-review',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(model.call_count,2)
        self.assertNotIn('verification',response.json['identity_review'])
        self.assertIsNone(response.json['submission_id'])
        self.assertIn('读法不一致',response.json['match_details']['reason'])

    def test_one_free_reading_cannot_change_previous_best_candidate_and_auto_link(self):
        pid=self.photo('张同字')
        with sqlite3.connect(self.db_path) as conn:
            original=json.loads(conn.execute('SELECT ocr_result FROM batch_photos WHERE id=?',(pid,)).fetchone()[0])
            original['identity_review']={'status':'inconclusive','read_name':'张同字','read_code':'','confidence':'low','created_at':'old'}
            conn.execute('UPDATE batch_photos SET ocr_result=? WHERE id=?',(json.dumps(original),pid))
        first=dict(name='李同字',student_code='',class_name='',confidence='low')
        second=dict(first)
        with patch('settings._chat',side_effect=[json.dumps(first),json.dumps(second)]) as model:
            response=self.post(f'/api/batch-photos/{pid}/identity-review/retry',{})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(model.call_count,2)
        self.assertNotIn('verification',response.json['identity_review'])
        self.assertIsNone(response.json['submission_id'])

    def test_high_exact_name_still_respects_contrary_saved_primary_candidate(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE assignment_students SET name=? WHERE id=?',('王同宇',self.students[1]['id']))
        pid=self.photo('张同字')
        response,_=self.review(pid,name='王同宇',student_code='',confidence='high')
        self.assertIsNone(response.json['submission_id'])
        self.assertIn('冲突',response.json['match_details']['reason'])


if __name__ == '__main__':
    unittest.main()
