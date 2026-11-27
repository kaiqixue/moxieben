import unittest
import test_auto_grade
from unittest.mock import patch

class PendingScoreTests(unittest.TestCase):
    setUpClass=test_auto_grade.AutoGradeTests.__dict__['setUpClass']
    setUp=test_auto_grade.AutoGradeTests.setUp
    post=test_auto_grade.AutoGradeTests.post
    create_task=test_auto_grade.AutoGradeTests.create_task
    submission=test_auto_grade.AutoGradeTests.submission
    image=test_auto_grade.AutoGradeTests.image

    def test_missing_ocr_does_not_become_a_final_zero(self):
        task=self.create_task();sid=self.submission(task);self.image(sid)
        with patch('settings.recognize_selected',return_value={'blocks':[]}):
            result=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
        self.assertIsNone(result['score'])
        self.assertFalse(result['is_final'])
        self.assertEqual(result['wrong_count'],0)
        self.assertEqual(result['machine_graded_count'],0)
        self.assertTrue(all(q['score'] is None for q in result['reviews']))

    def test_new_paper_mapping_invalidates_old_number_match_but_keeps_teacher_confirmation(self):
        task=self.create_task();sid=self.submission(task);self.image(sid)
        with patch('settings.recognize_selected',return_value={'blocks':[
                {'id':0,'text':'1. word1','box':[0,0,100,20],'confidence':.99}]}):
            result=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
        q=task['questions'][0]
        self.assertTrue(result['reviews'][0]['graded'])
        self.post(f"/api/tasks/{task['id']}/paper-mapping",{'questions':[
            {'question_id':q['id'],'paper_section':'B','paper_number':'1'}]})
        result=self.client.get(f'/api/submissions/{sid}').json
        self.assertIsNone(result['reviews'][0]['score'])
        confirmed=self.post(f"/api/submissions/{sid}/reviews/{q['id']}",{'quick_decision':'correct'}).json
        self.assertEqual(confirmed['reviews'][0]['score'],q['points'])
        self.assertTrue(confirmed['reviews'][0]['confirmed'])

if __name__=='__main__':unittest.main()
