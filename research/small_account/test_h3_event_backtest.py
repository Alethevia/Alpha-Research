from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from research.small_account.m2_backtest import Bar, ET, HERE, simulate, trading_days
from research.small_account.h3_event_backtest import matched_prior_day, merged_design, read_futures, response_statistic


class H3Tests(unittest.TestCase):
    def setUp(self):
        self.design = merged_design(json.loads((HERE / "h3_design.json").read_text()))
        self.period = {"name": "synthetic", "start": "2025-01-29", "end": "2025-01-31", "full_calendar_year": False}

    def day(self, d):
        start = datetime.combine(d, time(9, 30), ET)
        rows = [Bar(start + timedelta(minutes=5 * i), 104, 105, 103, 104, 1000) for i in range(78)]
        for minute, prices in ((0, (100, 102, 99, 101)), (5, (101, 103, 100, 102)), (10, (102, 104, 101, 103.5))):
            ts = datetime.combine(d, time(14, minute), ET)
            rows[(ts - start) // timedelta(minutes=5)] = Bar(ts, *prices)
        return rows

    def test_only_listed_day_and_delayed_event_entry(self):
        first, second = date(2025, 1, 29), date(2025, 1, 30)
        result = simulate(self.day(first) + self.day(second), self.design, self.period, self.design["cost_cases"][0],
                          "opening_drive_long", "SYNTHETIC", active_dates={first})
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["entry_time"], "2025-01-29T14:20:00-05:00")
        self.assertTrue(result["daily_equity"][1]["inactive_event_session"])

    def test_inactive_months_still_pay_overhead(self):
        period = {"name": "idle", "start": "2025-01-01", "end": "2025-03-01", "full_calendar_year": False}
        rows = [b for d in trading_days(date(2025, 1, 1), date(2025, 3, 1), self.design) for b in self.day(d)]
        result = simulate(rows, self.design, period, self.design["cost_cases"][1], "opening_drive_long", "SYNTHETIC", active_dates=set())
        self.assertEqual(result["metrics"]["round_trips"], 0)
        self.assertEqual(result["metrics"]["overhead_usd"], 20)
        self.assertEqual(result["metrics"]["final_equity"], 1980)

    def test_no_event_entry_when_exact_bar_missing(self):
        d = date(2025, 1, 29)
        rows = [b for b in self.day(d) if b.timestamp.time() != time(14, 20)] + self.day(date(2025, 1, 30))
        result = simulate(rows, self.design, self.period, self.design["cost_cases"][0], "opening_drive_long", "SYNTHETIC", active_dates={d})
        self.assertEqual(result["metrics"]["round_trips"], 0)
        self.assertEqual(result["metrics"]["missing_entry_bar_sessions"], 1)

    def test_event_signal_does_not_read_later_prices(self):
        d = date(2025, 1, 29)
        rows = self.day(d) + self.day(date(2025, 1, 30))
        original = simulate(rows, self.design, self.period, self.design["cost_cases"][0], "opening_drive_long", "SYNTHETIC", active_dates={d})
        changed = [Bar(b.timestamp, 1000, 1001, 999, 1000) if b.timestamp == datetime.combine(d, time(15), ET) else b for b in rows]
        other = simulate(changed, self.design, self.period, self.design["cost_cases"][0], "opening_drive_long", "SYNTHETIC", active_dates={d})
        for key in ("entry_time", "entry_fill", "quantity", "stop"):
            self.assertEqual(original["trades"][0][key], other["trades"][0][key])

    def test_same_weekday_control_skips_known_closure_and_other_event(self):
        full = set(trading_days(date(2025, 1, 1), date(2025, 2, 1), self.design))
        self.assertEqual(matched_prior_day(date(2025, 1, 16), full, {date(2025, 1, 16)}), date(2025, 1, 2))
        self.assertEqual(matched_prior_day(date(2025, 1, 29), full, {date(2025, 1, 22)}), date(2025, 1, 15))

    def test_response_is_a_signed_raw_price_statistic(self):
        d = date(2025, 1, 29)
        rows = {b.timestamp: b for b in self.day(d)}
        stamp = datetime.combine(d, time(15, 50), ET)
        rows[stamp] = Bar(stamp, 105.04, 106, 105, 105.5)
        result = response_statistic(rows, d)
        self.assertEqual(result["direction"], 1)
        self.assertAlmostEqual(result["directional_continuation_bps"], 100)

    def test_futures_tick_alignment_and_no_invented_missing_bars(self):
        header = "ticker,window_start,session_end_date,open,high,low,close,transactions,volume,dollar_volume\n"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nano.csv"
            p.write_text(header + "NESZ6,1789516800000000000,2026-09-16,7663.5,7664,7663,7663,2,2,15327\n")
            rows, _ = read_futures(p, "NESZ6", .5)
            self.assertEqual(len(rows), 1)
            p.write_text(header + "NESZ6,1789516800000000000,2026-09-16,7663.25,7664,7663,7663,2,2,15327\n")
            with self.assertRaisesRegex(ValueError, "tick"):
                read_futures(p, "NESZ6", .5)


if __name__ == "__main__":
    unittest.main()
