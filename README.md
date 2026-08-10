# lego-buyer

Find the cheapest way to buy a set of missing LEGO pieces across BrickLink stores.

Buying replacement parts is not a "sort by price" problem. Store A may sell your
part cheaper than Store B, but Store A has a $10 minimum and charges shipping you
would pay twice if you split the order. The cheapest basket is the one that
balances part price, per-store shipping, and store minimums at the same time.
This solves that as a mixed-integer program and shows you the runners-up.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Add pieces one at a time (part number, quantity,
optional color), then hit **Find cheapest basket**.

You can type either a **BrickLink part number** (`3001`) or a **LEGO.com element
ID** (`300121`). Element IDs encode the color, so the color field fills itself in.

## What the numbers mean

| Column | Where it comes from |
| --- | --- |
| Part price, quantity, store, feedback, country | Live from BrickLink |
| Store minimum buy | Live from BrickLink |
| **Shipping cost** | **Estimated** from `config/shipping.yaml` |
| **Delivery time** | **Estimated** from `config/shipping.yaml` |
| Filler | Computed: what you must overspend to clear a store minimum |

### Shipping is an estimate, and it matters

BrickLink sellers publish shipping terms as free-form prose on their own store
pages. There is no API and no consistent format, so shipping cannot be fetched.
It is estimated per seller country in `config/shipping.yaml`.

This is not a rounding error: on a typical small parts order, **shipping is 80–95%
of the total**. The ranking between plans is therefore only as good as those
estimates. Once you know a store's real rate, pin it and every future run gets
more accurate:

```yaml
store_overrides:
  SomeSeller: { base: 4.25, per_lot: 0.0, days: [2, 4] }
```

Pinned stores are shown without the `~` estimate marker.

### Filler for store minimums

A store minimum cannot be met by buying more of the parts you need — those
quantities are fixed. In reality you pad the cart with parts you don't want. The
solver prices that padding honestly and reports it separately, so a plan that
only looks cheap because it ignores a $20 minimum can't hide.

## How it picks

For each requested part it pulls up to 500 listings (cheapest first), applies
your filters, then optimizes over the cheapest lots per part plus every store
that can cover two or more of your parts — those are what make single-shipment
consolidation possible.

```
minimise  sum(unit_price x qty)        parts
        + sum(shipping_s x used_s)     one shipping charge per store used
        + sum(padding_s)               filler to clear a store minimum
```

subject to exact quantities per part, per-lot stock limits, and an optional cap
on the number of stores. Runner-ups come from re-solving with a constraint that
forbids each store combination already shown, so they're genuinely different
baskets rather than trivial reshuffles.

## Filters

Condition (new/used/any), minimum seller feedback, country allow/deny lists, and
a maximum number of stores. Restricting to a single country is also pushed down
into the BrickLink query, so you get 500 *US* listings instead of 500 worldwide
ones — worth doing when shipping domestically.

## Tests

```bash
.venv/bin/python tests/test_optimizer.py    # cost-model behaviour, no network
.venv/bin/python tests/test_bruteforce.py   # MILP vs exhaustive enumeration
```

## Caveats

- **This scrapes BrickLink.** Its official API is a *store* API and exposes no
  cross-store listing search, so this reads the same JSON endpoints the catalog
  pages call. That's against BrickLink's terms of service. Requests are
  serialized ~1/sec and cached for 6 hours; keep it to personal use. If BrickLink
  changes those endpoints, this breaks.
- **Prices must come back in USD.** BrickLink converts to a geo-detected display
  currency; the client raises a clear error rather than silently mixing currencies.
- Quantity-tier discounts are captured in the raw data but not yet used in the
  optimization, so large quantities may be slightly cheaper than quoted.
- Availability moves. Verify in the cart before paying.
