#!/usr/bin/env python3
"""Frozen I037 residual screen using the unchanged M2 cash-account engine."""
from __future__ import annotations

import argparse
from datetime import date, time
import json
import math
from pathlib import Path
import statistics
import subprocess

try:
    from .m2_backtest import HERE, read_bars, build_manifest, save_json, sha256, trading_days, simulate
    from .experiment_registry import register, complete
except ImportError:
    from m2_backtest import HERE, read_bars, build_manifest, save_json, sha256, trading_days, simulate
    from experiment_registry import register, complete


def fit_past(pairs):
    """OLS on explicitly supplied past opening returns; never full-sample fit."""
    n = len(pairs)
    if n < 3:
        return None
    mx, my = (statistics.mean(p[i] for p in pairs) for i in (0, 1))
    xx = sum((x - mx) ** 2 for x, _ in pairs)
    if xx <= 1e-20:
        return None
    beta = sum((x - mx) * (y - my) for x, y in pairs) / xx
    alpha = my - beta * mx
    sigma = math.sqrt(sum((y - alpha - beta * x) ** 2 for x, y in pairs) / (n - 2))
    if beta <= 0 or sigma <= 1e-12:
        return None
    return {"alpha": alpha, "beta": beta, "sigma": sigma}


def session_map(bars):
    result = {}
    for b in bars:
        if time(9, 30) <= b.timestamp.time() < time(16):
            result.setdefault(b.timestamp.date(), {})[b.timestamp.strftime("%H:%M")] = b
    return result


def data_integrity(datasets, design):
    maps = {s: session_map(b) for s, b in datasets.items()}
    expected = trading_days(date(2025, 1, 1), date(2026, 10, 1), design)
    early = set(design["calendar"]["skip_known_early_close_sessions"])
    full, missing, max_jump = 0, 0, 0.0
    for d in expected:
        if d.isoformat() in early:
            continue
        full += 1
        left, right = maps["SPY"].get(d, {}), maps["QQQ"].get(d, {})
        if not left or not right or set(left) != set(right):
            raise ValueError(f"Absent or unsynchronized session: {d}")
        missing += 78 - len(left)
        for s in ("SPY", "QQQ"):
            rows = sorted(maps[s][d].values(), key=lambda b: b.timestamp)
            for a, b in zip(rows, rows[1:]):
                jump = abs(b.open / a.close - 1)
                max_jump = max(max_jump, jump)
                if jump > .20:
                    raise ValueError(f"Unresolved within-session discontinuity: {s} {b.timestamp}")
    return {"synchronized_full_sessions": full, "missing_synchronized_slots": missing,
            "maximum_adjacent_intraday_open_close_jump": max_jump,
            "corporate_action_database_verified": False,
            "scope": "Intraday-only returns avoid overnight split/dividend jumps; this is not a full corporate-action audit."}


def make_signals(datasets, design):
    maps = {s: session_map(b) for s, b in datasets.items()}
    dates = trading_days(date(2025, 1, 1), date(2026, 10, 1), design)
    early = set(design["calendar"]["skip_known_early_close_sessions"])
    keys = design["strategy"]["opening_bar_times"]
    history, result = [], []
    lookback = design["relative_model"]["lookback_sessions"]
    threshold = design["relative_model"]["z_threshold"]
    previous_eligible = {"QQQ": False, "SPY": False}
    for d in dates:
        if d.isoformat() in early:
            continue
        row = {"date": d.isoformat(), "eligible": False, "candidate": {"QQQ": False, "SPY": False},
               "lagged": {"QQQ": False, "SPY": False}}
        if any(any(k not in maps[s].get(d, {}) for k in keys) for s in maps):
            row["reason"] = "missing_opening_input"
            result.append(row)
            continue
        opening = {s: math.log(maps[s][d][keys[-1]].close / maps[s][d][keys[0]].open) for s in maps}
        x, y = opening["SPY"], opening["QQQ"]
        row["opening_log_returns"] = opening
        if len(history) >= lookback:
            past = history[-lookback:]
            model = fit_past([(h[1], h[2]) for h in past])
            if model is not None:
                z = (y - model["alpha"] - model["beta"] * x) / model["sigma"]
                row.update(eligible=True, model=model, z=z, fit_start=past[0][0].isoformat(),
                           fit_end=past[-1][0].isoformat(), fit_sessions=lookback,
                           signal_available_at=d.isoformat() + "T10:00:00[America/New_York]")
                row["candidate"] = {"QQQ": z <= -threshold, "SPY": z >= threshold}
                row["lagged"] = dict(previous_eligible)
                previous_eligible = dict(row["candidate"])
            else:
                row["reason"] = "invalid_past_only_fit"
        else:
            row["reason"] = "warmup"
        # Current observation is appended only AFTER its signal is determined.
        history.append((d, x, y))
        result.append(row)
    return result


def active_dates(rows, symbol, rule):
    field = {"relative_laggard": "candidate", "previous_eligible_signal": "lagged"}.get(rule)
    if field is None and rule != "clock_only":
        raise ValueError("unknown rule")
    return {date.fromisoformat(r["date"]) for r in rows if r["eligible"] and (field is None or r[field][symbol])}


def diagnostic(datasets, signals, period):
    maps = {s: session_map(b) for s, b in datasets.items()}
    rows = []
    for r in signals:
        if not r["eligible"] or not period["start"] <= r["date"] < period["end"]:
            continue
        d = date.fromisoformat(r["date"])
        out = {"date": r["date"], "candidate": r["candidate"], "beta": r["model"]["beta"], "z": r["z"]}
        if any(k not in maps[s][d] for s in maps for k in ("10:05", "15:50")):
            out["missing_timed_return"] = True
        else:
            future = {s: math.log(maps[s][d]["15:50"].open / maps[s][d]["10:05"].open) for s in maps}
            residual = future["QQQ"] - out["beta"] * future["SPY"]
            out.update(future_timed_log_returns=future, future_residual=residual,
                       oriented_residual=residual if r["z"] < 0 else -residual)
        rows.append(out)
    sides = []
    for s in ("QQQ", "SPY"):
        selected = [r for r in rows if r["candidate"][s]]
        values = [r["oriented_residual"] for r in selected if "oriented_residual" in r]
        sides.append({"symbol": s, "events": len(selected), "observed_timed_returns": len(values),
                      "mean_oriented_residual_bps": statistics.mean(values) * 10000 if values else None,
                      "median_oriented_residual_bps": statistics.median(values) * 10000 if values else None})
    return {"evidence_type": "historical_development", "period": period, "sides": sides, "daily_rows": rows,
            "limits": "Ex-ante-beta diagnostic, no intercept subtraction for a different-duration future window. Not cash return, not shortable hedge, no stops or fees, no causal/significance claim. Sector/rate exposures not independently controlled."}


def run(data_dir, output, published_revision):
    if output.exists():
        raise ValueError("Use new output directory; preserve attempts")
    design = json.loads((HERE / "i037_design.json").read_text())
    m2_design = json.loads((HERE / "m2_design.json").read_text())
    output.mkdir(parents=True)
    manifest = build_manifest(data_dir, m2_design)
    save_json(output / "data_manifest.json", manifest)
    data_hash = sha256(output / "data_manifest.json")
    if data_hash != design["source_data_manifest_sha256"]:
        raise ValueError("M2 data snapshot differs from frozen public hash")
    journal = output / "experiments.jsonl"
    local = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
    cases = [(f"I037_{s}__{p['name']}__{rule}__{cost['name']}", s, p, rule, cost)
             for s in design["symbols"] for p in design["periods"]
             for rule in design["relative_model"]["controls"] for cost in design["cost_cases"]]
    diagnostics = [(f"I037_DIAGNOSTIC__{p['name']}", None, p, "timed_residual_diagnostic", None) for p in design["periods"]]
    for key, s, period, rule, cost in cases + diagnostics:
        register(journal, key, {"hypothesis": design["hypothesis"], "data_provenance": design["data_provenance"],
                 "data_sha256": data_hash, "code_revision": published_revision, "local_code_revision": local,
                 "parameters": {"design": design, "symbol": s, "rule": rule}, "sample_split": period,
                 "cost_model": cost or {"none": "descriptive price diagnostic, not PnL"},
                 "primary_metric": design["primary_metric"], "rejection_rules": design["rejection_rules"],
                 "evidence_type": design["evidence_type"]})
    datasets = {s: read_bars(data_dir, s)[0] for s in design["symbols"]}
    integrity = data_integrity(datasets, design)
    signals = make_signals(datasets, design)
    save_json(output / "signals.json", signals)
    results, descriptions = [], []
    for key, s, period, rule, cost in cases + diagnostics:
        path = output / f"{key}.json"
        try:
            if s is None:
                r = diagnostic(datasets, signals, period)
            else:
                r = simulate(datasets[s], design, period, cost, "clock_only_long", s,
                             active_dates=active_dates(signals, s, rule))
                r["rule"] = rule
                r["metrics"]["active_model_sessions"] = sum(period["start"] <= d.isoformat() < period["end"] for d in active_dates(signals, s, rule))
                r["limits"].append("QQQ and SPY track different portfolios. No law-of-one-price arbitrage or market-neutral account is modeled. Controls share instrument/time/risk rules but not exact trade count or volatility.")
            r.update(experiment_id=key, data_manifest_sha256=data_hash, code_revision=published_revision)
            save_json(path, r)
            complete(journal, key, "completed", path)
            slim = {k: v for k, v in r.items() if k not in ("trades", "equity_observations", "daily_equity", "daily_rows")}
        except Exception as exc:
            slim = {"experiment_id": key, "symbol": s, "rule": rule, "period": period, "cost_case": cost,
                    "evidence_type": design["evidence_type"], "error": f"{type(exc).__name__}: {exc}"}
            save_json(path, slim)
            complete(journal, key, "error", path)
        (results if s is not None else descriptions).append(slim)
    gates = []
    for s in design["symbols"]:
        windows = []
        for p in design["periods"]:
            by = {(r["rule"], r["cost_case"]["name"]): r for r in results if r["symbol"] == s and r["period"] == p}
            ds = next(r for r in descriptions if r["period"] == p)
            if any("error" in v for v in by.values()) or "error" in ds:
                windows.append({"period": p["name"], "passed": False, "reason": "error"})
                continue
            base = {rule: by[rule, "base_cost_assumption"]["metrics"] for rule in design["relative_model"]["controls"]}
            stress = by["relative_laggard", "double_variable_cost_stress"]["metrics"]
            residual = next(v for v in ds["sides"] if v["symbol"] == s)["mean_oriented_residual_bps"]
            passed = (base["relative_laggard"]["net_period_return"] > max(0, base["previous_eligible_signal"]["net_period_return"], base["clock_only"]["net_period_return"])
                      and stress["net_period_return"] > 0 and residual is not None and residual > 0
                      and not any(v["metrics"]["observed_40pct_breach"] for v in by.values() if v["rule"] == "relative_laggard"))
            windows.append({"period": p["name"], "passed": passed,
                            "base_net_returns": {k: v["net_period_return"] for k, v in base.items()},
                            "candidate_stress_return": stress["net_period_return"], "mean_oriented_residual_bps": residual})
        gates.append({"symbol": s, "advance_to_independent_factor_and_quote_research": all(w["passed"] for w in windows), "windows": windows})
    result = {"experiment_family": design["experiment_family"], "evidence_type": design["evidence_type"],
              "execution_authorized": False, "code_revision": published_revision, "local_code_revision": local,
              "data_manifest_sha256": data_hash, "design_sha256": sha256(HERE / "i037_design.json"),
              "signals_sha256": sha256(output / "signals.json"), "data_integrity": integrity,
              "account_scenarios": len(cases), "diagnostics": len(diagnostics),
              "error_count": sum("error" in r for r in results + descriptions),
              "eligible_sessions": sum(r["eligible"] for r in signals),
              "selection_gates": gates, "results": results, "residual_diagnostics": descriptions}
    save_json(output / "summary.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--published-revision", required=True)
    a = p.parse_args()
    r = run(a.data_dir, a.output, a.published_revision)
    print(json.dumps({k: r[k] for k in ("account_scenarios", "diagnostics", "error_count", "selection_gates")}, indent=2))
