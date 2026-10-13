#!/usr/bin/env python3
"""Frozen FOMC pilot and real-contract data gate; never submits orders."""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import statistics
import subprocess

try:
    from .m2_backtest import ET, HERE, Bar, read_bars, sha256, save_json, simulate, trading_days, opening_signal
    from .experiment_registry import register, complete, load_events
except ImportError:
    from m2_backtest import ET, HERE, Bar, read_bars, sha256, save_json, simulate, trading_days, opening_signal
    from experiment_registry import register, complete, load_events


def merged_design(h3: dict) -> dict:
    result = json.loads((HERE / "m2_design.json").read_text())
    result.update(copy.deepcopy(h3))
    return result


def matched_prior_day(event: date, full_days: set[date], events: set[date]) -> date:
    candidate = event - timedelta(days=7)
    for _ in range(52):
        if candidate in full_days and candidate not in events:
            return candidate
        candidate -= timedelta(days=7)
    raise ValueError("No prior matching full weekday within one year")


def response_statistic(bars: dict[datetime, Bar], d: date) -> dict:
    def at(t):
        return bars[datetime.combine(d, time.fromisoformat(t), ET)]
    first, last, enter, exit_ = at("14:00"), at("14:10"), at("14:20"), at("15:50")
    reaction = last.close / first.open - 1
    direction = (reaction > 0) - (reaction < 0)
    later = exit_.open / enter.open - 1
    return {"date": d.isoformat(), "reaction_return": reaction, "later_raw_return": later,
            "direction": direction, "directional_continuation_bps": direction * later * 10000}


def paired_diagnostics(dataset: list[Bar], design: dict) -> dict:
    bars = {b.timestamp: b for b in dataset}
    events = set(map(date.fromisoformat, design["event_dates"]))
    full = set(trading_days(date(2025, 1, 1), date(2026, 10, 1), design))
    full -= set(map(date.fromisoformat, design["calendar"]["skip_known_early_close_sessions"]))
    rows = []
    for d in sorted(events):
        control_day = matched_prior_day(d, full, events)
        event, control = response_statistic(bars, d), response_statistic(bars, control_day)
        rows.append({"event": event, "control": control,
                     "difference_bps": event["directional_continuation_bps"] - control["directional_continuation_bps"]})
    values = [r["difference_bps"] for r in rows]
    loo = [(sum(values) - v) / (len(values) - 1) for v in values]
    return {"statistic_only_not_account_return": True, "paired_events": len(rows),
            "event_mean_bps": statistics.mean(r["event"]["directional_continuation_bps"] for r in rows),
            "control_mean_bps": statistics.mean(r["control"]["directional_continuation_bps"] for r in rows),
            "mean_difference_bps": statistics.mean(values), "positive_pair_differences": sum(v > 0 for v in values),
            "leave_one_event_out_mean_difference_range_bps": [min(loo), max(loo)], "pairs": rows,
            "limits": "No costs, stops, risk sizing or borrow modeled in this bidirectional price statistic. Not a short-trading strategy. All three ETFs share the same 14 event dates; 42 pairs are not 42 independent events. No causal or statistical-significance claim."}


def read_futures(path: Path, ticker: str, tick: float) -> tuple[list[Bar], dict[datetime, int]]:
    rows, transactions, seen = [], {}, set()
    previous = None
    with path.open(newline="") as source:
        for r in csv.DictReader(source):
            stamp = int(r["window_start"])
            if r["ticker"] != ticker or stamp % 300_000_000_000 != 0 or stamp in seen or (previous is not None and stamp <= previous):
                raise ValueError("Invalid, duplicate, unordered or unaligned futures record")
            previous = stamp
            seen.add(stamp)
            ts = datetime.fromtimestamp(stamp / 1e9, timezone.utc).astimezone(ET)
            o, h, low, c, vol = [float(r[k]) for k in ("open", "high", "low", "close", "volume")]
            n = int(r["transactions"])
            if not all(math.isfinite(x) for x in (o, h, low, c, vol)) or min(o, h, low, c) <= 0 or vol < 0 or n < 0:
                raise ValueError("Invalid futures price or volume")
            if not low <= min(o, c) <= max(o, c) <= h:
                raise ValueError("Invalid futures OHLC")
            if any(abs(p / tick - round(p / tick)) > 1e-7 for p in (o, h, low, c)):
                raise ValueError("Price does not align with contract tick")
            rows.append(Bar(ts, o, h, low, c, vol))
            transactions[ts] = n
    return rows, transactions


def nano_data_gate(folder: Path, design: dict) -> dict:
    results = []
    expected_days = trading_days(date(2026, 9, 1), date(2026, 10, 1), design)
    for ticker in design["nano_data_gate"]["contracts"]:
        specs = design["nano_data_gate"]["NES_specs" if ticker.startswith("NES") else "MES_specs"]
        bars, counts = read_futures(folder / f"{ticker}_202609.csv", ticker, specs["minimum_tick_points"])
        index = {b.timestamp: b for b in bars}
        regular, coverage = [], []
        for day in expected_days:
            start = datetime.combine(day, time(9, 30), ET)
            slots = [start + timedelta(minutes=i * 5) for i in range(78)]
            found = [index[t] for t in slots if t in index]
            regular.extend(found)
            longest = current = 0
            for t in slots:
                current = 0 if t in index else current + 1
                longest = max(longest, current)
            coverage.append({"date": day.isoformat(), "present_slots": len(found), "missing_slots": 78 - len(found),
                             "longest_missing_run_minutes": longest * 5,
                             "volume_contracts": sum(b.volume for b in found),
                             "transactions": sum(counts[b.timestamp] for b in found)})
        d = date(2026, 9, 16)
        timestamps = [datetime.combine(d, time.fromisoformat(t), ET) for t in ("14:00", "14:05", "14:10", "14:20", "15:50")]
        available = {t.strftime("%H:%M"): t in index for t in timestamps}
        risk_gate = {"event_date": d.isoformat(), "required_bars_available": available,
                     "account_capital_eligibility": "UNKNOWN", "execution_authorized": False}
        if all(available.values()):
            reaction = [index[t] for t in timestamps[:3]]
            signal, stop = opening_signal(reaction, design, "opening_drive_long")
            enter = index[timestamps[3]].open
            tick, multiplier = specs["minimum_tick_points"], specs["usd_per_index_point"]
            risk_gate.update({"positive_reaction_signal": signal, "entry_raw_price": enter,
                              "reaction_low": stop, "reaction_high": max(b.high for b in reaction),
                              "one_contract_notional_reference_usd": enter * multiplier,
                              "round_trip_assumed_friction_usd": 2 + 2 * tick * multiplier})
            if enter > stop:
                risk = (enter - stop + 2 * tick) * multiplier + 2
                risk_gate.update({"illustrative_one_contract_stop_risk_usd": risk,
                                  "within_20_usd_planned_risk_if_long": risk <= 20,
                                  "signal_and_unit_risk_gate_pass": signal and risk <= 20})
            else:
                risk_gate["invalid_long_stop_distance"] = True
        missing = sum(x["missing_slots"] for x in coverage)
        results.append({"ticker": ticker, "all_session_bars": len(bars), "regular_session_bars": len(regular),
                        "expected_full_sessions": len(expected_days), "expected_regular_slots": len(expected_days) * 78,
                        "missing_regular_slots": missing, "missing_slot_fraction": missing / (len(expected_days) * 78),
                        "regular_volume_contracts": sum(b.volume for b in regular),
                        "median_present_bar_contract_volume": statistics.median(b.volume for b in regular) if regular else None,
                        "present_bars_volume_le_1": sum(b.volume <= 1 for b in regular),
                        "present_bars_volume_lt_10": sum(b.volume < 10 for b in regular),
                        "present_bars_transactions_le_3": sum(counts[b.timestamp] <= 3 for b in regular),
                        "maximum_missing_run_minutes": max(x["longest_missing_run_minutes"] for x in coverage),
                        "daily_coverage": coverage, "september_fomc_unit_check": risk_gate})
    return {"evidence_type": "historical_development", "execution_authorized": False, "results": results,
            "limits": "Coverage uses real contract prices, no MES-to-NES scaling. Missing bars may reflect inactivity or vendor omissions; do not forward fill. Aggregate volume is not available bid/ask depth or a guaranteed fill. Contract/account margin, all-in fees and live receipt latency remain unverified. No nano account return is reported."}


def run(data_dir: Path, nano_dir: Path, output: Path, revision: str) -> dict:
    if output.exists():
        raise ValueError("Preserve existing trials; use a new output directory")
    design = merged_design(json.loads((HERE / "h3_design.json").read_text()))
    datasets, files = {}, []
    for symbol in design["symbols"]:
        datasets[symbol], inventory = read_bars(data_dir, symbol)
        files.extend({**x, "group": "reused_m2_data"} for x in inventory)
    for p in sorted(nano_dir.glob("*")):
        if p.is_file():
            files.append({"file": p.name, "group": "new_h3_data", "sha256": sha256(p)})
    output.mkdir(parents=True)
    save_json(output / "data_manifest.json", {"files": files, "design_sha256": sha256(HERE / "h3_design.json")})
    data_hash = sha256(output / "data_manifest.json")
    journal = output / "experiments.jsonl"
    events = set(map(date.fromisoformat, design["event_dates"]))
    cases = []
    for symbol in design["symbols"]:
        for period in design["periods"]:
            for rule in design["rules"]:
                for cost in design["cost_cases"]:
                    key = "H3_" + "__".join((symbol, period["name"], rule, cost["name"]))
                    spec = {"hypothesis": design["hypothesis"], "data_provenance": design["data_provenance"],
                            "data_sha256": data_hash, "code_revision": revision,
                            "parameters": {"design": design, "symbol": symbol, "rule": rule},
                            "sample_split": period, "cost_model": cost, "primary_metric": design["primary_metric"],
                            "rejection_rules": design["rejection_rules"], "evidence_type": design["evidence_type"]}
                    register(journal, key, spec)
                    cases.append((key, symbol, period, rule, cost))
    for key, description in (("H3_PAIRED_PRICE_DIAGNOSTIC", design["descriptive_control"]), ("M1_NANO_DATA_GATE", design["nano_data_gate"])):
        provenance = ("Massive /futures/v1/aggs/{ticker}; actual NESZ6 and MESZ6 September 2026 CSV exports with request metadata and hashes"
                      if key == "M1_NANO_DATA_GATE" else design["data_provenance"])
        register(journal, key, {"hypothesis": description, "data_provenance": provenance,
                               "data_sha256": data_hash, "code_revision": revision, "parameters": design,
                               "sample_split": design["periods"], "cost_model": "As separately disclosed in the frozen diagnostic",
                               "primary_metric": description, "rejection_rules": "No execution or annual-performance claim from diagnostics",
                               "evidence_type": design["evidence_type"]})
    results = []
    for key, symbol, period, rule, cost in cases:
        artifact = output / f"{key}.json"
        try:
            result = simulate(datasets[symbol], design, period, cost, rule, symbol, active_dates=events)
            biggest = max([0.0] + [t["net_pnl"] for t in result["trades"]])
            m = result["metrics"]
            m["event_opportunities"] = sum(period["start"] <= d < period["end"] for d in design["event_dates"])
            m["largest_winning_trade_usd"] = biggest
            m["return_without_largest_win_arithmetic_only"] = (m["final_equity"] - biggest) / m["initial_equity"] - 1
            result.update({"experiment_id": key, "data_manifest_sha256": data_hash, "code_revision": revision})
            save_json(artifact, result)
            complete(journal, key, "completed", artifact)
            results.append({k: v for k, v in result.items() if k not in ("trades", "equity_observations", "daily_equity")})
        except Exception as exc:
            error = {"evidence_type": design["evidence_type"], "experiment_id": key, "symbol": symbol,
                     "period": period, "rule": rule, "cost_case": cost, "error": f"{type(exc).__name__}: {exc}"}
            save_json(artifact, error)
            complete(journal, key, "error", artifact)
            results.append(error)
    extra = {}
    for key, calculate in (("H3_PAIRED_PRICE_DIAGNOSTIC", lambda: {s: paired_diagnostics(datasets[s], design) for s in design["symbols"]}),
                           ("M1_NANO_DATA_GATE", lambda: nano_data_gate(nano_dir, design))):
        artifact = output / f"{key}.json"
        try:
            result = {"evidence_type": design["evidence_type"], "experiment_id": key, "result": calculate()}
            save_json(artifact, result)
            complete(journal, key, "completed", artifact)
        except Exception as exc:
            result = {"evidence_type": design["evidence_type"], "experiment_id": key, "error": f"{type(exc).__name__}: {exc}"}
            save_json(artifact, result)
            complete(journal, key, "error", artifact)
        extra[key] = result
    gates = []
    for symbol in design["symbols"]:
        windows = []
        for period in design["periods"]:
            same = [r for r in results if r["symbol"] == symbol and r["period"] == period and r["cost_case"]["name"] == "base_cost_assumption"]
            candidate = next(r for r in same if r["rule"] == "opening_drive_long")
            control = next(r for r in same if r["rule"] == "clock_only_long")
            if "error" in candidate or "error" in control:
                windows.append({"period": period["name"], "passed": False, "error": True})
            else:
                a, b = candidate["metrics"], control["metrics"]
                windows.append({"period": period["name"], "candidate_return": a["net_period_return"], "control_return": b["net_period_return"],
                                "passed": a["net_period_return"] > 0 and a["net_period_return"] > b["net_period_return"] and not a["observed_40pct_breach"]})
        gates.append({"symbol": symbol, "advance_quote_collection_only": all(w["passed"] for w in windows), "windows": windows})
    summary = {"experiment_family": design["experiment_family"], "evidence_type": design["evidence_type"],
               "execution_authorized": False, "code_revision": revision,
               "local_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip(),
               "data_manifest_sha256": data_hash, "design_sha256": sha256(HERE / "h3_design.json"),
               "scenario_count": len(cases), "separately_registered_diagnostics": len(extra),
               "error_count": sum("error" in r for r in results) + sum("error" in r for r in extra.values()),
               "selection_gates": gates, "results": results, "diagnostics": extra}
    save_json(output / "summary.json", summary)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--nano-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--published-revision", required=True)
    args = p.parse_args()
    result = run(args.data_dir, args.nano_dir, args.output, args.published_revision)
    print(json.dumps({k: result[k] for k in ("scenario_count", "error_count", "selection_gates")}, indent=2))
