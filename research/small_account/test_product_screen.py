import unittest

from product_screen import CHECKS, screen


def proposal(**updates):
    p = {"id":"test", "kind":"future", "currency":"USD", "quantity_step":"1",
         "minimum_quantity":"1", "loss_before_cost_per_unit_usd":"10",
         "variable_round_trip_cost_per_unit_usd":"2", "fixed_round_trip_cost_usd":"0",
         "capital_per_unit_usd":"353.70", "risk_basis":"planned_stop_not_max_loss",
         "verified":{}}
    p.update(updates)
    return p


class ProductScreenTests(unittest.TestCase):
    def test_integer_contract_risk_and_margin_constraints(self):
        nano = screen(proposal(), "20", "2000", "300")
        self.assertEqual(nano["provisional_quantity"], "1")
        micro = screen(proposal(loss_before_cost_per_unit_usd="100", capital_per_unit_usd="3538.14"), "20", "2000", "300")
        self.assertEqual(micro["provisional_quantity"], "0")
        self.assertEqual(len(micro["arithmetic_blockers"]), 2)

    def test_missing_margin_is_not_treated_as_zero(self):
        r = screen(proposal(capital_per_unit_usd=None), "20", "2000", "300")
        self.assertIsNone(r["provisional_quantity"])
        self.assertIn("capital_requirement_unknown", r["arithmetic_blockers"])

    def test_option_contract_cannot_be_fractional(self):
        with self.assertRaises(ValueError):
            screen(proposal(kind="long_option", quantity_step="0.1"), "20", "2000", "0")

    def test_decimal_budget_boundary_does_not_round_up_or_down(self):
        r = screen(proposal(loss_before_cost_per_unit_usd="0.1", variable_round_trip_cost_per_unit_usd="0", capital_per_unit_usd="1"), "0.3", "2000", "0")
        self.assertEqual(r["provisional_quantity"], "3")
        self.assertEqual(r["provisional_planned_loss_usd"], "0.3")

    def test_fixed_order_minimum_can_consume_entire_budget(self):
        r = screen(proposal(fixed_round_trip_cost_usd="20"), "20", "2000", "0")
        self.assertEqual(r["provisional_quantity"], "0")

    def test_permissions_and_fills_not_inferred_from_small_margin(self):
        r = screen(proposal(), "20", "2000", "0")
        self.assertEqual(r["status"], "ARITHMETIC_FITS_BUT_UNVERIFIED")
        self.assertIn("executable_quotes", r["unverified_requirements"])
        verified = screen(proposal(verified={k:True for k in CHECKS}), "20", "2000", "0")
        self.assertFalse(verified["execution_authorized"])

    def test_paused_zero_risk_budget_blocks_entry(self):
        r = screen(proposal(), "0", "2000", "0")
        self.assertEqual(r["provisional_quantity"], "0")

    def test_invalid_numbers_and_unconverted_currency_rejected(self):
        for update in ({"loss_before_cost_per_unit_usd":"NaN"}, {"capital_per_unit_usd":"-1"},
                       {"currency":"EUR"}, {"verified":{"account_permission":"yes"}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                screen(proposal(**update), "20", "2000", "0")


if __name__ == "__main__":
    unittest.main()
