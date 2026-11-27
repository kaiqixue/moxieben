"""Identity detail geometry uses synthetic ink, without cloud requests."""
import io
import unittest
from PIL import Image, ImageDraw
from identity_review import identity_image


class IdentityCropTests(unittest.TestCase):
    def test_sideways_label_is_upright_and_not_a_page_long_strip(self):
        upright=Image.new('RGB',(1200,800),'white')
        ImageDraw.Draw(upright).rectangle((900,600,1099,639),fill='red')
        for angle,box in [(0,[900,600,1100,640]),(90,[160,900,200,1100]),
                          (180,[100,160,300,200]),(270,[600,100,640,300])]:
            with self.subTest(angle=angle):
                raw=io.BytesIO();upright.rotate(-angle,expand=True).save(raw,'PNG')
                png,crop=identity_image(raw.getvalue(),[{'text':'姓名：测试','box':box}],rotation=angle)
                detail=Image.open(io.BytesIO(png))
                self.assertGreaterEqual(detail.width,detail.height,'Do not shrink a name into a vertical page strip')
                self.assertLess(detail.width,600,'Keep nearby context rather than the entire page width')
                self.assertEqual(detail.getpixel((40,40)),(255,0,0))

    def test_saved_legacy_crop_keeps_the_exact_image_previously_sent(self):
        original=Image.new('RGB',(800,1200),'white')
        ImageDraw.Draw(original).rectangle((20,100,80,400),fill='red')
        raw=io.BytesIO();original.save(raw,'PNG')
        saved={'kind':'identity_labels','box':[0,60,800,500],'rotation':90}
        png,crop=identity_image(raw.getvalue(),[],rotation=90,saved_crop=saved)
        expected=original.crop(saved['box']).rotate(90,expand=True)
        actual=Image.open(io.BytesIO(png))
        self.assertEqual(actual.size,expected.size)
        self.assertEqual(actual.tobytes(),expected.tobytes())


if __name__=='__main__':unittest.main()
