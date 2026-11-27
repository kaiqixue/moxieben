import io,json,unittest
from unittest.mock import patch
from docx import Document
import test_app


def document(lines):
    doc=Document()
    for line in lines:doc.add_paragraph(line)
    raw=io.BytesIO();doc.save(raw);return raw.getvalue()


class WordAssistanceTests(unittest.TestCase):
    setUpClass=test_app.WebsiteTests.__dict__['setUpClass']
    setUp=test_app.WebsiteTests.setUp

    def upload(self,raw,mode='auto'):
        return self.client.post('/api/tasks/import-word',data={'mode':mode,
            'file':(io.BytesIO(raw),'combined.docx')},headers=self.headers)

    def test_translation_original_works_from_main_import_without_model_or_writes(self):
        raw=document(['Section A','1. 她回家。','She ____ home.',
                      'Section B','1. 他工作。（work）','翻译：','半句','1. went',
                      '整句','1. He works.'])
        with patch('settings._chat') as model:response=self.upload(raw)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual([q['answer'] for q in response.json['questions']],['went','He works.'])
        self.assertEqual([q['paper_section'] for q in response.json['questions']],['A','B'])
        self.assertNotIn('He works.',response.json['passage'])
        self.assertEqual(self.client.get('/api/tasks').json['tasks'],[])
        model.assert_not_called()

    def test_ai_uses_literal_paragraphs_and_rejects_invalid_references(self):
        raw=document(['翻译练习','1. 她回家。（go）','参考译文','1. She went home.'])
        config=dict(base_url='https://fake.invalid',model='fake',api_key='test-key')
        item=dict(kind='sentence',section='B',number=1,prompt=[1],answer=[3])
        with patch('settings.read_ai_review',return_value=config),patch('settings._chat',return_value=json.dumps({'questions':[item]})) as model:
            response=self.upload(raw,'ai')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['questions'][0]['answer'],'She went home.')
        self.assertIn('她回家',response.json['questions'][0]['context'])
        self.assertIsNone(model.call_args.args[1])
        for bad in (dict(item,answer=[99]),dict(item,answer=[True]),dict(item,answer=[1]),dict(item,answer_text='invented')):
            with patch('settings.read_ai_review',return_value=config),patch('settings._chat',return_value=json.dumps({'questions':[bad]})):
                self.assertEqual(self.upload(raw,'ai').status_code,400)
        with patch('settings.read_ai_review',return_value=config),patch('settings._chat',side_effect=ValueError('test-key')):
            failed=self.upload(raw,'ai')
            self.assertNotIn('test-key',failed.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/tasks').json['tasks'],[])


if __name__=='__main__':unittest.main()
