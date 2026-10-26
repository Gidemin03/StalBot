"""Offline modal recognition; no desktop access or input."""
from itertools import combinations
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from stalbot.interface import Interface


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'validation/insufficient-20261015'
BOX = (802, 506, 1120, 530)
OUTCOMES = {'success': 'SUCCESS', 'unavailable': 'UNAVAILABLE',
            'insufficient': 'INSUFFICIENT'}


class InsufficientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = Interface(ROOT / 'assets/ui')
        cls.source = np.array(Image.open(EVIDENCE / 'source.png').convert('RGB'))

    def test_template_is_exact_source_crop_and_source_keeps_dimensions(self):
        self.assertEqual(self.source.shape, (1079, 1915, 3))
        x1, y1, x2, y2 = BOX
        np.testing.assert_array_equal(self.ui.templates['insufficient'],
                                      self.source[y1:y2, x1:x2])

    def test_source_and_small_translations(self):
        for dx, dy in ((0, 0), (-8, -8), (-8, 8), (8, -8), (8, 8)):
            with self.subTest(dx=dx, dy=dy):
                frame = np.zeros_like(self.source)
                height, width = frame.shape[:2]
                sx, sy = max(0, -dx), max(0, -dy)
                tx, ty = max(0, dx), max(0, dy)
                w, h = width - abs(dx), height - abs(dy)
                frame[ty:ty+h, tx:tx+w] = self.source[sy:sy+h, sx:sx+w]
                self.assertEqual(self.ui.outcome(frame), 'INSUFFICIENT')

    def test_existing_screenshots_preserve_outcomes(self):
        for path in sorted((ROOT / 'dataset/templates').glob('*.png')):
            with self.subTest(path=path.name):
                frame = np.array(Image.open(path).convert('RGB'))
                expected = ('SUCCESS' if path.name == 'Скриншот успешной покупки.png'
                            else 'UNAVAILABLE' if path.name ==
                            'Скриншот уведомления о том что предмет выкуплен.png'
                            else None)
                self.assertLess(self.ui.score(frame, 'insufficient',
                                              (730, 430, 1190, 650)), .95)
                self.assertEqual(self.ui.outcome(frame), expected)

    def test_generic_error_title_and_ok_button_are_not_enough(self):
        frame = self.source.copy()
        x1, y1, x2, y2 = BOX
        frame[y1:y2, x1:x2] = self.source[490, 780]
        self.assertIsNone(self.ui.outcome(frame))

    def test_each_outcome_template_and_conflicting_pairs(self):
        for count in (1, 2, 3):
            for names in combinations(OUTCOMES, count):
                with self.subTest(names=names):
                    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
                    y = 440
                    for name in names:
                        template = self.ui.templates[name]
                        h, w = template.shape[:2]
                        frame[y:y+h, 750:750+w] = template
                        y += h + 12
                    self.assertEqual(self.ui.outcome(frame),
                                     OUTCOMES[names[0]] if count == 1 else None)

    def test_existing_threshold_is_preserved(self):
        for score, expected in ((.949999, None), (.95, 'INSUFFICIENT')):
            with patch.object(self.ui, 'score',
                              side_effect=lambda frame, name, box:
                              score if name == 'insufficient' else 0):
                self.assertEqual(self.ui.outcome(self.source), expected)


if __name__ == '__main__':
    unittest.main()
