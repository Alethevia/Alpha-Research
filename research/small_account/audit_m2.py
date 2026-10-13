#!/usr/bin/env python3
"""Check saved M2 evidence against snapshots without rerunning the strategy."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
import math
from pathlib import Path

try:
    from .experiment_registry import load_events
    from .m2_backtest import read_bars, save_json, sha256
except ImportError:
    from experiment_registry import load_events
    from m2_backtest import read_bars, save_json, sha256


def audit(data_dir: Path, output: Path) -> dict:
    summary = json.loads((output / "summary.json").read_text())
    manifest_path = output / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert sha256(manifest_path) == summary["data_manifest_sha256"]
    assert sha256(data_dir / "requests.json") == manifest["requests_sha256"]
    for item in manifest["files"]:
        assert sha256(data_dir / item["file"]) == item["sha256"]
    events = load_events(output / "experiments.jsonl")
    registered = {e["experiment_id"]: e for e in events if e["event"] == "registered"}
    finished = {e["experiment_id"]: e for e in events if e["event"] == "completed"}
    assert len(registered) == len(finished) == summary["scenario_count"] == 36
    assert set(registered) == set(finished)
    assert all(e["event"] == "registered" for e in events[:36])
    assert all(e["event"] == "completed" for e in events[36:])
    datasets = {symbol: {b.timestamp: b for b in read_bars(data_dir, symbol)[0]}
                for symbol in summary["data_inventory"]}
    day_index = {}
    for symbol, by_time in datasets.items():
        day_index[symbol] = {}
        for stamp, bar in by_time.items():
            day_index[symbol].setdefault(stamp.date(), []).append(bar)
    largest_error = 0.0
    trades_checked = 0
    error_cases = []
    for case_id, event in finished.items():
        path = output / event["artifact_name"]
        assert sha256(path) == event["artifact_sha256"]
        result = json.loads(path.read_text())
        if "error" in result:
            error_cases.append(case_id)
            continue
        assert result["code_revision"] == registered[case_id]["spec"]["code_revision"] == summary["code_revision"]
        bars = datasets[result["symbol"]]
        cost = result["cost_case"]
        slip, fee = cost["slippage_bps_per_side"] / 10000, cost["cash_fee_per_order_usd"]
        seen_days = set()
        net_total = gross_total = slippage_total = 0.0
        for trade in result["trades"]:
            entry, exit_ = datetime.fromisoformat(trade["entry_time"]), datetime.fromisoformat(trade["exit_time"])
            q = trade["quantity"]
            assert isinstance(q, int) and q > 0
            assert entry.time() == time(9, 50) and entry.date() == exit_.date() and exit_ > entry
            assert entry.date() not in seen_days
            seen_days.add(entry.date())
            assert math.isclose(trade["entry_raw_price"], bars[entry].open, abs_tol=1e-10)
            assert math.isclose(trade["exit_raw_price"], bars[exit_].open, abs_tol=1e-10)
            buy, sell = bars[entry].open * (1 + slip), bars[exit_].open * (1 - slip)
            net = q * (sell - buy) - 2 * fee
            gross = q * (bars[exit_].open - bars[entry].open)
            implied_slip = q * (bars[entry].open + bars[exit_].open) * slip
            largest_error = max(largest_error, abs(net - trade["net_pnl"]), abs(gross - trade["gross_pnl"]))
            assert math.isclose(buy, trade["entry_fill"], abs_tol=1e-10)
            assert math.isclose(sell, trade["exit_fill"], abs_tol=1e-10)
            assert trade["planned_risk_usd"] <= trade["risk_budget_usd"] + 1e-8
            # Independently inspect the three signal bars, which end at 09:45.
            opening = [bars[entry.replace(hour=9, minute=m)] for m in (30, 35, 40)]
            high, low = max(b.high for b in opening), min(b.low for b in opening)
            assert trade["stop"] == low
            if result["rule"] == "opening_drive_long":
                assert opening[-1].close - opening[0].open >= .25 * (high - low)
                assert opening[-1].close > opening[0].open
                assert opening[-1].close >= low + .8 * (high - low)
            if trade["exit_reason"] == "close_below_opening_low":
                triggers = [b.timestamp for b in day_index[result["symbol"]][entry.date()]
                            if entry <= b.timestamp < exit_ and b.close <= low]
                assert triggers and exit_ >= min(triggers) + timedelta(minutes=10)
            net_total += net
            gross_total += gross
            slippage_total += implied_slip
            trades_checked += 1
        metrics = result["metrics"]
        expected_final = metrics["initial_equity"] + net_total - metrics["overhead_usd"]
        largest_error = max(largest_error, abs(expected_final - metrics["final_equity"]),
                            abs(slippage_total - metrics["assumed_slippage_usd"]),
                            abs(net_total - (gross_total - slippage_total - metrics["cash_fees_usd"])))
        assert metrics["round_trips"] == len(result["trades"])
        assert abs(metrics["cash_fees_usd"] - len(result["trades"]) * 2 * fee) < 1e-8
        assert abs(metrics["final_equity"] - metrics["settled_cash_at_end"] - metrics["unsettled_cash_at_end"]) < 1e-8
        assert all(p.get("settled_cash", 0) >= -1e-8 for p in result["equity_observations"])
        if not result["period"]["full_calendar_year"]:
            assert metrics["annualized_return"] is None and metrics["full_year_1000pct_target_hit"] is None
    assert largest_error < 1e-7
    result = {"journal_events_verified": len(events), "all_cases_registered_before_calculation": True,
              "result_hashes_verified": len(finished), "data_file_hashes_verified": len(manifest["files"]),
              "scenario_trade_records_checked": trades_checked, "maximum_reconciliation_error_usd": largest_error,
              "error_cases": error_cases, "validation_status": "passed" if not error_cases else "incomplete_with_preserved_errors",
              "scope": "Data/journal integrity, arithmetic, raw entry/exit prices, signal timing, integer sizes and nonnegative cash. Does not certify source truth, actual fills, intrabar risk, unseen data or profitable forward performance."}
    save_json(output / "audit.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.data_dir, args.output), indent=2))
