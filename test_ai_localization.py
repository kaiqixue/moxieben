"""Non-square rotated photos: precise OCR must map back to unchanged original pixels."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from ai_localization import offline_blocks


class LocalizationGeometryTests(unittest.TestCase):
    def test_all_rotations_map_upright_ocr_to_original_without_editing_photo(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic.png'
            Image.new('RGB',(800,1000),'white').save(path)
            original=path.read_bytes()
            for angle,expected in [(0,[100,200,300,250]),(90,[550,100,600,300]),
                                   (180,[500,750,700,800]),(270,[200,700,250,900])]:
                page=dict(id=1,status='done',width=800,height=1000,display_rotation=angle)
                def recognize(crop):
                    with Image.open(crop) as image:
                        self.assertEqual(image.size,(1000,800) if angle in (90,270) else (800,1000))
                    return dict(blocks=[dict(id=0,text='(1) synthetic',box=[100,200,300,250])])
                with patch('ocr_engine.recognize_offline',side_effect=recognize):
                    result=offline_blocks(dict(pages=[page]),{1:path})
                self.assertEqual(result[1][0]['box'],expected)
                self.assertEqual(path.read_bytes(),original)

    def test_approximate_or_invalid_local_boxes_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic.png';Image.new('RGB',(100,100),'white').save(path)
            page=dict(id=1,status='done',width=100,height=100,display_rotation=0)
            for block in [dict(text='test',box=[0,0,101,40]),dict(text='test',box=[0,0,20,40],approximate=True)]:
                with patch('ocr_engine.recognize_offline',return_value=dict(blocks=[dict(block,id=0)])),self.assertRaises(ValueError):
                    offline_blocks(dict(pages=[page]),{1:path})


if __name__ == '__main__':
    unittest.main()
