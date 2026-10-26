import unittest
import numpy as np
from optimization_capture import bgra_to_rgb


class CaptureConversionTests(unittest.TestCase):
    def test_top_down_bgra_rgb_detached(self):
        raw = np.array([3, 2, 1, 255, 6, 5, 4, 0, 9, 8, 7, 22, 12, 11, 10, 34], dtype=np.uint8)
        result = bgra_to_rgb(raw, 2, 2)
        np.testing.assert_array_equal(result, [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]])
        raw[:] = 0
        self.assertEqual(result[1, 1].tolist(), [10, 11, 12])
        self.assertTrue(result.flags.c_contiguous)


if __name__ == '__main__':
    unittest.main()
