"""Exact arithmetic checks for conditional propositions, NOT a market backtest.

All cash, prices, probabilities and graph fixtures below are hypothetical.
The accompanying report supplies assumptions and algebraic proofs. Enumerating
finite examples is not a proof of universal market profitability or novelty.
"""
from __future__ import annotations

from fractions import Fraction as F
from heapq import heappop, heappush
import json
from typing import Mapping


def peak_prefix_exposure(ratio: int, nanos_before_micro: int) -> int:
    """One +ratio fill and ratio -1 fills; order is confirmed, not just sent."""
    if not isinstance(ratio, int) or ratio < 1:
        raise ValueError("positive integer ratio required")
    if not 0 <= nanos_before_micro <= ratio:
        raise ValueError("invalid insertion point")
    fills = ([-1] * nanos_before_micro + [ratio]
             + [-1] * (ratio - nanos_before_micro))
    exposure = peak = 0
    for fill in fills:
        exposure += fill
        peak = max(peak, abs(exposure))
    assert exposure == 0
    return peak


def variance_optimal_split(ratio: int, t_before: F, t_after: F,
                           peak_limit: int | None = None) -> tuple[int, F]:
    """Ideal instantaneous batches, fixed delays independent of size/prices.

    Minimize k^2*t_before + (ratio-k)^2*t_after subject to peak exposure.
    Common-factor Brownian variance is this coefficient times sigma^2*m^2.
    Actual stochastic partial-fill delays require a different model.
    """
    if ratio < 1 or not isinstance(ratio, int):
        raise ValueError("positive integer ratio required")
    if t_before <= 0 or t_after <= 0:
        raise ValueError("strictly positive fixed intervals required")
    limit = ratio if peak_limit is None else peak_limit
    if not isinstance(limit, int) or limit < (ratio + 1) // 2:
        raise ValueError("infeasible integer peak limit")
    lo, hi = max(0, ratio - limit), min(ratio, limit)
    center = F(ratio) * t_after / (t_before + t_after)
    floor = center.numerator // center.denominator
    candidates = {max(lo, min(hi, floor)), max(lo, min(hi, floor + 1))}
    value = lambda k: k * k * t_before + (ratio - k) ** 2 * t_after
    best = min(candidates, key=lambda k: (value(k), k))
    return best, value(best)


def joint_failure_value(q_a: F, q_b: F, both_fail: F,
                       gain: F, one_fail_loss: F, overhead: F) -> F:
    """Both fill: +gain; exactly one fills: -loss; neither: 0; pay overhead."""
    if not 0 <= q_a <= 1 or not 0 <= q_b <= 1:
        raise ValueError("invalid marginal probability")
    if not max(F(0), q_a + q_b - 1) <= both_fail <= min(q_a, q_b):
        raise ValueError("infeasible dependence")
    p_success = 1 - q_a - q_b + both_fail
    p_one_failure = q_a + q_b - 2 * both_fail
    return p_success * gain - p_one_failure * one_fail_loss - overhead


def conservative_value(gain: F, loss_bound: F, overhead: F,
                       failure_bounds: list[F]) -> F:
    """Union-bound lower value; inputs must be conditional on the signal/policy."""
    if gain < 0 or loss_bound < 0 or overhead < 0:
        raise ValueError("nonnegative amounts required")
    if any(not 0 <= q <= 1 for q in failure_bounds):
        raise ValueError("invalid probability")
    q = min(F(1), sum(failure_bounds, F(0)))
    return (1 - q) * gain - q * loss_bound - overhead


def minimum_path_capital(edges: Mapping[str, list[str]],
                         required: Mapping[str, int], start: str, goal: str):
    """Deterministic bottleneck path, not an adversarial execution certificate.

    required[v] is EXTERNAL capital required at the full state v, including
    cumulative cash debits, fees, collateral and reserved unwind cash.
    Prices, fill ordering, and reachable nodes must already be specified.
    """
    if start not in required or goal not in required:
        raise ValueError("missing state capital")
    heap = [(required[start], start, (start,))]
    best = {start: required[start]}
    while heap:
        cost, node, path = heappop(heap)
        if cost != best[node]:
            continue
        if node == goal:
            return cost, path
        for nxt in edges.get(node, []):
            cand = max(cost, required[nxt])
            if nxt not in best or cand < best[nxt]:
                best[nxt] = cand
                heappush(heap, (cand, nxt, path + (nxt,)))
    return None, ()


def robust_one_step_value(actions: Mapping[str, list[F]]) -> tuple[str, F]:
    """All outcomes must be included. Abstention is a zero-P&L choice."""
    values = {"abstain": F(0)}
    for action, outcomes in actions.items():
        if not outcomes:
            raise ValueError("an action needs a nonempty outcome set")
        values[action] = min(outcomes)
    action = max(values, key=values.get)
    return action, values[action]


def floor_to_grid(value: F, tick: F) -> F:
    if tick <= 0:
        raise ValueError("positive tick required")
    return (value // tick) * tick


def ceil_to_grid(value: F, tick: F) -> F:
    if tick <= 0:
        raise ValueError("positive tick required")
    return -((-value) // tick) * tick


def inventory_cycle_cash(q: F, buy_a: F, sell_b: F,
                         buy_b: F, sell_a: F, all_fees: F) -> F:
    """Quote-currency fees; completed four fills restore asset units per venue.

    This is incremental trading cash relative to retaining initial inventory.
    It is not whole-account profit: initial coins still have market exposure.
    """
    return q * (sell_b - buy_a + sell_a - buy_b) - all_fees


def run_checks() -> dict:
    # Independent direct enumeration of all insertion points for many ratios.
    arrangements = 0
    for ratio in range(1, 101):
        observed = [peak_prefix_exposure(ratio, k) for k in range(ratio + 1)]
        assert observed == [max(k, ratio - k) for k in range(ratio + 1)]
        assert min(observed) == (ratio + 1) // 2
        arrangements += len(observed)
    r10 = [peak_prefix_exposure(10, k) for k in range(11)]
    # Real multiplier only; every price shock/cost elsewhere is hypothetical.
    multiplier = F(1, 2)
    assert multiplier * r10[0] == 5
    assert multiplier * r10[5] == F(5, 2)
    # Smaller peak exposure need not mean less time-integrated variance.
    naive_variance_coefficient = F(10) ** 2 * F(1, 1000)
    split_variance_coefficient = 2 * F(5) ** 2 * F(1)
    assert split_variance_coefficient > naive_variance_coefficient
    # Not an assertion about observed latency or Brownian markets.
    variance_cases = 0
    for ratio in range(1, 41):
        for before, after in [(F(1), F(1)), (F(9, 10), F(1, 10)),
                              (F(1, 10), F(9, 10)), (F(1, 1000), F(2))]:
            for limit in range((ratio + 1) // 2, ratio + 1):
                best, value = variance_optimal_split(ratio, before, after, limit)
                # Independent all-feasible-splits check, including odd ratios.
                feasible = [k for k in range(ratio + 1)
                            if peak_prefix_exposure(ratio, k) <= limit]
                oracle = min(k*k*before + (ratio-k)**2*after for k in feasible)
                assert value == oracle and best in feasible
                variance_cases += 1
    asym_k, asym_value = variance_optimal_split(10, F(9, 10), F(1, 10))
    bounded_k, bounded_value = variance_optimal_split(10, F(9, 10), F(1, 10), 5)
    assert (asym_k, asym_value) == (1, F(9))
    assert (bounded_k, bounded_value) == (5, F(25))

    # Exact feasible dependence family with unchanged 10% marginal failures.
    qa = qb = F(1, 10)
    gain, loss, overhead = F(5), F(20), F(1, 8)
    values = [joint_failure_value(qa, qb, F(i, 10000), gain, loss, overhead)
              for i in range(1001)]
    independent = joint_failure_value(qa, qb, qa * qb, gain, loss, overhead)
    worst = conservative_value(gain, loss, overhead, [qa, qb])
    assert independent == F(13, 40)  # +$0.325
    assert min(values) == worst == F(-1, 8)  # -$0.125
    assert values[-1] == F(35, 8)

    # Final state looks affordable while EVERY deterministic entry path fails.
    required = {"start": 0, "naked": 2500, "flat_goal": 0}
    capital, path = minimum_path_capital(
        {"start": ["naked"], "naked": ["flat_goal"]},
        required, "start", "flat_goal")
    assert capital == 2500 and capital > 2000
    # A hypothetical added route can help; it is not a verified broker route.
    req2 = {**required, "protected": 1200, "hedged": 1800}
    cap2, path2 = minimum_path_capital(
        {"start": ["naked", "protected"], "naked": ["flat_goal"],
         "protected": ["hedged"], "hedged": ["flat_goal"]},
        req2, "start", "flat_goal")
    assert cap2 == 1800
    assert minimum_path_capital({}, required, "start", "flat_goal") == (None, ())
    # A successful path is not a robust policy when an adverse fill is possible.
    choice, robust = robust_one_step_value({"submit": [F(6), F(-7)]})
    assert (choice, robust) == ("abstain", F(0))

    # Tick rounding of one simultaneous common value cannot create a crossing.
    grid_checks = 0
    for i in range(2001):
        common = F(100) + F(i, 1000)
        for buy_tick, sell_tick in [(F(1, 4), F(1, 2)), (F(1, 2), F(1, 4))]:
            ask = ceil_to_grid(common, buy_tick)
            bid = floor_to_grid(common, sell_tick)
            assert bid - ask <= 0
            grid_checks += 1

    # Positive first dislocation, negative full reset cycle.
    quantity = F(1, 100)
    cycle = inventory_cycle_cash(quantity, F(10000), F(10010),
                                 F(10020), F(10000), F(0))
    assert quantity * (F(10010) - F(10000)) == F(1, 10)
    assert cycle == F(-1, 10)
    reverse_good = inventory_cycle_cash(quantity, F(10000), F(10010),
                                        F(10000), F(10020), F(1, 20))
    assert reverse_good == F(1, 4)
    # Cash payoff at maturity does not bound the bid on the short horizon.
    entry, short_horizon_bid, final_payoff = F(95), F(90), F(100)
    assert final_payoff - entry == 5
    assert short_horizon_bid - entry == -5

    return {
        "evidence_type": "synthetic_exact_arithmetic_and_counterexamples",
        "market_data_used": False,
        "new_performance_backtests": 0,
        "novelty_verified": False,
        "profitable_strategy_validated": False,
        "N01": {"ratios_checked": 100, "arrangements_checked": arrangements,
                "ratio_10_peak_units_by_insertion": r10,
                "MES_NES_peak_usd_per_point_first_micro": 5,
                "MES_NES_peak_usd_per_point_middle_micro": 2.5,
                "synthetic_variance_coeff_naive": str(naive_variance_coefficient),
                "synthetic_variance_coeff_split": str(split_variance_coefficient),
                "fixed_latency_optimality_cases_checked": variance_cases,
                "asymmetric_latency_unconstrained_split": asym_k,
                "asymmetric_latency_unconstrained_variance_coeff": str(asym_value),
                "asymmetric_latency_peak_5_split": bounded_k,
                "asymmetric_latency_peak_5_variance_coeff": str(bounded_value),
                "meaning": "Peak position exposure only; not a margin, total-risk or profit bound."},
        "N02": {"joint_distributions_checked": len(values),
                "independent_expected_usd": str(independent),
                "worst_dependent_expected_usd": str(worst),
                "meaning": "Same marginal fill rates permit opposite expected-PnL signs."},
        "N03": {"original_minimum_capital": capital,
                "hypothetical_added_route_capital": cap2,
                "added_path": path2, "adverse_branch_optimal_action": choice,
                "meaning": "All graph states, prices, collateral and outcomes are fictional."},
        "N04": {"grid_inequalities_checked": grid_checks,
                "decision": "Reject grid-rounding-only executable arbitrage under common-value assumptions."},
        "N05": {"first_trade_incremental_cash_usd": "1/10",
                "forced_reset_cycle_cash_usd": str(cycle),
                "favorable_reset_cycle_cash_usd": str(reverse_good),
                "decision": "Reject guaranteed finite-time reset; full account still carries seed inventory risk."},
        "N06": {"maturity_pnl_usd": 5, "short_horizon_liquidation_pnl_usd": -5,
                "decision": "Reject inference from maturity payoff to intraday cashout."},
    }


if __name__ == "__main__":
    print(json.dumps(run_checks(), indent=2, ensure_ascii=False))
