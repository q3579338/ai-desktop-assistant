"""Real common raster inputs, preserved originals and normalized creation."""
import hashlib
import json
import unittest
from pathlib import Path
import uuid
from contextlib import contextmanager

@contextmanager
def workspace():
    folder=Path(__file__).resolve().parents[1]/"evidence/tests"/("common-image-"+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    yield str(folder)
from PIL import Image
from PySide6.QtGui import QImage
from starpet.image_input import decode_image
from starpet.creation import prepare_creation,load_creation,build_creation_package
from starpet.creation_tools import CreationJob

class CommonImageInputs(unittest.TestCase):
    def test_common_formats_create_recover_and_package(self):
        with workspace() as temp:
            base=Path(temp)
            for ext,fmt in [('jpg','JPEG'),('jpeg','JPEG'),('webp','WEBP'),('bmp','BMP'),('tif','TIFF'),('tiff','TIFF'),('gif','GIF')]:
                with self.subTest(ext=ext):
                    path=base/('original.'+ext)
                    Image.new('RGB',(64,80),'#edaa42').save(path,format=fmt)
                    raw=path.read_bytes()
                    report=prepare_creation({'image':str(path)},base/'output',CreationJob())
                    self.assertEqual(report['status'],'ready',report['message'])
                    self.assertEqual(path.read_bytes(),raw)
                    original=report['input_original']
                    self.assertEqual(Path(original['path']).read_bytes(),raw)
                    self.assertEqual(original['sha256'],hashlib.sha256(raw).hexdigest())
                    checked=load_creation(report['report'])
                    self.assertEqual(checked,report)
                    self.assertTrue(Path(build_creation_package(checked,'测试图片','','gentle')).is_file())
    def test_transparent_webp_keeps_alpha(self):
        with workspace() as temp:
            path=Path(temp)/'transparent.webp'
            Image.new('RGBA',(32,32),(200,80,10,70)).save(path,lossless=True)
            image,info=decode_image(path.read_bytes(),'.webp')
            self.assertEqual(image.pixelColor(1,1).alpha(),70)
    def test_jpeg_orientation_applied(self):
        with workspace() as temp:
            path=Path(temp)/'phone.jpg';exif=Image.Exif();exif[274]=6
            Image.new('RGB',(40,80),'red').save(path,exif=exif)
            image,_=decode_image(path.read_bytes(),'.jpg')
            self.assertEqual((image.width(),image.height()),(80,40))
    def test_animated_first_frame_is_explicit(self):
        with workspace() as temp:
            path=Path(temp)/'animated.gif'
            Image.new('RGB',(32,32),'red').save(path,save_all=True,append_images=[Image.new('RGB',(32,32),'blue')],duration=100,loop=0)
            image,info=decode_image(path.read_bytes(),'.gif')
            self.assertTrue(info['first_frame_only'])
            self.assertEqual(image.pixelColor(5,5).name(),'#ff0000')
    def test_corrupt_unsupported_and_oversized_rejected(self):
        for data,suffix in [(b'invalid','.jpg'),(b'<svg/>','.svg'),(b'x'*(32*1024*1024+1),'.png')]:
            with self.assertRaises(ValueError):decode_image(data,suffix)
    def test_preserved_original_tampering_refuses_recovery(self):
        with workspace() as temp:
            base=Path(temp);path=base/'sample.jpg';Image.new('RGB',(32,32),'red').save(path)
            report=prepare_creation({'image':str(path)},base/'output',CreationJob())
            Path(report['input_original']['path']).write_bytes(b'modified')
            with self.assertRaises(ValueError):load_creation(report['report'])

if __name__=='__main__':unittest.main()
