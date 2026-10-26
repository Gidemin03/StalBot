import unittest
import numpy as np
from field_experiments import name_line,quantity_presence,parse_upgrade


class FieldTests(unittest.TestCase):
    def test_upgrade_rejects_truncation_and_unreadable(self):
        self.assertEqual(parse_upgrade('15'),15)
        for raw in ('152','1 5','+15','','16','0','I5'):
            self.assertIsNone(parse_upgrade(raw))

    def test_presence_uses_location_and_height_not_just_brightness(self):
        im=np.zeros((21,37,3),dtype='uint8')
        im[:2,:]=255
        self.assertFalse(quantity_presence(im)[0])
        im[5:17,27:33]=255
        self.assertTrue(quantity_presence(im)[0])

    def test_name_line_removes_lower_isolated_bid_without_cutting_right_edge(self):
        im=np.full((19,213,3),16,dtype='uint8')
        im[3:13,205:212]=255
        im[11:19,170:176]=255
        before=im.copy()
        out=name_line(im)
        self.assertTrue(np.array_equal(im,before))
        self.assertEqual(int(out[5,210,0]),255)
        self.assertLess(int(out[15,173,0]),100)
