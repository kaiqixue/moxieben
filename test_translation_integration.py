import tempfile
import unittest
from app import create_app


class TranslationIntegrationTests(unittest.TestCase):
    def test_explicit_translation_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'DATA_DIR': directory, 'TESTING': True})
            client = app.test_client()
            client.get('/')
            token = app.config['CSRF_TOKEN']
            response = client.post('/api/tasks', json={'name': '翻译', 'questions': [
                {'number': 1, 'kind': 'sentence', 'answer': 'She goes home.',
                 'points': 3, 'scoring_mode': 'translation', 'source_text': '她回家。'}]},
                headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 201, response.json)
            self.assertEqual(response.json['questions'][0]['scoring_mode'], 'translation')
            task = response.json['id']
            headers = {'X-CSRF-Token': token}
            klass = client.post('/api/classes', json={'name': '测试班'}, headers=headers).json['id']
            assignment = client.post(f'/api/tasks/{task}/assignments',
                                     json={'class_ids': [klass]}, headers=headers).json['assignments'][0]['id']
            self.assertIs(client.get(f'/api/assignments/{assignment}').json['has_translation'], True)


if __name__ == '__main__':
    unittest.main()
