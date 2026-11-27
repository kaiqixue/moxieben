import json,unittest
from unittest.mock import patch
import test_app


class WritingWorkflowTests(unittest.TestCase):
    setUpClass=test_app.WebsiteTests.__dict__['setUpClass']
    setUp=test_app.WebsiteTests.setUp
    post=test_app.WebsiteTests.post
    submission=test_app.WebsiteTests.submission
    image=test_app.WebsiteTests.image

    def setup_writing(self):
        response=self.post('/api/tasks',{'name':'Writing','questions':[{'number':1,'kind':'essay',
            'answer':'','points':1,'context':'Describe an important experience.',
            'writing_config':{'profile':'custom'}}]})
        self.assertEqual(response.status_code,201,response.json)
        task=response.json;sid=self.submission(task);self.image(sid)
        qid=task['questions'][0]['id'];route=f'/api/submissions/{sid}/reviews/{qid}'
        dto=self.client.get(f'/api/submissions/{sid}/writing').json['questions'][0]
        saved=self.post(route+'/writing-confirm',{'action':'transcript','expected_revision':dto['revision'],
            'transcript':'I went home. It was a happy experience.','confirmed_blank':False})
        self.assertEqual(saved.status_code,200,saved.json)
        return sid,qid,route,saved.json['writing']['questions'][0]

    def result(self):
        return {'components':{'content':.3,'language':.3,'organization':.1},'errors':[],
                'coverage':[],'feedback':'Explain why the experience mattered.',
                'strengths':['Clear event.'],'next_steps':['Add reflection.']}

    def test_suggestion_stays_pending_is_reused_and_can_be_confirmed_then_undone(self):
        sid,qid,route,q=self.setup_writing()
        config={'base_url':'https://fake.invalid','model':'fake','api_key':'test-key'}
        with patch('settings.read_ai_review',return_value=config),patch('settings._chat',return_value=json.dumps(self.result())) as model:
            result=self.post(route+'/writing-grade',{'expected_revision':q['revision']})
            self.assertEqual(result.status_code,200,result.json)
            q=result.json['writing']['questions'][0]
            self.assertEqual(q['ai_result']['score'],.7)
            self.assertIsNone(result.json['submission']['score'])
            self.assertEqual(self.post(route+'/writing-grade',{'expected_revision':q['revision']}).status_code,200)
            self.assertEqual(model.call_count,1)
        saved=self.post(route+'/writing-confirm',{'action':'adopt','expected_revision':q['revision'],
            'event_id':q['ai_result']['event_id']})
        self.assertEqual(saved.status_code,200,saved.json)
        self.assertEqual(saved.json['submission']['score'],.7)
        undone=self.post(route+'/writing-undo',{'event_id':saved.json['undo']['event_id']})
        self.assertEqual(undone.status_code,200,undone.json)
        self.assertIsNone(undone.json['submission']['score'])

    def test_manual_grade_and_stale_ai_cannot_override_new_transcript(self):
        sid,qid,route,q=self.setup_writing()
        old=q['revision']
        saved=self.post(route+'/writing-confirm',{'action':'manual','expected_revision':old,'score':.8,'reason':'Teacher checked the full response.'})
        self.assertEqual(saved.status_code,200,saved.json)
        self.assertEqual(saved.json['submission']['score'],.8)
        edited=self.post(route+'/writing-confirm',{'action':'transcript','expected_revision':old,
            'transcript':'I wrote a different answer.','confirmed_blank':False})
        self.assertEqual(edited.status_code,409,edited.json)
        current=saved.json['writing']['questions'][0]['revision']
        edited=self.post(route+'/writing-confirm',{'action':'transcript','expected_revision':current,
            'transcript':'I wrote a different answer.','confirmed_blank':False})
        self.assertEqual(edited.status_code,200,edited.json)
        self.assertIsNone(edited.json['submission']['score'])
        self.assertEqual(self.post(route+'/writing-grade',{'expected_revision':old}).status_code,409)

    def test_strict_route_cannot_final_score_an_unconfirmed_essay(self):
        sid,qid,route,q=self.setup_writing()
        response=self.post(route,{'transcript':'I went home.','confirmed':True})
        self.assertEqual(response.status_code,409)
        self.assertIsNone(self.client.get(f'/api/submissions/{sid}').json['score'])

    def test_invalid_ai_evidence_stays_failed_and_does_not_repeat_without_retry(self):
        sid,qid,route,q=self.setup_writing()
        result=self.result();result['errors']=[dict(quote='invented answer',type='grammar',explanation='Bad',suggestion='Other')]
        with patch('settings.read_ai_review',return_value=dict(base_url='https://fake.invalid',model='fake',api_key='test-key')),patch('settings._chat',return_value=json.dumps(result)) as model:
            failed=self.post(route+'/writing-grade',{'expected_revision':q['revision']})
            self.assertEqual(failed.status_code,502,failed.json)
            self.assertEqual(failed.json['writing']['questions'][0]['ai_state'],'failed')
            self.assertIsNone(failed.json['submission']['score'])
            self.assertEqual(self.post(route+'/writing-grade',{'expected_revision':q['revision']}).status_code,409)
            self.assertEqual(model.call_count,1)

    def test_duplicate_confirmation_is_rejected_and_blank_requires_explicit_zero(self):
        sid,qid,route,q=self.setup_writing()
        body={'action':'manual','expected_revision':q['revision'],'score':.9,'reason':'Checked'}
        saved=self.post(route+'/writing-confirm',body)
        self.assertEqual(saved.status_code,200,saved.json)
        self.assertEqual(self.post(route+'/writing-confirm',body).status_code,409)
        self.assertEqual(self.post(route+'/writing-undo',{'event_id':saved.json['undo']['event_id']}).status_code,200)
        current=self.client.get(f'/api/submissions/{sid}/writing').json['questions'][0]
        blank=self.post(route+'/writing-confirm',{'action':'transcript','expected_revision':current['revision'],
            'transcript':'','confirmed_blank':True}).json['writing']['questions'][0]
        self.assertEqual(self.post(route+'/writing-confirm',{'action':'manual','expected_revision':blank['revision'],'score':1,'reason':'Blank'}).status_code,400)
        graded=self.post(route+'/writing-confirm',{'action':'manual','expected_revision':blank['revision'],'score':0,'reason':'Confirmed blank'})
        self.assertEqual(graded.status_code,200,graded.json)
        self.assertEqual(graded.json['submission']['score'],0)

    def test_regions_must_belong_to_current_answer_and_require_explicit_transcript_check(self):
        sid,qid,route,q=self.setup_writing()
        invalid=self.post(route+'/writing-confirm',{'action':'transcript','expected_revision':q['revision'],
            'transcript':'Checked text','regions':[{'page_id':99999,'box':[0,0,10,10],'block_ids':[]}]})
        self.assertEqual(invalid.status_code,404)
        page=self.client.get(f'/api/submissions/{sid}').json['pages'][0]
        saved=self.post(route+'/writing-confirm',{'action':'transcript','expected_revision':q['revision'],
            'transcript':'Checked text','regions':[{'page_id':page['id'],'box':[0,0,page['width'],page['height']],'block_ids':[]}]})
        self.assertEqual(saved.status_code,200,saved.json)
        self.assertEqual(len(saved.json['submission']['reviews'][0]['regions']),1)

    def test_summary_requires_reading_and_cloze_template_before_any_model_call(self):
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        sid,qid,route,q=self.setup_writing()
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute("UPDATE questions SET kind='summary',writing_config='{}' WHERE id=?",(qid,))
        with patch('settings._chat') as model:
            q=self.client.get(f'/api/submissions/{sid}/writing').json['questions'][0]
            blocked=self.post(route+'/writing-grade',{'expected_revision':q['revision']})
            self.assertEqual(blocked.status_code,409,blocked.json)
            self.assertIn('阅读材料',blocked.json['error'])
            with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
                conn.execute("UPDATE questions SET kind='summary_cloze',context='',writing_config=? WHERE id=?",
                             (json.dumps({'source_passage':'Original reading material.'}),qid))
            q=self.client.get(f'/api/submissions/{sid}/writing').json['questions'][0]
            blocked=self.post(route+'/writing-grade',{'expected_revision':q['revision']})
            self.assertEqual(blocked.status_code,409,blocked.json)
            self.assertIn('填空模板',blocked.json['error'])
            model.assert_not_called()


if __name__=='__main__':unittest.main()
