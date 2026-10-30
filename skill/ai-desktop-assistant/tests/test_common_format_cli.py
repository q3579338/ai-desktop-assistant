"""Actual CLI normalization and role reimport for common raster sources."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile
from PIL import Image
from test_build_rolepack import fixture_directory,SKILL,PROJECT

class CommonFormatCLI(unittest.TestCase):
    def run_tool(self,name,*args):
        return subprocess.run([sys.executable,'-X','utf8','-B',str(SKILL/'scripts'/name),*map(str,args)],
            capture_output=True,text=True,encoding='utf-8',timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)

    def test_all_common_formats_actual_builder_roundtrip(self):
        with fixture_directory() as folder:
            for extension,encoding in [('jpg','JPEG'),('jpeg','JPEG'),('webp','WEBP'),('bmp','BMP'),('tif','TIFF'),('tiff','TIFF'),('gif','GIF')]:
                with self.subTest(extension=extension):
                    source=folder/('original.'+extension);Image.new('RGB',(64,80),'orange').save(source,format=encoding)
                    original=source.read_bytes();output=folder/(extension+'.zip')
                    result=self.run_tool('build_rolepack.py','--app-source',PROJECT,'--image',source,'--name','格式测试','--output',output)
                    self.assertEqual(result.returncode,0,result.stderr)
                    value=json.loads(result.stdout);self.assertTrue(value['import_verified']);self.assertEqual(value['original_sha256'],hashlib.sha256(original).hexdigest())
                    with zipfile.ZipFile(output) as archive:
                        data=archive.read('image.png');self.assertEqual(hashlib.sha256(data).hexdigest(),value['source_sha256'])
                    self.assertEqual(source.read_bytes(),original)

    def test_creator_preserves_oriented_jpeg_and_reports_normalized_image(self):
        with fixture_directory() as folder:
            source=folder/'phone.jpg';exif=Image.Exif();exif[274]=6
            Image.new('RGB',(40,80),'red').save(source,exif=exif);raw=source.read_bytes()
            result=self.run_tool('create_assistant.py','--app-source',PROJECT,'--image',source,'--name','方向测试','--output-dir',folder/'result')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            value=json.loads(result.stdout);out=Path(value['output_dir']);self.assertTrue(value['created'])
            self.assertEqual((out/'source.jpg').read_bytes(),raw);self.assertEqual(source.read_bytes(),raw)
            with Image.open(out/'source.png') as image:self.assertEqual(image.size,(80,40))
            self.assertEqual(value['normalization']['sha256'],value['rolepack']['image_sha256'])
            self.assertIn('source.jpg',(out/'README.md').read_text(encoding='utf-8'))

    def test_tiff_creator_preserves_original_alpha_orientation_and_first_frame(self):
        with fixture_directory() as folder:
            for extension in ('tif','tiff'):
                with self.subTest(extension=extension):
                    source=folder/('pages.'+extension);exif=Image.Exif();exif[274]=6
                    Image.new('RGBA',(40,80),(200,80,10,70)).save(source,format='TIFF',exif=exif,
                        save_all=True,append_images=[Image.new('RGBA',(40,80),(10,80,200,120))])
                    original=source.read_bytes()
                    result=self.run_tool('create_assistant.py','--app-source',PROJECT,'--image',source,'--name','多页图片测试','--output-dir',folder/extension)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    value=json.loads(result.stdout);out=Path(value['output_dir'])
                    self.assertTrue(value['created']);self.assertTrue(value['normalization']['first_frame_only'])
                    self.assertEqual(value['normalization']['format'],'tiff')
                    self.assertEqual((out/('source.'+extension)).read_bytes(),original);self.assertEqual(source.read_bytes(),original)
                    with Image.open(out/'source.png') as image:
                        self.assertEqual(image.size,(80,40));self.assertEqual(image.convert('RGBA').getpixel((4,4)),(200,80,10,70))
                    self.assertEqual(value['normalization']['sha256'],value['rolepack']['image_sha256'])

    def test_tiff_signature_with_jpeg_suffix_uses_same_safe_builder_path(self):
        with fixture_directory() as folder:
            source=folder/'renamed.jpg';Image.new('RGB',(40,80),'orange').save(source,format='TIFF')
            original=source.read_bytes();output=folder/'renamed.zip'
            result=self.run_tool('build_rolepack.py','--app-source',PROJECT,'--image',source,'--name','签名测试','--output',output)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            value=json.loads(result.stdout);self.assertTrue(value['import_verified'])
            self.assertEqual(value['normalization']['format'],'tiff');self.assertEqual(source.read_bytes(),original)

    def test_creator_animation_is_first_frame_and_original_survives(self):
        with fixture_directory() as folder:
            source=folder/'animated.gif';Image.new('RGB',(32,32),'red').save(source,save_all=True,append_images=[Image.new('RGB',(32,32),'blue')],duration=80,loop=0)
            raw=source.read_bytes()
            result=self.run_tool('create_assistant.py','--app-source',PROJECT,'--image',source,'--name','首帧测试','--output-dir',folder/'result')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            value=json.loads(result.stdout);out=Path(value['output_dir']);self.assertTrue(value['normalization']['first_frame_only'])
            self.assertEqual((out/'source.gif').read_bytes(),raw)
            with Image.open(out/'source.png') as image:self.assertEqual(image.convert('RGB').getpixel((4,4)),(255,0,0))
            self.assertIn('只取第一帧',(out/'README.md').read_text(encoding='utf-8'))

    def test_generation_receives_normalized_png_not_raw_jpeg_hash(self):
        from unittest.mock import patch
        import test_create_assistant as fixtures
        creator=fixtures.creator
        with fixture_directory() as folder:
            source=folder/'source.jpg';Image.new('RGB',(32,48),'red').save(source)
            with patch.object(creator.generate_local_image,'generate',side_effect=fixtures.CreateAssistantTests.fake_generator) as generator:
                result=creator.create(source,'重绘输入测试',PROJECT,folder/'result',generate=True,
                    comfy_url='http://127.0.0.1:8188',checkpoint='fixture.safetensors',prompt='fixture only')
            self.assertTrue(result['created'],result.get('message'))
            called=Path(generator.call_args.args[0])
            self.assertEqual(called.suffix,'.png')
            self.assertEqual(hashlib.sha256(called.read_bytes()).hexdigest(),result['normalization']['sha256'])
            self.assertNotEqual(result['normalization']['sha256'],result['source']['sha256'])

    def test_corrupt_jpeg_does_not_deliver_a_rolepack(self):
        with fixture_directory() as folder:
            source=folder/'bad.jpg';source.write_bytes(b'not an image')
            result=self.run_tool('create_assistant.py','--app-source',PROJECT,'--image',source,'--name','损坏测试','--output-dir',folder/'result')
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(list((folder/'result').rglob('*.zip')))
            self.assertEqual(source.read_bytes(),b'not an image')

if __name__=='__main__':unittest.main()
