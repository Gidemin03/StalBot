import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from fractions import Fraction
import numpy as np
from stalbot.domain import Catalog,Identity,Number,Rule
from stalbot.vision import BOXES,STEP
from tools.validate_bot import check_numeric_coverage,selection_oracle,grouped_replay,check_grouped_replays


class NumericCoverageTests(unittest.TestCase):
    def setUp(self):
        self.baseline=json.loads((Path(__file__).resolve().parents[1]/'validation/baseline.json').read_text(encoding='utf-8'))
        self.output={'labels_sha256':self.baseline['labels_sha256'],'rows':170,'numeric':{}}
        for field,rule in self.baseline['numeric'].items():
            self.output['numeric'][field]={'value_n':rule['value_n'],**{
                metric:rule['minimum_'+metric] for metric in ('value_correct','correct','semantic_correct')}}

    def test_baseline_passes_without_mutation(self):
        before=copy.deepcopy(self.output)
        check_numeric_coverage(self.output,self.baseline)
        self.assertEqual(before,self.output)

    def test_full_numeric_abstention_fails_even_without_dangerous_errors(self):
        for field in ('quantity','buyout'):
            with self.subTest(field=field):
                output=copy.deepcopy(self.output)
                output['numeric'][field].update(value_correct=0,correct=0,dangerous=0)
                with self.assertRaises(AssertionError):check_numeric_coverage(output,self.baseline)

    def test_semantic_quantity_does_not_replace_visible_digit_coverage(self):
        self.output['numeric']['quantity'].update(value_correct=52,semantic_correct=170)
        with self.assertRaises(AssertionError):check_numeric_coverage(self.output,self.baseline)

    def test_dataset_change_requires_explicit_baseline(self):
        self.output['labels_sha256']='different'
        with self.assertRaises(ValueError):check_numeric_coverage(self.output,self.baseline)

    def test_wrong_value_denominator_fails(self):
        self.output['numeric']['buyout']['value_n']=152
        with self.assertRaises(AssertionError):check_numeric_coverage(self.output,self.baseline)


class GroupedReplayTests(unittest.TestCase):
    def test_upgrade_ranges_match_oracle_and_cached_scanner(self):
        frame,observations,catalog=self.fixture()
        catalog=Catalog([replace(rule,upgrade=10,upgrade_max=13) for rule in catalog.rules])
        for item in observations:item['identity']=replace(item['identity'],upgrade=12)
        observations[0]['identity']=replace(observations[0]['identity'],upgrade=14)
        observations[1]['identity']=replace(observations[1]['identity'],upgrade=None)
        observations[2]['identity']=replace(observations[2]['identity'],upgrade=13)
        self.assertEqual(selection_oracle(observations,catalog,1000,1,'profit')[0],2)
        check_grouped_replays([{'source':'ranges','live_group1_agreement':True,
                               'strategies':grouped_replay(frame,observations,catalog,1000)}])

    def fixture(self):
        frame=np.zeros((1080,1920,3),dtype=np.uint8)
        observations=[];rules=[]
        for row in range(9):
            for x1,y1,x2,y2 in BOXES.values():frame[y1+STEP*row:y2+STEP*row,x1:x2]=row+1
            identity=Identity('Item '+str(row),'white',0)
            observations.append({'identity':identity,'quantity':Number('AMBIGUOUS',None),
                                 'buyout':Number('VALUE',100+row)})
            rules.append(Rule(identity.name,1000,1000+row*100,'white',0))
        return frame,observations,Catalog(rules)

    def test_oracle_first_eligible_group_and_independent_metrics(self):
        _,observations,catalog=self.fixture()
        observations[0]['buyout']=Number('ABSENT',None)
        observations[1]['buyout']=Number('VALUE',200)
        observations[2]['buyout']=Number('VALUE',100)
        for metric in ('profit','roi'):
            with self.subTest(metric=metric):
                row,end,eligible=selection_oracle(observations,catalog,1000,3,metric)
                self.assertEqual((row,end),(2,3))
                self.assertNotIn(0,eligible)
                self.assertEqual(eligible[2]['quantity'],1)
                self.assertEqual(eligible[2]['profit'],Fraction(1040))
        self.assertEqual(selection_oracle(observations,catalog,1000,1,'profit')[:2],(1,2))
        self.assertEqual(selection_oracle(observations,catalog,1000,9,'profit')[:2],(8,9))

    def test_oracle_uses_exact_roi_and_upper_row_on_ties(self):
        _,observations,_=self.fixture()
        # Both displayed ROI values round to 90.00%; row 1 has the larger exact ratio.
        catalog=Catalog([Rule('Item '+str(r),100_000_000,20_000_000,'white',0) for r in range(9)])
        for o in observations:o['buyout']=Number('VALUE',10_000_001)
        observations[1]['buyout']=Number('VALUE',10_000_000)
        self.assertEqual(selection_oracle(observations,catalog,100_000_000,3,'roi')[:2],(1,3))
        observations[0]['buyout']=Number('VALUE',10_000_000)
        self.assertEqual(selection_oracle(observations,catalog,100_000_000,3,'roi')[:2],(0,3))

    def test_oracle_profit_and_roi_can_choose_different_rows(self):
        _,observations,_=self.fixture()
        catalog=Catalog([Rule('Item '+str(r),1000,1000 if r==0 else 300,'white',0) for r in range(9)])
        observations[0]['buyout']=Number('VALUE',500)
        observations[1]['buyout']=Number('VALUE',100)
        observations[2]['buyout']=Number('ABSENT',None)
        self.assertEqual(selection_oracle(observations,catalog,1000,3,'profit')[:2],(0,3))
        self.assertEqual(selection_oracle(observations,catalog,1000,3,'roi')[:2],(1,3))

    def test_grouped_cached_replay_and_report_guards(self):
        frame,observations,catalog=self.fixture()
        report=[{'source':'synthetic','live_group1_agreement':True,
                 'strategies':grouped_replay(frame,observations,catalog,1000)}]
        check_grouped_replays(report)
        for key in ('oracle_choice','group_boundary','eligibility','eligible_fields'):
            with self.subTest(key=key):
                broken=copy.deepcopy(report)
                broken[0]['strategies'][0]['checks'][0][key]=False
                with self.assertRaises(AssertionError):check_grouped_replays(broken)
        broken=copy.deepcopy(report)
        broken[0]['strategies'][0]['batch_agreement']=False
        with self.assertRaises(AssertionError):check_grouped_replays(broken)
        broken=copy.deepcopy(report)
        broken[0]['strategies'].pop()
        with self.assertRaises(AssertionError):check_grouped_replays(broken)

    def test_truncated_prefix_in_oracle_and_cached_scanner(self):
        frame,observations,catalog=self.fixture()
        observations[0]['identity']=Identity('Item 0','white',0,truncated=True)
        self.assertEqual(selection_oracle(observations,catalog,1000,1,'profit')[0],0)
        check_grouped_replays([{'source':'truncated','live_group1_agreement':True,
                               'strategies':grouped_replay(frame,observations,catalog,1000)}])
        ambiguous=Catalog([*catalog.rules,Rule('Item 0 other',1000,1000,'white',0)])
        self.assertNotIn(0,selection_oracle(observations,ambiguous,1000,1,'profit')[2])


if __name__=='__main__':unittest.main()
