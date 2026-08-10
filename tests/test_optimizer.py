"""Deterministic tests for the cost model, using synthetic lots (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Filters, Lot, ResolvedPart  # noqa: E402
from app.optimizer import merge_parts, select_candidates, solve  # noqa: E402
from app.shipping import ShippingEstimator, ShippingQuote  # noqa: E402


class FlatShipping(ShippingEstimator):
    """Fixed shipping per store so expected totals are hand-checkable."""

    def __init__(self, per_store: dict[str, float], default: float = 5.0):
        self.per_store = per_store
        self.default = default

    def quote(self, seller, country, lot_count):
        return ShippingQuote(self.per_store.get(seller, self.default), 3, 7, True, "test")

    def base_cost(self, seller, country):
        return self.per_store.get(seller, self.default)


def part(key: str, qty: int) -> ResolvedPart:
    return ResolvedPart(key=key, query=key, item_id=1, part_no=key, name=key,
                        color_id=5, color_name="Red", quantity=qty)


def lot(lid, part_key, seller, price, qty=100, min_buy=0.0) -> Lot:
    return Lot(lot_id=lid, part_key=part_key, part_no=part_key, store_name=seller,
               seller=seller, country="US", feedback=500, unit_price=price,
               quantity=qty, min_buy=min_buy, color_id=5, color_name="Red", condition="N")


def totals(plan):
    return {(l.part_no, s.seller): l.quantity for s in plan.stores for l in s.lines}


def test_shipping_beats_unit_price():
    """One store at a higher unit price wins over two cheap stores + 2x shipping."""
    parts = [part("A", 10), part("B", 10)]
    lots = [
        lot(1, "A", "Cheap1", 0.10), lot(2, "B", "Cheap2", 0.10),   # $2.00 + $10 ship
        lot(3, "A", "OneStop", 0.30), lot(4, "B", "OneStop", 0.30),  # $6.00 + $5 ship
    ]
    plan = solve(parts, lots, FlatShipping({}), Filters(runner_ups=0))[0]
    assert plan.store_count == 1
    assert plan.stores[0].seller == "OneStop"
    assert plan.total == 11.00, plan.total


def test_min_buy_padding_counted():
    """A store with a high minimum loses despite the cheapest parts."""
    parts = [part("A", 5)]
    lots = [
        lot(1, "A", "CheapButMin", 0.10, min_buy=20.0),  # $0.50 -> padded to $20 + $5
        lot(2, "A", "NoMin", 0.50),                      # $2.50 + $5 = $7.50
    ]
    plan = solve(parts, lots, FlatShipping({}), Filters(runner_ups=0))[0]
    assert plan.stores[0].seller == "NoMin"
    assert plan.padding == 0.0
    assert plan.total == 7.50, plan.total


def test_min_buy_padding_when_unavoidable():
    """If the only store has a minimum, the filler cost is reported, not hidden."""
    parts = [part("A", 2)]
    lots = [lot(1, "A", "Only", 0.25, min_buy=15.0)]
    plan = solve(parts, lots, FlatShipping({}), Filters(runner_ups=0))[0]
    assert plan.merchandise == 0.50
    assert plan.padding == 14.50           # topped up to the $15 minimum
    assert plan.total == 20.00, plan.total  # 15 merchandise+filler + 5 shipping


def test_splits_across_stores_when_stock_short():
    """No single store has enough stock, so the order must be split."""
    parts = [part("A", 10)]
    lots = [lot(1, "A", "Small", 0.10, qty=4), lot(2, "A", "Big", 0.20, qty=6)]
    plan = solve(parts, lots, FlatShipping({"Small": 1.0, "Big": 1.0}), Filters(runner_ups=0))[0]
    bought = totals(plan)
    assert sum(bought.values()) == 10
    assert bought.get(("A", "Small")) == 4
    assert bought.get(("A", "Big")) == 6
    assert plan.total == 3.60, plan.total      # 0.40 + 1.20 parts + 2.00 shipping


def test_avoids_split_when_extra_shipping_outweighs_savings():
    """Cheap stock is left on the table when a second parcel costs more than it saves."""
    parts = [part("A", 10)]
    lots = [lot(1, "A", "Small", 0.10, qty=4), lot(2, "A", "Big", 0.20, qty=100)]
    plan = solve(parts, lots, FlatShipping({"Small": 1.0, "Big": 1.0}), Filters(runner_ups=0))[0]
    assert plan.store_count == 1
    assert plan.stores[0].seller == "Big"
    assert plan.total == 3.00, plan.total      # 2.00 parts + 1.00 shipping, beats 3.60


def test_max_stores_constraint():
    parts = [part("A", 5), part("B", 5), part("C", 5)]
    lots = [lot(i, k, f"S{k}", 0.01) for i, k in enumerate("ABC", 1)]
    lots += [lot(10 + i, k, "AllInOne", 0.50) for i, k in enumerate("ABC")]
    plan = solve(parts, lots, FlatShipping({}), Filters(runner_ups=0, max_stores=1))[0]
    assert plan.store_count == 1
    assert plan.stores[0].seller == "AllInOne"


def test_runner_ups_are_distinct_and_ordered():
    parts = [part("A", 1)]
    lots = [lot(i, "A", f"S{i}", 0.10 * i) for i in range(1, 5)]
    plans = solve(parts, lots, FlatShipping({}), Filters(runner_ups=2))
    assert len(plans) == 3
    sellers = [p.stores[0].seller for p in plans]
    assert len(set(sellers)) == 3, sellers            # genuinely different baskets
    assert plans == sorted(plans, key=lambda p: p.total)


def test_merge_parts_sums_duplicates():
    merged = merge_parts([part("A", 4), part("B", 1), part("A", 3)])
    assert len(merged) == 2
    assert {p.key: p.quantity for p in merged} == {"A": 7, "B": 1}


def test_select_candidates_keeps_consolidation_stores():
    """A store that is not cheapest per-part must survive pruning if it covers both."""
    lots_by_part = {
        "A": [lot(i, "A", f"cheapA{i}", 0.01 + i / 1000) for i in range(60)]
             + [lot(900, "A", "Both", 0.90)],
        "B": [lot(100 + i, "B", f"cheapB{i}", 0.01 + i / 1000) for i in range(60)]
             + [lot(901, "B", "Both", 0.90)],
    }
    kept = {l.seller for l in select_candidates(lots_by_part)}
    assert "Both" in kept


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
