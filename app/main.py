"""FastAPI app: resolve parts, scrape lots, optimize, return ranked plans."""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .bricklink import BrickLinkClient, BrickLinkError
from .config import color_names
from .models import SolveRequest
from .optimizer import InfeasibleError, apply_filters, merge_parts, select_candidates, solve
from .shipping import ShippingEstimator

log = logging.getLogger("lego-buyer")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="LEGO Buyer", description="Least-cost BrickLink basket planner")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/colors")
def colors() -> list[dict]:
    return [{"id": cid, "name": name} for cid, name in sorted(color_names().items(), key=lambda kv: kv[1])]


@app.post("/api/solve")
def api_solve(req: SolveRequest) -> dict:
    if not req.parts:
        raise HTTPException(400, "Add at least one part")

    client = BrickLinkClient()
    warnings: list[str] = []
    try:
        resolved = []
        for line in req.parts:
            try:
                resolved.append(client.resolve_part(line.part, line.color, line.quantity))
            except BrickLinkError as exc:
                raise HTTPException(400, str(exc)) from exc

        parts = merge_parts(resolved)

        lots_by_part = {}
        for p in parts:
            try:
                raw = client.fetch_lots(p, condition=req.filters.condition,
                                        only_countries=req.filters.only_countries)
            except BrickLinkError as exc:
                raise HTTPException(502, f"{p.part_no}: {exc}") from exc
            kept = apply_filters(raw, req.filters)
            if not kept:
                raise HTTPException(
                    400,
                    f"No listings for {p.part_no}"
                    f"{' in ' + p.color_name if p.color_name else ''} match your filters "
                    f"({len(raw)} listings existed before filtering).",
                )
            if len(kept) < len(raw):
                log.info("%s: %d/%d lots kept after filters", p.part_no, len(kept), len(raw))
            lots_by_part[p.key] = kept

        candidates = select_candidates(lots_by_part)
        log.info("optimizing over %d candidate lots from %d stores",
                 len(candidates), len({l.seller for l in candidates}))

        estimator = ShippingEstimator()
        try:
            plans = solve(parts, candidates, estimator, req.filters)
        except InfeasibleError as exc:
            raise HTTPException(400, str(exc)) from exc

        if any(s.shipping_is_estimate for pl in plans for s in pl.stores):
            warnings.append(
                "Shipping costs are estimates from config/shipping.yaml, not real "
                "seller rates. Verify at checkout; pin exact values under "
                "store_overrides to improve future runs."
            )
        if any(pl.padding > 0 for pl in plans):
            warnings.append(
                "Some plans include filler spend to clear a store minimum. That is "
                "money spent on parts you did not ask for."
            )

        return {
            "parts": [
                {
                    "key": p.key, "query": p.query, "part_no": p.part_no, "name": p.name,
                    "color_id": p.color_id, "color_name": p.color_name or "Any",
                    "quantity": p.quantity,
                    "lots_considered": len(lots_by_part[p.key]),
                }
                for p in parts
            ],
            "plans": [asdict(pl) for pl in plans],
            "warnings": warnings,
        }
    finally:
        client.close()
