import copy
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import unittest

from research.small_account.i037_backtest import fit_past, make_signals, active_dates, data_integrity
from research.small_account.m2_backtest import Bar, ET, simulate


class I037Tests(unittest.TestCase):
    def setUp(self):
        self.design = json.loads((Path(__file__).parent / 'i037_design.json').read_text())
        self.design['relative_model']['lookback_sessions'] = 3
        self.days = [date(2025, 1, x) for x in (2, 3, 6, 7, 8, 10)]
        self.data = {'SPY': [], 'QQQ': []}
        xs = [.001, -.002, .003, .0015, -.004, .002]
        ys = [.0011, -.0025, .005, -.020, .025, -.003]
        for s, rs in (('SPY', xs), ('QQQ', ys)):
            for d, ret in zip(self.days, rs):
                for i in range(78):
                    stamp = datetime.combine(d, time(9, 30), ET) + timedelta(minutes=5*i)
                    op = 100 if i == 0 else 100 * math.exp(ret)
                    cl = 100 * math.exp(ret)
                    self.data[s].append(Bar(stamp, op, max(op, cl) * 1.001, min(op, cl) * .99, cl))

    def test_ols_uses_intercept_and_rejects_invalid_fits(self):
        model = fit_past([(-2, -3.9), (-1, -2.2), (1, 2.2), (2, 3.9)])
        self.assertAlmostEqual(model['alpha'], 0)
        self.assertAlmostEqual(model['beta'], 2)
        self.assertIsNone(fit_past([(1, 1), (1, 2), (1, 3)]))
        self.assertIsNone(fit_past([(-1, 1), (0, 0), (1, -1)]))

    def test_past_only_fit_and_future_bar_invariance(self):
        first = make_signals(self.data, self.design)
        altered = copy.deepcopy(self.data)
        for s in altered:
            altered[s] = [Bar(b.timestamp, b.open*2, b.high*2, b.low*2, b.close*2)
                          if b.timestamp.date() >= self.days[3] and b.timestamp.time() >= time(10)
                          else b for b in altered[s]]
        self.assertEqual(first, make_signals(altered, self.design))
        row = next(r for r in first if r['date'] == self.days[3].isoformat())
        self.assertEqual(row['fit_end'], self.days[2].isoformat())
        self.assertTrue(row['candidate']['QQQ'])
        self.assertFalse(any(r['eligible'] for r in first[:3]))

    def test_lagged_signal_is_previous_eligible_only(self):
        rows = [r for r in make_signals(self.data, self.design) if r['eligible']]
        self.assertEqual(rows[0]['lagged'], {'QQQ': False, 'SPY': False})
        self.assertEqual(rows[1]['lagged'], rows[0]['candidate'])
        self.assertNotIn(self.days[3], active_dates(rows, 'QQQ', 'previous_eligible_signal'))
        self.assertIn(self.days[4], active_dates(rows, 'QQQ', 'previous_eligible_signal'))

    def test_missing_opening_input_never_forward_filled(self):
        self.data['SPY'] = [b for b in self.data['SPY'] if not (b.timestamp.date() == self.days[3] and b.timestamp.time() == time(9, 55))]
        row = next(r for r in make_signals(self.data, self.design) if r['date'] == self.days[3].isoformat())
        self.assertFalse(row['eligible'])
        self.assertEqual(row['reason'], 'missing_opening_input')

    def test_cash_engine_honors_active_dates_and_entry_delay(self):
        d = self.days[0]
        period = {'name': 'synthetic', 'start': d.isoformat(), 'end': (d + timedelta(days=1)).isoformat(), 'full_calendar_year': False}
        cost = self.design['cost_cases'][0]
        r = simulate(self.data['QQQ'], self.design, period, cost, 'clock_only_long', 'QQQ', active_dates={d})
        self.assertEqual(len(r['trades']), 1)
        self.assertIn('T10:05:', r['trades'][0]['entry_time'])
        empty = simulate(self.data['QQQ'], self.design, period, cost, 'clock_only_long', 'QQQ', active_dates=set())
        self.assertEqual(empty['metrics']['round_trips'], 0)

    def test_incomplete_pair_fails_dataset_gate(self):
        with self.assertRaisesRegex(ValueError, 'Absent or unsynchronized'):
            data_integrity(self.data, self.design)


if __name__ == '__main__':
    unittest.main()
