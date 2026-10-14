#!/usr/bin/env python3
"""Frozen M2 aggregate-price screen. No broker connection or order submission.

Trade prices plus assumed friction are not executable quotes. All dates are
exchange-local. Low marks diagnose risk retrospectively and never trigger an
order before the bar is complete. No forward fills or overnight positions.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

try:
    from .risk_controls import RiskController, RiskLimits
    from .experiment_registry import register, complete
except ImportError:
    from risk_controls import RiskController, RiskLimits
    from experiment_registry import register, complete

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def trading_days(start: date, end: date, design: dict) -> list[date]:
    closed = set(design["calendar"]["closed"])
    days = []
    while start < end:
        if start.weekday() < 5 and start.isoformat() not in closed:
            days.append(start)
        start += timedelta(days=1)
    return days


def settlement_date(trade_date: date, design: dict) -> date:
    """Conservative availability convention, not exact DTC/broker settlement."""
    excluded = set(design["calendar"]["closed"]) | set(design["settlement"]["bank_only_holidays"])
    candidate = trade_date + timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.isoformat() in excluded:
        candidate += timedelta(days=1)
    return candidate


def read_bars(folder: Path, symbol: str) -> tuple[list[Bar], list[dict]]:
    bars, files, seen = [], [], set()
    paths = sorted(folder.glob(f"{symbol}_*.csv"))
    if not paths:
        raise ValueError(f"No CSV files for {symbol}")
    for path in paths:
        count = 0
        prior = None
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"t", "o", "h", "l", "c", "v"}.issubset(reader.fieldnames or []):
                raise ValueError(f"Invalid CSV header: {path.name}")
            for row in reader:
                stamp = int(row["t"])
                if stamp % 300000 != 0 or stamp in seen or (prior is not None and stamp <= prior):
                    raise ValueError(f"Duplicate, unaligned or unordered timestamp: {path.name}:{stamp}")
                seen.add(stamp)
                prior = stamp
                values = [float(row[key]) for key in ("o", "h", "l", "c", "v")]
                o, h, low, c, volume = values
                if not all(math.isfinite(x) for x in values) or min(o, h, low, c) <= 0 or volume < 0:
                    raise ValueError("Invalid price or volume")
                if not low <= min(o, c) <= max(o, c) <= h:
                    raise ValueError("OHLC values inconsistent")
                bars.append(Bar(datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(ET), o, h, low, c, volume))
                count += 1
        files.append({"file": path.name, "sha256": sha256(path), "bars": count})
    bars.sort(key=lambda b: b.timestamp)
    return bars, files


def opening_signal(bars: list[Bar], design: dict, rule: str) -> tuple[bool, float | None]:
    if rule not in design["rules"]:
        raise ValueError("Unknown rule")
    by_time = {b.timestamp.strftime("%H:%M"): b for b in bars}
    keys = design["strategy"]["opening_bar_times"]
    if any(key not in by_time for key in keys):
        return False, None
    opening = [by_time[key] for key in keys]
    high, low = max(b.high for b in opening), min(b.low for b in opening)
    width = high - low
    if width <= 0:
        return False, low
    if rule == "clock_only_long":
        return True, low
    body = opening[-1].close - opening[0].open
    signal = (body > 0 and body >= design["strategy"]["minimum_positive_body_fraction"] * width
              and opening[-1].close >= low + design["strategy"]["minimum_close_position_in_range"] * width)
    return signal, low


def simulate(bars: list[Bar], design: dict, period: dict, cost: dict, rule: str, symbol: str,
             *, active_dates: set[date] | None = None) -> dict:
    start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
    expected = trading_days(start, end, design)
    early = set(design["calendar"]["skip_known_early_close_sessions"])
    sessions = {}
    for bar in bars:
        d, t = bar.timestamp.date(), bar.timestamp.time()
        if start <= d < end and time(9, 30) <= t < time(16):
            sessions.setdefault(d, []).append(bar)
    for d in expected:
        if d.isoformat() not in early and d not in sessions:
            raise ValueError(f"Missing complete trading session {symbol} {d}")
    for d, rows in sessions.items():
        if d not in expected:
            raise ValueError(f"Bars on unexpected regular-session date: {d}")
        stamps = [b.timestamp for b in rows]
        if stamps != sorted(set(stamps)):
            raise ValueError("Session bars must be unique and sorted")

    initial = float(design["initial_capital_usd"])
    settled = initial
    receivables: list[tuple[date, float]] = []
    control = RiskController(initial, RiskLimits(**design["risk"]))
    slip = cost["slippage_bps_per_side"] / 10000
    fee, monthly = cost["cash_fee_per_order_usd"], cost["monthly_overhead_usd"]
    overhead = fees = slippage = max_low_dd = 0.0
    position = None
    trades, curve, daily = [], [], []
    charged_months = set()
    counts = {"expected_sessions": len(expected), "known_early_close_skips": 0,
              "missing_regular_bars": 0, "missing_opening_input_sessions": 0,
              "missing_entry_bar_sessions": 0, "signal_sessions": 0,
              "below_stop_entry_skips": 0, "zero_quantity_skips": 0,
              "pause_entry_skips": 0, "settled_cash_binding_entries": 0}

    def equity(mark: float = 0.0) -> float:
        return settled + sum(amount for _, amount in receivables) + (position["quantity"] * mark if position else 0)

    def observe(stamp: datetime, price: float, kind: str) -> dict:
        state = control.observe(equity(price))
        curve.append({"time": stamp.isoformat(), "kind": kind, "equity": state["equity"],
                      "settled_cash": settled, "unsettled_cash": sum(x[1] for x in receivables),
                      "quantity": position["quantity"] if position else 0, "drawdown": state["drawdown"]})
        return state

    for d in expected:
        released = sum(amount for due, amount in receivables if due <= d)
        settled += released
        receivables = [(due, amount) for due, amount in receivables if due > d]
        stamp0 = datetime.combine(d, time(9, 30), ET)
        month_key = (d.year, d.month)
        if month_key not in charged_months:
            charged_months.add(month_key)
            settled -= monthly
            overhead += monthly
        observe(stamp0, 0, "session_start_after_settlement_and_overhead")
        if d.isoformat() in early:
            counts["known_early_close_skips"] += 1
            daily.append({"date": d.isoformat(), "equity": equity(), "early_close_skipped": True})
            continue
        if active_dates is not None and d not in active_dates:
            daily.append({"date": d.isoformat(), "equity": equity(), "early_close_skipped": False,
                          "inactive_event_session": True})
            continue
        rows = sessions[d]
        counts["missing_regular_bars"] += 78 - len(rows)
        opening = [b for b in rows if b.timestamp.strftime("%H:%M") in design["strategy"]["opening_bar_times"]]
        signal, stop = opening_signal(opening, design, rule)
        if stop is None:
            counts["missing_opening_input_sessions"] += 1
        counts["signal_sessions"] += int(signal)
        entry_clock = time.fromisoformat(design["strategy"]["entry_time"])
        exit_clock = time.fromisoformat(design["strategy"]["timed_exit"][:5])
        if signal and not any(b.timestamp.time() == entry_clock for b in rows):
            counts["missing_entry_bar_sessions"] += 1

        for bar in rows:
            stamp = bar.timestamp
            if position:
                state = observe(stamp, bar.open, "open")
                if state["pause_latched"] and position["exit_due"] is None:
                    position["exit_due"] = stamp + timedelta(minutes=5)
                    position["exit_reason"] = "drawdown_pause"
                if stamp.time() >= exit_clock or (position["exit_due"] and stamp >= position["exit_due"]):
                    q = position["quantity"]
                    fill = bar.open * (1 - slip)
                    sale = q * fill - fee
                    receivables.append((settlement_date(d, design), sale))
                    fees += fee
                    slippage += q * bar.open * slip
                    reason = (position["exit_reason"] if position["exit_due"] and stamp >= position["exit_due"] else "timed_exit")
                    trades.append({"entry_time": position["entry_time"].isoformat(), "exit_time": stamp.isoformat(),
                                   "quantity": q, "entry_raw_price": position["entry_raw_price"],
                                   "entry_fill": position["entry_fill"], "exit_raw_price": bar.open, "exit_fill": fill,
                                   "stop": position["stop"], "planned_risk_usd": position["planned_risk_usd"],
                                   "risk_budget_usd": position["risk_budget_usd"],
                                   "gross_pnl": q * (bar.open - position["entry_raw_price"]),
                                   "net_pnl": sale - position["purchase_cash"], "cash_fees": 2 * fee,
                                   "exit_reason": reason, "proceeds_available": settlement_date(d, design).isoformat()})
                    position = None
                    observe(stamp, 0, "exit_fill")

            if stamp.time() == entry_clock and signal:
                # This is the sole entry attempt of the session, using only completed opening bars.
                state = control.observe(equity())
                if state["pause_latched"]:
                    counts["pause_entry_skips"] += 1
                elif bar.open <= stop:
                    counts["below_stop_entry_skips"] += 1
                else:
                    fill = bar.open * (1 + slip)
                    loss_per_share = fill - stop * (1 - slip)
                    budget = state["new_entry_risk_budget"]
                    reserve = design["strategy"]["cash_reserve_months_of_overhead"] * monthly
                    risk_qty = math.floor(max(0, budget - 2 * fee) / loss_per_share)
                    capital_qty = math.floor(max(0, equity()) * design["strategy"]["max_entry_notional_fraction"] / fill)
                    cash_qty = math.floor(max(0, settled - reserve - fee) / fill)
                    q = min(risk_qty, capital_qty, cash_qty)
                    if q <= 0:
                        counts["zero_quantity_skips"] += 1
                    else:
                        counts["settled_cash_binding_entries"] += int(cash_qty < min(risk_qty, capital_qty))
                        purchase = q * fill + fee
                        settled -= purchase
                        fees += fee
                        slippage += q * bar.open * slip
                        position = {"quantity": q, "entry_time": stamp, "entry_raw_price": bar.open,
                                    "entry_fill": fill, "purchase_cash": purchase, "stop": stop,
                                    "planned_risk_usd": q * loss_per_share + 2 * fee, "risk_budget_usd": budget,
                                    "exit_due": None, "exit_reason": None}
                        observe(stamp, bar.open, "entry_fill")
            if position:
                # A historical low is a diagnostic, not a real-time execution event.
                low_equity = equity(bar.low)
                max_low_dd = max(max_low_dd, 1 - low_equity / control.peak)
                curve.append({"time": stamp.isoformat(), "kind": "bar_low_diagnostic", "equity": low_equity,
                              "drawdown_from_prior_observed_peak": 1 - low_equity / control.peak})
                state = observe(stamp + timedelta(minutes=5), bar.close, "close")
                if position["exit_due"] is None and (bar.close <= position["stop"] or state["pause_latched"]):
                    position["exit_due"] = stamp + timedelta(minutes=10)
                    position["exit_reason"] = "drawdown_pause" if state["pause_latched"] else "close_below_opening_low"
        if position:
            raise ValueError(f"Open position has no observed regular-session exit: {symbol} {d}")
        daily.append({"date": d.isoformat(), "equity": equity(), "early_close_skipped": False})

    expected_months = set()
    cursor = start
    while cursor < end:
        expected_months.add((cursor.year, cursor.month))
        cursor += timedelta(days=1)
    if charged_months != expected_months:
        raise ValueError("A monthly overhead charge has no observed session")
    final = equity()
    pnl = sum(t["net_pnl"] for t in trades)
    identity_error = abs(final - (initial + pnl - overhead))
    if identity_error > 1e-7:
        raise AssertionError("Cash/PnL reconciliation failed")
    observed_dd = max(control.max_observed_drawdown, max_low_dd)
    metrics = {"initial_equity": initial, "final_equity": final, "net_period_return": final / initial - 1,
               "max_observed_drawdown": observed_dd, "open_close_max_drawdown": control.max_observed_drawdown,
               "intrabar_low_drawdown_diagnostic": max_low_dd,
               "drawdown_pause_latched": control.paused, "observed_40pct_breach": observed_dd > .40 + 1e-12,
               "round_trips": len(trades), "profitable_round_trips": sum(t["net_pnl"] > 0 for t in trades),
               "cash_fees_usd": fees, "assumed_slippage_usd": slippage, "overhead_usd": overhead,
               "trade_net_pnl_usd": pnl, "cash_identity_error_usd": identity_error,
               "settled_cash_at_end": settled, "unsettled_cash_at_end": sum(x[1] for x in receivables),
               "cash_only_final_after_overhead": initial - overhead,
               "full_year_1000pct_target_hit": (final >= initial * 11) if period["full_calendar_year"] else None,
               "annualized_return": None, **counts}
    return {"evidence_type": "historical_development", "execution_authorized": False,
            "symbol": symbol, "rule": rule, "period": period, "cost_case": cost, "metrics": metrics,
            "trades": trades, "equity_observations": curve, "daily_equity": daily,
            "limits": ["Bar-price proxy with assumed fills, not actual NBBO/executions.",
                       "Unknown intrabar high/low ordering and missing bars can understate maximum drawdown.",
                       "Cash availability uses a conservative union calendar, not verified broker settlement.",
                       "Historical development and chronological check; no statistical confidence or forward validation claimed."]}


def build_manifest(folder: Path, design: dict) -> dict:
    records, symbols = [], {}
    for symbol in design["symbols"]:
        bars, files = read_bars(folder, symbol)
        records.extend(files)
        symbols[symbol] = {"bars": len(bars), "first": bars[0].timestamp.isoformat(),
                           "last": bars[-1].timestamp.isoformat()}
    return {"provider": "Massive connector CSV export", "files": records, "symbols": symbols,
            "requests_sha256": sha256(folder / "requests.json"), "design_sha256": sha256(HERE / "m2_design.json")}


def run(data_dir: Path, output: Path, published_revision: str) -> dict:
    design = json.loads((HERE / "m2_design.json").read_text())
    manifest = build_manifest(data_dir, design)
    if output.exists():
        raise ValueError("Use a new output directory; preserve prior attempts")
    output.mkdir(parents=True)
    save_json(output / "data_manifest.json", manifest)
    data_hash = sha256(output / "data_manifest.json")
    local_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
    journal = output / "experiments.jsonl"
    cases = []
    # Register every case before ANY performance calculation in this family.
    for symbol in design["symbols"]:
        for period in design["periods"]:
            for rule in design["rules"]:
                for cost in design["cost_cases"]:
                    case_id = "M2_" + "__".join((symbol, period["name"], rule, cost["name"]))
                    spec = {"hypothesis": design["hypothesis"], "data_provenance": design["data_provenance"],
                            "data_sha256": data_hash, "code_revision": published_revision,
                            "local_code_revision": local_revision, "parameters": {"design": design, "symbol": symbol, "rule": rule},
                            "sample_split": period, "cost_model": cost, "primary_metric": design["primary_metric"],
                            "rejection_rules": design["rejection_rules"], "evidence_type": design["evidence_type"]}
                    register(journal, case_id, spec)
                    cases.append((case_id, symbol, period, rule, cost))
    datasets = {symbol: read_bars(data_dir, symbol)[0] for symbol in design["symbols"]}
    results = []
    for case_id, symbol, period, rule, cost in cases:
        artifact = output / f"{case_id}.json"
        try:
            result = simulate(datasets[symbol], design, period, cost, rule, symbol)
            result.update({"experiment_id": case_id, "data_manifest_sha256": data_hash, "code_revision": published_revision})
            save_json(artifact, result)
            complete(journal, case_id, "completed", artifact)
            results.append({k: v for k, v in result.items() if k not in ("trades", "equity_observations", "daily_equity")})
        except Exception as exc:
            error = {"evidence_type": design["evidence_type"], "experiment_id": case_id,
                     "symbol": symbol, "period": period, "rule": rule, "cost_case": cost,
                     "error": f"{type(exc).__name__}: {exc}"}
            save_json(artifact, error)
            complete(journal, case_id, "error", artifact)
            results.append(error)
    gates = []
    for symbol in design["symbols"]:
        base = [r for r in results if r["symbol"] == symbol and r["cost_case"]["name"] == "base_cost_assumption"]
        matched = []
        for period in design["periods"]:
            candidate = next(r for r in base if r["rule"] == "opening_drive_long" and r["period"] == period)
            benchmark = next(r for r in base if r["rule"] == "clock_only_long" and r["period"] == period)
            if "error" in candidate or "error" in benchmark:
                matched.append({"period": period["name"], "passed": False, "status": "error"})
            else:
                cm, bm = candidate["metrics"], benchmark["metrics"]
                passed = cm["net_period_return"] > 0 and cm["net_period_return"] > bm["net_period_return"] and not cm["observed_40pct_breach"]
                matched.append({"period": period["name"], "passed": passed,
                                "candidate_return": cm["net_period_return"], "benchmark_return": bm["net_period_return"],
                                "excess_percentage_points": 100 * (cm["net_period_return"] - bm["net_period_return"])})
        gates.append({"symbol": symbol, "advance_quote_validation": all(x["passed"] for x in matched), "windows": matched})
    summary = {"experiment_family": design["experiment_family"], "evidence_type": design["evidence_type"],
               "execution_authorized": False, "code_revision": published_revision, "local_code_revision": local_revision,
               "data_manifest_sha256": data_hash, "design_sha256": sha256(HERE / "m2_design.json"),
               "scenario_count": len(cases), "error_count": sum("error" in r for r in results),
               "data_inventory": manifest["symbols"], "selection_gates": gates, "results": results}
    save_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--published-revision", required=True)
    args = parser.parse_args()
    result = run(args.data_dir, args.output, args.published_revision)
    print(json.dumps({k: result[k] for k in ("scenario_count", "error_count", "selection_gates")}, indent=2))


if __name__ == "__main__":
    main()
