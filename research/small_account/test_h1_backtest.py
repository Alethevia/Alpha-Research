import copy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import unittest

from h1_backtest import Bar, HOUR, indicators, normalize, run_window
from risk_controls import RiskController, RiskLimits


class RiskTests(unittest.TestCase):
    def test_floor_ratchets_with_high_watermark(self):
        control = RiskController(2000)
        self.assertEqual(control.observe(2000)["reference_floor"], 1200)
        self.assertEqual(control.observe(3000)["reference_floor"], 1800)
        self.assertEqual(control.observe(2500)["reference_floor"], 1800)

    def test_reduce_pause_and_no_automatic_restart(self):
        control = RiskController(2000)
        self.assertAlmostEqual(control.observe(1600)["new_entry_risk_budget"], 8)
        self.assertTrue(control.observe(1400)["pause_latched"])
        self.assertEqual(control.observe(2100)["new_entry_risk_budget"], 0)

    def test_jump_beyond_ceiling_is_not_clipped(self):
        control = RiskController(2000)
        state = control.observe(900)
        self.assertEqual(state["equity"], 900)
        self.assertAlmostEqual(state["drawdown"], 0.55)
        self.assertTrue(state["ceiling_breached"])

    def test_reject_invalid_limit_ordering(self):
        with self.assertRaises(ValueError):
            RiskLimits(pause_drawdown=0.45)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.design = json.loads(Path(__file__).with_name("h1_design.json").read_text())
        self.cost = {"name": "test", "fee_per_side": 0, "slippage_per_side": 0, "monthly_overhead_usd": 0}

    def fixture(self, prices):
        bars = [Bar(self.start+i*HOUR, p, p, p, p, 1, 1) for i, p in enumerate(prices)]
        features = [{"atr": 1, "entry": i == 0, "exit_level": None, "continuous": True}
                    for i in range(len(prices))]
        period = {"name": "synthetic", "start": self.start.isoformat(),
                  "end": (self.start+len(prices)*HOUR).isoformat()}
        return bars, features, period

    def test_entry_executes_strictly_after_signal_is_available(self):
        bars, features, period = self.fixture([100]*8)
        result = run_window(bars, features, self.design, period, self.cost)
        buy = result["events"][0]
        self.assertEqual(buy["signal_time"], (self.start+HOUR).isoformat())
        self.assertEqual(buy["time"], (self.start+2*HOUR).isoformat())
        self.assertEqual(result["final_equity"], 2000)

    def test_credited_asset_fees_reconcile_to_cash(self):
        bars, features, period = self.fixture([100]*8)
        self.cost["fee_per_side"] = 0.01
        result = run_window(bars, features, self.design, period, self.cost)
        spend = result["events"][0]["cash_outlay_usd"]
        self.assertAlmostEqual(result["final_equity"], 2000-spend*(1-0.99**2))
        self.assertAlmostEqual(result["final_equity"]-2000, result["net_trade_pnl_sum"])

    def test_gap_losses_can_exceed_stated_risk_ceiling(self):
        bars, features, period = self.fixture([100, 100, 100, 50, 10, 1, 1, 1])
        result = run_window(bars, features, self.design, period, self.cost)
        self.assertTrue(result["drawdown_ceiling_breached"])
        self.assertTrue(result["drawdown_pause_latched"])
        self.assertLess(result["final_equity"], 1200)
        self.assertEqual(len([e for e in result["events"] if e["side"] == "buy"]), 1)

    def test_known_intrabar_ceiling_breach_is_not_hidden_by_recovery(self):
        bars, features, period = self.fixture([100]*8)
        bar = bars[3]
        bars[3] = Bar(bar.time, 100, 100, 1, 100, 1, 1)
        result = run_window(bars, features, self.design, period, self.cost)
        self.assertTrue(result["drawdown_ceiling_breached"])
        self.assertEqual(result["maximum_observed_open_close_drawdown"], 0)

    def test_missing_entry_bar_does_not_create_a_fill(self):
        bars, features, period = self.fixture([100]*8)
        del bars[2]
        del features[2]
        result = run_window(bars, features, self.design, period, self.cost)
        self.assertEqual(result["round_trips_including_terminal_valuation"], 0)
        self.assertEqual(result["expired_entries_due_to_missing_bar"], 1)
        self.assertEqual(result["data_quality"]["missing_hours"], 1)

    def test_future_price_changes_do_not_change_past_features(self):
        bars, _, _ = self.fixture([100+math.sin(i/5) for i in range(200)])
        before = indicators(bars, self.design["strategy"])
        bars[-1] = Bar(bars[-1].time, 9000, 9000, 9000, 9000, 1, 1)
        after = indicators(bars, self.design["strategy"])
        self.assertEqual(before[:-1], after[:-1])

    def test_monthly_overhead_applies_even_without_trading(self):
        bars, features, period = self.fixture([100]*(24*60))
        for feature in features:
            feature["entry"] = False
        self.cost["monthly_overhead_usd"] = 10
        result = run_window(bars, features, self.design, period, self.cost)
        self.assertEqual(result["overhead_usd"], 20)
        self.assertEqual(result["final_equity"], 1980)
        self.assertIsNone(result["annualized_return_if_at_least_365_days"])

    def test_incomplete_year_boundary_is_rejected(self):
        bars, features, period = self.fixture([100]*8)
        with self.assertRaises(ValueError):
            run_window(bars[1:], features[1:], self.design, period, self.cost)

    def test_duplicate_and_invalid_prices_are_rejected(self):
        row = {"t": self.start.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 0, "n": 0}
        with self.assertRaises(ValueError):
            normalize([row, row])
        row["c"] = math.nan
        with self.assertRaises(ValueError):
            normalize([row])


if __name__ == "__main__":
    unittest.main()
