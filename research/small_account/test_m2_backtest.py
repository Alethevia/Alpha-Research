import copy
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import unittest

from research.small_account.m2_backtest import Bar, ET, opening_signal, settlement_date, simulate


class M2Tests(unittest.TestCase):
    def setUp(self):
        self.design = json.loads((Path(__file__).parent / "m2_design.json").read_text())
        self.cost = self.design["cost_cases"][0]
        self.period = {"name": "synthetic_day", "start": "2025-01-02", "end": "2025-01-03", "full_calendar_year": False}

    def day(self, d=date(2025, 1, 2)):
        start = datetime.combine(d, time(9, 30), ET)
        bars = [Bar(start + timedelta(minutes=i * 5), 104, 105, 103, 104, 1000) for i in range(78)]
        bars[:3] = [Bar(start, 100, 102, 99, 101), Bar(start + timedelta(minutes=5), 101, 103, 100, 102),
                    Bar(start + timedelta(minutes=10), 102, 104, 101, 103.5)]
        bars[76] = Bar(start + timedelta(minutes=380), 106, 107, 105, 106)
        return bars

    def run_case(self, bars=None, **kwargs):
        return simulate(bars if bars is not None else self.day(), kwargs.get("design", self.design),
                        kwargs.get("period", self.period), kwargs.get("cost", self.cost),
                        kwargs.get("rule", "opening_drive_long"), "SYNTHETIC")

    def test_signal_only_uses_completed_opening_bars_and_entry_waits(self):
        result = self.run_case()
        trade = result["trades"][0]
        self.assertTrue(trade["entry_time"].startswith("2025-01-02T09:50"))
        self.assertTrue(trade["exit_time"].startswith("2025-01-02T15:50"))
        changed = self.day()
        changed[10] = Bar(changed[10].timestamp, 200, 201, 198, 200)
        self.assertEqual(opening_signal(self.day(), self.design, "opening_drive_long"),
                         opening_signal(changed, self.design, "opening_drive_long"))
        second = self.run_case(changed)["trades"][0]
        self.assertEqual((trade["entry_time"], trade["quantity"]), (second["entry_time"], second["quantity"]))

    def test_missing_entry_is_not_forward_filled(self):
        result = self.run_case([b for b in self.day() if b.timestamp.time() != time(9, 50)])
        self.assertEqual(result["metrics"]["round_trips"], 0)
        self.assertEqual(result["metrics"]["missing_entry_bar_sessions"], 1)

    def test_missing_opening_bar_skips_signal(self):
        result = self.run_case(self.day()[1:])
        self.assertEqual(result["metrics"]["round_trips"], 0)
        self.assertEqual(result["metrics"]["missing_opening_input_sessions"], 1)

    def test_missing_exit_is_error_instead_of_dropping_day(self):
        with self.assertRaisesRegex(ValueError, "no observed regular-session exit"):
            self.run_case(self.day()[:76])

    def test_missing_full_session_is_error(self):
        with self.assertRaisesRegex(ValueError, "Missing complete trading session"):
            self.run_case([])

    def test_stop_waits_and_gap_is_not_clipped(self):
        bars = self.day()
        bars[4] = Bar(bars[4].timestamp, 104, 104, 98, 98)
        bars[5] = Bar(bars[5].timestamp, 97, 98, 96, 97)
        bars[6] = Bar(bars[6].timestamp, 90, 91, 89, 90)
        trade = self.run_case(bars)["trades"][0]
        self.assertTrue(trade["exit_time"].startswith("2025-01-02T10:00"))
        self.assertEqual(trade["exit_fill"], 90)
        self.assertGreater(-trade["net_pnl"], trade["planned_risk_usd"])

    def test_gap_can_exceed_drawdown_ceiling(self):
        bars = self.day()
        bars[4] = Bar(bars[4].timestamp, 100, 100, 98, 98)
        bars[5] = Bar(bars[5].timestamp, 1, 1, 1, 1)
        bars[6] = Bar(bars[6].timestamp, 1, 1, 1, 1)
        result = self.run_case(bars)
        self.assertTrue(result["metrics"]["observed_40pct_breach"])
        self.assertTrue(result["metrics"]["drawdown_pause_latched"])
        self.assertLess(result["metrics"]["final_equity"], 1200)

    def test_fees_overhead_and_cash_identity(self):
        cost = self.design["cost_cases"][1]
        result = self.run_case(cost=cost)
        trade = result["trades"][0]
        self.assertAlmostEqual(trade["entry_fill"], 104 * 1.0005)
        self.assertAlmostEqual(trade["exit_fill"], 106 * .9995)
        self.assertEqual(result["metrics"]["cash_fees_usd"], .04)
        self.assertEqual(result["metrics"]["overhead_usd"], 10)
        self.assertLess(result["metrics"]["cash_identity_error_usd"], 1e-9)
        self.assertGreater(result["metrics"]["unsettled_cash_at_end"], 0)
        self.assertLessEqual(trade["planned_risk_usd"], trade["risk_budget_usd"])

    def test_integer_shares_and_no_forced_minimum(self):
        design = copy.deepcopy(self.design)
        design["initial_capital_usd"] = 90
        result = self.run_case(design=design)
        self.assertEqual(result["metrics"]["round_trips"], 0)
        self.assertEqual(result["metrics"]["zero_quantity_skips"], 1)
        self.assertIsInstance(self.run_case()["trades"][0]["quantity"], int)

    def test_conservative_settlement_holiday_and_weekend(self):
        self.assertEqual(settlement_date(date(2025, 10, 10), self.design), date(2025, 10, 14))
        self.assertEqual(settlement_date(date(2025, 11, 10), self.design), date(2025, 11, 12))
        self.assertEqual(settlement_date(date(2025, 1, 2), self.design), date(2025, 1, 3))

    def test_bank_holiday_does_not_make_unsettled_sales_spendable(self):
        period = {"name": "synthetic_cash", "start": "2025-10-10", "end": "2025-10-15", "full_calendar_year": False}
        design = copy.deepcopy(self.design)
        # Tight initial stop allows the capital constraint to bind on Friday.
        days = []
        for d in (date(2025, 10, 10), date(2025, 10, 13), date(2025, 10, 14)):
            rows = self.day(d)
            rows[4] = Bar(rows[4].timestamp, 100, 105, 100, 104)
            days.extend(rows)
        result = self.run_case(days, period=period, design=design)
        by_day = {t["entry_time"][:10]: t["quantity"] for t in result["trades"]}
        self.assertEqual(by_day["2025-10-10"], 19)
        self.assertLess(by_day.get("2025-10-13", 0), by_day["2025-10-10"])
        self.assertGreater(by_day["2025-10-14"], by_day.get("2025-10-13", 0))
        self.assertLess(result["metrics"]["cash_identity_error_usd"], 1e-8)

    def test_known_early_close_skipped_and_dst_clock_local(self):
        period = {"name": "early", "start": "2025-07-03", "end": "2025-07-04", "full_calendar_year": False}
        result = self.run_case([], period=period)
        self.assertEqual(result["metrics"]["known_early_close_skips"], 1)
        self.assertEqual(result["metrics"]["round_trips"], 0)
        d = date(2025, 7, 2)
        period.update(start=d.isoformat(), end="2025-07-03")
        trade = self.run_case(self.day(d), period=period)["trades"][0]
        self.assertEqual(trade["entry_time"], "2025-07-02T09:50:00-04:00")

    def test_clock_control_removes_signal_filter(self):
        bars = self.day()
        bars[2] = Bar(bars[2].timestamp, 102, 104, 99, 99)
        self.assertEqual(self.run_case(bars)["metrics"]["round_trips"], 0)
        self.assertEqual(self.run_case(bars, rule="clock_only_long")["metrics"]["round_trips"], 1)


if __name__ == "__main__":
    unittest.main()
