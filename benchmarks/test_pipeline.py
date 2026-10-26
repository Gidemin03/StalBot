import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from candidate_pipeline import CandidateReader


class PipelineTests(unittest.TestCase):
    def reader(self,text,enabled):
        r=CandidateReader.__new__(CandidateReader)
        r.price_fallback=enabled
        r.engines=SimpleNamespace(tess=SimpleNamespace(read=Mock(return_value=(text,0.9))),
                                  rapid=SimpleNamespace(read=Mock(side_effect=AssertionError('Unexpected fallback'))))
        return r

    def test_valid_price_and_absence_are_not_replaced_by_fallback(self):
        im=np.zeros((25,154,3),dtype='uint8')
        for text in ('500 000 рублей','---'):
            result=self.reader(text,True).read(im,'buyout')
            self.assertEqual(result[0],text)
            self.assertFalse(result[2]['fallback_used'])

    def test_disabled_fallback_leaves_ambiguous_observation_unchanged(self):
        r=self.reader('лей650 000 000 рублей',False)
        result=r.read(np.zeros((25,154,3),dtype='uint8'),'buyout')
        self.assertEqual(result[2]['primary_state'],'AMBIGUOUS')
        self.assertFalse(result[2]['fallback_used'])
