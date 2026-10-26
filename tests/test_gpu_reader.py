"""Reader device selection with fake native sessions; no OCR or desktop input."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from stalbot.vision import Reader


class ReaderDeviceTests(unittest.TestCase):
    def setUp(self):
        self.api=Mock()
        self.api.GetUTF8Text.return_value='123 456'
        self.options=lambda: SimpleNamespace(
            intra_op_num_threads=2,inter_op_num_threads=1,
            enable_mem_pattern=True,enable_cpu_mem_arena=True,
            execution_mode='ExecutionMode.ORT_SEQUENTIAL',
            graph_optimization_level='GraphOptimizationLevel.ORT_ENABLE_ALL')
        self.cpu=self.session(['CPUExecutionProvider'])
        self.dml=self.session(['DmlExecutionProvider','CPUExecutionProvider'])
        self.model=SimpleNamespace(text_rec=None)

        def load():
            self.model.text_rec=SimpleNamespace(session=SimpleNamespace(session=self.cpu))

        self.model._load_rec_model=Mock(side_effect=load)
        self.rapid=SimpleNamespace(RapidOCR=Mock(return_value=self.model),
            ModelType=SimpleNamespace(MOBILE='mobile'),OCRVersion=lambda value:value)
        self.ort=SimpleNamespace(
            get_available_providers=Mock(return_value=['DmlExecutionProvider','CPUExecutionProvider']),
            SessionOptions=Mock(side_effect=self.options),
            InferenceSession=Mock(return_value=self.dml),
            ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='ExecutionMode.ORT_SEQUENTIAL'),
            GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL='GraphOptimizationLevel.ORT_ENABLE_ALL'))
        self.tess=SimpleNamespace(PyTessBaseAPI=Mock(return_value=self.api))
        self.modules=patch.dict('sys.modules',{
            'rapidocr':self.rapid,'onnxruntime':self.ort,'tesserocr':self.tess})
        self.modules.start()
        self.files=patch('stalbot.vision.Path.is_file',return_value=True)
        self.files.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.files.stop)

    def session(self,providers):
        session=Mock()
        session._model_path='local-name.onnx'
        session.get_providers.return_value=providers
        session.get_provider_options.return_value={name:{} for name in providers}
        session.get_session_options.side_effect=self.options
        return session

    def test_default_cpu_stays_lazy_and_reports_actual_options_after_loading(self):
        reader=Reader('models')
        self.model._load_rec_model.assert_not_called()
        params=self.rapid.RapidOCR.call_args.kwargs['params']
        self.assertFalse(params['EngineConfig.onnxruntime.use_dml'])
        self.assertEqual(params['EngineConfig.onnxruntime.intra_op_num_threads'],2)
        self.assertTrue(params['Rec.model_path'].endswith('cyrillic_PP-OCRv5_rec_mobile.onnx'))
        before=reader.backend_info()
        self.assertEqual(before['requested_device'],'cpu')
        self.assertFalse(before['name']['loaded'])
        self.assertIsNone(before['name']['session_options'])
        self.model._load_rec_model()
        info=reader.backend_info()
        self.assertEqual(info['name']['providers'],['CPUExecutionProvider'])
        self.assertTrue(info['name']['session_options']['enable_mem_pattern'])
        json.dumps(info)
        self.ort.InferenceSession.assert_not_called()
        reader.close()

    def test_dml_installs_one_safe_session_with_runtime_fallback_disabled(self):
        reader=Reader('models',device='dml')
        self.model._load_rec_model.assert_called_once_with()
        self.ort.InferenceSession.assert_called_once()
        args=self.ort.InferenceSession.call_args
        self.assertEqual(args.args,('local-name.onnx',))
        self.assertEqual(args.kwargs['providers'],['DmlExecutionProvider','CPUExecutionProvider'])
        options=args.kwargs['sess_options']
        self.assertEqual(options.execution_mode,self.ort.ExecutionMode.ORT_SEQUENTIAL)
        self.assertFalse(options.enable_mem_pattern)
        self.assertEqual(options.intra_op_num_threads,2)
        self.assertEqual(options.inter_op_num_threads,1)
        self.dml.disable_fallback.assert_called_once_with()
        self.assertIs(self.model.text_rec.session.session,self.dml)
        self.dml.get_session_options.return_value=options
        self.dml.get_session_options.side_effect=None
        info=reader.backend_info()
        self.assertEqual(info['name']['session_options']['execution_mode'],'ORT_SEQUENTIAL')
        self.assertFalse(info['name']['session_options']['enable_mem_pattern'])
        self.assertFalse(info['name']['runtime_provider_fallback'])
        self.assertEqual(info['numbers']['device'],'cpu')
        json.dumps(info)
        reader.close()

    def test_dml_unavailable_never_constructs_cpu_substitute(self):
        self.ort.get_available_providers.return_value=['CPUExecutionProvider']
        with self.assertRaisesRegex(RuntimeError,'DmlExecutionProvider'):
            Reader('models',device='dml')
        self.rapid.RapidOCR.assert_not_called()
        self.tess.PyTessBaseAPI.assert_not_called()

    def test_dml_provider_dropped_or_wrong_priority_is_error(self):
        for providers in ([],['CPUExecutionProvider'],['CPUExecutionProvider','DmlExecutionProvider']):
            with self.subTest(providers=providers):
                self.dml.get_providers.return_value=providers
                reader=Reader.__new__(Reader)
                with self.assertRaisesRegex(RuntimeError,'GPU OCR'):
                    reader.__init__('models',device='dml')
                self.assertIsNone(reader.api)
                self.assertIsNone(reader.model)
        self.tess.PyTessBaseAPI.assert_not_called()

    def test_dml_creation_error_releases_bootstrap_model(self):
        self.ort.InferenceSession.side_effect=RuntimeError('device failed')
        reader=Reader.__new__(Reader)
        with self.assertRaisesRegex(RuntimeError,'device failed'):
            reader.__init__('models',device='dml')
        self.assertIsNone(reader.model)
        self.assertIsNone(reader.api)

    def test_native_configuration_failure_ends_api_and_releases_model(self):
        self.api.SetVariable.side_effect=RuntimeError('native configuration failed')
        reader=Reader.__new__(Reader)
        with self.assertRaisesRegex(RuntimeError,'native configuration failed'):
            reader.__init__('models',device='dml')
        self.api.End.assert_called_once_with()
        self.assertIsNone(reader.model)
        self.assertIsNone(reader.api)

    def test_native_numbers_use_same_cpu_path_for_both_devices(self):
        image=np.full((25,154,3),200,dtype=np.uint8)
        for device in ('cpu','dml'):
            with self.subTest(device=device):
                reader=Reader('models',device=device)
                result=reader.number(image,'buyout')
                self.assertEqual(result.value,123456)
                self.api.SetPageSegMode.assert_called_with(7)
                self.api.SetVariable.assert_called_with('tessedit_char_whitelist','')
                reader.close()

    def test_close_releases_model_even_when_native_end_fails(self):
        reader=Reader('models',device='dml')
        self.api.End.side_effect=RuntimeError('native close failed')
        with self.assertRaisesRegex(RuntimeError,'native close failed'):
            reader.close()
        self.assertIsNone(reader.api)
        self.assertIsNone(reader.model)
        reader.close()
        self.api.End.assert_called_once_with()

    def test_backend_info_and_close_enforce_owner_thread(self):
        reader=Reader('models',device='dml')
        with ThreadPoolExecutor(max_workers=1) as executor:
            for method in (reader.backend_info,reader.close):
                with self.assertRaisesRegex(RuntimeError,'different thread'):
                    executor.submit(method).result(timeout=5)
        self.assertIsNotNone(reader.model)
        reader.close()

    def test_invalid_device_rejected_before_native_initialization(self):
        for device in ('gpu','cuda','CPU',None):
            with self.subTest(device=device),self.assertRaises(ValueError):
                Reader('models',device=device)
        self.rapid.RapidOCR.assert_not_called()
        self.tess.PyTessBaseAPI.assert_not_called()


if __name__=='__main__':
    unittest.main()
