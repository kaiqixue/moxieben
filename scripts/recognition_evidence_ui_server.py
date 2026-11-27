"""Synthetic conflicting OCR readings. No real photos or cloud requests."""
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_ocr_alignment import OCRAlignmentTests

case=OCRAlignmentTests()
case.setUpClass()
case.setUp()
case.app.config['GRADING_JOBS_AUTOSTART']=True
sid,page,blocks=case.setup_grammar(missing=True)
blocks[2]['text']='They (2) a kind of code that special rights be given today.'
case.seed(sid,page['id'],blocks,'done')
crop={'source':'automatic_region_supplement','region_strategy':3,'question_numbers':[1],
      'blocks':[{'id':0,'text':'(1) why Western societies are known for this.',
                 'box':[200,100,400,200],'approximate':True},
                {'id':1,'text':'They (2) demand that special rights be given today.',
                 'box':[200,400,500,480],'approximate':True}]}
with closing(sqlite3.connect(Path(case.tmp.name)/'dictation.sqlite3')) as conn,conn:
    conn.execute('UPDATE pages SET region_supplement_attempted=1 WHERE id=?',(page['id'],))
    conn.execute('INSERT INTO ocr_runs(page_id,created_at,success,result) VALUES(?,?,1,?)',
                 (page['id'],'now',json.dumps(crop)))
with patch('settings.recognize_selected') as cloud:
    result=case.post(f'/api/submissions/{sid}/auto-grade',{'reextract':True})
    assert result.status_code==200,result.json
    cloud.assert_not_called()
case.client.patch('/api/settings/ai-review',json=dict(enabled=True,limit=8,
    base_url='https://vision.example.test/v1',model='synthetic-vision',api_key='synthetic-key'),headers=case.headers)
with patch('settings.visual_review_config',return_value={'model':'synthetic-vision'}), \
     patch('settings.reread_page',return_value=[dict(number=2,status='readable',transcript='demand')]), \
     patch('settings.reread_handwriting',return_value={'status':'readable','transcript':'demand'}):
    case.app.run(host='127.0.0.1',port=int(os.environ.get('RECOGNITION_TEST_PORT','5033')),debug=False,use_reloader=False)
