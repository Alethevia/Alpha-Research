"""Check financial identities and model boundaries, without network access."""

import itertools
import math
import unittest

from target_lab import (
    Scenario, analytical_scenario, binomial_tail, required_win_fraction,
    simulate_scenario, target_arithmetic,
)


class TargetLabTests(unittest.TestCase):
    def test_percent_definition_and_monthly_compounding(self):
        result = target_arithmetic()
        self.assertEqual(result["terminal_equity"], 22000)
        self.assertEqual(result["net_profit"], 20000)
        monthly = result["equivalent_constant_compounded_returns"]["monthly_12"]
        self.assertAlmostEqual(2000 * (1 + monthly)**12, 22000)

    def test_binomial_matches_exhaustive_path_enumeration(self):
        n, p, threshold = 6, 0.37, 4
        expected = sum(
            p**sum(path) * (1-p)**(n-sum(path))
            for path in itertools.product((0, 1), repeat=n) if sum(path) >= threshold
        )
        self.assertAlmostEqual(binomial_tail(n, p, threshold), expected)

    def test_terminal_target_matches_direct_wealth_enumeration(self):
        case = Scenario(trades=8, win_probability=0.4, risk_fraction=0.08, cost_r=0.1)
        expected = 0.0
        for outcomes in itertools.product((0, 1), repeat=8):
            wealth, probability = 1.0, 1.0
            for win in outcomes:
                wealth *= 1.112 if win else 0.912
                probability *= 0.4 if win else 0.6
            if wealth >= 1.2:
                expected += probability
        result = analytical_scenario(case, 1.2)
        self.assertAlmostEqual(result["iid_exact_terminal_target_probability"], expected)

    def test_all_losses_include_initial_equity_in_drawdown(self):
        case = Scenario(trades=10, win_probability=0, risk_fraction=0.1)
        result = simulate_scenario(case, paths=2)
        self.assertAlmostEqual(result["max_drawdown_quantiles"]["0.5"], 1 - 0.9**10)
        self.assertEqual(result["ever_at_or_below_half_initial_capital_probability_conditional"], 1)
        self.assertEqual(result["terminal_target_probability_conditional"], 0)

    def test_all_wins_have_no_drawdown(self):
        case = Scenario(trades=4, win_probability=1, risk_fraction=0.1)
        result = simulate_scenario(case, target_multiple=1.5, paths=3)
        self.assertEqual(result["max_drawdown_quantiles"]["0.95"], 0)
        self.assertEqual(result["terminal_target_probability_conditional"], 1)
        self.assertAlmostEqual(result["terminal_multiple_quantiles"]["0.5"], 1.15**4)

    def test_costs_reduce_success_under_common_random_numbers(self):
        base = simulate_scenario(Scenario(trades=60), target_multiple=1.2, paths=300)
        costly = simulate_scenario(Scenario(trades=60, cost_r=0.2), target_multiple=1.2, paths=300)
        self.assertLessEqual(costly["terminal_target_probability_conditional"], base["terminal_target_probability_conditional"])
        self.assertLess(costly["terminal_multiple_quantiles"]["0.5"], base["terminal_multiple_quantiles"]["0.5"])

    def test_correlated_model_must_not_report_iid_exact_probability(self):
        result = analytical_scenario(Scenario(persistence=0.4))
        self.assertIsNone(result["iid_exact_terminal_target_probability"])

    def test_invalid_inputs_fail_explicitly(self):
        for kwargs in ({"trades": 0}, {"trades": 1.5}, {"trades": True},
                       {"risk_fraction": 0.6, "cost_r": 1}, {"win_probability": math.nan},
                       {"reward_r": -1}, {"persistence": 1}, {"cost_r": math.inf}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Scenario(**kwargs)

    def test_infeasible_frontier_is_not_clipped_into_feasibility(self):
        case = Scenario(trades=1, risk_fraction=0.005)
        self.assertGreater(required_win_fraction(case, 11), 1)
        self.assertFalse(analytical_scenario(case)["attainable_under_assumed_payoffs"])
        self.assertEqual(analytical_scenario(case)["iid_exact_terminal_target_probability"], 0)

    def test_exact_integer_threshold_includes_equality(self):
        target = 1.15**2 * 0.9**2
        result = analytical_scenario(Scenario(trades=4, risk_fraction=0.1), target)
        self.assertEqual(result["minimum_wins"], 2)
        self.assertAlmostEqual(result["iid_exact_terminal_target_probability"], 11/16)

    def test_simulations_are_reproducible(self):
        case = Scenario(trades=30, persistence=0.3)
        self.assertEqual(simulate_scenario(case, paths=40, seed=8), simulate_scenario(case, paths=40, seed=8))


if __name__ == "__main__":
    unittest.main()
