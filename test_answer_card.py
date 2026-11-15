"""Independent answer cards use isolated SQLite and fake OCR, never student cloud uploads."""
import io
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import test_app
import test_batch


class AnswerCardTests(unittest.TestCase):
    setUpClass = test_app.WebsiteTests.__dict__['setUpClass']
    setUp = test_app.WebsiteTests.setUp
    post = test_app.WebsiteTests.post
    submission = test_app.WebsiteTests.submission

    def task(self, count=3):
        answers=['when','with','What']
        return self.post('/api/tasks',{'name':'独立答题测试','passage':'ARTICLE MUST STAY OFF CARD',
            'questions':[{'number':n,'kind':'grammar','answer':answers[(n-1)%3], 'points':1,
                'context':f'Article ({n}) ____ a printed suffix which is not on the answer card.'}
                for n in range(1,count+1)]}).json

    def upload(self,sid,layout='separate'):
        image=io.BytesIO();Image.new('RGB',(900,1200),'white').save(image,'PNG')
        response=self.client.post(f'/api/submissions/{sid}/pages',data={
            'image':(io.BytesIO(image.getvalue()),'answer.png'),'answer_layout':layout},headers=self.headers)
        response.request.environ['wsgi.input'].close()
        return response

    def seed(self,pid,text):
        blocks=[{'id':i,'text':t,'box':[60,80+i*160,800,210+i*160],
            'approximate':True,'reading_order':i} for i,t in enumerate(text)]
        with closing(sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3')) as conn,conn:
            conn.execute("UPDATE pages SET status='done',blocks=?,needs_review=0 WHERE id=?",(json.dumps(blocks),pid))
        return blocks

    def test_print_card_has_numbered_spaces_and_repeated_identity_without_answers(self):
        task=self.task(12)
        response=self.client.get(f"/tasks/{task['id']}/answer-card")
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('独立答题卡',html)
        self.assertEqual(html.count('class="answer-card-page"'),2)
        self.assertEqual(html.count('data-card-question='),12)
        self.assertEqual(html.count('学号：'),2)
        for hidden in ('ARTICLE MUST STAY OFF CARD','when','with','What','printed suffix'):
            self.assertNotIn(hidden,html)
        self.assertIn('/tasks/%s/answer-card/download' % task['id'], html)
        downloaded=self.client.get('/tasks/%s/answer-card/download' % task['id'])
        self.assertEqual(downloaded.status_code,200)
        self.assertIn('attachment;', downloaded.headers.get('Content-Disposition',''))
        self.assertIn('application/pdf', downloaded.headers.get('Content-Type',''))

    def test_upload_persists_mode_and_rejects_unknown_layout(self):
        task=self.task();sid=self.submission(task)
        response=self.upload(sid)
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.json['answer_layout'],'separate')
        self.assertEqual(self.upload(sid,'unknown').status_code,400)
        restarted=self.factory({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test-token'}).test_client()
        pages=restarted.get(f'/api/submissions/{sid}').json['pages']
        self.assertEqual(len(pages),1)
        self.assertEqual(pages[0]['answer_layout'],'separate')

    def test_pdf_and_word_downloads_are_real_documents_without_answers(self):
        from docx import Document
        from pypdf import PdfReader
        task = self.task(12)
        base = f"/tasks/{task['id']}/answer-card"
        html = self.client.get(base).get_data(as_text=True)
        for kind in ('pdf', 'docx'):
            self.assertIn(f'{base}/download?format={kind}', html)
        with patch('settings.recognize_selected') as cloud:
            pdf = self.client.get(base + '/download?format=pdf')
            word = self.client.get(base + '/download?format=docx')
            cloud.assert_not_called()
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, 'application/pdf')
        self.assertTrue(pdf.data.startswith(b'%PDF-'))
        reader = PdfReader(io.BytesIO(pdf.data))
        self.assertEqual(len(reader.pages), 2)
        pdf_text = '\n'.join(p.extract_text() for p in reader.pages)
        self.assertEqual(pdf_text.count('姓名：'), 2)
        self.assertIn('12.', pdf_text)
        self.assertEqual(word.status_code, 200)
        self.assertEqual(word.mimetype, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        doc = Document(io.BytesIO(word.data))
        text = '\n'.join([p.text for p in doc.paragraphs] +
                         [cell.text for table in doc.tables for row in table.rows for cell in row.cells])
        for content in (pdf_text, text):
            self.assertIn('独立答题测试', content)
            self.assertIn('12.', content)
            for hidden in ('ARTICLE MUST STAY OFF CARD', 'when', 'What', 'printed suffix'):
                self.assertNotIn(hidden, content)
        self.assertIn('.pdf', pdf.headers['Content-Disposition'])
        self.assertIn('.docx', word.headers['Content-Disposition'])
        self.assertEqual(self.client.get(base + '/download?format=exe').status_code, 400)

    def test_numbered_card_for_context_grammar_is_graded_without_printed_article(self):
        task=self.task();sid=self.submission(task);page=self.upload(sid).json
        self.seed(page['id'],['姓名：测试同学 学号：001','1. when\n2、 with\n(3) what'])
        with patch('settings.recognize_selected') as cloud:
            response=self.post(f'/api/submissions/{sid}/auto-grade',{})
            cloud.assert_not_called()
        self.assertEqual(response.status_code,200,response.json)
        result=response.json
        self.assertEqual([q['transcript'] for q in result['reviews']],['when','with','what'])
        self.assertEqual(result['score'],2,'Case differences must still lose the point')
        self.assertTrue(result['is_final'])
        self.assertEqual(result['uncertain_count'],0)
        self.assertEqual(result['teacher_reviewed_count'],0)

    def test_inline_mode_does_not_misread_article_as_a_standalone_card(self):
        task=self.task(1);sid=self.submission(task);page=self.upload(sid,'inline').json
        self.seed(page['id'],['1. when'])
        self.assertEqual(self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions'],[])

    def test_multiline_sentence_keeps_punctuation_and_locates_all_blocks(self):
        task=self.post('/api/tasks',{'name':'Sentence card','questions':[
            {'number':1,'kind':'sentence','answer':'I went home. Then I read.','points':2},
            {'number':2,'kind':'word','answer':'colour','variants':['color'],'points':1}]}).json
        sid=self.submission(task);page=self.upload(sid).json
        self.seed(page['id'],['1. I went home.','Then I read.','2. color'])
        with patch('settings.recognize_selected'):
            result=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
        self.assertEqual(result['score'],3)
        self.assertEqual(result['reviews'][0]['transcript'],'I went home. Then I read.')
        self.assertEqual(len(result['reviews'][0]['regions']),2)

    def test_missing_and_duplicate_numbers_remain_uncertain_without_fake_blanks(self):
        task=self.task();sid=self.submission(task);page=self.upload(sid).json
        self.seed(page['id'],['1. when\n1. where\n2. _____'])
        result=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True}).json
        self.assertEqual(result['uncertain_count'],3)
        self.assertTrue(all(not q['confirmed_blank'] and not q['transcript'] for q in result['reviews']))

    def test_repeated_number_across_card_and_inline_pages_is_not_selected(self):
        task=self.task(1);sid=self.submission(task)
        self.seed(self.upload(sid).json['id'],['1. when'])
        self.seed(self.upload(sid,'inline').json['id'],['Article (1) where a printed suffix which is not on the answer card.'])
        self.assertEqual(self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions'],[])

    def test_card_draft_and_manual_region_use_the_same_numbered_extraction(self):
        task=self.task();sid=self.submission(task);page=self.upload(sid).json
        self.seed(page['id'],['1. when\n2. with\n3. what'])
        response=self.post(f'/api/submissions/{sid}/extract-answers',{})
        self.assertEqual(response.status_code,200,response.json)
        result=self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual([r['machine_text'] for r in result['reviews']],['when','with','what'])
        self.assertFalse(result['is_final'])
        self.assertTrue(all(not r['confirmed'] for r in result['reviews']))

    def test_missing_card_number_can_use_local_crop_and_saved_region_evidence(self):
        task=self.task(1);sid=self.submission(task);page=self.upload(sid).json
        self.seed(page['id'],['1. _____'])
        with patch('settings.recognize_selected',return_value={'blocks':[
            {'id':0,'text':'1. when','box':[10,10,200,50]}]}) as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{}).json
        self.assertEqual(cloud.call_count,1)
        self.assertTrue(result['is_final']);self.assertEqual(result['score'],1)
        self.assertEqual(result['uncertain_count'],0)
        self.assertEqual(result['reviews'][0]['auto_result']['source'],'region_supplementary_ocr')


class BatchAnswerCardTests(unittest.TestCase):
    setUp = test_batch.BatchTests.setUp
    post = test_batch.BatchTests.post
    klass = test_batch.BatchTests.klass
    task = test_batch.BatchTests.task
    setup_assignment = test_batch.BatchTests.setup_assignment

    def test_downloads_preserve_saved_identity_format_and_are_read_only(self):
        from docx import Document
        from pypdf import PdfReader
        aid, students = self.setup_assignment()
        saved = self.post(f'/api/assignments/{aid}/answer-card-format',
                          {'entry_mode': 'handwritten', 'id_length': 3, 'id_prefix': '0'})
        self.assertEqual(saved.status_code, 200, saved.json)
        code = saved.json['format']['format_code']
        database = Path(self.tmp.name) / 'dictation.sqlite3'
        with closing(sqlite3.connect(database)) as conn:
            before = list(conn.iterdump())
        for kind in ('pdf', 'docx'):
            response = self.client.get(f'/assignments/{aid}/answer-card/download?format={kind}')
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'assignment-{aid}.{kind}', response.headers['Content-Disposition'])
            if kind == 'pdf':
                text = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(response.data)).pages)
            else:
                doc = Document(io.BytesIO(response.data))
                text = '\n'.join([p.text for p in doc.paragraphs] +
                                 [cell.text for table in doc.tables for row in table.rows for cell in row.cells])
            for expected in ('五年级一班', '学号前缀：0', '学号后2位：', code):
                self.assertIn(expected, text)
            for hidden in ('receive', '张同学', '李同学'):
                self.assertNotIn(hidden, text)
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(self.client.get('/assignments/999999/answer-card/download?format=pdf').status_code, 404)

    def test_batch_link_keeps_card_mode_and_class_print_header(self):
        aid,students=self.setup_assignment()
        buf=io.BytesIO();Image.new('RGB',(900,1200),'white').save(buf,'PNG')
        response=self.client.post(f'/api/assignments/{aid}/photos',data={
            'image':(io.BytesIO(buf.getvalue()),'card.png'),'answer_layout':'separate'},headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code,201,response.json)
        photo=response.json
        self.assertEqual(photo['answer_layout'],'separate')
        with patch('batch_photos.recognize',return_value={'blocks':[
            {'id':0,'text':'学号：001 姓名：张同学','box':[0,0,800,80]},
            {'id':1,'text':'1. receive','box':[0,120,800,240]}]}):
            linked=self.post(f"/api/batch-photos/{photo['id']}/identify",{}).json
        sub=self.client.get(f"/api/submissions/{linked['submission_id']}").json
        self.assertEqual(sub['pages'][0]['answer_layout'],'separate')
        result=self.post(f"/api/submissions/{sub['id']}/auto-grade",{}).json
        self.assertTrue(result['is_final']);self.assertEqual(result['score'],2)
        html=self.client.get(f'/assignments/{aid}/answer-card').get_data(as_text=True)
        self.assertIn('五年级一班',html)
        self.assertNotIn('receive',html)


if __name__=='__main__':
    unittest.main()
