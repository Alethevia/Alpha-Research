#!/usr/bin/env python3
"""Frozen H1 price-bar falsification screen, not a broker execution replay."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics

from experiment_registry import complete, register
from risk_controls import RiskController, RiskLimits


HOUR = timedelta(hours=1)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int


def normalize(rows: list[dict]) -> list[Bar]:
    bars = []
    for row in rows:
        bar = Bar(timestamp(row["t"]), *[float(row[k]) for k in ("o", "h", "l", "c", "v")], int(row["n"]))
        if not all(math.isfinite(x) and x > 0 for x in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("invalid price")
        if not math.isfinite(bar.volume) or bar.volume < 0 or bar.trades < 0:
            raise ValueError("invalid volume or trade count")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("inconsistent OHLC")
        if bars and bar.time <= bars[-1].time:
            raise ValueError("duplicate or out-of-order bar; do not silently deduplicate")
        bars.append(bar)
    return bars


def indicators(bars: list[Bar], params: dict) -> list[dict]:
    atr_n = params["atr_hours"]
    reference_n = params["compression_reference_hours"]
    channel_n = params["channel_hours"]
    recent_n = params["compression_recent_hours"]
    exit_n = params["exit_channel_hours"]
    ranges, atr, normalized_atr, compressed, result = [], [], [], [], []
    warmup = atr_n + reference_n + recent_n
    for i, bar in enumerate(bars):
        previous = bars[i-1].close if i else bar.open
        ranges.append(max(bar.high-bar.low, abs(bar.high-previous), abs(bar.low-previous)))
        current_atr = statistics.fmean(ranges[-atr_n:]) if i >= atr_n-1 else None
        atr.append(current_atr)
        normalized_atr.append(current_atr / bar.close if current_atr is not None else None)
        is_compressed = False
        if i >= atr_n + reference_n:
            reference = statistics.fmean(normalized_atr[i-reference_n:i])
            is_compressed = normalized_atr[i] <= params["compression_ratio"] * reference
        compressed.append(is_compressed)
        continuous = i >= warmup and bar.time - bars[i-warmup].time == warmup * HOUR
        result.append({
            "atr": current_atr,
            "entry": bool(continuous and bar.close > max(b.high for b in bars[i-channel_n:i])
                          and any(compressed[i-recent_n:i])),
            "exit_level": min(b.low for b in bars[i-exit_n:i]) if continuous else None,
            "continuous": continuous,
        })
    return result


def run_window(bars: list[Bar], features: list[dict], design: dict, period: dict, cost: dict) -> dict:
    start, end = timestamp(period["start"]), timestamp(period["end"])
    selected = [(i, bar) for i, bar in enumerate(bars) if start <= bar.time < end]
    if not selected:
        raise ValueError("empty research window")
    if selected[0][1].time != start or selected[-1][1].time + HOUR != end:
        raise ValueError("incomplete window boundary; no annual result allowed")
    params = design["strategy"]
    delay = params["signal_to_open_bar_offset"]
    if delay < 2:
        raise ValueError("require at least one full hour after signal-bar close")
    fee, slip = cost["fee_per_side"], cost["slippage_per_side"]
    overhead = cost["monthly_overhead_usd"]
    if not all(math.isfinite(x) and x >= 0 for x in (fee, slip, overhead)) or fee >= 1 or slip >= 1:
        raise ValueError("invalid cost assumptions")
    capital = design["initial_capital_usd"]
    risk = RiskController(capital, RiskLimits(**design["risk"]))
    cash, units = float(capital), 0.0
    entry_spend = entry_price = stop_price = 0.0
    entry_time = None
    pending = None
    months = set()
    events, trades, curve = [], [], []
    fees = overhead_paid = low_mark_dd = 0.0
    expired_entries = 0

    def sell(bar: Bar, price: float, reason: str, signal_time: datetime | None):
        nonlocal cash, units, fees
        fill = price * (1-slip)
        sell_fee = units * fill * fee
        proceeds = units * fill - sell_fee
        fees += sell_fee
        cash += proceeds
        pnl = proceeds - entry_spend
        events.append({"side": "sell", "time": bar.time.isoformat(),
                       "signal_time": signal_time.isoformat() if signal_time else None,
                       "reason": reason, "fill_proxy": fill, "net_units": units,
                       "fee_usd_equivalent": sell_fee, "proceeds_usd": proceeds})
        trades.append({"entry_time": entry_time.isoformat(), "exit_time": bar.time.isoformat(),
                       "net_trade_pnl": pnl, "entry_cash_outlay": entry_spend, "exit_reason": reason})
        units = 0.0

    for i, bar in selected:
        month = (bar.time.year, bar.time.month)
        if month not in months:
            cash -= overhead
            overhead_paid += overhead
            months.add(month)
        state = risk.observe(cash + units * bar.open)
        if pending and pending["due"] <= bar.time:
            order = pending
            pending = None
            if order["side"] == "sell" and units:
                sell(bar, bar.open, order["reason"], order["signal_time"])
            elif order["side"] == "buy" and not units and not risk.paused:
                if order["due"] != bar.time:
                    expired_entries += 1
                else:
                    fill = bar.open * (1+slip)
                    stop_distance = params["stop_atr_multiple"] * order["atr"]
                    # Includes credited-asset fees in the estimated stop loss;
                    # close-based exits can lose more than this sizing estimate.
                    estimated_unit_loss = fill - max(0.0, fill-stop_distance) * (1-fee)**2 * (1-slip)
                    cap = min(state["equity"] * params["max_entry_notional_fraction"],
                              max(0.0, cash - 3*overhead))
                    quantity = min(cap/fill, state["new_entry_risk_budget"]/estimated_unit_loss)
                    step = params["quantity_step_research_assumption"]
                    quantity = math.floor(max(0.0, quantity)/step) * step
                    spent = quantity * fill
                    if spent >= params["minimum_entry_usd_research_assumption"]:
                        cash -= spent
                        units = quantity * (1-fee)  # buy fee charged in received crypto
                        fees += spent * fee
                        entry_spend, entry_price = spent, fill
                        entry_time = bar.time
                        stop_price = max(0.0, fill-stop_distance)
                        events.append({"side": "buy", "time": bar.time.isoformat(),
                                       "signal_time": order["signal_time"].isoformat(),
                                       "reason": "compression_breakout", "fill_proxy": fill,
                                       "gross_units": quantity, "net_units": units,
                                       "fee_usd_equivalent": spent*fee, "cash_outlay_usd": spent})
        state = risk.observe(cash + units * bar.open)
        if units:
            low_mark_dd = max(low_mark_dd, 1 - (cash+units*bar.low)/risk.peak)
        close_time = bar.time + HOUR
        close_equity = cash + units * bar.close
        state = risk.observe(close_equity)
        curve.append({"time": close_time.isoformat(), "equity": close_equity, "cash": cash,
                      "units": units, "drawdown": state["drawdown"], "paused": risk.paused})
        if risk.paused:
            if pending and pending["side"] == "buy":
                pending = None
            if units and not pending:
                pending = {"side": "sell", "due": bar.time + delay*HOUR,
                           "signal_time": close_time, "reason": "drawdown_pause"}
            continue
        feature = features[i]
        if units and not pending:
            time_exit = close_time - entry_time >= params["maximum_hold_hours"] * HOUR
            channel_exit = feature["exit_level"] is not None and bar.close < feature["exit_level"]
            if bar.close <= stop_price or channel_exit or time_exit:
                reason = "close_stop" if bar.close <= stop_price else ("channel_exit" if channel_exit else "time_exit")
                pending = {"side": "sell", "due": bar.time + delay*HOUR,
                           "signal_time": close_time, "reason": reason}
        elif not units and not pending and feature["entry"]:
            pending = {"side": "buy", "due": bar.time + delay*HOUR,
                       "signal_time": close_time, "atr": feature["atr"]}
    if units:
        last = selected[-1][1]
        # Mark-to-liquidation at the final close, explicitly not an executable
        # same-close trading decision. Charge estimated exit costs to all capital.
        terminal = Bar(last.time+HOUR, last.close, last.close, last.close, last.close, 0, 0)
        sell(terminal, last.close, "end_of_window_liquidation_valuation", None)
        risk.observe(cash)
        curve[-1]["equity_after_terminal_liquidation_valuation"] = cash
    net_return = cash/capital - 1
    ceiling_breached = risk.ceiling_breached or low_mark_dd > risk.limits.max_drawdown + 1e-12
    positive = sum(trade["net_trade_pnl"] > 0 for trade in trades)
    elapsed_days = (end-start).total_seconds()/86400
    first, last = selected[0][1], selected[-1][1]
    reserve = overhead * len(months)
    bh_final = max(0.0, capital-reserve) / (first.open*(1+slip)) * (1-fee)**2 * last.close*(1-slip)
    return {
        "evidence_type": "historical_development",
        "classification": "price_proxy_only_not_verified_execution",
        "period": period, "cost_case": cost,
        "initial_equity": capital, "final_equity": cash, "net_return": net_return,
        "elapsed_days": elapsed_days,
        "annualized_return_if_at_least_365_days": (
            (cash/capital)**(365.25/elapsed_days)-1 if elapsed_days >= 365 and cash > 0 else None
        ),
        "maximum_observed_open_close_drawdown": risk.max_observed_drawdown,
        "low_mark_drawdown_against_previously_observed_peak": low_mark_dd,
        "drawdown_ceiling_breached": ceiling_breached,
        "drawdown_pause_latched": risk.paused,
        "round_trips_including_terminal_valuation": len(trades),
        "win_fraction_after_variable_costs": positive/len(trades) if trades else None,
        "fees_usd_equivalent": fees, "overhead_usd": overhead_paid,
        "net_trade_pnl_sum": sum(trade["net_trade_pnl"] for trade in trades),
        "cash_only_return_with_same_overhead": -reserve/capital,
        "passive_buy_hold_return_with_overhead_reserved_upfront": bh_final/capital-1,
        "expired_entries_due_to_missing_bar": expired_entries,
        "year_target_arithmetic_condition_met": net_return >= 10 and not ceiling_breached,
        "data_quality": {
            "observed_bars": len(selected), "expected_hours": int(elapsed_days*24),
            "missing_hours": int(elapsed_days*24)-len(selected),
            "zero_trade_count_bars": sum(bar.trades == 0 for _, bar in selected),
            "zero_volume_bars": sum(bar.volume == 0 for _, bar in selected),
            "bars_do_not_establish_executable_price_or_size": True,
        },
        "events": events, "trades": trades, "equity_curve": curve,
    }


def run_study(snapshot: Path, design_path: Path, output: Path) -> dict:
    design = json.loads(design_path.read_text())
    data_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    payload = json.loads(gzip.decompress(snapshot.read_bytes()))
    code_hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ("h1_backtest.py", "risk_controls.py", "experiment_registry.py")}
    output.mkdir(parents=True, exist_ok=True)
    journal = output / "trials.jsonl"
    summaries = []
    for symbol in design["symbols"]:
        bars = normalize(payload["bars"][symbol])
        features = indicators(bars, design["strategy"])
        for period in design["periods"]:
            for cost in design["cost_cases"]:
                experiment_id = f"H1_{symbol.replace('/', '_')}_{period['name']}_{cost['name']}"
                spec = {
                    "hypothesis": "H1 frozen compression breakout", "data_provenance": payload["endpoint"],
                    "data_sha256": data_hash, "code_revision": code_hashes,
                    "parameters": {"strategy": design["strategy"], "risk": design["risk"], "symbol": symbol},
                    "sample_split": period, "cost_model": cost, "primary_metric": design["primary_metric"],
                    "rejection_rules": design["rejection_rules"], "evidence_type": "historical_development",
                    "design_sha256": hashlib.sha256(design_path.read_bytes()).hexdigest(),
                }
                register(journal, experiment_id, spec)
                artifact = output / (experiment_id + ".json")
                try:
                    result = run_window(bars, features, design, period, cost)
                    result.update({"symbol": symbol, "experiment_id": experiment_id,
                                   "data_sha256": data_hash, "code_sha256": code_hashes})
                    artifact.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
                    complete(journal, experiment_id, "completed", artifact)
                    summaries.append({k: v for k, v in result.items() if k not in ("events", "trades", "equity_curve")})
                except Exception as error:
                    artifact.write_text(json.dumps({"evidence_type": "historical_development", "error": str(error)}))
                    complete(journal, experiment_id, "error", artifact)
                    raise
    summary = {
        "status": "HISTORICAL_PRICE_PROXY_SCREEN_ONLY",
        "data_sha256": data_hash, "code_sha256": code_hashes, "results": summaries,
        "limitations": [
            "Bar prices and assumed slippage are not actual executable bid/ask fills or capacity evidence.",
            "Alpaca crypto bars may contain quote-derived prices and zero trades; trade counts are reported.",
            "Historical bars have no local receive timestamps; delayed execution does not certify point-in-time availability.",
            "Current tier-1 fees are projected onto historical prices; historical fee schedules were not reconstructed.",
            "Slippage, monthly overhead, quantity step and minimum entry are research assumptions.",
            "Each symbol and each calendar year resets to $2000 as an alternative scenario, not a combined account.",
            "Open/close risk observations omit intrabar peaks; true maximum drawdown can be larger.",
            "A pause request is not a guaranteed liquidation; gaps can exceed the 40% drawdown ceiling.",
            "These are development results; no parameter tuning or independent forward trading was performed.",
            "12 fixed cases are retained; relevant legacy trials must also be considered for selection bias.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--design", type=Path, default=Path(__file__).with_name("h1_design.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run_study(args.snapshot, args.design, args.output)
    for result in summary["results"]:
        print(json.dumps({k: result[k] for k in ("symbol", "period", "cost_case", "net_return", "maximum_observed_open_close_drawdown", "round_trips_including_terminal_valuation")}))
