import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from stalbot.balance import BALANCE_BOX, balance_crop, parse_balance, read_balance


class BalanceParsingTests(unittest.TestCase):
    def test_complete_balance_and_zero(self):
        for raw, value in (
            ('Счет: 2 384 070 руб.', 2384070),
            ('Счёт: 0 руб.', 0),
            (' СЧЕТ: 1\u00a0234\u202f567 руб.\n', 1234567),
            ('Счет: 1234567 руб.', 1234567),
            ('Счет: 9 223 372 036 854 775 807 руб.', 2**63-1),
        ):
            with self.subTest(raw=raw):
                result = parse_balance(raw)
                self.assertEqual((result.state, result.value), ('VALUE', value))
                self.assertEqual(result.raw, raw)

    def test_reject_partial_damaged_negative_fractional_or_other_text(self):
        for raw in (
            '', '1234567', '2 384 070 руб.', 'Счет: 123', 'Счет: -- руб.',
            'Счет: -10 руб.', 'Счет: +10 руб.', 'Счет: 1.5 руб.',
            'Счет: 1,500 руб.', 'Счет: 12 34 руб.', 'Счет: 1234 567 руб.',
            'Счет: О руб.', 'Счет: 1О0 руб.', 'Счет: 001 руб.',
            'Счет: 1 руб. 123', 'долг Счет: 1 руб.',
            'Счет: 9 223 372 036 854 775 808 руб.',
            'Счет: '+('9'*5000)+' руб.',
        ):
            with self.subTest(raw=raw[:80]):
                result = parse_balance(raw)
                self.assertEqual((result.state, result.value), ('AMBIGUOUS', None))


class BalanceReaderTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        self.api = Mock()
        self.api.GetUTF8Text.return_value = 'Счет: 2 384 070 руб.\n'
        self.reader = SimpleNamespace(check_thread=Mock(), api=self.api)

    def test_native_and_cached_reader_share_fresh_ocr_without_name_calls(self):
        cached = SimpleNamespace(reader=self.reader, check_thread=Mock())
        for reader in (self.reader, cached):
            self.assertEqual(read_balance(reader, self.frame).value, 2384070)
        self.assertEqual(self.api.GetUTF8Text.call_count, 2)
        self.api.SetPageSegMode.assert_called_with(7)
        self.api.SetVariable.assert_called_with('tessedit_char_whitelist', '')
        image = self.api.SetImage.call_args.args[0]
        x1,y1,x2,y2 = BALANCE_BOX
        self.assertEqual(image.size, (3*(x2-x1), 3*(y2-y1)))
        self.api.GetUTF8Text.return_value = 'Счет: 0 руб.'
        self.assertEqual(read_balance(cached, self.frame).value, 0)

    def test_crop_preserves_pixels_and_does_not_mutate_frame(self):
        x1,y1,x2,y2 = BALANCE_BOX
        self.frame[y1:y2,x1:x2] = (20,40,60)
        cropped = balance_crop(self.frame)
        self.assertTrue(np.all(cropped == (20,40,60)))
        cropped[:] = 0
        self.assertTrue(np.all(self.frame[y1:y2,x1:x2] == (20,40,60)))

    def test_geometry_dtype_and_owner_thread_are_checked_before_ocr(self):
        for frame in (self.frame[:, :1900], self.frame.astype(np.float32)):
            with self.assertRaises(ValueError):
                read_balance(self.reader, frame)
        self.reader.check_thread.side_effect = RuntimeError('wrong thread')
        with self.assertRaisesRegex(RuntimeError, 'wrong thread'):
            read_balance(self.reader, self.frame)
        self.api.GetUTF8Text.assert_not_called()

    def test_closed_reader_and_bad_ocr_do_not_produce_budget(self):
        self.api.GetUTF8Text.return_value = '2 384 070'
        self.assertEqual(read_balance(self.reader, self.frame).state, 'AMBIGUOUS')
        self.reader.api = None
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            read_balance(self.reader, self.frame)


if __name__ == '__main__':
    unittest.main()
