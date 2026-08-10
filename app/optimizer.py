"""Minimum-cost basket solver.

The problem is a mixed-integer program. For each lot we choose a quantity, and
for each store a use/don't-use binary that switches on its shipping cost and
its minimum-spend requirement.

    minimise   sum(unit_price * qty)          merchandise
             + sum(shipping_s * use_s)        one shipping charge per store used
             + sum(pad_s)                     filler needed to clear a minimum

Modelling note: a store's minimum spend cannot be satisfied by buying *more* of
the parts you need, because the required quantities are fixed. In practice you
pad the order with filler parts you don't want. That is what `pad_s` represents,
and it is reported separately so a plan that leans on filler is obvious.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace

import pulp

from .models import Filters, Lot, ResolvedPart
from .shipping import ShippingEstimator

# Candidate pruning. BrickLink returns up to 500 lots per part; feeding all of
# them to CBC is pointless because the optimum is built from cheap lots plus a
# few stores that can consolidate several parts into one shipment.
CHEAPEST_PER_PART = 40
MAX_CONSOLIDATION_STORES = 250
SOLVE_TIME_LIMIT_SECONDS = 30


@dataclass
class PlanLine:
    part_key: str
    part_no: str
    part_name: str
    color_name: str
    quantity: int
    unit_price: float
    subtotal: float
    condition: str


@dataclass
class PlanStore:
    seller: str
    store_name: str
    country: str
    feedback: int
    store_url: str
    merchandise: float
    padding: float
    min_buy: float
    shipping: float
    shipping_is_estimate: bool
    days_low: int
    days_high: int
    lines: list[PlanLine] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(self.merchandise + self.padding + self.shipping, 2)


@dataclass
class Plan:
    rank: int
    total: float
    merchandise: float
    shipping: float
    padding: float
    store_count: int
    days_low: int
    days_high: int
    stores: list[PlanStore] = field(default_factory=list)


class InfeasibleError(RuntimeError):
    pass


def merge_parts(parts: list[ResolvedPart]) -> list[ResolvedPart]:
    """Collapse lines that resolved to the same part+color, summing quantities.

    Typing "3001" in Red and the element ID "300121" produces two lines for one
    physical piece; they must become a single requirement of the combined count.
    """
    merged: dict[str, ResolvedPart] = {}
    for p in parts:
        prev = merged.get(p.key)
        merged[p.key] = replace(p, quantity=prev.quantity + p.quantity) if prev else p
    return list(merged.values())


def apply_filters(lots: list[Lot], f: Filters) -> list[Lot]:
    only = {c.upper() for c in f.only_countries if c.strip()}
    excl = {c.upper() for c in f.exclude_countries if c.strip()}
    out = []
    for lot in lots:
        if lot.quantity < 1 or lot.unit_price <= 0:
            continue
        if f.condition in ("N", "U") and lot.condition != f.condition:
            continue
        if lot.feedback < f.min_feedback:
            continue
        if only and lot.country not in only:
            continue
        if lot.country in excl:
            continue
        out.append(lot)
    return out


def select_candidates(lots_by_part: dict[str, list[Lot]]) -> list[Lot]:
    """Keep the cheapest lots per part, plus lots from stores that can cover
    several parts at once (those are what make consolidation wins possible)."""
    chosen: dict[int, Lot] = {}

    for lots in lots_by_part.values():
        for lot in sorted(lots, key=lambda l: l.unit_price)[:CHEAPEST_PER_PART]:
            chosen[lot.lot_id] = lot

    # Cheapest lot per (store, part) for any store carrying 2+ requested parts.
    by_store: dict[str, dict[str, Lot]] = defaultdict(dict)
    for lots in lots_by_part.values():
        for lot in lots:
            cur = by_store[lot.seller].get(lot.part_key)
            if cur is None or lot.unit_price < cur.unit_price:
                by_store[lot.seller][lot.part_key] = lot

    multi = [(s, d) for s, d in by_store.items() if len(d) >= 2]
    multi.sort(key=lambda kv: (-len(kv[1]), sum(l.unit_price for l in kv[1].values())))
    for _, per_part in multi[:MAX_CONSOLIDATION_STORES]:
        for lot in per_part.values():
            chosen[lot.lot_id] = lot

    return list(chosen.values())


def _build_and_solve(
    parts: list[ResolvedPart],
    lots: list[Lot],
    estimator: ShippingEstimator,
    filters: Filters,
    banned_store_sets: list[frozenset[str]],
) -> tuple[dict[int, int], set[str]] | None:
    """Solve once, excluding any previously-seen store combination."""
    # Sum rather than assign: the same part+color can arrive as two lines (e.g.
    # typed as a part number once and as a LEGO element ID once).
    need: dict[str, int] = defaultdict(int)
    for p in parts:
        need[p.key] += p.quantity
    lots_by_id = {l.lot_id: l for l in lots}
    sellers = sorted({l.seller for l in lots})
    lots_by_seller: dict[str, list[Lot]] = defaultdict(list)
    lots_by_part: dict[str, list[Lot]] = defaultdict(list)
    for l in lots:
        lots_by_seller[l.seller].append(l)
        lots_by_part[l.part_key].append(l)

    prob = pulp.LpProblem("lego_basket", pulp.LpMinimize)
    x = {l.lot_id: pulp.LpVariable(f"x{l.lot_id}", lowBound=0, upBound=l.quantity, cat="Integer")
         for l in lots}
    y = {s: pulp.LpVariable(f"y_{i}", cat="Binary") for i, s in enumerate(sellers)}
    pad = {s: pulp.LpVariable(f"p_{i}", lowBound=0) for i, s in enumerate(sellers)}

    ship = {s: estimator.base_cost(s, lots_by_seller[s][0].country) for s in sellers}
    minbuy = {s: max((l.min_buy for l in lots_by_seller[s]), default=0.0) for s in sellers}

    prob += (
        pulp.lpSum(lots_by_id[i].unit_price * v for i, v in x.items())
        + pulp.lpSum(ship[s] * y[s] for s in sellers)
        + pulp.lpSum(pad.values())
    )

    for key, qty in need.items():
        candidates = lots_by_part.get(key, [])
        if not candidates:
            raise InfeasibleError(f"No lots available for {key} after filters")
        if sum(l.quantity for l in candidates) < qty:
            raise InfeasibleError(
                f"Only {sum(l.quantity for l in candidates)} of {key} available, need {qty}"
            )
        prob += pulp.lpSum(x[l.lot_id] for l in candidates) == qty

    for s in sellers:
        merch = pulp.lpSum(l.unit_price * x[l.lot_id] for l in lots_by_seller[s])
        for l in lots_by_seller[s]:
            prob += x[l.lot_id] <= l.quantity * y[s]
        prob += pad[s] >= minbuy[s] * y[s] - merch
        prob += pad[s] <= minbuy[s] * y[s]

    if filters.max_stores:
        prob += pulp.lpSum(y.values()) <= filters.max_stores

    # No-good cuts: force a different store combination than each prior plan.
    for banned in banned_store_sets:
        inside = [y[s] for s in banned if s in y]
        outside = [y[s] for s in sellers if s not in banned]
        if inside:
            prob += pulp.lpSum(inside) - pulp.lpSum(outside) <= len(inside) - 1

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=SOLVE_TIME_LIMIT_SECONDS))
    if pulp.LpStatus[status] != "Optimal":
        return None

    picked = {i: int(round(v.value() or 0)) for i, v in x.items() if (v.value() or 0) > 0.5}
    used = {s for s in sellers if (y[s].value() or 0) > 0.5}
    return picked, used


def solve(
    parts: list[ResolvedPart],
    lots: list[Lot],
    estimator: ShippingEstimator,
    filters: Filters,
) -> list[Plan]:
    """Return the optimal plan followed by up to `filters.runner_ups` alternates."""
    part_by_key = {p.key: p for p in parts}
    lots_by_id = {l.lot_id: l for l in lots}
    plans: list[Plan] = []
    banned: list[frozenset[str]] = []

    for rank in range(1 + filters.runner_ups):
        result = _build_and_solve(parts, lots, estimator, filters, banned)
        if result is None:
            break
        picked, used = result
        banned.append(frozenset(used))
        plans.append(_assemble(rank + 1, picked, lots_by_id, part_by_key, estimator))

    if not plans:
        raise InfeasibleError("No feasible basket found for these parts and filters")
    return plans


def _assemble(
    rank: int,
    picked: dict[int, int],
    lots_by_id: dict[int, Lot],
    part_by_key: dict[str, ResolvedPart],
    estimator: ShippingEstimator,
) -> Plan:
    grouped: dict[str, list[tuple[Lot, int]]] = defaultdict(list)
    for lot_id, qty in picked.items():
        grouped[lots_by_id[lot_id].seller].append((lots_by_id[lot_id], qty))

    stores: list[PlanStore] = []
    for seller, entries in grouped.items():
        first = entries[0][0]
        quote = estimator.quote(seller, first.country, len(entries))
        merch = round(sum(l.unit_price * q for l, q in entries), 2)
        min_buy = max(l.min_buy for l, _ in entries)
        padding = round(max(0.0, min_buy - merch), 2)

        lines = [
            PlanLine(
                part_key=l.part_key,
                part_no=l.part_no,
                part_name=part_by_key[l.part_key].name if l.part_key in part_by_key else "",
                color_name=l.color_name,
                quantity=q,
                unit_price=l.unit_price,
                subtotal=round(l.unit_price * q, 2),
                condition=l.condition,
            )
            for l, q in sorted(entries, key=lambda e: e[0].part_no)
        ]
        stores.append(
            PlanStore(
                seller=seller,
                store_name=first.store_name,
                country=first.country,
                feedback=first.feedback,
                store_url=f"https://store.bricklink.com/{seller}",
                merchandise=merch,
                padding=padding,
                min_buy=min_buy,
                shipping=quote.cost,
                shipping_is_estimate=quote.is_estimate,
                days_low=quote.days_low,
                days_high=quote.days_high,
                lines=lines,
            )
        )

    stores.sort(key=lambda s: -s.total)
    merchandise = round(sum(s.merchandise for s in stores), 2)
    shipping = round(sum(s.shipping for s in stores), 2)
    padding = round(sum(s.padding for s in stores), 2)
    return Plan(
        rank=rank,
        total=round(merchandise + shipping + padding, 2),
        merchandise=merchandise,
        shipping=shipping,
        padding=padding,
        store_count=len(stores),
        # Everything ships in parallel, so the wait is the slowest store.
        days_low=max((s.days_low for s in stores), default=0),
        days_high=max((s.days_high for s in stores), default=0),
        stores=stores,
    )
