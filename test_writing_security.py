import unittest
from unittest.mock import patch
import test_tenant_isolation as fixtures


class WritingSecurityTests(unittest.TestCase):
    setUp=fixtures.TenantIsolationTests.setUp
    token=fixtures.TenantIsolationTests.token
    register=fixtures.TenantIsolationTests.register
    post=fixtures.TenantIsolationTests.post

    def test_foreign_teacher_cannot_read_score_confirm_or_undo(self):
        task=self.post(self.a,'/api/tasks',{'name':'Private writing','questions':[
            {'number':1,'kind':'summary','answer':'A reference.','points':1,'context':'Summarize.'}]}).json
        qid=task['questions'][0]['id'];sid=self.post(self.a,f"/api/tasks/{task['id']}/submissions",{'student_code':'A'}).json['id']
        route=f'/api/submissions/{sid}/reviews/{qid}'
        with patch('settings._chat') as model:
            self.assertEqual(self.b.get(f'/api/submissions/{sid}/writing').status_code,404)
            for suffix,payload in [('writing-grade',{'expected_revision':'bad'}),
                    ('writing-confirm',{'action':'manual','score':1,'reason':'Other','expected_revision':'bad'}),
                    ('writing-undo',{'event_id':1})]:
                self.assertEqual(self.post(self.b,route+'/'+suffix,payload).status_code,404)
            model.assert_not_called()


if __name__=='__main__':unittest.main()
