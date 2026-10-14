#!/usr/bin/env python3
"""Coverage only: use dated futures contracts on a fixed CME roll calendar."""
import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import statistics

try:
    from .m2_backtest import HERE, ET, sha256, save_json, trading_days
    from .h3_event_backtest import read_futures
    from .experiment_registry import register, complete, load_events
except ImportError:
    from m2_backtest import HERE, ET, sha256, save_json, trading_days
    from h3_event_backtest import read_futures
    from experiment_registry import register, complete, load_events


def selected_contract(day, product, spec):
    matches = [r for r in spec["fixed_schedule"] if r["start_inclusive"] <= day.isoformat() < r["end_exclusive"]]
    if len(matches) != 1:
        raise ValueError("Date outside or overlapping fixed schedule")
    return matches[0][product]


def measure(by_contract, transactions, days, product, spec):
    daily, present = [], []
    for day in days:
        ticker = selected_contract(day, product, spec)
        start = datetime.combine(day, time(9, 30), ET)
        longest = run = 0
        observed = []
        for i in range(78):
            stamp = start + timedelta(minutes=i * 5)
            bar = by_contract[ticker].get(stamp)
            if bar is None:
                run += 1
                longest = max(longest, run)
            else:
                run = 0
                observed.append((bar, transactions[ticker][stamp]))
        present.extend(observed)
        daily.append({"date": str(day), "selected_contract": ticker, "present_slots": len(observed),
                      "missing_slots": 78 - len(observed), "longest_missing_run_minutes": 5 * longest,
                      "volume_contracts": sum(b.volume for b, _ in observed),
                      "transactions": sum(n for _, n in observed)})
    missing = sum(r["missing_slots"] for r in daily)
    return {"product": product, "expected_regular_slots": len(days) * 78,
            "present_regular_slots": len(present), "missing_regular_slots": missing,
            "missing_slot_fraction": missing / (len(days) * 78),
            "median_present_bar_volume": statistics.median(b.volume for b, _ in present) if present else None,
            "total_contract_volume": sum(b.volume for b, _ in present),
            "total_transactions": sum(n for _, n in present),
            "present_bars_volume_lt_10": sum(b.volume < 10 for b, _ in present),
            "present_bars_transactions_le_3": sum(n <= 3 for _, n in present),
            "maximum_daily_missing_run_minutes": max(r["longest_missing_run_minutes"] for r in daily),
            "daily_coverage": daily}


def run(old_dir, new_dir, output, revision):
    if output.exists():
        raise ValueError("Preserve existing audit; use a new output directory")
    design = json.loads((HERE / "nano_roll_design.json").read_text())
    paths = [old_dir / f"{x}_202609.csv" for x in ("NESZ6", "MESZ6")] + sorted(new_dir.glob("*"))
    output.mkdir(parents=True)
    manifest = [{"file": p.name, "source": "h3_data" if p.parent == old_dir else "nano_roll_data", "sha256": sha256(p)} for p in paths if p.is_file()]
    save_json(output / "data_manifest.json", {"files": manifest})
    data_hash = sha256(output / "data_manifest.json")
    journal = output / "experiments.jsonl"
    key = design["experiment_id"]
    register(journal, key, {"hypothesis": design["purpose"], "data_provenance": design,
                           "data_sha256": data_hash, "code_revision": revision, "parameters": design,
                           "sample_split": design["fixed_schedule"], "cost_model": "No PnL or costs computed; coverage only",
                           "primary_metric": design["metrics"], "rejection_rules": design["interpretation"],
                           "evidence_type": design["evidence_type"]})
    result_file = output / "result.json"
    try:
        by_contract, transactions = {}, {}
        for ticker in ("NESU6", "NESZ6", "MESU6", "MESZ6"):
            source = old_dir / f"{ticker}_202609.csv" if "Z6" in ticker else new_dir / f"{ticker}_20260901_20260914.csv"
            bars, counts = read_futures(source, ticker, .5 if ticker.startswith("NES") else .25)
            by_contract[ticker] = {b.timestamp: b for b in bars}
            transactions[ticker] = counts
        calendar = json.loads((HERE / "m2_design.json").read_text())
        days = trading_days(date(2026, 9, 1), date(2026, 10, 1), calendar)
        results = [measure(by_contract, transactions, days, product, design) for product in ("NES", "MES")]
        # Independent set membership recount verifies each day's coverage denominator.
        for item in results:
            for d in item["daily_coverage"]:
                day = date.fromisoformat(d["date"])
                valid = {datetime.combine(day, time(9, 30), ET) + timedelta(minutes=i * 5) for i in range(78)}
                expected = len(valid.intersection(by_contract[d["selected_contract"]]))
                assert expected == d["present_slots"] and expected + d["missing_slots"] == 78
            assert item["expected_regular_slots"] == item["present_regular_slots"] + item["missing_regular_slots"] == 1638
        result = {"evidence_type": design["evidence_type"], "experiment_id": key,
                  "code_revision": revision, "data_manifest_sha256": data_hash,
                  "execution_authorized": False, "posthoc_followup": True, "performance_computed": False,
                  "results": results, "independent_coverage_recount": "passed", "limitations": design["interpretation"]}
        save_json(result_file, result)
        complete(journal, key, "completed", result_file)
    except Exception as exc:
        result = {"evidence_type": design["evidence_type"], "experiment_id": key, "error": f"{type(exc).__name__}: {exc}"}
        save_json(result_file, result)
        complete(journal, key, "error", result_file)
    events = load_events(journal)
    assert len(events) == 2 and sha256(result_file) == events[-1]["artifact_sha256"]
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--old-dir", type=Path, required=True)
    p.add_argument("--new-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--published-revision", required=True)
    args = p.parse_args()
    r = run(args.old_dir, args.new_dir, args.output, args.published_revision)
    print(json.dumps({**r, "results": [{k: v for k, v in row.items() if k != "daily_coverage"} for row in r.get("results", [])]}, indent=2))
