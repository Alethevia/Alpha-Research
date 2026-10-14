import copy
import json
import math
from pathlib import Path
import re
import unittest

try:
    from .idea_screen import validate_catalogue, geometric_hurdle, illustrative_friction, unit_examples, make_result, render_report
except ImportError:
    from idea_screen import validate_catalogue, geometric_hurdle, illustrative_friction, unit_examples, make_result, render_report

HERE=Path(__file__).resolve().parent


class IdeaScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cb=(HERE/'idea_catalogue_100.json').read_bytes()
        cls.qb=(HERE/'idea_validation_queue.json').read_bytes()
        cls.c=json.loads(cls.cb)
        cls.q=json.loads(cls.qb)

    def test_complete_catalogue(self):
        self.assertEqual(validate_catalogue(self.c,self.q),{'B':61,'C':27,'P':12})

    def test_duplicate_or_missing_id_rejected(self):
        c=copy.deepcopy(self.c);c['ideas'][1]['id']='I001'
        with self.assertRaises(ValueError):validate_catalogue(c,self.q)

    def test_duplicate_title_rejected(self):
        c=copy.deepcopy(self.c);c['ideas'][1]['title']=c['ideas'][0]['title']
        with self.assertRaises(ValueError):validate_catalogue(c,self.q)

    def test_unknown_source_rejected(self):
        c=copy.deepcopy(self.c);c['ideas'][0]['source_ids'].append('INVENTED')
        with self.assertRaises(ValueError):validate_catalogue(c,self.q)

    def test_priority_card_mismatch_rejected(self):
        q=copy.deepcopy(self.q);q['cards'].pop()
        with self.assertRaises(ValueError):validate_catalogue(self.c,q)

    def test_invented_ready_or_performance_rejected(self):
        q=copy.deepcopy(self.q);q['cards'][0]['performance_ready']=True
        with self.assertRaises(ValueError):validate_catalogue(self.c,q)
        q=copy.deepcopy(self.q);q['new_backtests_run']=100
        with self.assertRaises(ValueError):validate_catalogue(self.c,q)

    def test_growth_reconciles_to_eleven(self):
        for n in (8,12,50,100,250,500,1000):
            r=geometric_hurdle(n)
            self.assertAlmostEqual((1+r['equal_net_equity_growth_per_opportunity'])**n,11,places=10)
        with self.assertRaises(ValueError):geometric_hurdle(0)
        with self.assertRaises(ValueError):geometric_hurdle(True)
        with self.assertRaises(ValueError):geometric_hurdle(8,multiple=math.inf)

    def test_cost_allocation_and_invalid_inputs(self):
        r=illustrative_friction(50)
        self.assertAlmostEqual(r['variable_usd_per_round_trip'],1.94)
        self.assertAlmostEqual(r['total_usd_per_round_trip'],4.34)
        with self.assertRaises(ValueError):illustrative_friction(0)
        with self.assertRaises(ValueError):illustrative_friction(50,slip_bps=-1)

    def test_integer_units_and_unknown_margin_stay_unverified(self):
        a,b,c,d=[x['result'] for x in unit_examples()]
        self.assertEqual(a['provisional_quantity'],'12')
        self.assertEqual(b['provisional_quantity'],'1')
        self.assertEqual(c['provisional_quantity'],'0')
        self.assertIn('minimum_trade_exceeds_planned_risk_budget',c['arithmetic_blockers'])
        self.assertIsNone(d['provisional_quantity'])
        self.assertIn('capital_requirement_unknown',d['arithmetic_blockers'])
        self.assertTrue(all(x['execution_authorized'] is False for x in (a,b,c,d)))

    def test_render_has_exactly_100_distinct_cards_and_no_new_backtest(self):
        r=make_result(self.c,self.q,self.cb,self.qb)
        report=render_report(self.c,self.q,r)
        ids=re.findall(r'^#### (I\d{3})',report,re.M)
        self.assertEqual(len(ids),100)
        self.assertEqual(len(set(ids)),100)
        self.assertEqual(r['new_performance_backtests'],0)
        self.assertEqual(r['validated_profitable'],0)
        self.assertFalse(r['execution_authorized'])


if __name__=='__main__':
    unittest.main()
