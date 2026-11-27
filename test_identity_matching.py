"""Two-pass identity integration checks; no real student files or cloud calls."""
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_batch


class IdentityMatchingTests(unittest.TestCase):
    setUp = test_batch.BatchTests.setUp
    post = test_batch.BatchTests.post
    klass = test_batch.BatchTests.klass
    task = test_batch.BatchTests.task
    upload_batch = test_batch.BatchTests.upload_batch

    def setup_roster(self,names):
        cid=self.klass()
        for i,name in enumerate(names,1):
            self.post(f'/api/classes/{cid}/members',{'student_code':f'{i:03}','name':name})
        tid=self.task()
        aid=self.post(f'/api/tasks/{tid}/assignments',{'class_ids':[cid]}).json['assignments'][0]['id']
        return aid,self.client.get(f'/api/assignments/{aid}').json['students']

    def identify(self,aid,text,defer=True):
        photo=self.upload_batch(aid)
        blocks=[{'id':i,'text':t,'box':[0,i*60,500,i*60+40]} for i,t in enumerate((text+'\n1. receive').splitlines())]
        with patch('batch_photos.recognize',return_value={'blocks':blocks}):
            response=self.post(f"/api/batch-photos/{photo['id']}/identify",{'defer_link':defer})
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def match(self,aid):
        with patch('settings.recognize_selected') as cloud:
            response=self.post(f'/api/assignments/{aid}/photos/match',{})
            cloud.assert_not_called()
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def test_defer_exact_then_fuzzy_is_order_independent_and_idempotent(self):
        for reverse in (False,True):
            aid,students=self.setup_roster(['许映舟','许瀛舟'])
            texts=['许咏舟','姓名：许瀛舟']
            if reverse:texts.reverse()
            uploaded=[self.identify(aid,t) for t in texts]
            self.assertTrue(all(p['submission_id'] is None for p in uploaded))
            result=self.match(aid)
            self.assertEqual(result['summary']['matched'],2)
            self.assertEqual(result['summary']['inferred'],1)
            fuzzy=next(p for p in result['photos'] if p['match_details']['method']=='inferred')
            self.assertEqual(fuzzy['suggested_student_id'],students[0]['id'])
            self.assertEqual(fuzzy['match_details']['observed_name'],'许咏舟')
            self.assertIn('许咏舟',fuzzy['identified_text'])
            ids=[p['page_id'] for p in result['photos']]
            again=self.match(aid)
            self.assertEqual([p['page_id'] for p in again['photos']],ids)
            self.assertEqual(again['linked_submission_ids'],[])
            sid=fuzzy['submission_id']
            graded=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
            self.assertTrue(graded['is_final']);self.assertEqual(graded['score'],2)

    def test_one_character_at_any_position_including_surname_is_inferred(self):
        for expected,observed,index in [('顾青禾','杜青禾',0),('顾青禾','顾清禾',1),
                                        ('顾青禾','顾青和',2),('欧阳明月','区阳明月',0),
                                        ('欧阳明月','欧阳明日',3)]:
            with self.subTest(observed=observed):
                aid,students=self.setup_roster([expected])
                photo=self.identify(aid,observed,False)
                self.assertIsNotNone(photo['submission_id'])
                details=photo['match_details']
                self.assertEqual(details['method'],'inferred')
                self.assertEqual(details['observed_name'],observed)
                self.assertEqual(details['candidates'][0]['differences'],[
                    {'index':index,'observed':observed[index],'expected':expected[index]}])
                for text in (observed,expected,students[0]['student_code'],f'第 {index+1} 字'):
                    self.assertIn(text,details['reason'])
                self.assertEqual(photo['blocks'][0]['text'],observed)
                self.assertEqual(self.match(aid)['linked_submission_ids'],[])
                cid=self.client.get(f'/api/assignments/{aid}').json['class_id']
                self.assertEqual(self.client.get(f'/api/classes/{cid}/identity-memories').json['memories'],[])

    def test_surname_inference_waits_for_exact_matches_independent_of_order(self):
        for reverse in (False,True):
            aid,students=self.setup_roster(['顾青禾','吴青禾'])
            texts=['杜青禾','吴青禾']
            if reverse:texts.reverse()
            for text in texts:self.identify(aid,text)
            result=self.match(aid)
            self.assertEqual(result['summary']['matched'],2)
            self.assertEqual(result['summary']['inferred'],1)
            inferred=next(p for p in result['photos'] if p['match_details']['method']=='inferred')
            self.assertEqual(inferred['suggested_student_id'],students[0]['id'])

    def test_surname_candidates_remain_manual_when_ambiguous_or_competing(self):
        for names,texts,reason in [(['顾青禾','吴青禾'],['杜青禾'],'多个相似候选'),
                                   (['顾青禾'],['杜青禾','吴青禾'],'竞争'),
                                   (['顾青禾','张同学'],['学号：002 姓名：杜青禾'],'学号'),
                                   (['顾青禾'],['姓名：杜青禾\n姓名：吴青禾'],'多个身份')]:
            with self.subTest(texts=texts):
                aid,_=self.setup_roster(names)
                for text in texts:self.identify(aid,text)
                result=self.match(aid)
                self.assertEqual(result['summary']['matched'],0)
                self.assertTrue(all(p['candidates'] for p in result['photos']))
                self.assertTrue(all(reason in p['match_details']['reason'] for p in result['photos']))

    def test_short_or_incomplete_names_and_multiple_bare_names_do_not_auto_link(self):
        for expected,observed in [('李白','季白'),('顾青禾','顾禾'),
                                  ('顾青禾','杜清禾'),('顾青禾','Homework only'),
                                  ('顾青禾','杜青禾\n王小明')]:
            aid,_=self.setup_roster([expected])
            photo=self.identify(aid,observed,False)
            self.assertIsNone(photo['submission_id'])
            if observed=='季白':
                self.assertTrue(photo['candidates'])
                self.assertEqual(photo['candidates'][0]['name'],'李白')
                self.assertFalse(photo['candidates'][0]['auto_eligible'])

    def test_digit_suffix_and_insert_delete_auto_match_preserve_original(self):
        cases=[('何远川1','何远川','suffix'),('何远川１２','何远川','suffix'),
               ('何远川','姓名：何远川1','suffix'),('欧阳明月','欧阳月','edit'),
               ('顾青禾','顾青小禾','edit'),('欧阳明月','欧阳小明月','edit')]
        for expected,text,kind in cases:
            with self.subTest(expected=expected,text=text):
                aid,students=self.setup_roster([expected])
                photo=self.identify(aid,text,False)
                self.assertIsNotNone(photo['submission_id'])
                candidate=photo['match_details']['candidates'][0]
                self.assertEqual(candidate['comparison'],kind)
                self.assertEqual(candidate['distance'],0 if kind=='suffix' else 1)
                self.assertIn(expected,photo['match_details']['reason'])
                self.assertEqual(photo['blocks'][0]['text'],text)
                self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['students'][0]['name'],expected)
                again=self.match(aid)
                self.assertEqual(again['linked_submission_ids'],[])
                cid=self.client.get(f'/api/assignments/{aid}').json['class_id']
                self.assertEqual(self.client.get(f'/api/classes/{cid}/identity-memories').json['memories'],[])

    def test_suffix_collision_never_resolves_by_elimination(self):
        aid,students=self.setup_roster(['何远川1','何远川2'])
        self.identify(aid,'姓名：何远川1',False)
        photo=self.identify(aid,'何远川',False)
        self.assertIsNone(photo['submission_id'])
        self.assertTrue(photo['candidates'])
        self.assertTrue(all(not c['auto_eligible'] for c in photo['candidates']))
        self.assertIn('同名',photo['match_details']['reason'])

    def test_rematch_grades_only_new_links_from_saved_text_without_cloud(self):
        aid,_=self.setup_roster(['何远川1','张同学'])
        old=self.identify(aid,'张同学',False)
        self.identify(aid,'何远川')
        runner=self.app.extensions['grading_jobs']
        with patch.object(runner,'enqueue',wraps=runner.enqueue) as enqueue, patch('settings.recognize_selected') as cloud:
            result=self.post(f'/api/assignments/{aid}/photos/match',{'auto_grade':True})
            sid=result.json['linked_submission_ids'][0]
            enqueue.assert_called_once_with(submission_id=sid,options={'reextract':True})
            runner.drain()
            self.assertTrue(self.client.get(f'/api/submissions/{sid}').json['is_final'])
            self.assertFalse(self.client.get(f"/api/submissions/{old['submission_id']}").json['is_final'])
            enqueue.reset_mock()
            self.post(f'/api/assignments/{aid}/photos/match',{'auto_grade':True})
            enqueue.assert_not_called()
            cloud.assert_not_called()

    def test_weak_candidates_never_promoted_and_ranked_separately(self):
        from identity_matching import identity_evidence
        for expected,text in [('顾青禾1','杜青禾'),('顾青禾','杜清禾'),('顾青禾','顾禾'),('李白','李太白')]:
            aid,_=self.setup_roster([expected])
            photo=self.identify(aid,text,False)
            self.assertIsNone(photo['submission_id'])
            self.assertTrue(photo['candidates'])
            self.assertFalse(photo['candidates'][0]['auto_eligible'])
        students=[{'id':1,'student_code':'001','name':'顾青禾'},
                  {'id':2,'student_code':'002','name':'顾清禾'}]
        e=identity_evidence([{'text':'杜青禾'}],students)
        self.assertEqual([c['distance'] for c in e['candidates']],[1,2])
        self.assertFalse(e['candidates'][0]['supplementary'])
        self.assertTrue(e['candidates'][1]['supplementary'])
        aid,_=self.setup_roster(['顾青禾','顾清禾'])
        for text in ['杜青禾','顾青禾']:self.identify(aid,text)
        matched=self.match(aid)
        self.assertEqual(matched['summary']['matched'],1)
        pending=next(p for p in matched['photos'] if not p['submission_id'])
        self.assertFalse(pending['candidates'][1]['auto_eligible'])
        e=identity_evidence([{'text':'王小明'}],[students[0]])
        self.assertEqual(e['candidates'],[])

    def test_suffix_competition_conflict_exempt_and_exact_precedence(self):
        for names,texts in [(['何远川1'],['何远川','何远川２']),
                             (['何远川1','张同学'],['学号：002 姓名：何远川']),
                             (['欧阳明月'],['欧阳月','欧阳小明月'])]:
            aid,_=self.setup_roster(names)
            for text in texts:self.identify(aid,text)
            self.assertEqual(self.match(aid)['summary']['matched'],0)
        aid,students=self.setup_roster(['何远川1','何远川'])
        p=self.identify(aid,'何远川',False)
        self.assertEqual(p['suggested_student_id'],students[1]['id'])
        self.assertEqual(p['match_details']['method'],'exact')
        aid,students=self.setup_roster(['何远川1'])
        self.client.patch(f"/api/assignment-students/{students[0]['id']}",json={'exempt':True,'reason':'请假'},headers=self.headers)
        self.assertIsNone(self.identify(aid,'何远川',False)['submission_id'])

    def test_suffix_memory_priority_and_conflict_blocking(self):
        aid,students=self.setup_roster(['何远川1','张同学'])
        cid=self.client.get(f'/api/assignments/{aid}').json['class_id']
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute('''INSERT INTO identity_memories(class_id,observed_name,student_id,created_at,updated_at)
                VALUES(?, '何远川', ?, 'now','now')''',(cid,students[1]['student_id']))
        p=self.identify(aid,'何远川',False)
        self.assertEqual(p['suggested_student_id'],students[1]['id'])
        self.assertEqual(p['match_details']['method'],'memory')
        aid,students=self.setup_roster(['何远川1','张同学'])
        cid=self.client.get(f'/api/assignments/{aid}').json['class_id']
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            for s in students:
                conn.execute('''INSERT INTO identity_memories(class_id,observed_name,student_id,created_at,updated_at)
                    VALUES(?, '何远川', ?, 'now','now')''',(cid,s['student_id']))
        p=self.identify(aid,'何远川',False)
        self.assertIsNone(p['submission_id'])
        self.assertIn('记忆冲突',p['match_details']['reason'])

    def test_suffix_name_extraction_does_not_merge_student_code(self):
        from identity_matching import identity_evidence
        roster=[{'id':1,'student_code':'268146','name':'何远川1'}]
        for text in ['268146 何远川1','姓名：何远川1 学号：268146','何远川1\n268146']:
            e=identity_evidence([{'text':text}],roster)
            self.assertEqual(e['names'],['何远川1'])
            self.assertEqual(e['code'],'268146')
            self.assertEqual(e['exact_id'],1)

    def test_surname_inference_exempt_occupied_and_memory_conflict_protection(self):
        aid,students=self.setup_roster(['顾青禾','张同学'])
        self.client.patch(f"/api/assignment-students/{students[0]['id']}",json={'exempt':True,'reason':'请假'},headers=self.headers)
        self.assertIsNone(self.identify(aid,'杜青禾',False)['submission_id'])
        aid,students=self.setup_roster(['顾青禾','张同学'])
        original=self.identify(aid,'顾青禾',False)
        pending=self.identify(aid,'杜青禾',False)
        self.assertIsNone(pending['submission_id'])
        self.assertIn('已有照片',pending['match_details']['reason'])
        self.assertEqual(len(self.client.get(f"/api/submissions/{original['submission_id']}").json['pages']),1)
        # A conflicting teacher memory cannot be bypassed by the new surname rule.
        cid=self.client.get(f'/api/assignments/{aid}').json['class_id']
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            for student in students:
                conn.execute('''INSERT INTO identity_memories(class_id,observed_name,student_id,created_at,updated_at)
                    VALUES(?, '吴青禾', ?, 'now','now')''',(cid,student['student_id']))
        memory_photo=self.identify(aid,'吴青禾',False)
        self.assertIsNone(memory_photo['submission_id'])
        self.assertIn('记忆冲突',memory_photo['match_details']['reason'])

    def test_name_line_with_trailing_comma_matches_and_preserves_ocr_original(self):
        aid,students=self.setup_roster(['齐林雪','张同学'])
        photo=self.identify(aid,'齐林雪，')
        result=self.match(aid)
        saved=next(p for p in result['photos'] if p['id']==photo['id'])
        self.assertEqual(saved['suggested_student_id'],students[0]['id'])
        self.assertIsNotNone(saved['submission_id'])
        self.assertEqual(saved['match_details']['method'],'exact')
        self.assertEqual(saved['match_details']['observed_name'],'齐林雪')
        self.assertEqual(saved['blocks'][0]['text'],'齐林雪，')
        self.assertIn('齐林雪，',saved['identified_text'])
        repeated=self.match(aid)
        self.assertEqual(repeated['linked_submission_ids'],[])
        self.assertEqual(repeated['photos'][0]['page_id'],saved['page_id'])

    def test_name_edge_punctuation_does_not_remove_inner_separators_or_conflicts(self):
        from identity_matching import identity_evidence
        students=[{'id':1,'student_code':'001','name':'齐林雪'},
                  {'id':2,'student_code':'002','name':'张同学'}]
        for suffix in ['，',',','。','.','；',';',' ， ']:
            with self.subTest(suffix=suffix):
                evidence=identity_evidence([{'text':'齐林雪'+suffix}],students)
                self.assertEqual(evidence['exact_id'],1)
                self.assertEqual(evidence['text'],('齐林雪'+suffix).strip())
        pair=identity_evidence([{'text':'001 齐林雪，'}],students)
        self.assertEqual(pair['exact_id'],1)
        for text in ['齐林雪，张同学','齐林雪，\n张同学。','学号：002\n齐林雪，']:
            evidence=identity_evidence([{'text':text}],students)
            self.assertIsNone(evidence['exact_id'],text)
        duplicate=identity_evidence([{'text':'齐林雪，'}],students+[{'id':3,'student_code':'003','name':'齐林雪'}])
        self.assertTrue(duplicate['blocked'])
        self.assertIsNone(duplicate['exact_id'])

    def test_punctuated_duplicate_photos_show_candidate_without_auto_choosing_one(self):
        aid,students=self.setup_roster(['齐林雪'])
        self.identify(aid,'齐林雪，')
        self.identify(aid,'齐林雪，')
        result=self.match(aid)
        self.assertEqual(result['summary']['duplicate'],2)
        self.assertEqual(result['linked_submission_ids'],[])
        for photo in result['photos']:
            self.assertIsNone(photo['submission_id'])
            self.assertEqual(photo['suggested_student_id'],students[0]['id'])
            self.assertEqual(photo['candidates'][0]['name'],'齐林雪')

    def test_duplicate_exact_reserves_student_and_does_not_append(self):
        aid,students=self.setup_roster(['许映舟','许瀛舟'])
        self.identify(aid,'许映舟');self.identify(aid,'许映舟');self.identify(aid,'许咏舟')
        result=self.match(aid)
        self.assertEqual(result['summary']['duplicate'],2)
        self.assertEqual(result['summary']['matched'],0)
        self.assertTrue(all(p['submission_id'] is None for p in result['photos']))

    def test_competing_fuzzy_unknown_conflict_and_two_character_need_selection(self):
        aid,_=self.setup_roster(['许映舟','李白'])
        for value in ['许咏舟','许瀛舟','学号：999 姓名：许咏舟','李自','无名照片正文']:
            self.identify(aid,value)
        result=self.match(aid)
        self.assertEqual(result['summary']['matched'],0)
        self.assertTrue(any(p['candidates'] for p in result['photos']))
        aid,_=self.setup_roster(['许映舟'])
        self.identify(aid,'This is homework')
        self.assertEqual(self.match(aid)['summary']['matched'],0)

    def test_used_student_duplicate_and_empty_submission_still_available(self):
        aid,students=self.setup_roster(['许映舟','张同学'])
        empty=self.post(f"/api/assignment-students/{students[0]['id']}/submission",{})
        self.assertEqual(empty.status_code,201,empty.json)
        photo=self.identify(aid,'许咏舟',False)
        self.assertIsNotNone(photo['submission_id'])
        duplicate=self.identify(aid,'许映舟',False)
        self.assertIsNone(duplicate['submission_id'])
        self.assertEqual(duplicate['match_details']['method'],'duplicate')
        self.assertEqual(self.client.get(f"/api/submissions/{photo['submission_id']}").json['pages'].__len__(),1)

    def test_same_names_exempt_and_explicit_identity_conflict(self):
        aid,students=self.setup_roster(['张同学','张同学','许映舟'])
        self.client.patch(f"/api/assignment-students/{students[2]['id']}",json={'exempt':True,'reason':'请假'},headers=self.headers)
        for value in ['张同学','许咏舟','学号：001 姓名：许映舟']:
            self.identify(aid,value)
        result=self.match(aid)
        self.assertEqual(result['summary']['matched'],0)
        self.assertEqual(result['summary']['remaining_students'],2)

    def test_reassign_inferred_preserves_audit_resets_old_and_regrades_new(self):
        aid,students=self.setup_roster(['许映舟','张同学'])
        photo=self.identify(aid,'许咏舟',False)
        sid=photo['submission_id'];self.assertIsNotNone(sid)
        self.assertTrue(self.post(f'/api/submissions/{sid}/auto-grade',{}).json['is_final'])
        response=self.post(f"/api/batch-photos/{photo['id']}/reassign",{'assignment_student_id':students[1]['id'],'confirmed':True})
        self.assertEqual(response.status_code,200,response.json)
        new=response.json
        self.assertNotEqual(new['submission_id'],sid)
        old=self.client.get(f'/api/submissions/{sid}').json
        self.assertFalse(old['is_final']);self.assertEqual(old['pages'],[])
        self.assertIsNone(old['score'])
        self.assertTrue(self.post(f"/api/submissions/{new['submission_id']}/auto-grade",{}).json['is_final'])
        history=self.client.get(f'/api/submissions/{sid}/history').json['events']
        self.assertTrue(any(e['snapshot'].get('action')=='identity_reassigned' for e in history))
        self.assertEqual(new['photo']['match_details']['method'],'corrected')

    def test_reassign_rejects_manual_source_teacher_edits_and_target_photos(self):
        aid,students=self.setup_roster(['许映舟','张同学'])
        photo=self.identify(aid,'许咏舟',False);sid=photo['submission_id']
        result=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
        q=result['reviews'][0]
        self.post(f"/api/submissions/{sid}/reviews/{q['question_id']}",{'transcript':'receive','confirmed':False})
        payload={'assignment_student_id':students[1]['id'],'confirmed':True}
        self.assertEqual(self.post(f"/api/batch-photos/{photo['id']}/reassign",payload).status_code,409)
        self.assertEqual(self.client.get(f'/api/submissions/{sid}').json['identity']['name'],'许映舟')
        aid,students=self.setup_roster(['许映舟','张同学'])
        inferred=self.identify(aid,'许咏舟',False);exact=self.identify(aid,'张同学',False)
        self.assertEqual(self.post(f"/api/batch-photos/{inferred['id']}/reassign",{'assignment_student_id':students[1]['id'],'confirmed':True}).status_code,409)
        self.assertEqual(self.post(f"/api/batch-photos/{exact['id']}/reassign",{'assignment_student_id':students[0]['id'],'confirmed':True}).status_code,409)

    def test_simultaneous_identification_preserves_successful_ocr_and_later_matches_all(self):
        aid,students=self.setup_roster(['许映舟','张同学'])
        other=self.upload_batch(aid)
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute("UPDATE batch_photos SET status='identifying' WHERE id=?",(other['id'],))
        completed=self.identify(aid,'许咏舟',False)
        self.assertEqual(completed['status'],'identified')
        self.assertIn('许咏舟',completed['identified_text'])
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute("UPDATE batch_photos SET status='identified',blocks=? WHERE id=?",(json.dumps([{'id':0,'text':'张同学','box':[0,0,200,40]}]),other['id']))
        result=self.match(aid)
        self.assertEqual(result['summary']['matched'],2)

    def test_identify_returns_all_new_links_for_single_flow_grading(self):
        aid,students=self.setup_roster(['许映舟','许瀛舟'])
        first=self.identify(aid,'许咏舟',False)
        self.assertIsNone(first['submission_id'])
        last=self.identify(aid,'许瀛舟',False)
        self.assertEqual(len(last['linked_submission_ids']),2)

    def test_fuzzy_duplicate_names_do_not_become_unique_by_elimination(self):
        aid,_=self.setup_roster(['许映舟','许映舟'])
        self.identify(aid,'学号：001 姓名：许映舟');self.identify(aid,'许咏舟')
        result=self.match(aid)
        self.assertEqual(result['summary']['matched'],1)
        self.assertEqual(result['summary']['inferred'],0)

    def test_known_code_with_unrelated_chinese_heading_still_matches(self):
        aid,_=self.setup_roster(['许映舟'])
        photo=self.identify(aid,'学号：001\n周小测',False)
        self.assertIsNotNone(photo['submission_id'])
        self.assertEqual(photo['match_details']['method'],'exact')

    def test_manual_page_review_prevents_reassignment(self):
        aid,students=self.setup_roster(['许映舟','张同学'])
        photo=self.identify(aid,'许咏舟',False)
        self.assertEqual(self.post(f"/api/pages/{photo['page_id']}/reviewed",{'confirmed':True}).status_code,200)
        response=self.post(f"/api/batch-photos/{photo['id']}/reassign",{'assignment_student_id':students[1]['id'],'confirmed':True})
        self.assertEqual(response.status_code,409,response.json)


if __name__=='__main__':unittest.main()
