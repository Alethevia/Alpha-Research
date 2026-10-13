#!/usr/bin/env python3
"""Small-account target arithmetic. Hypothetical scenarios, NEVER a backtest.

Standard-library only. No network, broker, credentials, or order execution.
Costs are round-trip costs in units of the pre-trade risk budget (R).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path


def finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True)
class Scenario:
    trades: int = 1000
    win_probability: float = 0.5
    risk_fraction: float = 0.01
    reward_r: float = 1.5
    cost_r: float = 0.0
    persistence: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.trades, bool) or not isinstance(self.trades, int):
            raise ValueError("trades must be an integer")
        if self.trades <= 0:
            raise ValueError("trades must be positive")
        for name in ("win_probability", "risk_fraction", "reward_r", "cost_r", "persistence"):
            finite(getattr(self, name), name)
        if not 0 <= self.win_probability <= 1:
            raise ValueError("win_probability must be in [0, 1]")
        if not 0 < self.risk_fraction < 1:
            raise ValueError("risk_fraction must be in (0, 1)")
        if self.reward_r <= 0 or self.cost_r < 0:
            raise ValueError("reward_r must be positive and cost_r nonnegative")
        if not 0 <= self.persistence < 1:
            raise ValueError("persistence must be in [0, 1)")
        if self.risk_fraction * (1 + self.cost_r) >= 1:
            raise ValueError("a modeled loss must leave positive equity")

    @property
    def win_log(self) -> float:
        return math.log1p(self.risk_fraction * (self.reward_r - self.cost_r))

    @property
    def loss_log(self) -> float:
        return math.log1p(-self.risk_fraction * (1 + self.cost_r))


def target_arithmetic(capital: float = 2000, annual_return: float = 10) -> dict:
    """annual_return=10 means +1000%, hence 11 times the initial capital."""
    finite(capital, "capital")
    finite(annual_return, "annual_return")
    if capital <= 0 or annual_return < 0:
        raise ValueError("capital must be positive and annual_return nonnegative")
    multiple = 1 + annual_return
    return {
        "initial_capital": capital,
        "annual_return_decimal": annual_return,
        "terminal_multiple": multiple,
        "terminal_equity": capital * multiple,
        "net_profit": capital * annual_return,
        "equivalent_constant_compounded_returns": {
            "monthly_12": math.expm1(math.log(multiple) / 12),
            "weekly_52": math.expm1(math.log(multiple) / 52),
            "trading_day_252": math.expm1(math.log(multiple) / 252),
            "calendar_day_365": math.expm1(math.log(multiple) / 365),
        },
    }


def required_win_fraction(scenario: Scenario, target_multiple: float) -> float:
    """Realized win fraction needed with exactly these two net payoffs.

    Values above one mean unattainable under this specific payoff model.
    This is NOT an estimated win probability or a confidence bound.
    """
    finite(target_multiple, "target_multiple")
    if target_multiple <= 0:
        raise ValueError("target_multiple must be positive")
    return (math.log(target_multiple) / scenario.trades - scenario.loss_log) / (
        scenario.win_log - scenario.loss_log
    )


def binomial_tail(n: int, p: float, minimum: int) -> float:
    """P(K >= minimum) for independent Bernoulli trials, using log-sum-exp."""
    if n < 0 or not 0 <= p <= 1:
        raise ValueError("invalid binomial inputs")
    if minimum <= 0:
        return 1.0
    if minimum > n or p == 0:
        return 0.0
    if p == 1:
        return 1.0
    logs = [
        math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        + k * math.log(p) + (n - k) * math.log1p(-p)
        for k in range(minimum, n + 1)
    ]
    largest = max(logs)
    return min(1.0, math.exp(largest) * math.fsum(math.exp(x - largest) for x in logs))


def analytical_scenario(scenario: Scenario, target_multiple: float = 11) -> dict:
    required = required_win_fraction(scenario, target_multiple)
    # Tolerance only resolves floating-point noise at an integer threshold.
    minimum = math.ceil(required * scenario.trades - 1e-10)
    mean_log = scenario.trades * (
        scenario.win_probability * scenario.win_log
        + (1 - scenario.win_probability) * scenario.loss_log
    )
    return {
        "assumptions": asdict(scenario),
        "minimum_realized_win_fraction": required,
        "minimum_wins": max(0, minimum),
        "attainable_under_assumed_payoffs": minimum <= scenario.trades,
        "exp_expected_log_terminal_multiple": math.exp(mean_log),
        "exp_expected_log_is_not_expected_wealth_or_success_probability": True,
        "iid_exact_terminal_target_probability": (
            binomial_tail(scenario.trades, scenario.win_probability, minimum)
            if scenario.persistence == 0 else None
        ),
    }


def quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    location = p * (len(ordered) - 1)
    lo = math.floor(location)
    hi = math.ceil(location)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (location - lo)


def simulate_scenario(
    scenario: Scenario,
    target_multiple: float = 11,
    paths: int = 3000,
    seed: int = 20261013,
    drawdown_level: float = 0.4,
) -> dict:
    """Conditional simulation with exact fixed payoffs and fractional sizing.

    With persistence rho, P(win next | previous outcome) =
    (1-rho)*p + rho*previous_win. Starting at Bernoulli(p) preserves
    stationary marginal p, while allowing loss/win clustering.
    No strategy stops at the drawdown reporting threshold.
    """
    required_win_fraction(scenario, target_multiple)
    if isinstance(paths, bool) or not isinstance(paths, int) or paths <= 0:
        raise ValueError("paths must be a positive integer")
    finite(drawdown_level, "drawdown_level")
    if not 0 < drawdown_level < 1:
        raise ValueError("drawdown_level must be in (0, 1)")
    rng = random.Random(seed)
    terminal_logs, drawdowns = [], []
    floor_hits = target_hits = joint_hits = losses = 0
    win_log, loss_log = scenario.win_log, scenario.loss_log
    for _ in range(paths):
        equity_log = peak_log = max_dd = 0.0
        previous_win = False
        crossed_half_capital = False
        for step in range(scenario.trades):
            probability = scenario.win_probability
            if step:
                probability = (1 - scenario.persistence) * probability
                probability += scenario.persistence * float(previous_win)
            previous_win = rng.random() < probability
            equity_log += win_log if previous_win else loss_log
            peak_log = max(peak_log, equity_log)
            max_dd = max(max_dd, -math.expm1(equity_log - peak_log))
            crossed_half_capital |= equity_log <= math.log(0.5)
        target_hit = equity_log >= math.log(target_multiple) - 1e-10
        target_hits += target_hit
        joint_hits += target_hit and max_dd <= drawdown_level
        losses += equity_log < 0
        floor_hits += crossed_half_capital
        terminal_logs.append(equity_log)
        drawdowns.append(max_dd)
    target_rate = target_hits / paths
    return {
        "paths": paths,
        "seed": seed,
        "terminal_target_probability_conditional": target_rate,
        "monte_carlo_standard_error_target_only": math.sqrt(target_rate * (1-target_rate) / paths),
        "terminal_loss_probability_conditional": losses / paths,
        "ever_at_or_below_half_initial_capital_probability_conditional": floor_hits / paths,
        "max_drawdown_exceeds_level_probability_conditional": sum(
            x > drawdown_level for x in drawdowns
        ) / paths,
        "terminal_target_and_drawdown_within_level_probability_conditional": joint_hits / paths,
        "drawdown_reporting_level": drawdown_level,
        "drawdown_level_is_a_report_threshold_not_a_stop_rule": True,
        "terminal_multiple_quantiles": {
            str(p): math.exp(quantile(terminal_logs, p)) for p in (0.05, 0.5, 0.95)
        },
        "max_drawdown_quantiles": {str(p): quantile(drawdowns, p) for p in (0.5, 0.95)},
    }


def build_report(capital: float, annual_return: float, paths: int, seed: int) -> dict:
    goal = target_arithmetic(capital, annual_return)
    target = goal["terminal_multiple"]
    cases = {
        "illustrative_iid_zero_cost": Scenario(),
        "illustrative_iid_cost_0_05R": Scenario(cost_r=0.05),
        "illustrative_iid_cost_0_10R": Scenario(cost_r=0.10),
        "illustrative_clustered_zero_cost": Scenario(persistence=0.4),
        "illustrative_edge_decay_p_0_45": Scenario(win_probability=0.45),
    }
    return {
        "evidence_type": "HYPOTHETICAL_SENSITIVITY_ANALYSIS_NOT_A_BACKTEST",
        "goal": goal,
        "limitations": [
            "Probabilities are conditional on invented model inputs, not estimates of live success.",
            "Fractional sizing assumes divisible positions and immediate sequential reinvestment.",
            "No overlapping trades, margin calls, gaps, liquidity limits, or strategy degradation.",
            "Fixed two-point payoffs do not model actual stop execution or tail losses.",
            "cost_r is a round-trip cost divided by the pre-trade risk budget; not a broker quote.",
            "Taxes, infrastructure and data costs, financing and contract rounding are excluded.",
            "Half-capital crossing is not bankruptcy; this positive-payoff model cannot reach zero.",
            "Monte Carlo error does not quantify model uncertainty or uncertainty in the inputs.",
        ],
        "zero_cost_reward_1_5R_target_frontier": [
            {
                "trades": n, "risk_fraction": f,
                "required_realized_win_fraction": required_win_fraction(
                    Scenario(trades=n, risk_fraction=f), target
                ),
            }
            for n in (250, 500, 1000) for f in (0.005, 0.01, 0.02)
        ],
        "hypothetical_scenarios": {
            name: {
                **analytical_scenario(case, target),
                "simulation": simulate_scenario(case, target, paths, seed),
            }
            for name, case in cases.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=2000)
    parser.add_argument("--annual-return", type=float, default=10, help="10 means +1000%%")
    parser.add_argument("--paths", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = build_report(args.capital, args.annual_return, args.paths, args.seed)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
