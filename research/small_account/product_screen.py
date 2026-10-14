#!/usr/bin/env python3
"""USD-only, one-proposal research arithmetic. Never an order authorization.

Inputs describe a proposed trade, not an asset's permanent risk. Stop distances,
fees and margin buffers must come from a separately frozen strategy and current
account data. Multiple proposals here are alternatives, not additive positions.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
from pathlib import Path


CHECKS = (
    "account_permission", "account_minimum_equity", "contract_terms",
    "all_in_costs", "capital_requirement", "executable_quotes",
    "exercise_delivery_funding", "portfolio_risk_headroom",
)
INTEGER_PRODUCTS = {"future", "long_option", "option_spread"}
KINDS = INTEGER_PRODUCTS | {"stock", "etf", "crypto_spot", "fx", "other"}


def number(value, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid {name}") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def display(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def floor_quantity(budget: Decimal, unit_cost: Decimal, step: Decimal) -> Decimal:
    return (max(Decimal(0), budget) / unit_cost / step).to_integral_value(rounding=ROUND_FLOOR) * step


def screen(proposal: dict, risk_budget_usd, available_funds_usd, reserve_usd) -> dict:
    risk_budget = number(risk_budget_usd, "risk budget")
    funds = number(available_funds_usd, "available funds")
    reserve = number(reserve_usd, "reserve")
    if proposal["kind"] not in KINDS:
        raise ValueError("unsupported kind")
    if proposal.get("currency") != "USD":
        raise ValueError("convert all cash and risk inputs to USD before screening")
    step = number(proposal["quantity_step"], "quantity step", True)
    minimum = number(proposal["minimum_quantity"], "minimum quantity", True)
    if minimum % step != 0:
        raise ValueError("minimum quantity must be a multiple of the step")
    if proposal["kind"] in INTEGER_PRODUCTS and (step % 1 != 0 or minimum % 1 != 0):
        raise ValueError("futures and options cannot be sized as fractional contracts")
    variable_cost = number(proposal["variable_round_trip_cost_per_unit_usd"], "variable cost")
    fixed_cost = number(proposal["fixed_round_trip_cost_usd"], "fixed cost")
    risk_unit = number(proposal["loss_before_cost_per_unit_usd"], "loss per unit", True) + variable_cost
    risk_quantity = floor_quantity(risk_budget-fixed_cost, risk_unit, step)
    capital_input = proposal.get("capital_per_unit_usd")
    capital_unit = (number(capital_input, "capital per unit", True) + variable_cost
                    if capital_input is not None else None)
    capital_quantity = (floor_quantity(funds-reserve-fixed_cost, capital_unit, step)
                        if capital_unit is not None else None)
    quantity = min(risk_quantity, capital_quantity) if capital_quantity is not None else None
    reasons = []
    if risk_quantity < minimum:
        reasons.append("minimum_trade_exceeds_planned_risk_budget")
    if capital_quantity is None:
        reasons.append("capital_requirement_unknown")
    elif capital_quantity < minimum:
        reasons.append("minimum_trade_exceeds_available_funds_after_reserve")
    if quantity is not None and quantity < minimum:
        quantity = Decimal(0)
    verified = proposal.get("verified", {})
    if not isinstance(verified, dict) or any(type(v) is not bool for v in verified.values()):
        raise ValueError("verification flags must be explicit booleans")
    missing = [key for key in CHECKS if verified.get(key) is not True]
    if reasons:
        status = "ARITHMETIC_BLOCKED_OR_INCOMPLETE"
    elif missing:
        status = "ARITHMETIC_FITS_BUT_UNVERIFIED"
    else:
        status = "READY_FOR_FURTHER_RESEARCH_ONLY"
    proposed_loss = quantity*risk_unit+fixed_cost if quantity else Decimal(0)
    proposed_capital = quantity*capital_unit+fixed_cost if quantity else Decimal(0)
    if quantity is None:
        proposed_loss = proposed_capital = None
    return {
        "id": proposal["id"], "kind": proposal["kind"], "status": status,
        "risk_basis": proposal["risk_basis"],
        "risk_budget_usd": display(risk_budget),
        "minimum_trade_loss_including_cost_usd": display(minimum*risk_unit+fixed_cost),
        "risk_sized_quantity": display(risk_quantity),
        "capital_sized_quantity": display(capital_quantity),
        "provisional_quantity": display(quantity),
        "provisional_planned_loss_usd": display(proposed_loss),
        "provisional_capital_usd": display(proposed_capital),
        "arithmetic_blockers": reasons, "unverified_requirements": missing,
        "execution_authorized": False,
        "limitations": [
            "A planned stop loss is not a maximum loss; gaps and failed exits can exceed it.",
            "Capital inputs require full margin, premium, funding and liquidity-buffer treatment upstream.",
            "Flags are caller declarations; this tool does not verify evidence, buying power, or account state.",
            "Portfolio risk and latched drawdown pauses must limit the supplied risk budget upstream.",
            "Independent alternatives must not be summed into a simultaneous portfolio.",
            "No return estimate, fill simulation, or brokerage order is produced.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("product_screen_examples.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    account = data["illustrative_account"]
    result = {
        "evidence_type": "synthetic_product_arithmetic_not_backtest",
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "assumptions": data["assumptions"],
        "results": [screen(p, account["risk_budget_usd"], account["available_funds_usd"],
                           account["reserve_usd"]) for p in data["proposals"]],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    for item in result["results"]:
        print(json.dumps({k:item[k] for k in ("id", "status", "minimum_trade_loss_including_cost_usd", "provisional_quantity")}))


if __name__ == "__main__":
    main()
