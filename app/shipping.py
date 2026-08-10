"""Shipping cost and delivery-time estimates.

Nothing here is fetched -- BrickLink stores describe shipping in prose on their
own terms pages. These are heuristics from config/shipping.yaml, and every
number they produce is flagged as an estimate so the UI can say so.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from .config import CONFIG_DIR


@dataclass(frozen=True)
class ShippingRule:
    base: float
    per_lot: float
    days: tuple[int, int]


@dataclass(frozen=True)
class ShippingQuote:
    cost: float
    days_low: int
    days_high: int
    is_estimate: bool
    source: str  # "override" | "country" | "default"


class ShippingEstimator:
    def __init__(self, path=None):
        cfg = yaml.safe_load((path or CONFIG_DIR / "shipping.yaml").read_text(encoding="utf-8"))
        self.default = self._rule(cfg["default"])
        self.countries = {k.upper(): self._rule(v) for k, v in (cfg.get("countries") or {}).items()}
        self.overrides = {k: self._rule(v) for k, v in (cfg.get("store_overrides") or {}).items()}

    @staticmethod
    def _rule(d: dict) -> ShippingRule:
        lo, hi = d.get("days", [10, 25])
        return ShippingRule(float(d.get("base", 0)), float(d.get("per_lot", 0)), (int(lo), int(hi)))

    def quote(self, seller: str, country: str, lot_count: int) -> ShippingQuote:
        if seller in self.overrides:
            rule, source, estimate = self.overrides[seller], "override", False
        elif country.upper() in self.countries:
            rule, source, estimate = self.countries[country.upper()], "country", True
        else:
            rule, source, estimate = self.default, "default", True
        cost = round(rule.base + rule.per_lot * max(0, lot_count), 2)
        return ShippingQuote(cost, rule.days[0], rule.days[1], estimate, source)

    def base_cost(self, seller: str, country: str) -> float:
        """Cost used inside the MILP, before the real lot count is known."""
        return self.quote(seller, country, 1).cost
