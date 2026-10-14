#!/usr/bin/env python3
"""Reconcile saved H3 cases against raw prices and registration hashes."""
import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path

try:
    from .m2_backtest import read_bars, sha256, save_json
    from .experiment_registry import load_events
except ImportError:
    from m2_backtest import read_bars, sha256, save_json
    from experiment_registry import load_events


def audit(data_dir, nano_dir, output):
    summary = json.loads((output / "summary.json").read_text())
    manifest = json.loads((output / "data_manifest.json").read_text())
    assert sha256(output / "data_manifest.json") == summary["data_manifest_sha256"]
    for item in manifest["files"]:
        folder = data_dir if item["group"] == "reused_m2_data" else nano_dir
        assert sha256(folder / item["file"]) == item["sha256"]
    events = load_events(output / "experiments.jsonl")
    registered = {e["experiment_id"]: e for e in events if e["event"] == "registered"}
    completed = {e["experiment_id"]: e for e in events if e["event"] == "completed"}
    assert len(registered) == len(completed) == 38 and set(registered) == set(completed)
    assert all(e["event"] == "registered" for e in events[:38])
    assert all(e["event"] == "completed" for e in events[38:])
    datasets = {s: {b.timestamp: b for b in read_bars(data_dir, s)[0]} for s in ("SPY", "QQQ", "TQQQ")}
    max_error = 0
    checked = 0
    errors = []
    for key, e in completed.items():
        p = output / e["artifact_name"]
        assert sha256(p) == e["artifact_sha256"]
        r = json.loads(p.read_text())
        if "error" in r:
            errors.append(key)
            continue
        if "trades" not in r:
            continue
        spec = registered[key]["spec"]
        design = spec["parameters"]["design"]
        assert r["code_revision"] == spec["code_revision"] == summary["code_revision"]
        bars = datasets[r["symbol"]]
        eligible = set(design["event_dates"])
        fees = r["cost_case"]["cash_fee_per_order_usd"]
        slip = r["cost_case"]["slippage_bps_per_side"] / 10000
        total = friction = 0.0
        seen = set()
        for t in r["trades"]:
            enter, exit_ = datetime.fromisoformat(t["entry_time"]), datetime.fromisoformat(t["exit_time"])
            assert enter.date().isoformat() in eligible and enter.date() not in seen
            seen.add(enter.date())
            assert enter.time() == time(14, 20) and exit_.date() == enter.date() and enter < exit_
            assert isinstance(t["quantity"], int) and t["quantity"] > 0
            q = t["quantity"]
            buy, sell = bars[enter].open * (1 + slip), bars[exit_].open * (1 - slip)
            pnl = q * (sell - buy) - 2 * fees
            total += pnl
            friction += q * (bars[enter].open + bars[exit_].open) * slip
            max_error = max(max_error, abs(pnl - t["net_pnl"]), abs(buy - t["entry_fill"]), abs(sell - t["exit_fill"]))
            reaction = [bars[enter.replace(hour=14, minute=m)] for m in (0, 5, 10)]
            low, high = min(b.low for b in reaction), max(b.high for b in reaction)
            assert low == t["stop"]
            planned = q * (buy - low * (1 - slip)) + 2 * fees
            assert abs(planned - t["planned_risk_usd"]) < 1e-8
            assert planned <= t["risk_budget_usd"] + 1e-8
            if r["rule"] == "opening_drive_long":
                assert reaction[-1].close > reaction[0].open
                assert reaction[-1].close - reaction[0].open >= .25 * (high - low)
                assert reaction[-1].close >= low + .8 * (high - low)
            if t["exit_reason"] == "close_below_opening_low":
                stamp = enter
                triggers = []
                while stamp < exit_:
                    if stamp in bars and bars[stamp].close <= low:
                        triggers.append(stamp)
                    stamp += timedelta(minutes=5)
                assert triggers and exit_ >= min(triggers) + timedelta(minutes=10)
            checked += 1
        m = r["metrics"]
        months = 12 if r["period"]["full_calendar_year"] else 9
        assert m["overhead_usd"] == months * r["cost_case"]["monthly_overhead_usd"]
        assert len(r["trades"]) == m["round_trips"] <= m["event_opportunities"]
        max_error = max(max_error, abs(m["final_equity"] - (2000 + total - m["overhead_usd"])),
                        abs(friction - m["assumed_slippage_usd"]), abs(m["cash_fees_usd"] - 2 * fees * len(r["trades"])))
        assert all(p.get("settled_cash", 0) >= -1e-8 for p in r["equity_observations"])
        if not r["period"]["full_calendar_year"]:
            assert m["annualized_return"] is None and m["full_year_1000pct_target_hit"] is None
    assert max_error < 1e-7
    result = {"journal_events_verified": len(events), "all_38_specs_registered_before_computation": True,
              "artifact_hashes_verified": len(completed), "data_file_hashes_verified": len(manifest["files"]),
              "scenario_trade_records_checked": checked, "max_reconciliation_error_usd": max_error,
              "error_cases": errors, "status": "passed" if not errors else "incomplete_with_preserved_errors",
              "scope": "Integrity and accounting, event-date eligibility and timing; not actual executable fills, source truth or forward profitability."}
    save_json(output / "audit.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--nano-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(audit(args.data_dir, args.nano_dir, args.output), indent=2))
