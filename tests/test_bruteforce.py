"""Cross-check the MILP against exhaustive enumeration on small instances.

For a fixed set of stores and no store minimums, the cheapest assignment is just
"buy each part from the cheapest available lots", so we can enumerate every
store subset and compute the true optimum independently of the solver.
"""
from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Filters, Lot, ResolvedPart  # noqa: E402
from app.optimizer import solve  # noqa: E402
from app.shipping import ShippingEstimator, ShippingQuote  # noqa: E402


class TableShipping(ShippingEstimator):
    def __init__(self, table):
        self.table = table

    def quote(self, seller, country, lot_count):
        return ShippingQuote(self.table[seller], 3, 7, True, "test")

    def base_cost(self, seller, country):
        return self.table[seller]


def brute_force(needs, lots, ship) -> float:
    """Exact optimum by enumerating every subset of stores."""
    stores = sorted({l.seller for l in lots})
    best = float("inf")
    for r in range(1, len(stores) + 1):
        for subset in itertools.combinations(stores, r):
            avail = [l for l in lots if l.seller in subset]
            merch, used, ok = 0.0, set(), True
            for key, qty in needs.items():
                remaining = qty
                for l in sorted([x for x in avail if x.part_key == key], key=lambda x: x.unit_price):
                    if remaining <= 0:
                        break
                    take = min(remaining, l.quantity)
                    merch += take * l.unit_price
                    remaining -= take
                    if take:
                        used.add(l.seller)
                if remaining > 0:
                    ok = False
                    break
            if not ok:
                continue
            best = min(best, merch + sum(ship.table[s] for s in used))
    return round(best, 2)


def test_matches_brute_force_across_random_instances():
    rng = random.Random(20260809)
    for trial in range(25):
        n_parts = rng.randint(2, 4)
        n_stores = rng.randint(2, 6)
        part_keys = [f"P{i}" for i in range(n_parts)]
        stores = [f"S{i}" for i in range(n_stores)]
        ship = TableShipping({s: round(rng.uniform(2.0, 9.0), 2) for s in stores})

        lots, lid = [], 0
        for key in part_keys:
            for s in stores:
                if rng.random() < 0.65:          # not every store carries every part
                    lid += 1
                    lots.append(Lot(
                        lot_id=lid, part_key=key, part_no=key, store_name=s, seller=s,
                        country="US", feedback=100, unit_price=round(rng.uniform(0.05, 2.0), 2),
                        quantity=rng.randint(1, 8), min_buy=0.0, color_id=5,
                        color_name="Red", condition="N"))

        needs = {k: rng.randint(1, 5) for k in part_keys}
        # Skip instances that cannot be satisfied at all.
        if any(sum(l.quantity for l in lots if l.part_key == k) < q for k, q in needs.items()):
            continue

        parts = [ResolvedPart(key=k, query=k, item_id=1, part_no=k, name=k,
                              color_id=5, color_name="Red", quantity=q)
                 for k, q in needs.items()]

        expected = brute_force(needs, lots, ship)
        actual = solve(parts, lots, ship, Filters(runner_ups=0))[0].total
        assert abs(actual - expected) < 0.011, (
            f"trial {trial}: solver {actual} != brute force {expected}")


if __name__ == "__main__":
    try:
        test_matches_brute_force_across_random_instances()
        print("  PASS  test_matches_brute_force_across_random_instances")
        print("\n1/1 passed")
    except AssertionError as exc:
        print(f"  FAIL  {exc}")
        sys.exit(1)
