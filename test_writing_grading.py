import unittest
from writing_grading import score_response,word_count,config,limits


class WritingGradingTests(unittest.TestCase):
    def result(self):
        return dict(components={'content':3,'language':4},errors=[],coverage=[],feedback='Keep the key result.',strengths=[],next_steps=[])

    def test_literal_evidence_and_caps_are_enforced(self):
        q={'kind':'summary','points':10};r=self.result()
        r['errors']=[dict(quote='went home',type='content',explanation='Example explanation',suggestion='Example correction')]
        result=score_response(r,q,'She went home.')
        self.assertEqual(result['score'],7)
        self.assertEqual((result['errors'][0]['start'],result['errors'][0]['end']),(4,13))
        r['errors'][0]['quote']='imagined words'
        with self.assertRaises(ValueError):score_response(r,q,'She went home.')
        r=self.result();r['components']['content']=6
        with self.assertRaises(ValueError):score_response(r,q,'She went home.')
        r=self.result();r['coverage']=[dict(requirement='An experiment',verdict='met',evidence='invented')]
        with self.assertRaises(ValueError):score_response(r,q,'She went home.')

    def test_empty_error_list_does_not_invent_faults_and_total_is_computed(self):
        result=score_response(self.result(),{'kind':'summary_cloze','points':10},'I am well.')
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['score'],7)
        self.assertEqual(sum(limits({'kind':'essay','points':1.01}).values()),101)
        self.assertEqual(word_count("It's a well-known story about 14 students."),7)

    def test_config_does_not_guess_limits_or_exam_maximum(self):
        result=config({},'essay')
        self.assertIsNone(result['word_min']);self.assertIsNone(result['word_max'])
        self.assertEqual(result['profile'],'custom')
        for wrong in ({'word_max':True},{'word_min':150,'word_max':120},{'profile':'shanghai-summary'}):
            with self.assertRaises(ValueError):config(wrong,'essay')


if __name__=='__main__':unittest.main()
