import unittest
from answer_extraction import worksheet_candidates

class WorksheetExtractionTests(unittest.TestCase):
    def page(self,*texts):
        return {'id':1,'status':'done','blocks':[{'id':i,'text':t,'box':[0,i*40,500,i*40+35]} for i,t in enumerate(texts)]}

    def test_section_numbers_and_literal_answers_do_not_use_reference(self):
        qs=[{'question_id':1,'number':1,'kind':'phrase','paper_section':'A','paper_number':'1','answer':'SECRET'},
            {'question_id':11,'number':11,'kind':'sentence','paper_section':'B','paper_number':'1','answer':'SECRET'}]
        page=self.page('Section A','1. 中文题干',r'This is $\underline{\text{no special}}$.',
                       'Section B','1. 她回家。','She go home.','III. Summary Writing','Printed article')
        found=worksheet_candidates(qs,[page])
        self.assertEqual([x['transcript'] for x in found],['no special','She go home.'])
        self.assertNotIn('SECRET',str(found))

    def test_chinese_prompt_is_never_an_english_answer(self):
        qs=[{'question_id':1,'number':1,'kind':'phrase','paper_section':'A','paper_number':'1'}]
        self.assertEqual(worksheet_candidates(qs,[self.page('Section A','1. 中文题干')]),[])

    def test_duplicate_markers_and_plain_printed_phrase_are_rejected(self):
        q={'question_id':1,'number':1,'kind':'phrase','paper_section':'A','paper_number':'1'}
        self.assertEqual(worksheet_candidates([q],[self.page('Section A','1. 中文','Printed English sentence')]),[])
        self.assertEqual(worksheet_candidates([q],[self.page('Section A','1. 中文',r'\underline{\text{one}}','1. 中文',r'\underline{\text{two}}')]),[])

    def test_first_unnumbered_answer_and_context_anchor_remain_uncertain(self):
        qs=[{'question_id':11,'number':11,'kind':'sentence','paper_section':'B','paper_number':'1'},
            {'question_id':13,'number':13,'kind':'sentence','paper_section':'B','paper_number':'3','context':'她签合同。'}]
        page=self.page('Section B','She go home.','III. Summary Writing','Article',
                       '3. 她签合同。','She signed it.')
        found=worksheet_candidates(qs,[page])
        self.assertEqual([x['transcript'] for x in found],['She go home.','She signed it.'])
        self.assertTrue(all(x['uncertain'] for x in found))

    def test_original_fullwidth_hint_can_anchor_ocr_after_reordered_summary(self):
        q={'question_id':13,'number':13,'kind':'sentence','paper_section':'B','paper_number':'3',
           'context':'她签订合同。（spot）'}
        page=self.page('III. Summary Writing','3.她签订合同。(spot)','She signed it.')
        self.assertEqual([x['transcript'] for x in worksheet_candidates([q],[page])],['She signed it.'])

if __name__=='__main__': unittest.main()
