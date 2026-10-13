"""Account-level research controls; returns decisions, never sends orders."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RiskLimits:
    max_drawdown: float = 0.40
    pause_drawdown: float = 0.30
    reduce_drawdown: float = 0.20
    risk_per_trade: float = 0.01

    def __post_init__(self):
        values = (self.max_drawdown, self.pause_drawdown, self.reduce_drawdown, self.risk_per_trade)
        if not all(math.isfinite(x) for x in values):
            raise ValueError("risk limits must be finite")
        if not 0 < self.reduce_drawdown < self.pause_drawdown < self.max_drawdown < 1:
            raise ValueError("require 0 < reduce < pause < maximum drawdown < 1")
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("research risk_per_trade must be in (0, 2%]")


class RiskController:
    """Latched pause after a drawdown; loss observations are never clipped.

    The caller must mark open positions and handle outstanding orders. This
    object does not promise that a stop fills or that losses cannot exceed 40%.
    Cash deposits/withdrawals are not supported by this research controller.
    """

    def __init__(self, initial_equity: float, limits: RiskLimits | None = None):
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("initial equity must be positive and finite")
        self.limits = limits or RiskLimits()
        self.peak = initial_equity
        self.equity = initial_equity
        self.max_observed_drawdown = 0.0
        self.paused = False
        self.ceiling_breached = False

    def observe(self, equity: float) -> dict:
        if not math.isfinite(equity):
            raise ValueError("invalid equity observation")
        self.equity = equity
        self.peak = max(self.peak, equity)
        drawdown = 1 - equity / self.peak
        self.max_observed_drawdown = max(self.max_observed_drawdown, drawdown)
        self.paused |= drawdown >= self.limits.pause_drawdown - 1e-12
        self.ceiling_breached |= drawdown > self.limits.max_drawdown + 1e-12
        scale = 0.0 if self.paused else (0.5 if drawdown >= self.limits.reduce_drawdown - 1e-12 else 1.0)
        return {
            "equity": equity, "peak_equity": self.peak,
            "drawdown": drawdown, "maximum_allowed_drawdown": self.limits.max_drawdown,
            "reference_floor": self.peak * (1 - self.limits.max_drawdown),
            "pause_latched": self.paused, "ceiling_breached": self.ceiling_breached,
            "new_entry_risk_budget": max(0.0, equity) * self.limits.risk_per_trade * scale,
            "action": "cancel_entries_and_request_liquidation" if self.paused else (
                "reduce_new_entry_risk" if scale < 1 else "normal_research_sizing"
            ),
        }
