import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stalbot.ocr_session import OcrSession
from stalbot.vision import Reader, quantity_presence


class FakeReader:
    def __init__(self, events, fail_warmup=False, fail_close=False):
        self.events = events
        self.owner = threading.get_ident()
        self.fail_warmup = fail_warmup
        self.fail_close = fail_close
        self.events.append(('create', self.owner))

    def warmup(self, batch_size):
        assert self.owner == threading.get_ident()
        self.events.append(('warmup', batch_size, self.owner))
        if self.fail_warmup:
            raise RuntimeError('warmup failed')
        return 1.0

    def close(self):
        assert self.owner == threading.get_ident()
        self.events.append(('close', self.owner))
        if self.fail_close:
            raise RuntimeError('close failed')


class OcrSessionTests(unittest.TestCase):
    def test_device_switch_closes_previous_before_create_and_reuses_same_key(self):
        session=self.make_session()
        def get(key):
            return session.submit(lambda current:current.get_reader(9,key=key)).result(timeout=5)
        first,_=get('cpu')
        same,metadata=get('cpu')
        self.assertIs(first,same);self.assertFalse(metadata.created)
        gpu,metadata=get('dml')
        self.assertIsNot(first,gpu);self.assertTrue(metadata.created);self.assertTrue(metadata.warmed)
        cpu,_=get('cpu')
        self.assertIsNot(first,cpu)
        self.assertEqual([event[0] for event in self.events],
            ['create','warmup','close','create','warmup','close','create','warmup'])
        self.assertEqual(len({event[-1] for event in self.events}),1)

    def test_failed_backend_switch_does_not_reuse_old_reader_or_silently_fallback(self):
        session=self.make_session()
        session.submit(lambda current:current.get_reader(1,key='cpu')).result(timeout=5)
        def unavailable():raise RuntimeError('DirectML unavailable')
        with self.assertRaisesRegex(RuntimeError,'DirectML unavailable'):
            session.submit(lambda current:current.get_reader(9,key='dml',reader_factory=unavailable)).result(timeout=5)
        self.assertEqual([event[0] for event in self.events],['create','warmup','close'])
        _,metadata=session.submit(lambda current:current.get_reader(1,key='cpu')).result(timeout=5)
        self.assertTrue(metadata.created);self.assertTrue(metadata.warmed)

    def make_session(self, factory=None):
        self.events = []
        session = OcrSession(factory or (lambda: FakeReader(self.events)))
        self.addCleanup(lambda: session.close_async().result(timeout=5))
        return session

    def test_prepares_before_work_reuses_reader_and_owner_between_runs(self):
        session = self.make_session()

        def run(current):
            reader, metadata = current.get_reader(1)
            self.events.append(('work', threading.get_ident()))
            return reader, metadata

        first, initial = session.submit(run).result(timeout=5)
        second, reused = session.submit(run).result(timeout=5)
        self.assertIs(first, second)
        self.assertTrue(initial.created)
        self.assertTrue(initial.warmed)
        self.assertGreaterEqual(initial.duration_ms, initial.load_ms + initial.warmup_ms)
        self.assertFalse(reused.created)
        self.assertFalse(reused.warmed)
        self.assertEqual(reused.load_ms, 0)
        self.assertEqual(reused.warmup_ms, 0)
        self.assertEqual([event[0] for event in self.events], ['create', 'warmup', 'work', 'work'])
        self.assertNotEqual(first.owner, threading.get_ident())
        self.assertEqual({event[-1] for event in self.events}, {first.owner})

    def test_batch_changes_prepare_once_per_size(self):
        session = self.make_session()
        readers = []
        for batch in (1, 3, 9, 1, 3, 9):
            reader, _ = session.submit(lambda current: current.get_reader(batch)).result(timeout=5)
            readers.append(reader)
        self.assertTrue(all(reader is readers[0] for reader in readers))
        self.assertEqual([event[1] for event in self.events if event[0] == 'warmup'], [1, 3, 9])

    def test_owner_check_and_invalid_batch_do_not_create_reader(self):
        session = self.make_session()
        with self.assertRaisesRegex(RuntimeError, 'different thread'):
            session.get_reader()
        with self.assertRaises(ValueError):
            session.submit(lambda current: current.get_reader(2)).result(timeout=5)
        self.assertEqual(self.events, [])

    def test_callback_failure_retains_prepared_reader(self):
        session = self.make_session()
        reader, _ = session.submit(lambda current: current.get_reader()).result(timeout=5)

        def failed_run(current):
            current.get_reader()
            raise RuntimeError('work failed')

        with self.assertRaisesRegex(RuntimeError, 'work failed'):
            session.submit(failed_run).result(timeout=5)
        reused, _ = session.submit(lambda current: current.get_reader()).result(timeout=5)
        self.assertIs(reader, reused)
        self.assertFalse(any(event[0] == 'close' for event in self.events))

    def test_failed_warmup_closes_reader_and_next_run_retries(self):
        attempts = []

        def factory():
            reader = FakeReader(self.events, fail_warmup=not attempts)
            attempts.append(reader)
            return reader

        session = self.make_session(factory)
        with self.assertRaisesRegex(RuntimeError, 'warmup failed'):
            session.submit(lambda current: current.get_reader()).result(timeout=5)
        reader, metadata = session.submit(lambda current: current.get_reader()).result(timeout=5)
        self.assertIs(reader, attempts[1])
        self.assertTrue(metadata.created)
        self.assertEqual([event[0] for event in self.events], ['create', 'warmup', 'close', 'create', 'warmup'])

    def test_failed_new_batch_discards_previously_prepared_reader(self):
        session = self.make_session()
        reader, _ = session.submit(lambda current: current.get_reader()).result(timeout=5)
        reader.fail_warmup = True
        with self.assertRaisesRegex(RuntimeError, 'warmup failed'):
            session.submit(lambda current: current.get_reader(3)).result(timeout=5)
        replacement, metadata = session.submit(lambda current: current.get_reader()).result(timeout=5)
        self.assertIsNot(reader, replacement)
        self.assertTrue(metadata.created)
        self.assertTrue(metadata.warmed)

    def test_factory_failure_can_be_retried(self):
        calls = []

        def factory():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('create failed')
            return FakeReader(self.events)

        session = self.make_session(factory)
        with self.assertRaisesRegex(RuntimeError, 'create failed'):
            session.submit(lambda current: current.get_reader()).result(timeout=5)
        _, metadata = session.submit(lambda current: current.get_reader()).result(timeout=5)
        self.assertTrue(metadata.created)

    def test_shutdown_is_async_idempotent_and_runs_after_pending_work(self):
        session = self.make_session()
        entered, release = threading.Event(), threading.Event()

        def run(current):
            current.get_reader()
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            self.events.append(('work', threading.get_ident()))

        work = session.submit(run)
        self.assertTrue(entered.wait(timeout=5))
        queued = session.submit(lambda current: self.events.append(('queued', threading.get_ident())))
        try:
            closed = session.close_async()
            self.assertIs(session.close_async(), closed)
            self.assertFalse(closed.done())
            self.assertFalse(closed.cancel())
            with self.assertRaisesRegex(RuntimeError, 'closing'):
                session.submit(lambda current: None)
        finally:
            release.set()
        work.result(timeout=5)
        queued.result(timeout=5)
        closed.result(timeout=5)
        self.assertEqual([event[0] for event in self.events], ['create', 'warmup', 'work', 'queued', 'close'])
        self.assertEqual(len({event[-1] for event in self.events}), 1)

    def test_close_without_reader_and_close_failure_are_reported(self):
        empty = OcrSession(lambda: self.fail('unexpected reader creation'))
        empty.close_async().result(timeout=5)
        events = []
        session = OcrSession(lambda: FakeReader(events, fail_close=True))
        session.submit(lambda current: current.get_reader()).result(timeout=5)
        closed = session.close_async()
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            closed.result(timeout=5)
        self.assertIs(session.close_async(), closed)


class ReaderWarmupTests(unittest.TestCase):
    def make_reader(self):
        reader = Reader.__new__(Reader)
        reader.owner = threading.get_ident()
        reader._warmed_batches = set()
        reader._numbers_warmed = False
        reader.names = Mock()
        reader.number = Mock()
        return reader

    def test_uses_synthetic_crops_and_warms_numbers_once(self):
        reader = self.make_reader()
        self.assertGreaterEqual(reader.warmup(3), 0)
        images, batch = reader.names.call_args.args
        self.assertEqual(batch, 3)
        self.assertEqual(len(images), 3)
        self.assertTrue(all(image.shape == (19, 213, 3) for image in images))
        quantity, buyout = reader.number.call_args_list
        self.assertEqual(quantity.args[1], 'quantity')
        self.assertTrue(quantity_presence(quantity.args[0]))
        self.assertEqual(buyout.args[1], 'buyout')
        self.assertEqual(reader.warmup(3), 0)
        reader.warmup(1)
        self.assertEqual(reader.names.call_count, 2)
        self.assertEqual(reader.number.call_count, 2)

    def test_failed_inference_is_not_marked_warm(self):
        reader = self.make_reader()
        reader.names.side_effect = RuntimeError('inference failed')
        with self.assertRaisesRegex(RuntimeError, 'inference failed'):
            reader.warmup()
        self.assertEqual(reader._warmed_batches, set())
        reader.number.assert_not_called()
        reader.names.side_effect = None
        reader.warmup()
        self.assertEqual(reader._warmed_batches, {1})

    def test_thread_and_batch_checked_even_after_warmup(self):
        reader = self.make_reader()
        reader.warmup()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.assertRaisesRegex(RuntimeError, 'different thread'):
                executor.submit(reader.warmup).result(timeout=5)
        with self.assertRaises(ValueError):
            reader.warmup(2)
        self.assertEqual(reader.names.call_count, 1)

    def test_warmup_triggers_lazy_name_model_and_both_native_numeric_paths(self):
        reader = Reader.__new__(Reader)
        reader.owner = threading.get_ident()
        reader._warmed_batches = set()
        reader._numbers_warmed = False
        events = []

        def recognize(inputs):
            events.append(('recognize', len(inputs.img)))
            return SimpleNamespace(txts=['Warmup'] * len(inputs.img))

        class LazyModel:
            text_rec = None

            def __call__(self, image, **kwargs):
                events.append(('initialize', image.shape, kwargs))
                self.text_rec = Mock(side_effect=recognize)

        reader.model = LazyModel()
        reader.api = Mock()
        reader.api.GetUTF8Text.side_effect = ['1', '123 456']
        typings = SimpleNamespace(TextRecInput=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict('sys.modules', {'rapidocr.ch_ppocr_rec.typings': typings}):
            reader.warmup(9)
            reader.warmup(9)
        self.assertEqual([event[0] for event in events], ['initialize', 'recognize'])
        self.assertEqual(events[1], ('recognize', 9))
        self.assertEqual(events[0][2], dict(use_det=False, use_cls=False, use_rec=True))
        self.assertEqual(reader.model.text_rec.rec_batch_num, 9)
        self.assertEqual([call.args[0] for call in reader.api.SetPageSegMode.call_args_list], [8, 7])
        self.assertEqual(reader.api.SetImage.call_count, 2)
        self.assertEqual(reader.api.GetUTF8Text.call_count, 2)

    def test_numeric_failure_does_not_mark_reader_warm(self):
        reader = self.make_reader()
        reader.number.side_effect = RuntimeError('numeric failed')
        with self.assertRaisesRegex(RuntimeError, 'numeric failed'):
            reader.warmup()
        self.assertFalse(reader._numbers_warmed)
        self.assertEqual(reader._warmed_batches, set())
        reader.number.side_effect = None
        reader.warmup()
        self.assertTrue(reader._numbers_warmed)
        self.assertEqual(reader._warmed_batches, {1})

    def test_constructor_failure_releases_partial_native_state(self):
        api = Mock()
        api.SetVariable.side_effect = RuntimeError('configuration failed')
        rapidocr = SimpleNamespace(RapidOCR=Mock(), ModelType=SimpleNamespace(MOBILE='mobile'),
                                   OCRVersion=lambda value: value)
        tesserocr = SimpleNamespace(PyTessBaseAPI=Mock(return_value=api))
        with patch.dict('sys.modules', {'rapidocr': rapidocr, 'tesserocr': tesserocr}), \
                patch('stalbot.vision.Path.is_file', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'configuration failed'):
                Reader('unused-model-path')
        api.End.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
