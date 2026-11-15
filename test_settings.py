import io
from unittest import TestCase
from unittest.mock import patch

from app import create_app
import base64
import json
from pathlib import Path


class SettingsTests(TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test'})
        self.client=self.app.test_client(); self.headers={'X-CSRF-Token':'test'}

    def test_settings_are_masked_and_provider_can_be_changed(self):
        response=self.client.get('/api/settings')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['provider'],'local')
        response=self.client.patch('/api/settings',json={
            'provider':'openai_compatible','base_url':'https://example.test/v1',
            'model':'vision-model','api_key':'sk-secret-value'},headers=self.headers)
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.assertTrue(response.json['api_key_configured'])
        self.assertNotIn('sk-secret',response.get_data(as_text=True))
        stored=self.client.get('/api/settings').json
        self.assertEqual(stored['model'],'vision-model')
        self.assertNotIn('sk-secret',str(stored))
        self.client.patch('/api/settings',json={'model':'updated'},headers=self.headers)
        self.assertTrue(self.client.get('/api/settings').json['api_key_configured'])
        self.assertEqual(self.client.get('/api/settings').json['provider'],'openai_compatible')
        response=self.client.patch('/api/settings',json={'provider':'local','api_key':''},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertFalse(self.client.get('/api/settings').json['api_key_configured'])

    @patch('settings.test_openai_compatible', return_value={'ok':True,'message':'连接成功'})
    def test_cloud_connection_test_does_not_expose_key(self, test_call):
        self.client.patch('/api/settings',json={'provider':'openai_compatible','base_url':'https://example.test/v1','model':'m','api_key':'sk-secret'},headers=self.headers)
        response=self.client.post('/api/settings/test',headers=self.headers)
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.assertNotIn('sk-secret',response.get_data(as_text=True))
        test_call.assert_called_once()

    def test_cloud_image_request_and_safe_errors(self):
        import settings
        from PIL import Image
        image = Path(self.tmp.name) / 'sample.png'
        Image.new('RGB', (100, 50), 'white').save(image)
        response = __import__('unittest').mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'choices':[{'message':{'content':
            '```json\n{"blocks":[{"text":"1. recieve","box":[0,0,100,50]}]}\n```'}}]}).encode()
        with patch('settings._open', return_value=response) as network:
            result = settings.recognize_openai(image, {'base_url':'https://example.test/v1', 'model':'vision', 'api_key':'secret'})
        self.assertEqual(result['blocks'][0]['text'], '1. recieve')
        self.assertIsNone(result['blocks'][0]['confidence'])
        sent = json.loads(network.call_args.args[0].data)
        self.assertEqual(sent['model'], 'vision')
        url = sent['messages'][0]['content'][1]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as decoded:
            self.assertEqual(decoded.size, (100,50))
        self.assertNotIn('receive', sent['messages'][0]['content'][0]['text'])
        self.client.patch('/api/settings',json={'provider':'openai_compatible','base_url':'https://example.test/v1','model':'m','api_key':'sk-secret'},headers=self.headers)
        with patch('settings.test_openai_compatible',side_effect=RuntimeError('failed sk-secret')):
            result = self.client.post('/api/settings/test',headers=self.headers)
        self.assertNotIn('sk-secret', result.get_data(as_text=True))
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.client.patch('/api/settings',json={'base_url':'file:///private','provider':'openai_compatible'},headers=self.headers).status_code,400)
        response.read.return_value=json.dumps({'choices':[{'message':{'content':'1. recieve'},'finish_reason':'length'}]}).encode()
        with patch('settings._open',return_value=response), self.assertRaisesRegex(ValueError,'截断'):
            settings.recognize_openai(image,{'base_url':'https://example.test/v1','model':'vision','api_key':'secret'})

    def test_selected_provider_routes_both_existing_and_batch_ocr(self):
        from PIL import Image
        buf=io.BytesIO(); Image.new('RGB',(100,50),'white').save(buf,'PNG')
        task=self.client.post('/api/tasks',json={'name':'test','questions':[{'number':1,'kind':'word','answer':'receive','points':1}]},headers=self.headers).json
        sid=self.client.post(f"/api/tasks/{task['id']}/submissions",json={'student_code':'001'},headers=self.headers).json['id']
        uploaded=self.client.post(f'/api/submissions/{sid}/pages',data={'image':(io.BytesIO(buf.getvalue()),'p.png')},headers=self.headers)
        uploaded.request.environ['wsgi.input'].close()
        self.client.patch('/api/settings',json={'provider':'openai_compatible','base_url':'https://example.test/v1','model':'m','api_key':'secret'},headers=self.headers)
        with patch('settings.recognize_openai',return_value={'blocks':[],'model':'cloud-test'}) as cloud, patch('app.recognize') as local:
            result=self.client.post(f"/api/pages/{uploaded.json['id']}/ocr",json={},headers=self.headers)
        self.assertEqual(result.status_code,200)
        cloud.assert_called_once(); local.assert_not_called()
