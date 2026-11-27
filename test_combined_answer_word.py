import io
import unittest
from docx import Document
from word_import import parse_answer_word


class CombinedAnswerWordTests(unittest.TestCase):
    def test_combined_paper_only_imports_explicit_answer_section(self):
        doc=Document()
        for line in ['Section A','1. 中文题干','Printed ____ context.', 'Section B',
                     '1. 她工作。', 'III. Summary Writing', 'Printed passage',
                     '翻译：','半句','1. nothing unusual','整句','1. She works here.',
                     'Summary Version 1','1. First summary.','Version 2 Second summary.']:
            doc.add_paragraph(line)
        raw=io.BytesIO();doc.save(raw)
        draft=parse_answer_word(raw.getvalue(),'paper.docx')
        self.assertEqual([g['kind'] for g in draft['groups']],['phrase','sentence','summary'])
        self.assertEqual(draft['groups'][0]['items'],[{'number':1,'answer':'nothing unusual'}])
        self.assertEqual(len(draft['groups'][2]['items'][0]['versions']),2)
        self.assertNotIn('Printed',str([i.get('answer',i.get('versions')) for g in draft['groups'] for i in g['items']]))
        self.assertIn('Printed passage',draft['groups'][2]['items'][0]['writing_config']['source_passage'])


if __name__=='__main__':unittest.main()
