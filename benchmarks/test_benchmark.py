import unittest
import numpy as np
from benchmark import normalize_name,parse_number,score,preprocess,aggregate


class EvaluationTests(unittest.TestCase):
    def test_unicode_names_without_fuzzy_matching(self):
        self.assertEqual(normalize_name('Вихрь| +15'),'Вихрь | +15')
        self.assertEqual(normalize_name('Пластина Ⅳ'),'Пластина IV')
        self.assertNotEqual(normalize_name('Вихрь | +12'),normalize_name('Вихрь | +15'))

    def test_numeric_contamination_is_not_silently_stripped(self):
        self.assertEqual(parse_number('9 999 999 999 рублей','buyout'),('VALUE',9999999999))
        for text in ('лей650 000 000 рублей','9 999 рублей9 999 рублей','', 'рублей', '1O0', '-1', '4 9 рублей', '1 100 00 рублей'):
            self.assertEqual(parse_number(text,'buyout'),('AMBIGUOUS',None))
        self.assertEqual(parse_number('---','buyout'),('ABSENT',None))
        self.assertEqual(parse_number('','quantity'),('AMBIGUOUS',None))

    def test_error_directions_and_unknown_truth(self):
        s=dict(field='quantity',truth=1,original_truth=1,truth_state='ABSENT')
        self.assertTrue(score(s,'8')['dangerous_overquantity'])
        self.assertFalse(score(s,'')['correct'])
        s.update(field='buyout',truth=5700000,original_truth=5700000,truth_state='VALUE')
        result=score(s,'570 000 рублей')
        self.assertTrue(result['dangerous_underprice'])
        self.assertTrue(result['false_price_gate_5m'])
        s.update(truth=None,original_truth=None,truth_state='AMBIGUOUS')
        self.assertTrue(score(s,'123')['unsafe_ambiguous_accept'])
        self.assertTrue(score(s,'')['correct'])

    def test_preprocess_does_not_mutate_original(self):
        image=np.arange(25*154*3,dtype=np.uint8).reshape(25,154,3)
        before=image.copy()
        self.assertEqual(preprocess(image,'buyout','tight3').shape,(75,432,3))
        self.assertTrue(np.array_equal(image,before))

if __name__=='__main__':
    unittest.main()
