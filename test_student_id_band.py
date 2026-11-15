"""Unified answer-card identity band: format snapshot, band reading and safe matching.
Synthetic OCR blocks only; no real student photos and no cloud calls."""
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import test_batch

SNAPSHOT = {'version': 'AC1', 'entry_mode': 'handwritten', 'id_length': 6, 'id_prefix': '2681',
            'variable_count': 2, 'format_code': 'AC1-KMXPRT'}
STUDENTS = [{'id': 1, 'student_code': '268101', 'name': '张思远'},
            {'id': 2, 'student_code': '268129', 'name': '阮治藏'},
            {'id': 3, 'student_code': '268146', 'name': '欧阳明月'}]


def blocks(*lines):
    return [{'id': i, 'text': t, 'box': [0, i * 60, 600, i * 60 + 40]} for i, t in enumerate(lines)]


class ReadBandTests(unittest.TestCase):
    def read(self, *lines, snapshot=SNAPSHOT, students=STUDENTS):
        from student_id_band import read_band
        return read_band(blocks(*lines), format_snapshot=snapshot, students=students)

    def test_valid_suffix_same_block_split_digits_and_fullwidth(self):
        for value in ('29', '2 9', '２９', ' 2  9 。'):
            with self.subTest(value=value):
                band = self.read(f'学号前缀：2681 学号后2位：{value} 姓名：阮治藏', '模板：AC1-KMXPRT · 第 1 / 1 页')
                self.assertEqual(band['state'], 'valid', band)
                self.assertEqual((band['code'], band['observed_suffix'], band['student_id']), ('268129', '29', 2))
                self.assertEqual(band['format_code'], 'AC1-KMXPRT')
                self.assertNotIn('confidence', band)

    def test_value_in_next_line_block(self):
        band = self.read('学号前缀：2681 学号后2位：', '29', '姓名：阮治藏', 'AC1-KMXPRT')
        self.assertEqual(band['state'], 'valid', band)
        self.assertEqual(band['block_ids'], [0, 1])

    def test_without_marker_never_guesses_two_digits(self):
        band = self.read('学号后2位：29 姓名：阮治藏')
        self.assertEqual(band['state'], 'absent')
        self.assertIsNone(band['code'])

    def test_other_template_marker_is_mismatch(self):
        band = self.read('学号后2位：29', '模板：AC1-ACDEFG')
        self.assertEqual(band['state'], 'template_mismatch')
        self.assertIn('ACDEFG', band['reason'])

    def test_incomplete_missing_extra_and_non_digit_are_not_corrected(self):
        for value, words in (('2', '少 1 位'), ('291', '多 1 位'), ('', '未填写'), ('2O', '非数字'), ('l9', '非数字')):
            with self.subTest(value=value):
                band = self.read(f'学号后2位：{value} 姓名：阮治藏', 'AC1-KMXPRT')
                self.assertEqual(band['state'], 'incomplete', band)
                self.assertIsNone(band['code'])
                self.assertIn(words, band['reason'])

    def test_conflicting_values_and_number_outside_roster(self):
        band = self.read('学号后2位：29', '学号后2位：46', 'AC1-KMXPRT')
        self.assertEqual(band['state'], 'conflict')
        band = self.read('学号后2位：77', 'AC1-KMXPRT')
        self.assertEqual(band['state'], 'conflict')
        self.assertIn('268177', band['reason'])

    def test_full_length_mode_without_prefix(self):
        snapshot = dict(SNAPSHOT, id_prefix='', variable_count=6)
        band = self.read('学号（6位）：2 6 8 1 4 6', 'AC1-KMXPRT', snapshot=snapshot)
        self.assertEqual((band['state'], band['code'], band['student_id']), ('valid', '268146', 3))

    def test_no_snapshot_means_no_band(self):
        from student_id_band import read_band
        self.assertIsNone(read_band(blocks('学号后2位：29'), format_snapshot=None, students=STUDENTS))


class EvidenceTests(unittest.TestCase):
    def evidence(self, *lines):
        from identity_matching import identity_evidence
        from student_id_band import read_band
        b = blocks(*lines)
        return identity_evidence(b, STUDENTS, band=read_band(b, format_snapshot=SNAPSHOT, students=STUDENTS))

    def test_band_with_unreadable_name_links_by_band(self):
        e = self.evidence('学号前缀：2681 学号后2位：29 姓名：际治议', 'AC1-KMXPRT')
        self.assertEqual((e['exact_id'], e['blocked'], e['method']), (2, False, 'band'))

    def test_band_and_exact_name_agree(self):
        e = self.evidence('学号后2位：29 姓名：阮治藏', 'AC1-KMXPRT')
        self.assertEqual((e['exact_id'], e['method']), (2, 'band'))
        self.assertIn('姓名一致', e['reason'])

    def test_band_against_exact_other_name_or_full_code_blocks(self):
        for lines in (('学号后2位：29 姓名：张思远', 'AC1-KMXPRT'),
                      ('学号：268101', '学号后2位：29', 'AC1-KMXPRT'),
                      ('学号后2位：29 姓名：张思远 姓名：阮治藏', 'AC1-KMXPRT')):
            with self.subTest(lines=lines):
                e = self.evidence(*lines)
                self.assertTrue(e['blocked']); self.assertIsNone(e['exact_id'])

    def test_band_against_close_name_of_other_student_blocks(self):
        e = self.evidence('学号后2位：29 姓名：张思元', 'AC1-KMXPRT')
        self.assertTrue(e['blocked']); self.assertIsNone(e['exact_id'])

    def test_incomplete_band_falls_back_to_old_chain(self):
        e = self.evidence('学号后2位：2 姓名：阮治藏', 'AC1-KMXPRT')
        self.assertEqual((e['exact_id'], e['method']), (2, 'exact'))
        e = self.evidence('学号后2位：2 姓名：际治议', 'AC1-KMXPRT')
        self.assertIsNone(e['exact_id']); self.assertIn('少 1 位', e['reason'])

    def test_template_mismatch_blocks_even_with_exact_name(self):
        e = self.evidence('学号后2位：29 姓名：阮治藏', 'AC1-ACDEFG')
        self.assertTrue(e['blocked']); self.assertIsNone(e['exact_id'])

    def test_old_cards_keep_previous_behavior(self):
        from identity_matching import identity_evidence
        for lines, expected in ((('学号：268129 姓名：阮治藏',), 2), (('姓名：阮治藏',), 2)):
            e = identity_evidence(blocks(*lines), STUDENTS)
            self.assertEqual((e['exact_id'], e['method']), (expected, 'exact'))
        # Known limitation kept on purpose: split full numbers on old cards are not re-joined.
        e = identity_evidence(blocks('学号：2 6 8 1 2 9'), STUDENTS)
        self.assertEqual(e['code'], '2'); self.assertTrue(e['blocked'])


class AnswerCardIdentityFlowTests(unittest.TestCase):
    setUp = test_batch.BatchTests.setUp
    post = test_batch.BatchTests.post
    patch = test_batch.BatchTests.patch
    klass = test_batch.BatchTests.klass
    task = test_batch.BatchTests.task
    upload_batch = test_batch.BatchTests.upload_batch

    def roster(self, members=(('268101', '张思远'), ('268129', '阮治藏'), ('268146', '欧阳明月'))):
        cid = self.klass()
        for code, name in members:
            self.assertEqual(self.post(f'/api/classes/{cid}/members', {'student_code': code, 'name': name}).status_code, 201)
        tid = self.task()
        aid = self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [cid]}).json['assignments'][0]['id']
        students = {s['student_code']: s for s in self.client.get(f'/api/assignments/{aid}').json['students']}
        return cid, aid, students

    def save(self, aid, **values):
        return self.post(f'/api/assignments/{aid}/answer-card-format',
                         {'entry_mode': 'handwritten', 'id_length': 6, 'id_prefix': '2681'} | values)

    def code(self, aid):
        return self.client.get(f'/api/assignments/{aid}').json['answer_card']['format']['format_code']

    def identify(self, aid, *lines, defer=False):
        photo = self.upload_batch(aid)
        with patch('batch_photos.recognize', return_value={'blocks': blocks(*lines, '1. receive')}):
            response = self.post(f"/api/batch-photos/{photo['id']}/identify", {'defer_link': defer})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def match(self, aid):
        with patch('settings.recognize_selected') as cloud:
            response = self.post(f'/api/assignments/{aid}/photos/match', {})
            cloud.assert_not_called()
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_suggestion_save_idempotent_and_print(self):
        _, aid, _ = self.roster()
        card = self.client.get(f'/api/assignments/{aid}').json['answer_card']
        self.assertIsNone(card['format'])
        self.assertEqual(card['suggestion'], {'entry_mode': 'handwritten', 'id_length': 6, 'id_prefix': '2681', 'variable_count': 2})
        first = self.save(aid)
        self.assertEqual(first.status_code, 200, first.json)
        fmt = first.json['format']
        self.assertEqual((fmt['version'], fmt['variable_count']), ('AC1', 2))
        self.assertRegex(fmt['format_code'], r'^AC1-[ACDEFGHJKMNPRTUVWXY]{6}$')
        self.assertEqual(self.save(aid).json['format'], fmt)
        html = self.client.get(f'/assignments/{aid}/answer-card').get_data(as_text=True)
        self.assertIn('学号前缀：', html); self.assertIn('学号后2位：', html); self.assertIn(fmt['format_code'], html)
        self.assertEqual(html.count('class="digit-box"'), 2)
        self.assertNotIn('receive', html)
        downloaded = self.client.get(f'/assignments/{aid}/answer-card/download')
        self.assertIn('attachment', downloaded.headers['Content-Disposition'])
        import io
        from pypdf import PdfReader
        self.assertEqual(downloaded.mimetype, 'application/pdf')
        self.assertIn(fmt['format_code'], PdfReader(io.BytesIO(downloaded.data)).pages[0].extract_text())
        full = self.save(aid, id_prefix='')
        self.assertEqual(full.json['format']['variable_count'], 6)
        self.assertIn('学号（6位）：', self.client.get(f'/assignments/{aid}/answer-card').get_data(as_text=True))

    def test_invalid_formats_and_unsupported_modes_are_rejected(self):
        _, aid, _ = self.roster()
        for values in ({'entry_mode': 'bubbles'}, {'entry_mode': 'qr'}, {'id_length': 5}, {'id_length': True},
                       {'id_prefix': '2682'}, {'id_prefix': '268101'}, {'id_prefix': '28a1'}):
            with self.subTest(values=values):
                self.assertEqual(self.save(aid, **values).status_code, 400)
        self.assertIsNone(self.client.get(f'/api/assignments/{aid}').json['answer_card']['format'])
        mixed = self.roster((('268101', '甲'), ('A0129', '乙')))[1]
        card = self.client.get(f'/api/assignments/{mixed}').json['answer_card']
        self.assertIsNone(card['suggestion']); self.assertTrue(card['problem'])
        self.assertEqual(self.save(mixed).status_code, 400)

    def test_format_editable_until_first_photo_then_frozen(self):
        _, aid, _ = self.roster()
        self.save(aid)
        self.assertEqual(self.save(aid, id_prefix='268').status_code, 200)
        self.assertEqual(self.post(f'/api/assignments/{aid}/answer-card-format', {'clear': True}).status_code, 200)
        self.save(aid)
        self.upload_batch(aid)
        self.assertFalse(self.client.get(f'/api/assignments/{aid}').json['answer_card']['editable'])
        self.assertEqual(self.save(aid, id_prefix='268').status_code, 409)
        self.assertEqual(self.post(f'/api/assignments/{aid}/answer-card-format', {'clear': True}).status_code, 409)
        self.assertEqual(self.save(aid).status_code, 200)

    def test_archived_and_incompatible_appended_student(self):
        cid, aid, _ = self.roster()
        self.save(aid)
        sid = self.post(f'/api/classes/{cid}/members', {'student_code': '290101', 'name': '新同学'}).json['student_id']
        response = self.post(f'/api/assignments/{aid}/students', {'student_id': sid})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertIn('答题卡格式', response.json['error'])
        self.patch(f'/api/classes/{cid}', {'archived': True})
        self.assertEqual(self.save(aid, id_prefix='').status_code, 409)

    def test_band_links_when_name_is_unreadable_without_memory(self):
        _, aid, students = self.roster()
        self.save(aid)
        photo = self.identify(aid, '学号前缀：2681 学号后2位：29 姓名：际治议', f'模板：{self.code(aid)} · 第 1 / 1 页')
        self.assertEqual(photo['suggested_student_id'], students['268129']['id'])
        self.assertIsNotNone(photo['submission_id'])
        details = photo['match_details']
        self.assertEqual(details['method'], 'band')
        self.assertEqual(details['identity_band']['code'], '268129')
        self.assertEqual(details['identity_band']['observed_suffix'], '29')
        self.assertIn('268129', details['reason'])
        with closing(sqlite3.connect(self.tmp.name + '/dictation.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM identity_memories').fetchone()[0], 0)
        self.assertEqual(self.match(aid)['linked_submission_ids'], [])

    def test_conflicts_and_bad_bands_wait_for_teacher(self):
        _, aid, students = self.roster()
        self.save(aid)
        code = self.code(aid)
        cases = [('学号后2位：29 姓名：张思远', '学号与姓名'),
                 ('学号：268101 学号后2位：29', '完整学号'),
                 ('学号后2位：2 姓名：际治议', '少 1 位'),
                 ('学号后2位：77 姓名：际治议', '268177')]
        for line, words in cases:
            with self.subTest(line=line):
                photo = self.identify(aid, line, f'模板：{code}')
                self.assertIsNone(photo['submission_id'], photo['match_details'])
                self.assertIn(words, photo['match_details']['reason'])
        wrong = self.identify(aid, '学号后2位：46 姓名：欧阳明月', '模板：AC1-ACDEFG')
        self.assertIsNone(wrong['submission_id'])
        self.assertIn('模板', wrong['match_details']['reason'])

    def test_duplicate_band_photos_are_not_split_or_reassigned(self):
        _, aid, students = self.roster()
        self.save(aid)
        code = self.code(aid)
        for _ in range(2):
            self.identify(aid, '学号后2位：46', f'模板：{code}', defer=True)
        result = self.match(aid)
        self.assertEqual(result['summary']['duplicate'], 2)
        self.assertTrue(all(p['submission_id'] is None for p in result['photos']))

    def test_exempt_student_band_is_not_linked(self):
        _, aid, students = self.roster()
        self.save(aid)
        self.patch(f"/api/assignment-students/{students['268146']['id']}", {'exempt': True, 'reason': '请假'})
        photo = self.identify(aid, '学号后2位：46 姓名：欧阳明月', f'模板：{self.code(aid)}')
        self.assertIsNone(photo['submission_id'])

    def test_band_match_can_be_safely_reassigned(self):
        _, aid, students = self.roster()
        self.save(aid)
        photo = self.identify(aid, '学号后2位：29 姓名：际治议', f'模板：{self.code(aid)}')
        moved = self.post(f"/api/batch-photos/{photo['id']}/reassign",
                          {'confirmed': True, 'assignment_student_id': students['268101']['id']})
        self.assertEqual(moved.status_code, 200, moved.json)
        self.assertEqual(moved.json['photo']['match_details']['method'], 'corrected')
        with closing(sqlite3.connect(self.tmp.name + '/dictation.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM identity_memories').fetchone()[0], 0)

    def test_assignments_without_format_keep_old_card_and_matching(self):
        _, aid, students = self.roster()
        html = self.client.get(f'/assignments/{aid}/answer-card').get_data(as_text=True)
        self.assertIn('学号：________________', html); self.assertNotIn('class="digit-box"', html)
        photo = self.identify(aid, '学号后2位：29 姓名：阮治藏', 'AC1-KMXPRT')
        self.assertEqual(photo['match_details']['method'], 'exact')
        self.assertEqual(photo['suggested_student_id'], students['268129']['id'])


if __name__ == '__main__':
    unittest.main()
