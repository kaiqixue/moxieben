"""Teacher name memories, real isolated SQLite, synthetic OCR only."""
import unittest
from unittest.mock import patch

import test_identity_matching


class NameMemoryTests(unittest.TestCase):
    setUp = test_identity_matching.IdentityMatchingTests.setUp
    post = test_identity_matching.IdentityMatchingTests.post
    klass = test_identity_matching.IdentityMatchingTests.klass
    task = test_identity_matching.IdentityMatchingTests.task
    upload_batch = test_identity_matching.IdentityMatchingTests.upload_batch
    setup_roster = test_identity_matching.IdentityMatchingTests.setup_roster
    identify = test_identity_matching.IdentityMatchingTests.identify
    match = test_identity_matching.IdentityMatchingTests.match

    def cid(self, aid):
        return self.client.get(f'/api/assignments/{aid}').json['class_id']

    def memories(self, aid):
        response = self.client.get(f'/api/classes/{self.cid(aid)}/identity-memories')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['memories']

    def confirm(self, photo, student, remember=True, observed=None):
        payload = {'assignment_student_id': student['id'], 'confirmed': True, 'remember_identity': remember}
        if observed is not None:
            payload['observed_name'] = observed
        return self.post(f"/api/batch-photos/{photo['id']}/confirm", payload)

    def next_assignment(self, aid):
        tid = self.task()
        return self.post(f'/api/tasks/{tid}/assignments', {'class_ids': [self.cid(aid)]}).json['assignments'][0]['id']

    def learn(self):
        aid, students = self.setup_roster(['顾青禾', '张同学'])
        photo = self.identify(aid, '姓名：杜春禾')
        response = self.confirm(photo, students[0])
        self.assertEqual(response.status_code, 200, response.json)
        return aid, students, photo

    def test_teacher_confirmation_learns_cross_assignment_but_not_cross_class(self):
        aid, students, photo = self.learn()
        memory = self.memories(aid)[0]
        self.assertEqual(memory['observed_name'], '杜春禾')
        self.assertEqual(memory['student_id'], students[0]['student_id'])
        self.assertEqual(memory['source_photo_ids'], [photo['id']])
        self.confirm(photo, students[0])
        self.assertEqual(len(self.memories(aid)), 1)
        next_aid = self.next_assignment(aid)
        next_photo = self.identify(next_aid, '杜春禾', False)
        self.assertEqual(next_photo['match_details']['method'], 'memory')
        self.assertEqual(next_photo['match_details']['memory_id'], memory['id'])
        self.assertEqual(next_photo['identity_source'], 'machine')
        self.assertEqual(self.memories(aid)[0]['source_photo_ids'], [photo['id']], 'Machine matching cannot teach itself')
        self.assertEqual(self.match(next_aid)['summary']['memory'], 1)
        sub = self.post(f"/api/submissions/{next_photo['submission_id']}/auto-grade", {}).json
        self.assertTrue(sub['is_final'])
        self.assertEqual(sub['teacher_reviewed_count'], 0)
        other, _ = self.setup_roster(['顾青禾'])
        self.assertIsNone(self.identify(other, '杜春禾', False)['submission_id'])
        self.assertEqual(self.memories(other), [])

    def test_opt_out_and_machine_inference_do_not_learn(self):
        aid, students = self.setup_roster(['顾青禾', '许映舟'])
        photo = self.identify(aid, '杜春禾')
        self.assertEqual(self.confirm(photo, students[0], False).status_code, 200)
        self.assertEqual(self.memories(aid), [])
        self.identify(aid, '许咏舟', False)
        self.assertEqual(self.memories(aid), [])

    def test_preview_is_read_only_and_import_is_explicit_idempotent(self):
        aid, students = self.setup_roster(['顾青禾'])
        photo = self.identify(aid, '杜春禾')
        self.confirm(photo, students[0], False)
        empty_aid = self.next_assignment(aid)
        empty_student = self.client.get(f'/api/assignments/{empty_aid}').json['students'][0]
        empty = self.upload_batch(empty_aid)
        self.assertEqual(self.confirm(empty, empty_student, False).status_code, 200)
        base = f'/api/classes/{self.cid(aid)}/identity-memories'
        with patch('settings.recognize_selected') as cloud:
            candidates = self.client.get(base + '/candidates').json['candidates']
            self.assertEqual([c['photo_id'] for c in candidates], [photo['id']])
            self.assertEqual(self.memories(aid), [])
            for _ in range(2):
                response = self.post(base, {'photo_id': photo['id']})
                self.assertEqual(response.status_code, 200, response.json)
            cloud.assert_not_called()
        self.assertEqual(len(self.memories(aid)), 1)
        self.assertEqual(self.client.get(base + '/candidates').json['candidates'], [])
        self.assertEqual(self.post(base, {'photo_id': empty['id']}).status_code, 400)

    def test_disable_and_reenable_do_not_change_existing_photos(self):
        aid, _, photo = self.learn()
        memory = self.memories(aid)[0]
        path = f"/api/classes/{self.cid(aid)}/identity-memories/{memory['id']}"
        self.assertEqual(self.client.patch(path, json={'enabled': False}, headers=self.headers).status_code, 200)
        next_aid = self.next_assignment(aid)
        pending = self.identify(next_aid, '杜春禾', False)
        self.assertIsNone(pending['submission_id'])
        self.assertEqual(self.client.patch(path, json={'enabled': True}, headers=self.headers).status_code, 200)
        self.assertEqual(self.match(next_aid)['summary']['memory'], 1)
        original = self.client.get(f'/api/assignments/{aid}/photos').json['photos'][0]
        self.assertEqual(original['submission_id'], photo.get('submission_id') or self.client.get(f'/api/assignments/{aid}').json['students'][0]['submission_id'])

    def test_conflicting_memories_stop_inference_until_explicit_resolution(self):
        aid, students, _ = self.learn()
        photo = self.identify(aid, '杜春禾')
        self.assertEqual(self.confirm(photo, students[1]).status_code, 200)
        memories = self.memories(aid)
        self.assertTrue(all(m['conflict'] for m in memories))
        next_aid = self.next_assignment(aid)
        pending = self.identify(next_aid, '杜春禾', False)
        self.assertIsNone(pending['submission_id'])
        self.assertIn('记忆冲突', pending['match_details']['reason'])
        chosen = memories[0]
        path = f"/api/classes/{self.cid(aid)}/identity-memories/{chosen['id']}"
        response = self.client.patch(path, json={'enabled': True, 'resolve_conflict': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.match(next_aid)['summary']['memory'], 1)

    def test_explicit_conflicts_duplicates_and_missing_names_never_use_memory(self):
        aid, _, _ = self.learn()
        next_aid = self.next_assignment(aid)
        for text in ['学号：002 姓名：杜春禾', '姓名：杜春禾\n姓名：张同学', 'Homework only']:
            photo = self.identify(next_aid, text, False)
            self.assertIsNone(photo['submission_id'], text)
        another = self.next_assignment(aid)
        self.identify(another, '杜春禾')
        self.identify(another, '杜春禾')
        result = self.match(another)
        self.assertEqual(result['summary']['matched'], 0)
        self.assertEqual(result['summary']['duplicate'], 2)

    def test_memory_cannot_override_exact_name_or_exempt_student(self):
        aid, students, _ = self.learn()
        self.post(f'/api/classes/{self.cid(aid)}/members', {'student_code': '003', 'name': '杜春禾'})
        next_aid = self.next_assignment(aid)
        photo = self.identify(next_aid, '杜春禾', False)
        self.assertIsNone(photo['submission_id'])
        self.assertIn('冲突', photo['match_details']['reason'])
        aid2, roster = self.setup_roster(['顾青禾'])
        source = self.identify(aid2, '杜春禾')
        self.confirm(source, roster[0])
        next2 = self.next_assignment(aid2)
        target = self.client.get(f'/api/assignments/{next2}').json['students'][0]
        self.client.patch(f"/api/assignment-students/{target['id']}", json={'exempt': True, 'reason': '请假'}, headers=self.headers)
        self.assertIsNone(self.identify(next2, '杜春禾', False)['submission_id'])

    def test_reassign_memory_disables_wrong_source_and_learns_new_teacher_target(self):
        aid, students, _ = self.learn()
        next_aid = self.next_assignment(aid)
        photo = self.identify(next_aid, '杜春禾', False)
        roster = self.client.get(f'/api/assignments/{next_aid}').json['students']
        response = self.post(f"/api/batch-photos/{photo['id']}/reassign", {'assignment_student_id': roster[1]['id'], 'confirmed': True, 'remember_identity': True, 'observed_name': '杜春禾'})
        self.assertEqual(response.status_code, 200, response.json)
        memories = self.memories(aid)
        self.assertFalse(next(m for m in memories if m['student_id'] == students[0]['student_id'])['enabled'])
        self.assertTrue(next(m for m in memories if m['student_id'] == students[1]['student_id'])['enabled'])
        third = self.next_assignment(aid)
        linked = self.identify(third, '杜春禾', False)
        self.assertEqual(linked['match_details']['method'], 'memory')
        self.assertEqual(linked['suggested_student_id'], self.client.get(f'/api/assignments/{third}').json['students'][1]['id'])

    def test_missing_name_skips_memory_without_blocking_confirmation(self):
        aid, students = self.setup_roster(['顾青禾'])
        photo = self.identify(aid, 'Homework')
        response = self.confirm(photo, students[0])
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.memories(aid), [], 'No readable evidence stores no memory')
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['submitted'], 1)

    def test_invalid_payloads_do_not_link_or_store_memory(self):
        aid, students = self.setup_roster(['顾青禾'])
        photo = self.identify(aid, '杜春禾')
        path = f"/api/batch-photos/{photo['id']}/confirm"
        self.assertEqual(self.post(path, ['invalid']).status_code, 400)
        payload = {'assignment_student_id': students[0]['id'], 'confirmed': True, 'remember_identity': 'false'}
        self.assertEqual(self.post(path, payload).status_code, 400)
        self.assertEqual(self.memories(aid), [])
        self.assertEqual(self.client.get(f'/api/assignments/{aid}').json['counts']['submitted'], 0)

    def test_memory_competes_with_fuzzy_and_never_displaces_exact_photo(self):
        aid, students = self.setup_roster(['许映舟', '张同学'])
        source = self.identify(aid, '杜春禾')
        self.confirm(source, students[0])
        for reverse in (False, True):
            next_aid = self.next_assignment(aid)
            for text in (['许咏舟', '杜春禾'] if reverse else ['杜春禾', '许咏舟']):
                self.identify(next_aid, text)
            result = self.match(next_aid)
            self.assertEqual(result['summary']['matched'], 0)
        next_aid = self.next_assignment(aid)
        self.identify(next_aid, '杜春禾')
        self.identify(next_aid, '许映舟')
        result = self.match(next_aid)
        self.assertEqual(result['summary']['exact'], 1)
        self.assertEqual(result['summary']['duplicate'], 1)

    def test_disabled_memory_retry_archival_and_foreign_class_are_protected(self):
        aid, students, photo = self.learn()
        memory = self.memories(aid)[0]
        path = f"/api/classes/{self.cid(aid)}/identity-memories/{memory['id']}"
        self.client.patch(path, json={'enabled': False}, headers=self.headers)
        self.confirm(photo, students[0])
        self.assertFalse(self.memories(aid)[0]['enabled'], 'Retry must not reactivate stopped memory')
        other, _ = self.setup_roster(['顾青禾'])
        wrong = f"/api/classes/{self.cid(other)}/identity-memories/{memory['id']}"
        self.assertEqual(self.client.patch(wrong, json={'enabled': True}, headers=self.headers).status_code, 404)
        self.assertEqual(self.post(f'/api/classes/{self.cid(other)}/identity-memories', {'photo_id': photo['id']}).status_code, 409)
        self.client.patch(f'/api/classes/{self.cid(aid)}', json={'archived': True}, headers=self.headers)
        self.assertEqual(self.client.patch(path, json={'enabled': True}, headers=self.headers).status_code, 409)

    def test_blank_observed_name_skips_memory_and_numeric_evidence_is_accepted(self):
        aid, _, _ = self.learn()
        next_aid = self.next_assignment(aid)
        roster = self.client.get(f'/api/assignments/{next_aid}').json['students']
        blank = self.identify(next_aid, '杜春禾', False)
        self.assertEqual(self.confirm(blank, roster[0], observed='').status_code, 200)
        self.assertEqual([m['observed_name'] for m in self.memories(aid)], ['杜春禾'])
        numbered = self.identify(next_aid, '杜春禾', False)
        self.assertEqual(self.confirm(numbered, roster[1], observed='12').status_code, 200)
        self.assertIn('12', [m['observed_name'] for m in self.memories(aid)])

    def test_memory_correction_cannot_override_teacher_work_and_validates_before_commit(self):
        aid, _, _ = self.learn()
        next_aid = self.next_assignment(aid)
        photo = self.identify(next_aid, '杜春禾', False)
        roster = self.client.get(f'/api/assignments/{next_aid}').json['students']
        payload = {'assignment_student_id': roster[1]['id'], 'confirmed': True, 'remember_identity': True, 'observed_name': '张!'}
        path = f"/api/batch-photos/{photo['id']}/reassign"
        self.assertEqual(self.post(path, payload).status_code, 400)
        self.assertTrue(self.memories(aid)[0]['enabled'])
        original = self.client.get(f'/api/assignments/{next_aid}/photos').json['photos'][0]
        self.assertEqual(original['submission_id'], photo['submission_id'])
        sub = self.client.get(f"/api/submissions/{photo['submission_id']}").json
        self.post(f"/api/submissions/{sub['id']}/reviews/{sub['reviews'][0]['question_id']}", {'transcript': 'receive', 'confirmed': False})
        payload['observed_name'] = '杜春禾'
        self.assertEqual(self.post(path, payload).status_code, 409)
        self.assertTrue(self.memories(aid)[0]['enabled'])

    def test_memory_conflict_blocks_fuzzy_fallback_and_survives_restart(self):
        aid, students = self.setup_roster(['许映舟', '张同学'])
        for student in students:
            photo = self.identify(aid, '许咏舟')
            self.confirm(photo, student)
        next_aid = self.next_assignment(aid)
        photo = self.identify(next_aid, '许咏舟', False)
        self.assertIsNone(photo['submission_id'], 'Conflict cannot fall back to a one-character inference')
        import app
        restarted = app.create_app({'DATA_DIR': self.tmp.name, 'TESTING': True, 'CSRF_TOKEN': 'test'})
        response = restarted.test_client().get(f'/api/classes/{self.cid(aid)}/identity-memories')
        self.assertEqual(len(response.json['memories']), 2)
        self.assertTrue(all(m['conflict'] for m in response.json['memories']))


if __name__ == '__main__':
    unittest.main()
