import unittest
import test_pending_scores as fixtures

class PaperMappingTests(unittest.TestCase):
    setUpClass=fixtures.PendingScoreTests.__dict__['setUpClass']
    setUp=fixtures.PendingScoreTests.setUp
    post=fixtures.PendingScoreTests.post
    create_task=fixtures.PendingScoreTests.create_task

    def test_mapping_is_saved_without_changing_reference_answers(self):
        task=self.create_task();q=task['questions'][0]
        response=self.post(f"/api/tasks/{task['id']}/paper-mapping",{'questions':[
            {'question_id':q['id'],'paper_section':'B','paper_number':'1','context':'她回家。'}]})
        self.assertEqual(response.status_code,200,response.json)
        saved=response.json['questions'][0]
        self.assertEqual(saved['paper_section'],'B')
        self.assertEqual(saved['answer'],q['answer'])
        self.assertEqual(saved['paper_number'],'1')
        other=self.create_task()
        denied=self.post(f"/api/tasks/{other['id']}/paper-mapping",{'questions':[
            {'question_id':q['id'],'paper_section':'B','paper_number':'1'}]})
        self.assertEqual(denied.status_code,404)

    def test_invalid_mapping_is_atomic(self):
        task=self.create_task();a,b=task['questions']
        route=f"/api/tasks/{task['id']}/paper-mapping"
        duplicate=[{'question_id':q['id'],'paper_section':'B','paper_number':'1'} for q in (a,b)]
        self.assertEqual(self.post(route,{'questions':duplicate}).status_code,400)
        saved=self.client.get(f"/api/tasks/{task['id']}").json
        self.assertTrue(all(not q['paper_section'] for q in saved['questions']))
        self.assertEqual(self.post('/api/tasks',{'name':'Invalid','questions':[
            {'number':1,'kind':'word','answer':'book','points':1,'paper_section':'A'}]}).status_code,400)

if __name__=='__main__':unittest.main()
