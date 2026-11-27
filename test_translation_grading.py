import copy
import json
import unittest

try:
    import translation_grading as grading
except ImportError:
    grading = None


RUBRIC = {'criteria': [
    {'id': 'meaning', 'dimension': 'semantic', 'description': '回家',
     'max_points_cents': 200, 'allowed_levels': {'full': 200, 'partial': 100, 'none': 0}},
    {'criterion_id': 'verb', 'dimension': 'grammar', 'description': '主谓一致',
     'max_points_cents': 100, 'allowed_levels': {'full': 100, 'partial': 50, 'none': 0}},
]}


def response(level='full'):
    value = {'criteria_results': [
        {'criterion_id': 'meaning', 'level': 'full'},
        {'criterion_id': 'verb', 'level': level}],
        'errors': [], 'feedback': '核对谓语。', 'confidence_level': 'high'}
    if level != 'full':
        value['criteria_results'][1]['deduction_root_id'] = 'r1'
        value['errors'] = [{
            'id': 'e1', 'root_cause_id': 'r1', 'error_type': 'grammar',
            'error_subtype': 'subject_verb_agreement', 'severity': 'minor',
            'start': 4, 'end': 6, 'student_segment': 'go',
            'original_segment': '', 'source_start': None, 'source_end': None,
            'explanation': '第三人称单数', 'suggested_correction': 'goes',
            'affected_criterion': 'verb', 'knowledge_points': ['主谓一致']}]
    return value


class TranslationGradingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(grading, 'pure translation grading module must exist')
        self.rubric = grading.validate_rubric(copy.deepcopy(RUBRIC), 3)

    def score(self, value, answer='She go home.', source='她回家。'):
        return grading.score_response(value, self.rubric, answer, source)

    def test_rubric_budget_alias_and_exact_cents(self):
        self.assertEqual(self.rubric['criteria'][1]['id'], 'verb')
        self.assertEqual(self.rubric['max_points_cents'], 300)
        for points in [True, float('nan'), float('inf'), 0, -1, '3.001']:
            with self.subTest(points=points), self.assertRaises(ValueError):
                grading.validate_rubric(RUBRIC, points)
        for mutate in [lambda r: r['criteria'][0].update(max_points_cents=199),
                       lambda r: r['criteria'][0]['allowed_levels'].update(full=201),
                       lambda r: r['criteria'][1].update(id='meaning'),
                       lambda r: r['criteria'][0].update(dimension='other'),
                       lambda r: r['criteria'][0]['allowed_levels'].update(partial=True)]:
            bad = copy.deepcopy(RUBRIC)
            mutate(bad)
            with self.assertRaises(ValueError):
                grading.validate_rubric(bad, 3)

    def test_correct_different_expression_has_no_errors(self):
        result = self.score(response(), answer='She returns to her house.')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['suggested_score'], 3)
        self.assertEqual(result['grammar_score'], 1)
        self.assertEqual(sum(result[d + '_score'] for d in grading.DIMENSIONS), 3)
        self.assertTrue(result['needs_review'])
        self.assertTrue(result['can_adopt'])

    def test_score_comes_only_from_frozen_levels(self):
        value = response('partial')
        value['suggested_score'] = 999
        value['criteria_results'][1]['awarded_points_cents'] = 100
        value['errors'][0]['deduction'] = 999
        result = self.score(json.dumps(value))
        self.assertEqual(result['suggested_score'], 2.5)
        self.assertEqual(result['errors'][0]['deduction'], .5)
        self.assertEqual(result['criteria_results'][1]['awarded_points_cents'], 50)
        self.assertEqual(result['suggested_score_cents'], 250)

    def test_strict_json_rejects_duplicates_wrappers_nan_and_extra_fields(self):
        for value in ['{"criteria":[],"criteria":[]}', '```json\n{}\n```',
                      '{"x":NaN}', '{} trailing', '{"x":1e999}']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                grading.validate_rubric(value, 3)
        bad = response()
        bad['execute'] = 'anything'
        with self.assertRaises(ValueError):
            self.score(bad)

    def test_unknown_missing_duplicate_criteria_and_levels_rejected(self):
        for mutate in [lambda v: v['criteria_results'].pop(),
                       lambda v: v['criteria_results'].append(v['criteria_results'][0]),
                       lambda v: v['criteria_results'][0].update(criterion_id='fake'),
                       lambda v: v['criteria_results'][0].update(level='invented'),
                       lambda v: v['criteria_results'][1].update(level='partial')]:
            bad = response()
            mutate(bad)
            with self.assertRaises(ValueError):
                self.score(bad)

    def test_repeated_root_blocks_scoring_instead_of_order_dependent_deduction(self):
        bad = response('partial')
        bad['criteria_results'][0].update(level='partial', deduction_root_id='r1')
        result = self.score(bad)
        self.assertFalse(result['can_adopt'])
        self.assertIsNone(result['suggested_score'])
        self.assertIn('duplicate_deduction_root', result['risk_flags'])

    def test_unicode_exact_position_repetition_and_deletion(self):
        value = response('partial')
        error = value['errors'][0]
        error.update(start=3, end=4, student_segment='x', suggested_correction='')
        result = self.score(value, answer='😀x x')
        self.assertEqual(result['errors'][0]['start'], 3)
        for change in [dict(start=2), dict(end=99), dict(start=True), dict(student_segment='X')]:
            bad = copy.deepcopy(value)
            bad['errors'][0].update(change)
            with self.assertRaises(ValueError):
                self.score(bad, answer='😀x x')

    def test_omission_has_zero_width_anchor_and_exact_source(self):
        value = response('partial')
        value['errors'][0].update(error_type='omission', start=3, end=3,
            student_segment='', original_segment='她', source_start=0, source_end=1)
        self.assertTrue(self.score(value)['can_adopt'])
        value['errors'][0]['source_start'] = 1
        with self.assertRaises(ValueError):
            self.score(value)

    def test_multiple_segments_validated_and_only_one_error_gets_deduction(self):
        value = response('partial')
        extra = copy.deepcopy(value['errors'][0])
        extra.update(id='e2', start=7, end=11, student_segment='home')
        value['errors'].append(extra)
        result = self.score(value)
        self.assertEqual(sum(e['deduction'] for e in result['errors']), .5)
        value['errors'][1]['segments'] = [{'start': 99, 'end': 99, 'student_segment': ''}]
        with self.assertRaises(ValueError):
            self.score(value)

    def test_low_confidence_unknown_judgment_and_empty_answer_never_zero(self):
        for confidence in ['low', 'unknown']:
            value = response()
            value['confidence_level'] = confidence
            result = self.score(value)
            self.assertIsNone(result['suggested_score'])
            self.assertFalse(result['can_adopt'])
        value = response()
        value['criteria_results'][0]['level'] = 'unknown'
        self.assertIsNone(self.score(value)['suggested_score'])
        with self.assertRaises(ValueError):
            self.score(response(), answer='  ')

    def test_malformed_untrusted_types_always_raise_value_error(self):
        for mutate in [lambda v: v.update(confidence_level=[]),
                       lambda v: v['errors'][0].update(error_type=[]),
                       lambda v: v['errors'][0].update(severity={}),
                       lambda v: v['errors'][0].update(affected_criterion=[]),
                       lambda v: v['criteria_results'][0].update(awarded_points_cents=True),
                       lambda v: v.update(suggested_score=True),
                       lambda v: v['errors'][0].update(deduction=False)]:
            bad = response('partial')
            mutate(bad)
            with self.assertRaises(ValueError):
                self.score(bad)
        with self.assertRaises(ValueError):
            grading.score_response(response(), {'max_points_cents': 'invalid'}, 'answer', '原文')

    def test_overlapping_distinct_roots_require_teacher_review(self):
        value = response('partial')
        value['criteria_results'][0].update(level='partial', deduction_root_id='r2')
        extra = copy.deepcopy(value['errors'][0])
        extra.update(id='e2', root_cause_id='r2', affected_criterion='meaning')
        value['errors'].append(extra)
        result = self.score(value)
        self.assertFalse(result['can_adopt'])
        self.assertIn('overlapping_evidence', result['risk_flags'])
        self.assertIsNone(result['suggested_score'])

    def test_prompts_explain_equivalence_empty_errors_and_data_boundary(self):
        question = {'source_text': '她回家。', 'answer': 'She goes home.', 'points': 3}
        prompt = grading.grading_prompt(question, self.rubric, 'She returns home.')
        for text in ['errors', '[]', '等价', '数据', 'Unicode', 'deduction_root_id']:
            self.assertIn(text, prompt)
        self.assertIn('max_points_cents', grading.rubric_prompt(question))
        self.assertNotIn('She returns home.', grading.rubric_prompt(question))


if __name__ == '__main__':
    unittest.main()
