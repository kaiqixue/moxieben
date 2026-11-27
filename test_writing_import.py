import io,json,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from docx import Document
from word_import import parse_task_word,parse_task_word_ai,parse_answer_word,migrate_word_fields


def word(lines):
    doc=Document()
    for line in lines:doc.add_paragraph(line)
    raw=io.BytesIO();doc.save(raw);return raw.getvalue()


class WritingImportTests(unittest.TestCase):
    def test_summary_cloze_preserves_source_template_versions_and_explicit_points(self):
        draft=parse_task_word(word(['英语练习','Section A（每题2分）','1. 他跑步。','He ____.',
            'III. Summary Writing（10分）','Directions: Use no more than 60 words.',
            'A source passage about food.','Summary填空','He ____ because ____.',
            '翻译：','半句','1. runs','Summary Version 1','He runs because food matters.',
            'Version 2 He exercises for health.']),'source.docx')
        a,b=draft['questions']
        self.assertEqual((a['points'],b['points']),(2,10))
        self.assertEqual(b['kind'],'summary_cloze')
        self.assertIn('He ____',b['context'])
        self.assertEqual(b['writing_config']['word_max'],60)
        self.assertIn('source passage',b['writing_config']['source_passage'])
        self.assertEqual(b['variants'],['He exercises for health.'])
        self.assertNotIn('He exercises',draft['passage'])
        self.assertEqual(draft['name'],'英语练习')
        self.assertIn('60 words',draft['instructions'])

    def test_essay_without_reference_defaults_to_one_and_retains_word_range(self):
        q=parse_task_word(word(['Guided Writing','Write an English composition in 120-150 words.',
                              '请给同学写一封信，说明你的建议。']),'essay.docx')['questions'][0]
        self.assertEqual((q['kind'],q['answer'],q['points']),('essay','',1))
        self.assertEqual((q['writing_config']['word_min'],q['writing_config']['word_max']),(120,150))

    def test_global_points_apply_without_inventing_exam_full_marks(self):
        d=parse_task_word(word(['英语测试','每题3分','Section A','1. 中文','Some ___.',
            '翻译：','半句','1. text']),'practice.docx')
        self.assertEqual(d['questions'][0]['points'],3)
        self.assertIn('每题3分',d['instructions'])
        with self.assertRaises(ValueError):
            parse_task_word(word(['Guided Writing（本题-2分）','Write about school.']),'bad.docx')

    def test_ai_writing_reference_is_optional_and_source_indices_are_literal(self):
        raw=word(['Summary Writing','Use no more than 60 words.','A source passage.','Reference one.','Reference two.'])
        item=dict(kind='summary',section='III',number=1,prompt=[0,1],source=[2],answer=[3],versions=[[4]])
        config=dict(base_url='https://fake.invalid',model='fake',api_key='test')
        with patch('settings._chat',return_value=json.dumps({'questions':[item]})):
            q=parse_task_word_ai(raw,'summary.docx',config)['questions'][0]
        self.assertEqual(q['variants'],['Reference two.'])
        self.assertEqual(q['writing_config']['source_passage'],'A source passage.')
        self.assertEqual(q['writing_config']['word_max'],60)
        self.assertEqual(q['points'],1)
        with patch('settings._chat',return_value=json.dumps({'questions':[dict(item,source=[3])]})):
            with self.assertRaises(ValueError):parse_task_word_ai(raw,'summary.docx',config)

    def test_real_sample_has_sixteen_questions(self):
        source=Path('C:/Users/cqhep/Desktop/翻译批改样本/翻译批改电子稿+答案.docx')
        if not source.is_file():self.skipTest('Local original not available')
        d=parse_task_word(source.read_bytes(),source.name)
        self.assertEqual(len(d['questions']),16)
        q=d['questions'][-1]
        self.assertEqual(q['kind'],'summary_cloze')
        self.assertEqual(q['writing_config']['word_max'],60)
        self.assertEqual(len(q['variants']),1)
        self.assertIn('Spurlock',q['writing_config']['source_passage'])
        self.assertTrue(all(q['points']==1 for q in d['questions']))
        group=parse_answer_word(source.read_bytes(),source.name)['groups'][-1]
        self.assertEqual(group['kind'],'summary_cloze')
        self.assertEqual(group['items'][0]['writing_config']['word_max'],60)

    def test_writing_config_migration_is_additive_and_backed_up(self):
        import sqlite3,tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'old.sqlite3'
            with closing(sqlite3.connect(p)) as c,c:
                c.execute('create table tasks(id integer primary key)')
                c.execute('create table questions(id integer primary key)')
                c.execute('insert into questions values(7)')
            migrate_word_fields(p)
            with closing(sqlite3.connect(p)) as c:self.assertEqual(c.execute('select id,writing_config from questions').fetchall(),[(7,'{}')])
            self.assertEqual(len(list(Path(tmp).glob('*.bak-word-*'))),1)


if __name__=='__main__':unittest.main()
