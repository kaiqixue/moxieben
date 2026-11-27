"""Paddle Jobs API contract and provider integration; never uses a real token."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
from app import create_app


class PaddleCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'sample.png'
        Image.new('RGB',(640,240),'white').save(self.path)
        self.config={'provider':'paddle_cloud','paddle_url':'https://paddleocr.aistudio-app.com/api/v2/ocr/jobs',
                     'paddle_model':'PaddleOCR-VL-1.6','paddle_token':'test-token-do-not-echo'}

    def response(self, data=None, text=None, status=200):
        response=Mock(); response.status_code=status
        raw=text if text is not None else json.dumps(data)
        response.iter_content.return_value=[raw.encode()]
        response.json.return_value=data
        return response

    def result(self, markdown='1. recieve', parsing=None):
        page={'markdown':{'text':markdown,'images':{'assets/x.png':'https://example.test/image.png'}},'outputImages':{}}
        if parsing is not None: page['prunedResult']={'parsing_res_list':parsing}
        return self.response(text=json.dumps({'result':{'layoutParsingResults':[page]}})+'\n')

    def test_upload_poll_download_and_preserve_spelling_and_regions(self):
        import paddle_cloud
        states=[self.response({'data':{'state':s}}) for s in ('pending','running')]
        states+=[self.response({'data':{'state':'done','resultUrl':{'jsonUrl':'https://example.test/result.jsonl'}}}),
                 self.result(parsing=[{'block_content':'学号：001 姓名：张同学','block_bbox':[0,0,400,50]},
                                      {'block_content':'1. recieve','block_bbox':[0,70,300,120]}])]
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'job-1'}})) as submit, patch('paddle_cloud.requests.get',side_effect=states) as get, patch('paddle_cloud.time.sleep'):
            result=paddle_cloud.recognize(self.path,self.config)
        self.assertEqual([b['text'] for b in result['blocks']],['学号：001 姓名：张同学','1. recieve'])
        self.assertEqual(result['blocks'][1]['box'],[0,70,300,120])
        self.assertIsNone(result['blocks'][1]['confidence'])
        self.assertNotIn('test-token',json.dumps(result))
        self.assertEqual(result['job_id'],'job-1')
        self.assertEqual(submit.call_args.kwargs['data']['model'],'PaddleOCR-VL-1.6')
        self.assertFalse(json.loads(submit.call_args.kwargs['data']['optionalPayload'])['useDocUnwarping'])
        self.assertTrue(json.loads(submit.call_args.kwargs['data']['optionalPayload'])['useDocOrientationClassify'])
        self.assertFalse(submit.call_args.kwargs['allow_redirects'])
        self.assertNotIn('Authorization',get.call_args_list[-1].kwargs.get('headers',{}))
        self.assertEqual(get.call_count,4,'Only download JSONL, never third-party images.')

    def test_markdown_fallback_and_no_silent_empty_on_bad_result(self):
        import paddle_cloud
        done=self.response({'data':{'state':'done','resultUrl':{'jsonUrl':'https://example.test/r'}}})
        for output, expected in [(self.result('1. recieve\n2. went'),['1. recieve','2. went']),
                                 (self.result(''),[])]:
            with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})), patch('paddle_cloud.requests.get',side_effect=[done,output]):
                result=paddle_cloud.recognize(self.path,self.config)
            self.assertEqual([b['text'] for b in result['blocks']],expected)
            if expected: self.assertTrue(result['blocks'][0]['approximate'])
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})), patch('paddle_cloud.requests.get',side_effect=[done,self.response(text='{"wrong":true}')]):
            with self.assertRaisesRegex(ValueError,'结果|格式'):
                paddle_cloud.recognize(self.path,self.config)

    def test_provenance_records_actual_input_and_safe_options(self):
        import hashlib
        import paddle_cloud
        done=self.response({'data':{'state':'done','jobId':'job-1','resultUrl':{'jsonUrl':'https://example.test/r'}}})
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'job-1'}})) as post, patch('paddle_cloud.requests.get',side_effect=[done,self.result('1. recieve')]):
            result=paddle_cloud.recognize(self.path,self.config)
        self.assertEqual(result['input_sha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result['request_options'],json.loads(post.call_args.kwargs['data']['optionalPayload']))
        self.assertNotIn(self.config['paddle_token'],json.dumps(result))

    def test_poll_cannot_accept_result_for_different_job(self):
        import paddle_cloud
        done=self.response({'data':{'state':'done','jobId':'different-job','resultUrl':{'jsonUrl':'https://example.test/r'}}})
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'submitted-job'}})), patch('paddle_cloud.requests.get',return_value=done) as get:
            with self.assertRaisesRegex(ValueError,'任务.*不一致'):
                paddle_cloud.recognize(self.path,self.config)
        self.assertEqual(get.call_count,1, 'Never download a different job result')

    def test_request_recognizes_image_headers_and_keeps_header_text(self):
        import paddle_cloud
        done=self.response({'data':{'state':'done','resultUrl':{'jsonUrl':'https://example.test/r'}}})
        output=self.result('1. recieve', parsing=[
            {'block_label':'header_image','block_content':'张同学','block_bbox':[10,10,200,50]},
            {'block_label':'header','block_content':'学号：001','block_bbox':[210,10,400,50]},
            {'block_label':'text','block_content':'1. recieve','block_bbox':[10,70,400,120]}])
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})) as submit, patch('paddle_cloud.requests.get',side_effect=[done,output]):
            result=paddle_cloud.recognize(self.path,self.config)
        options=json.loads(submit.call_args.kwargs['data']['optionalPayload'])
        self.assertIs(options.get('useOcrForImageBlock'),True)
        self.assertEqual(options.get('markdownIgnoreLabels'),[])
        self.assertEqual([b['text'] for b in result['blocks']],['张同学','学号：001','1. recieve'])
        self.assertEqual(result['blocks'][0]['box'],[10,10,200,50])

    def test_provider_reading_order_is_preserved_instead_of_coordinate_sorting(self):
        import paddle_cloud
        regions = [
            {'block_content':'continuation', 'block_bbox':[400,0,600,100], 'block_order':2},
            {'block_content':'Start (1)', 'block_bbox':[0,100,200,200], 'block_order':1}]
        raw = json.dumps({'result':{'layoutParsingResults':[{'prunedResult':{'parsing_res_list':regions}}]}}).encode()
        result = paddle_cloud._parse_result(raw,640,240)
        self.assertEqual([b['text'] for b in result['blocks']], ['Start (1)','continuation'])
        self.assertEqual([b['reading_order'] for b in result['blocks']], [0,1])
        self.assertEqual(result['raw']['pruned_result']['parsing_res_list'],regions)

    def test_orientation_boxes_map_back_to_original_and_raw_stays_untouched(self):
        import paddle_cloud
        for angle, expected in [(0,[10,20,60,80]),(90,[560,10,620,60]),
                                (180,[580,160,630,220]),(270,[20,180,80,230])]:
            with self.subTest(angle=angle):
                region={'block_content':'(4) why Western societies', 'block_bbox':[10,20,60,80]}
                pruned={'doc_preprocessor_res':{'angle':angle},'parsing_res_list':[region]}
                raw=json.dumps({'result':{'layoutParsingResults':[{'prunedResult':pruned}]}}).encode()
                result=paddle_cloud._parse_result(raw,640,240)
                self.assertEqual(result['blocks'][0]['box'],expected)
                self.assertEqual(result['raw']['pruned_result']['parsing_res_list'][0]['block_bbox'],[10,20,60,80])

    def test_region_request_disables_layout_and_retains_original_input_file(self):
        import paddle_cloud
        done=self.response({'data':{'state':'done','resultUrl':{'jsonUrl':'https://example.test/r'}}})
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})) as submit, patch('paddle_cloud.requests.get',side_effect=[done,self.result('1. why')]):
            paddle_cloud.recognize(self.path,dict(self.config,paddle_layout_detection=False))
        options=json.loads(submit.call_args.kwargs['data']['optionalPayload'])
        self.assertFalse(options['useLayoutDetection'])
        self.assertTrue(options['useDocOrientationClassify'])

    def test_auth_failure_timeout_and_unknown_state_have_safe_errors(self):
        import paddle_cloud
        with patch('paddle_cloud.requests.post',return_value=self.response(status=401)), self.assertRaisesRegex(ValueError,'Token'):
            paddle_cloud.recognize(self.path,self.config)

        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})), patch('paddle_cloud.requests.get',return_value=self.response({'data':{'state':'failed','errorMsg':self.config['paddle_token']}})):
            with self.assertRaises(ValueError) as caught: paddle_cloud.recognize(self.path,self.config)
        self.assertNotIn(self.config['paddle_token'],str(caught.exception))
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})), patch('paddle_cloud.time.monotonic',side_effect=[0,181]), self.assertRaisesRegex(ValueError,'超时'):
            paddle_cloud.recognize(self.path,self.config)
        with patch('paddle_cloud.requests.post',return_value=self.response({'data':{'jobId':'j'}})), patch('paddle_cloud.requests.get',return_value=self.response({'data':{'state':'unexpected'}})), self.assertRaisesRegex(ValueError,'状态'):
            paddle_cloud.recognize(self.path,self.config)

    def test_http_400_reports_safe_provider_reason_without_echoing_response_secrets(self):
        import paddle_cloud
        for code,expected in [(10003,'文件大小'),(10007,'模型'),(10008,'参数'),(11002,'过期')]:
            with self.subTest(code=code):
                response=self.response({'code':code,'msg':'test-token private request'},status=400)
                with self.assertRaisesRegex(ValueError,expected) as caught:paddle_cloud._check(response)
                self.assertIn(str(code),str(caught.exception))
                self.assertNotIn('test-token',str(caught.exception))

    def test_settings_separate_secrets_and_routes_use_paddle(self):
        app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test'})
        client=app.test_client(); headers={'X-CSRF-Token':'test'}
        client.patch('/api/settings',json={'provider':'openai_compatible','base_url':'https://example.test/v1','model':'vision','api_key':'openai-only'},headers=headers)
        saved=client.patch('/api/settings',json=self.config,headers=headers)
        self.assertEqual(saved.status_code,200,saved.get_data(as_text=True))
        self.assertTrue(saved.json['paddle_token_configured']);self.assertTrue(saved.json['api_key_configured'])
        self.assertNotIn(self.config['paddle_token'],saved.get_data(as_text=True))
        client.patch('/api/settings',json={'paddle_model':'PaddleOCR-VL-1.6'},headers=headers)
        self.assertTrue(client.get('/api/settings').json['paddle_token_configured'])
        with patch('paddle_cloud.test_connection',return_value={'ok':True,'message':'图片识别成功'}) as test:
            self.assertEqual(client.post('/api/settings/test',headers=headers).status_code,200)
            test.assert_called_once()
        from settings import recognize_selected
        from contextlib import contextmanager
        import sqlite3
        @contextmanager
        def db():
            conn=sqlite3.connect(Path(self.tmp.name)/'dictation.sqlite3');conn.row_factory=sqlite3.Row
            try: yield conn
            finally: conn.close()
        with patch('paddle_cloud.recognize',return_value={'blocks':[]}) as recognize, patch('settings.recognize_openai') as openai:
            recognize_selected(db,self.path,Mock())
        recognize.assert_called_once();openai.assert_not_called()
        client.patch('/api/settings',json={'provider':'local','paddle_token':''},headers=headers)
        self.assertFalse(client.get('/api/settings').json['paddle_token_configured'])
        self.assertTrue(client.get('/api/settings').json['api_key_configured'])

    def test_existing_page_and_batch_identity_both_call_paddle(self):
        app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'CSRF_TOKEN':'test'})
        client=app.test_client(); headers={'X-CSRF-Token':'test'}
        def post(url, data):
            response=client.post(url,json=data,headers=headers)
            self.assertLess(response.status_code,300,response.get_data(as_text=True))
            return response.json
        client.patch('/api/settings',json=self.config,headers=headers)
        task=post('/api/tasks',{'name':'Paddle test','questions':[{'number':1,'kind':'word','answer':'receive','points':1}]})
        klass=post('/api/classes',{'name':'Test class'})
        post(f"/api/classes/{klass['id']}/members",{'student_code':'001','name':'张同学'})
        assignment=post(f"/api/tasks/{task['id']}/assignments",{'class_ids':[klass['id']]} )['assignments'][0]
        uploaded=client.post(f"/api/assignments/{assignment['id']}/photos",data={'image':(io.BytesIO(self.path.read_bytes()),'p.png')},headers=headers)
        uploaded.request.environ['wsgi.input'].close(); photo=uploaded.json
        result={'blocks':[{'id':0,'text':'学号：001 姓名：张同学','box':[0,0,400,50],'confidence':None}]}
        with patch('paddle_cloud.recognize',return_value=result) as engine:
            identified=post(f"/api/batch-photos/{photo['id']}/identify",{})
            self.assertEqual(identified['suggested_student_id'],assignment['students'][0]['id'])
            linked=post(f"/api/batch-photos/{photo['id']}/confirm",{'assignment_student_id':assignment['students'][0]['id'],'confirmed':True})
            post(f"/api/pages/{linked['page_id']}/ocr",{})
            self.assertEqual(engine.call_count,2)
