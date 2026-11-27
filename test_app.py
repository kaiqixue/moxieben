r"""Run: .venv\Scripts\python.exe -m unittest -v test_app"""
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class GradingTests(unittest.TestCase):
    def test_strict_scoring_and_readable_differences(self):
        self.assertIsNotNone(importlib.util.find_spec('grading'), 'Missing strict grading implementation')
        from grading import compare
        self.assertTrue(compare('  go\n home ', 'go home', [])['matched'])
        for actual in ['Go home', 'go home.', 'gohome', 'go', 'home go', 'go to home']:
            self.assertFalse(compare(actual, 'go home', [])['matched'], actual)
        self.assertTrue(compare('colour', 'color', ['colour'])['matched'])
        result = compare('I go school.', 'I went to school.', [])
        self.assertTrue(any(d['op'] != 'equal' for d in result['diff']))
        self.assertFalse(compare('', 'answer', [])['matched'])


class WebsiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec('app') is None:
            raise AssertionError('Missing website implementation')
        from app import create_app
        cls.factory = staticmethod(create_app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.factory({'TESTING': True, 'DATA_DIR': self.tmp.name, 'CSRF_TOKEN': 'test-token'})
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': 'test-token'}

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers=self.headers)

    def test_assignment_management_has_its_own_page(self):
        response = self.client.get('/assignments?archived=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/static/workbench.js', response.get_data(as_text=True))

    def test_word_preview_extracts_grammar_blanks_without_writing(self):
        source = Path(__file__).parent / 'tests/fixtures/grammar_sample.docx'
        if not source.exists():
            self.skipTest('真实 Word 样本不在当前环境')
        response = self.client.post('/api/tasks/import-word', data={
            'file': (io.BytesIO(source.read_bytes()), source.name)}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.json
        self.assertEqual(len(data['questions']), 10)
        self.assertEqual([q['answer'] for q in data['questions']], [
            'when', 'with', 'is favored', 'why', 'have adopted', 'to demand',
            'making', 'until', 'incorporated', 'What'])
        self.assertEqual(data['questions'][2]['hint'], 'favor')
        self.assertIn('(3)', data['questions'][2]['context'])
        self.assertEqual(self.client.get('/api/tasks').json['tasks'], [])

    def test_answer_word_preview_keeps_sections_and_versions_without_writes(self):
        from docx import Document

        def preview(lines):
            doc = Document()
            for line in lines:
                doc.add_paragraph(line)
            stream = io.BytesIO()
            doc.save(stream)
            stream.seek(0)
            return self.client.post('/api/tasks/import-word',
                data={'mode': 'answers', 'file': (stream, '翻译答案.docx')},
                headers=self.headers)

        response = preview(['翻译：', '半句', '1. nothing unusual', '2．a new word',
            '整句翻译：', '1. She works here / there.', 'She started last year.',
            'Summary writing', 'Version 1', '1. First point', '2. Second point',
            'Version 2 Another complete version.'])
        self.assertEqual(response.status_code, 200, response.json)
        half, full, summary = response.json['groups']
        self.assertEqual((half['kind'], full['kind'], summary['kind']), ('phrase', 'sentence', 'summary'))
        self.assertEqual([q['number'] for q in half['items']], [1, 2])
        self.assertEqual(full['items'], [{'number': 1, 'answer': 'She works here / there.\nShe started last year.'}])
        self.assertEqual(summary['items'], [{'number': 1, 'versions': [
            {'label': 'Version 1', 'answer': '1. First point\n2. Second point'},
            {'label': 'Version 2', 'answer': 'Another complete version.'}]}])
        response = preview(['1. First', '1. Another answer'])
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json['groups'][0]['items']), 2)
        self.assertTrue(any('重复' in text for text in response.json['warnings']))
        self.assertEqual(preview(['只有一行说明']).status_code, 400)
        self.assertEqual(self.client.get('/api/tasks').json['tasks'], [])

    def test_word_draft_validation_context_persistence_and_print(self):
        from docx import Document
        from word_import import parse_word, WORD_LIMIT
        def source(lines):
            doc = Document()
            for line in lines:
                doc.add_paragraph(line)
            stream = io.BytesIO()
            doc.save(stream)
            return stream.getvalue()
        raw = source(['每题一分，区分大小写', 'Grammar',
            '(3) ______ (favor) over a group. (10) ______ is important.',
            '答案：', '3. is favored  10. What'])
        draft = parse_word(raw, '语法.docx')
        self.assertEqual([q['number'] for q in draft['questions']], [3, 10])
        self.assertEqual(draft['questions'][0]['hint'], 'favor')
        self.assertNotIn('is favored', draft['passage'])
        created = self.post('/api/tasks', draft)
        self.assertEqual(created.status_code, 201, created.json)
        tid = created.json['id']
        sid = self.submission(created.json)
        restarted = self.factory({'TESTING':True,'DATA_DIR':self.tmp.name}).test_client()
        stored = restarted.get(f'/api/tasks/{tid}').json
        self.assertEqual(stored['questions'][0]['context'], draft['questions'][0]['context'])
        review = restarted.get(f'/api/submissions/{sid}').json['reviews'][0]
        self.assertEqual(review['hint'], 'favor')
        sheet = restarted.get(f'/tasks/{tid}/sheet').get_data(as_text=True)
        self.assertNotIn('is favored', sheet)
        self.assertIn('favor', sheet)
        report = restarted.get(f'/submissions/{sid}/report').get_data(as_text=True)
        self.assertIn('题目上下文', report)
        self.assertIn('提示词', report)
        for lines in [['(1) ____', '1. a'], ['(1) ____ (1) ____', '答案', '1. a'],
                      ['(1) ____', '答案', '1. a  1. b']]:
            with self.assertRaises(ValueError):
                parse_word(source(lines), 'bad.docx')
        missing = parse_word(source(['(1) ____', '答案']), 'missing.docx')
        self.assertEqual(missing['questions'][0]['answer'], '')
        self.assertTrue(any('缺少' in w for w in missing['warnings']))
        self.assertEqual(self.post('/api/tasks', missing).status_code, 400)
        for raw in [b'not-docx', b'x'*(WORD_LIMIT+1)]:
            with self.assertRaises(ValueError):
                parse_word(raw, 'bad.docx')

    def create_task(self):
        res = self.post('/api/tasks', {'name': 'Unit 1', 'questions': [
            {'number': 1, 'kind': 'word', 'answer': 'receive', 'variants': [], 'points': 2},
            {'number': 2, 'kind': 'sentence', 'answer': 'I went home.', 'variants': ['I returned home.'], 'points': 3}]})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()

    def submission(self, task):
        res = self.post(f"/api/tasks/{task['id']}/submissions", {'student_code': 'S001'})
        self.assertEqual(res.status_code, 201)
        return res.get_json()['id']

    def image(self, sid, filename='photo.png', content=None):
        from PIL import Image
        if content is None:
            stream = io.BytesIO()
            Image.new('RGB', (640, 240), 'white').save(stream, 'PNG')
            content = stream.getvalue()
        response = self.client.post(f'/api/submissions/{sid}/pages',
            data={'image': (io.BytesIO(content), filename)}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        return response

    def test_persistence_review_and_immutable_history(self):
        task = self.create_task()
        sid = self.submission(task)
        qid = task['questions'][0]['id']
        data = {'transcript': 'receive', 'regions': [], 'confirmed': True,
                'confirmed_blank': False, 'score_override': None, 'reason': ''}
        res = self.post(f'/api/submissions/{sid}/reviews/{qid}', data)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json['score'])
        self.assertEqual(res.json['known_score'], 2)
        self.assertEqual(res.json['status'], 'pending')
        data['transcript'] = 'recieve'
        data['score_override'] = 1
        self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{qid}', data).status_code, 400)
        data['reason'] = '教师确认部分给分'
        res = self.post(f'/api/submissions/{sid}/reviews/{qid}', data)
        self.assertIsNone(res.json['score'])
        self.assertEqual(res.json['known_score'], 0)
        self.assertEqual(res.json['history_count'], 2)
        other = self.factory({'TESTING': True, 'DATA_DIR': self.tmp.name}).test_client()
        self.assertIsNone(other.get(f'/api/submissions/{sid}').json['score'])
        self.assertEqual(other.get(f'/api/submissions/{sid}').json['known_score'], 0)
        csv = other.get(f"/tasks/{task['id']}/grades.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn('S001', csv.get_data(as_text=True))
        for path in [f"/tasks/{task['id']}/sheet", f'/submissions/{sid}/report']:
            self.assertEqual(other.get(path).status_code, 200)

    def test_blank_requires_explicit_confirmation(self):
        task = self.create_task()
        sid = self.submission(task)
        path = f"/api/submissions/{sid}/reviews/{task['questions'][0]['id']}"
        payload = {'transcript': '', 'regions': [], 'confirmed': True, 'confirmed_blank': False}
        self.assertEqual(self.post(path, payload).status_code, 400)
        payload['confirmed_blank'] = True
        self.assertEqual(self.post(path, payload).status_code, 200)
        payload['transcript'] = 'receive'
        self.assertEqual(self.post(path, payload).status_code, 400)

    def test_teacher_can_quick_decide_without_transcript(self):
        task = self.create_task()
        sid = self.submission(task)
        qid = task['questions'][0]['id']
        self.image(sid)
        import sqlite3
        conn = sqlite3.connect(Path(self.tmp.name) / 'dictation.sqlite3')
        conn.execute('UPDATE reviews SET auto_result=?, transcript=? WHERE submission_id=? AND question_id=?',
                     ('{"uncertain":true,"reason":"机器未取得有效作答"}', '', sid, qid))
        conn.execute("UPDATE submissions SET grading_state='final' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        path = f'/api/submissions/{sid}/reviews/{qid}'
        correct = self.post(path, {'quick_decision': 'correct'})
        self.assertEqual(correct.status_code, 200, correct.get_data(as_text=True))
        review = correct.json['reviews'][0]
        self.assertTrue(review['confirmed'])
        self.assertEqual(review['score'], task['questions'][0]['points'])
        self.assertEqual(review['teacher_decision'], 'correct')
        self.assertEqual(review['transcript'], '')
        self.assertEqual(correct.json['uncertain_count'], 0)
        self.assertFalse(review['confirmed_blank'])
        self.assertEqual(review['machine_text'], '')
        self.assertIn('教师判定正确', review['reason'])
        report = self.client.get(f'/submissions/{sid}/report').get_data(as_text=True)
        self.assertIn('教师判定正确', report)
        self.assertNotIn('教师确认未作答', report)
        wrong = self.post(path, {'quick_decision': 'incorrect'})
        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(wrong.json['reviews'][0]['score'], 0)
        self.assertFalse(wrong.json['reviews'][0]['confirmed_blank'])
        blank = self.post(path, {'quick_decision': 'blank'})
        self.assertEqual(blank.status_code, 200)
        self.assertTrue(blank.json['reviews'][0]['confirmed_blank'])
        restarted = self.factory({'TESTING':True,'DATA_DIR':self.tmp.name}).test_client()
        self.assertEqual(restarted.get(f'/api/submissions/{sid}').json['reviews'][0]['teacher_decision'], 'blank')
        self.assertEqual(blank.json['history_count'], 3)
        self.assertEqual(self.post(path, {'quick_decision':'invalid'}).status_code, 400)
        self.assertEqual(self.post(path, {'quick_decision':True}).status_code, 400)
        self.assertEqual(self.post(path, {'quick_decision':'correct','transcript':'receive'}).status_code, 400)
        with patch('settings.recognize_selected') as cloud:
            self.post(path, {'quick_decision':'correct'})
            cloud.assert_not_called()
        conn = sqlite3.connect(Path(self.tmp.name) / 'dictation.sqlite3')
        conn.execute("UPDATE submissions SET grading_state='running' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        self.assertEqual(self.post(path, {'quick_decision':'correct'}).status_code, 409)

    def test_quick_decision_preserves_evidence_on_rerun_and_can_be_edited(self):
        import json
        import sqlite3
        task=self.create_task();sid=self.submission(task);page=self.image(sid).json
        qid=task['questions'][0]['id'];path=f'/api/submissions/{sid}/reviews/{qid}'
        self.assertEqual(self.post(path, {'quick_decision':'correct'}).status_code,200)
        with patch('app.recognize',return_value={'blocks':[{'id':0,'text':'1. recieve','box':[0,0,100,40]}]}):
            result=self.post(f"/api/pages/{page['id']}/ocr",{})
        self.assertEqual(result.status_code,200)
        q=result.json['reviews'][0]
        self.assertTrue(q['confirmed']);self.assertEqual(q['score'],2)
        self.assertEqual(q['teacher_decision'],'correct');self.assertEqual(q['transcript'],'')
        with patch('settings.recognize_selected') as cloud:
            result=self.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True})
            cloud.assert_not_called()
        self.assertEqual(result.status_code,200)
        q=result.json['reviews'][0]
        self.assertEqual(q['teacher_decision'],'correct');self.assertEqual(q['score'],2)
        self.assertEqual(q['transcript'],'');self.assertTrue(q['confirmed'])
        # A direct error verdict is independent of the saved transcription.
        changed=self.post(path,{'transcript':'receive','confirmed':True})
        self.assertEqual(changed.status_code,200)
        self.assertEqual(changed.json['reviews'][0]['teacher_decision'],'')
        wrong=self.post(path,{'quick_decision':'incorrect'}).json['reviews'][0]
        self.assertEqual(wrong['transcript'],'receive');self.assertEqual(wrong['score'],0)
        self.assertFalse(wrong['confirmed_blank'])
        history=self.client.get(f'/api/submissions/{sid}/history').json['events'][-1]['snapshot']
        self.assertEqual(history['action'],'teacher_quick_decision')
        self.assertEqual(history['before']['transcript'],'receive')
        self.assertEqual(history['after']['teacher_decision'],'incorrect')
        empty=self.submission(task)
        self.assertEqual(self.post(f'/api/submissions/{empty}/reviews/{qid}',{'quick_decision':'correct'}).status_code,400)

    def test_upload_validation_ocr_failure_and_original_evidence(self):
        task = self.create_task()
        sid = self.submission(task)
        self.assertEqual(self.image(sid, 'x.jpg', b'not an image').status_code, 400)
        self.assertEqual(self.image(sid, 'x.gif').status_code, 400)
        self.assertEqual(self.image(sid, 'x.png', b'x' * (10 * 1024 * 1024 + 1)).status_code, 413)
        page = self.image(sid, '../../photo.png').get_json()
        self.assertEqual(page['width'], 640)
        with self.client.get(page['url']) as image_response:
            self.assertEqual(image_response.status_code, 200)
        with patch('app.recognize', side_effect=RuntimeError('Model download failed')), self.assertLogs(self.app.logger, level='ERROR'):
            res = self.post(f"/api/pages/{page['id']}/ocr", {})
            self.assertEqual(res.status_code, 503)
        stored = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(stored['pages'][0]['status'], 'error')
        self.assertIn('识别失败', stored['pages'][0]['error'])
        self.assertNotIn('Model download failed', stored['pages'][0]['error'])
        blocks = [
            {'id': 0, 'text': '1. recieve', 'confidence': 0.95, 'box': [10, 10, 160, 40]},
            {'id': 1, 'text': '2. I went', 'confidence': 0.9, 'box': [10, 60, 200, 90]},
            {'id': 2, 'text': 'home.', 'confidence': 0.88, 'box': [10, 100, 160, 130]}]
        with patch('app.recognize', return_value={'blocks': blocks, 'elapsed_seconds': 1.0, 'model': 'test-only'}):
            res = self.post(f"/api/pages/{page['id']}/ocr", {})
            self.assertEqual(res.status_code, 200)
        suggestions = self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions']
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]['transcript'], 'recieve')
        self.assertIn('home.', suggestions[1]['transcript'])
        region = [{'page_id': page['id'], 'box': [10, 10, 160, 40], 'block_ids': [0]}]
        qid = task['questions'][0]['id']
        payload = {'transcript': 'receive', 'regions': region, 'confirmed': True}
        res = self.post(f'/api/submissions/{sid}/reviews/{qid}', payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json['reviews'][0]['machine_text'], 'recieve')
        self.assertEqual(res.json['reviews'][0]['transcript'], 'receive')
        self.assertEqual(res.json['pages'][0]['blocks'][0]['text'], '1. recieve')
        sentence_region = [{'page_id': page['id'], 'box': [10, 60, 200, 130], 'block_ids': [1, 2]}]
        res = self.post(f"/api/submissions/{sid}/reviews/{task['questions'][1]['id']}",
            {'transcript': 'I went\nhome.', 'regions': sentence_region, 'confirmed': True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json['reviews'][1]['machine_text'], 'I went\nhome.')
        self.assertEqual(res.json['status'], 'confirmed')
        # A rerun gets new block ids; old visual regions must be cleared rather than reused.
        with patch('app.recognize', return_value={'blocks': blocks[:1], 'elapsed_seconds': 1.0, 'model': 'test-only'}):
            self.assertEqual(self.post(f"/api/pages/{page['id']}/ocr", {}).status_code, 200)
        rerun = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(rerun['reviews'][0]['regions'], [])
        self.assertEqual(rerun['reviews'][0]['transcript'], 'receive')
        self.assertFalse(rerun['reviews'][0]['confirmed'])
        for q in task['questions']:
            self.post(f"/api/submissions/{sid}/reviews/{q['id']}", {'transcript': q['answer'], 'regions': [], 'confirmed': True})
        self.assertEqual(self.client.get(f'/api/submissions/{sid}').json['status'], 'confirmed')
        with patch('app.recognize', side_effect=RuntimeError('Retry failed')), self.assertLogs(self.app.logger, level='ERROR'):
            self.assertEqual(self.post(f"/api/pages/{page['id']}/ocr", {}).status_code, 503)
        failed = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(failed['status'], 'pending')
        self.assertTrue(failed['pages'][0]['needs_review'])

    def test_security_and_invalid_inputs(self):
        self.assertEqual(self.client.post('/api/tasks', json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/tasks', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/tasks', json={}, headers={
            **self.headers, 'Origin': 'https://evil.example'}).status_code, 403)
        for points in [-1, float('nan'), float('inf'), 1.234, 10**1000]:
            res = self.post('/api/tasks', {'name': 'bad', 'questions': [
                {'number': 1, 'kind': 'word', 'answer': 'a', 'points': points}]})
            self.assertEqual(res.status_code, 400, points)
        self.assertEqual(self.client.get('/api/tasks/9999').status_code, 404)
        task = self.create_task()
        sid = self.submission(task)
        res = self.post(f"/api/submissions/{sid}/reviews/{task['questions'][0]['id']}",
            {'transcript': 'receive', 'regions': [{'page_id': 999, 'box': [0, 0, 10, 10], 'block_ids': []}]})
        self.assertEqual(res.status_code, 400)

        page = self.image(sid, 'valid.png').get_json()
        res = self.post(f"/api/submissions/{sid}/reviews/{task['questions'][0]['id']}",
            {'transcript': 'receive', 'regions': [{'page_id': page['id'], 'box': [0, 0, 10**1000, 10], 'block_ids': []}]})
        self.assertEqual(res.status_code, 400)

    def test_parenthesized_answers_are_suggested_but_inline_passages_are_not(self):
        task = self.create_task()
        sid = self.submission(task)
        page = self.image(sid).json
        blocks = [
            {'id': 0, 'text': '(1) recieve', 'box': [10, 10, 300, 40], 'confidence': None},
            {'id': 1, 'text': '（2） I went home.', 'box': [10, 70, 300, 100], 'confidence': None}]
        with patch('app.recognize', return_value={'blocks': blocks}):
            self.post(f"/api/pages/{page['id']}/ocr", {})
        suggestions = self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions']
        self.assertEqual([s['transcript'] for s in suggestions], ['recieve', 'I went home.'])
        for suggestion in suggestions:
            saved = self.post(f"/api/submissions/{sid}/reviews/{suggestion['question_id']}",
                              dict(suggestion, confirmed=False))
            self.assertEqual(saved.status_code, 200, saved.json)
        stored = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(stored['confirmed_count'], 0)
        self.assertEqual(stored['reviews'][0]['machine_text'], 'recieve')
        self.assertEqual(self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions'], [])

        grammar = self.post('/api/tasks', {'name': 'Grammar', 'questions': [
            {'number': 1, 'kind': 'grammar', 'answer': 'when', 'points': 1}]}).json
        sid = self.submission(grammar)
        page = self.image(sid).json
        for content in ['(1) that they not only face school pressures but also deal with development.',
                        '1. that they face (2) many pressures.', 'It is the time (1) that they face pressures.']:
            with patch('app.recognize', return_value={'blocks': [dict(blocks[0], text=content)]}):
                self.post(f"/api/pages/{page['id']}/ocr", {})
            self.assertEqual(self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions'], [], content)

    def test_new_photo_and_changed_evidence_require_review(self):
        task = self.create_task()
        sid = self.submission(task)
        for q in task['questions']:
            self.assertEqual(self.post(f"/api/submissions/{sid}/reviews/{q['id']}",
                {'transcript': q['answer'], 'regions': [], 'confirmed': True}).status_code, 200)
        page = self.image(sid).json
        detail = self.client.get(f'/api/submissions/{sid}').json
        self.assertEqual(detail['status'], 'pending')
        self.assertTrue(detail['pages'][0]['needs_review'])
        self.assertEqual(self.post(f"/api/pages/{page['id']}/reviewed", {}).status_code, 400)
        detail = self.post(f"/api/pages/{page['id']}/reviewed", {'confirmed': True}).json
        self.assertEqual(detail['status'], 'confirmed')
        self.assertFalse(detail['pages'][0]['needs_review'])
        q = task['questions'][0]
        path = f"/api/submissions/{sid}/reviews/{q['id']}"
        detail = self.post(path, {'transcript': 'changed', 'regions': [], 'confirmed': True}).json
        self.assertFalse(detail['reviews'][0]['confirmed'])
        self.assertEqual(detail['status'], 'pending')
        detail = self.post(path, {'transcript': 'changed', 'regions': [], 'confirmed': True}).json
        self.assertTrue(detail['reviews'][0]['confirmed'])

    def test_suggestions_preserve_saved_blank_score_and_reason_drafts(self):
        task = self.create_task()
        sid = self.submission(task)
        page = self.image(sid).json
        qid = task['questions'][0]['id']
        blocks = [{'id': 0, 'text': '1. recieve', 'box': [10, 10, 300, 40], 'confidence': None}]
        for draft in [{'confirmed_blank': True},
                      {'score_override': 1, 'reason': '教师已核对原图，给部分分'},
                      {'reason': '暂缓判断，已记录教师意见'}]:
            payload = dict(transcript='', regions=[], confirmed=False, confirmed_blank=False,
                           score_override=None, reason='') | draft
            self.assertEqual(self.post(f'/api/submissions/{sid}/reviews/{qid}', payload).status_code, 200)
            with patch('app.recognize', return_value={'blocks': blocks}):
                self.post(f"/api/pages/{page['id']}/ocr", {})
            self.assertEqual(self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions'], [], draft)
            saved = self.client.get(f'/api/submissions/{sid}').json['reviews'][0]
            for key, value in payload.items():
                self.assertEqual(saved[key], value)

    def test_inline_grammar_from_real_ocr_uses_context_not_reference_answers(self):
        import json
        from PIL import Image
        path = Path(__file__).parent/'tests/fixtures/grammar_inline_ocr.json'
        if not path.is_file():
            self.skipTest('真实 OCR 样本仅保存在本机')
        fixture = json.loads(path.read_text('utf-8'))
        task = self.post('/api/tasks', {'name': '段落填空回归', 'questions': [
            dict(q, answer='NEVER USE THIS TO GUESS', points=1) for q in fixture['questions']]}).json
        sid = self.submission(task)
        buf = io.BytesIO(); Image.new('RGB', (1100, 1500), 'white').save(buf, 'PNG')
        page = self.image(sid, content=buf.getvalue()).json
        with patch('app.recognize', return_value={'blocks': fixture['blocks']}):
            self.assertEqual(self.post(f"/api/pages/{page['id']}/ocr", {}).status_code, 200)
        suggestions = self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions']
        expected = ['that','among','is favored','why','adopt','to demand','making','when','uncorporated','what']
        self.assertEqual([s['transcript'] for s in suggestions], expected)
        # Teacher work always wins, including a saved blank and a reason-only draft.
        first, second = task['questions'][:2]
        self.post(f"/api/submissions/{sid}/reviews/{first['id']}", {'transcript':'Teacher edit','confirmed':True})
        self.post(f"/api/submissions/{sid}/reviews/{second['id']}", {'transcript':'','confirmed_blank':True,'reason':'核对过的空白'})
        with patch('settings.recognize_selected') as cloud:
            result = self.post(f'/api/submissions/{sid}/extract-answers', {'page_id':page['id']})
            cloud.assert_not_called()
        self.assertEqual(result.status_code, 200, result.json)
        reviews = result.json['reviews']
        self.assertEqual(reviews[0]['transcript'], 'Teacher edit'); self.assertTrue(reviews[0]['confirmed'])
        self.assertTrue(reviews[1]['confirmed_blank']); self.assertEqual(reviews[1]['reason'],'核对过的空白')
        for review, answer in zip(reviews[2:], expected[2:]):
            self.assertEqual(review['machine_text'],answer)
            self.assertEqual(review['transcript'],answer)
            self.assertFalse(review['confirmed'])
        self.assertEqual(result.json['pages'][0]['blocks'], fixture['blocks'])
        self.assertEqual(self.post(f'/api/submissions/{sid}/extract-answers', {}).json['extracted_count'],0)

    def test_inline_grammar_rejects_ambiguous_or_missing_context(self):
        task = self.post('/api/tasks', {'name':'Ambiguous', 'questions':[
            {'number':1,'kind':'grammar','answer':'when','points':1,'context':'Start (1) ____ they not only work here.'}]}).json
        sid=self.submission(task);page=self.image(sid).json
        for value in ['Start (1) that unrelated text.',
                      'Start (1) that they not only work here. Again (1) why they not only work here.',
                      'Start (1) they not only work here.']:
            with patch('app.recognize',return_value={'blocks':[{'id':0,'text':value,'box':[0,0,600,100]}]}):
                self.post(f"/api/pages/{page['id']}/ocr",{})
            self.assertEqual(self.client.get(f'/api/submissions/{sid}/suggestions').json['suggestions'],[],value)

    def test_inline_extraction_rejects_cross_page_duplicates_and_preserves_currency(self):
        from answer_extraction import grammar_candidates
        def page(pid, value):
            return {'id':pid,'status':'done','blocks':[{'id':0,'text':value,'box':[0,0,500,100]}]}
        q = {'question_id':1,'number':1,'kind':'grammar','context':'Start (1) ____ they not only work here.'}
        self.assertEqual(grammar_candidates([q],[page(1,'Start (1) who unrelated text'),
                                                 page(2,'Start (1) when they not only work here.')]),[])
        money = dict(q,context='Cost (1) ____ dollars are needed today.')
        self.assertEqual(grammar_candidates([money],[page(1,'Cost (1) $100 dollars are needed today.')])[0]['transcript'],'$100')


if __name__ == '__main__':
    unittest.main()
