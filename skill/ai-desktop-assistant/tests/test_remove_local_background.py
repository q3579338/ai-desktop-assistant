"""Offline boundary tests; synthetic images/model bytes, never user state."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock
import uuid

import numpy as np
from PIL import Image, PngImagePlugin

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/remove_local_background.py'
spec = importlib.util.spec_from_file_location('background_under_test', SCRIPT)
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)


class FakeSession:
    def __init__(self, result=None):
        self.result = result
        self.input_shape = [1, 3, 320, 320]
        self.providers = ['CPUExecutionProvider']

    def get_inputs(self):
        return [types.SimpleNamespace(name='input.1', type='tensor(float)', shape=self.input_shape)]

    def get_outputs(self):
        return [types.SimpleNamespace(name=str(i), type='tensor(float)', shape=[1, 1, 320, 320]) for i in range(7)]

    def get_providers(self):
        return self.providers

    def run(self, outputs, inputs):
        tensor = inputs['input.1']
        assert tensor.shape == (1, 3, 320, 320) and tensor.dtype == np.float32
        assert np.isfinite(tensor).all()
        self.tensor = tensor
        if self.result is not None:
            return self.result
        pred = np.zeros((1, 1, 320, 320), dtype=np.float32)
        pred[:, :, 70:250, 70:250] = 1
        return [pred] * 7


class RemoveBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp_base = Path(tempfile.gettempdir()).resolve()
        self.root = self.temp_base / ('assistant-bg-test-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / 'source.png'
        values = np.arange(24 * 32 * 3, dtype=np.uint8).reshape(24, 32, 3)
        Image.fromarray(values).save(self.source)
        self.original = self.source.read_bytes()
        self.output = self.root / 'cutout.png'
        self.model = self.root / 'u2netp.onnx'
        self.model_bytes = b'approved-synthetic-model-fixture'
        self.model.write_bytes(self.model_bytes)
        self.patches = [patch.object(bg, 'MODEL_BYTES', len(self.model_bytes)),
                        patch.object(bg, 'MODEL_SHA256', hashlib.sha256(self.model_bytes).hexdigest()),
                        patch.object(bg, 'MODEL_MD5', hashlib.md5(self.model_bytes).hexdigest())]
        for item in self.patches:
            item.start()
        self.session = FakeSession()
        self.loader = patch.object(bg, '_load_session', return_value=(self.session, 'test-runtime'))
        self.load_mock = self.loader.start()

    def tearDown(self):
        self.loader.stop()
        for item in reversed(self.patches):
            item.stop()
        resolved = self.root.resolve()
        assert resolved.parent == self.temp_base and resolved.name.startswith('assistant-bg-test-')
        shutil.rmtree(resolved)

    def run_remove(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            return bg.remove_background(self.source, self.output, self.model)

    def test_rgb_dimensions_hashes_and_source_preserved(self):
        result = self.run_remove()
        self.assertEqual(self.source.read_bytes(), self.original)
        with Image.open(self.source) as a, Image.open(self.output) as b:
            self.assertEqual(b.mode, 'RGBA')
            self.assertEqual(a.size, b.size)
            np.testing.assert_array_equal(np.array(a), np.array(b)[:, :, :3])
            self.assertEqual(b.getchannel('A').getextrema(), (0, 255))
        record = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertEqual(result, record)
        self.assertEqual(result['input_sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(result['output_sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertTrue(result['model_verified'] and result['rgb_preserved'])
        self.assertTrue(result['needs_visual_review'])

    def test_existing_transparent_pixels_remain_transparent(self):
        rgba = np.full((24, 32, 4), 128, dtype=np.uint8)
        rgba[:, :, :3] = [19, 70, 120]
        rgba[:, 15:17, 3] = 0
        Image.fromarray(rgba).save(self.source)
        self.run_remove()
        with Image.open(self.output) as im:
            actual = np.array(im)
        np.testing.assert_array_equal(actual[:, :, :3], rgba[:, :, :3])
        self.assertTrue((actual[:, 15:17, 3] == 0).all())
        self.assertLessEqual(actual[:, :, 3].max(), 128)

    def test_color_profile_and_gamma_preserved_without_source_text(self):
        info = PngImagePlugin.PngInfo()
        info.add(b'gAMA', (45455).to_bytes(4, 'big'))
        info.add_text('private-note', 'must not be copied')
        with Image.open(self.source) as im:
            im.save(self.root / 'profile.png', pnginfo=info, icc_profile=b'fixture-color-profile')
        self.source = self.root / 'profile.png'
        self.run_remove()
        with Image.open(self.output) as im:
            self.assertEqual(im.info.get('icc_profile'), b'fixture-color-profile')
            self.assertEqual(im.info.get('gamma'), .45455)
            self.assertNotIn('private-note', im.info)

    def test_tampered_model_rejected_before_session(self):
        self.model.write_bytes(b'x' + self.model_bytes[1:])
        with self.assertRaises(bg.BackgroundRemovalError):
            self.run_remove()
        self.load_mock.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_bytes(b'user output')
        with self.assertRaises(bg.BackgroundRemovalError):
            self.run_remove()
        self.assertEqual(self.output.read_bytes(), b'user output')
        self.load_mock.assert_not_called()

    def test_existing_report_prevents_output_creation(self):
        report = self.root / 'cutout.background.json'
        report.write_bytes(b'user report')
        with self.assertRaises(bg.BackgroundRemovalError):
            self.run_remove()
        self.assertEqual(report.read_bytes(), b'user report')
        self.assertFalse(self.output.exists())

    def test_input_output_alias_is_rejected(self):
        with self.assertRaises(bg.BackgroundRemovalError):
            bg.remove_background(self.source, self.source, self.model)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_network_paths_and_symlinks_rejected(self):
        for path in ('https://example.org/input.png', r'\\server\share\input.png'):
            with self.assertRaises(bg.BackgroundRemovalError):
                bg.remove_background(path, self.output, self.model)
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(bg.BackgroundRemovalError):
                self.run_remove()

    def test_corrupt_png_is_rejected(self):
        self.source.write_bytes(self.original[:35])
        with self.assertRaises(bg.BackgroundRemovalError):
            self.run_remove()
        self.assertFalse(self.output.exists())

    def test_oversized_dimensions_rejected(self):
        Image.new('RGB', (4097, 1)).save(self.source)
        with self.assertRaises(bg.BackgroundRemovalError):
            self.run_remove()
        self.load_mock.assert_not_called()

    def test_animated_png_rejected(self):
        Image.new('RGBA', (16, 16), 'red').save(self.source, save_all=True,
                                             append_images=[Image.new('RGBA', (16, 16), 'blue')], duration=100)
        with self.assertRaises(bg.BackgroundRemovalError):
            self.run_remove()
        self.load_mock.assert_not_called()

    def test_invalid_and_constant_masks_leave_no_output(self):
        for array in (np.zeros((1, 1, 320, 320), dtype=np.float32),
                      np.full((1, 1, 320, 320), np.nan, dtype=np.float32),
                      np.zeros((1, 1, 12, 12), dtype=np.float32)):
            self.session.result = [array] * 7
            with self.assertRaises(bg.BackgroundRemovalError):
                self.run_remove()
            self.assertFalse(self.output.exists())

    def test_report_race_rolls_back_only_own_output(self):
        original_open = Path.open
        report = self.root / 'cutout.background.json'
        def racing_open(path, mode='r', *args, **kwargs):
            if path == report and mode == 'x':
                with original_open(path, 'wb') as f:
                    f.write(b'concurrent existing report')
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', racing_open):
            with self.assertRaises(bg.BackgroundRemovalError):
                self.run_remove()
        self.assertEqual(report.read_bytes(), b'concurrent existing report')
        self.assertFalse(self.output.exists())

    def test_report_write_failure_leaves_no_completed_record(self):
        report = self.root / 'cutout.background.json'
        original_dump = json.dump
        def failing_dump(value, handle, **kwargs):
            original_dump(value, handle, **kwargs)
            handle.flush()
            raise OSError('simulated failure after completed JSON flush')
        with patch.object(bg.json, 'dump', failing_dump):
            with self.assertRaises(bg.BackgroundRemovalError):
                self.run_remove()
        self.assertFalse(self.output.exists())
        self.assertFalse(report.exists())

    def test_partial_report_write_failure_rolls_back_both_files(self):
        report = self.root / 'cutout.background.json'
        def failing_dump(value, handle, **kwargs):
            handle.write('{\n  "schema": 1,')
            handle.flush()
            raise OSError('simulated disk-full partial JSON')
        with patch.object(bg.json, 'dump', failing_dump):
            with self.assertRaises(bg.BackgroundRemovalError):
                self.run_remove()
        self.assertFalse(self.output.exists())
        self.assertFalse(report.exists())

    def test_rollback_preserves_replaced_or_changed_report(self):
        report = self.root / 'cutout.background.json'
        report.write_bytes(b'other process report')
        current = report.stat()
        identity = (current.st_dev, current.st_ino)
        bg._rollback_created_file(report, identity, b'{"status": "completed"}')
        self.assertEqual(report.read_bytes(), b'other process report')
        bg._rollback_created_file(report, (identity[0], identity[1] + 1), b'other process report')
        self.assertTrue(report.exists())

    def test_session_enforces_cpu_fixed_shapes_and_telemetry_off(self):
        self.loader.stop()
        ort = types.SimpleNamespace(SessionOptions=lambda: types.SimpleNamespace(),
                                    ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL='sequential'),
                                    disable_telemetry_events=Mock(), __version__='test-runtime',
                                    InferenceSession=Mock(return_value=self.session))
        with patch.dict(sys.modules, {'onnxruntime': ort}):
            session, _ = bg._load_session(self.model_bytes)
            self.assertIs(session, self.session)
            ort.disable_telemetry_events.assert_called_once()
            self.assertEqual(ort.InferenceSession.call_args.kwargs['providers'], ['CPUExecutionProvider'])
            self.assertEqual(ort.InferenceSession.call_args.args[0], self.model_bytes)
            self.session.input_shape = [None, 3, 320, 320]
            with self.assertRaises(bg.BackgroundRemovalError):
                bg._load_session(self.model_bytes)
            self.session.input_shape = [1, 3, 320, 320]
            self.session.providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
            with self.assertRaises(bg.BackgroundRemovalError):
                bg._load_session(self.model_bytes)
        self.loader.start()


if __name__ == '__main__':
    unittest.main()
