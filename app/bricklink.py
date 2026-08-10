"""Scraping client for BrickLink's public AJAX endpoints.

BrickLink's official API is a *store* API: it exposes your own inventory and
aggregate price-guide stats, but nothing that lists lots for sale across all
stores. That is exactly the data this tool needs, so we read the same JSON
endpoints the catalog pages themselves call.

This is unofficial and unsupported. Everything is cached to disk and requests
are serialized behind a delay so a run costs BrickLink a handful of hits.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .config import (
    CACHE_DIR,
    CACHE_TTL_SECONDS,
    EXPECTED_CURRENCY_PREFIX,
    LOTS_PER_PAGE,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    color_id_by_name,
    color_names,
)
from .models import Lot, ResolvedPart

SEARCH_URL = "https://www.bricklink.com/ajax/clone/search/searchproduct.ajax"
IFS_URL = "https://www.bricklink.com/ajax/clone/catalogifs.ajax"

_PRICE_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
_PCC_RE = re.compile(r"(\d+)\s*\((\d+)\)")


class BrickLinkError(RuntimeError):
    pass


def parse_price(raw: str | None) -> float:
    """'US $1,499.00' -> 1499.0. Returns 0.0 for 'None'/blank."""
    if not raw or raw.strip().lower() == "none":
        return 0.0
    if not raw.startswith(EXPECTED_CURRENCY_PREFIX):
        raise BrickLinkError(
            f"Expected USD prices but BrickLink returned {raw!r}. "
            "Its display currency is geo-detected; a USD VPN/exit or a BrickLink "
            "session set to USD is required."
        )
    m = _PRICE_RE.search(raw)
    if not m:
        raise BrickLinkError(f"Could not parse price {raw!r}")
    return float(m.group(1).replace(",", ""))


class BrickLinkClient:
    def __init__(self, cache_dir: Path = CACHE_DIR, delay: float = REQUEST_DELAY_SECONDS):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self._last_request = 0.0
        self._client = httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def close(self) -> None:
        self._client.close()

    # ---- plumbing -------------------------------------------------------

    def _cache_path(self, url: str, params: dict[str, Any]) -> Path:
        key = hashlib.sha256(
            (url + json.dumps(params, sort_keys=True)).encode()
        ).hexdigest()[:32]
        return self.cache_dir / f"{key}.json"

    def _get_json(self, url: str, params: dict[str, Any], referer: str) -> dict:
        path = self._cache_path(url, params)
        if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                path.unlink(missing_ok=True)

        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            wait = self.delay - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self._client.get(url, params=params, headers={"Referer": referer})
                self._last_request = time.time()
                if resp.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_err = exc
                time.sleep(2 * (attempt + 1))
                continue
            path.write_text(json.dumps(data), encoding="utf-8")
            return data

        raise BrickLinkError(f"BrickLink request failed after {MAX_RETRIES} tries: {last_err}")

    # ---- catalog resolution ---------------------------------------------

    def resolve_part(self, query: str, color: str | None, quantity: int) -> ResolvedPart:
        """Turn a user line into a concrete BrickLink item id + color id.

        `query` may be a part number ("3001") or a LEGO element ID ("300121"),
        in which case the color is taken from BrickLink's part-color-code field
        unless the user explicitly supplied one.
        """
        q = query.strip()
        if not q:
            raise BrickLinkError("Empty part number")

        data = self._get_json(
            SEARCH_URL,
            {
                "q": q, "st": 0, "type": "P", "cat": "", "yf": 0, "yt": 0,
                "loc": "", "reg": 0, "ca": 0, "ss": "", "pmt": "", "nmp": 0,
                "color": -1, "min": 0, "max": 0, "minqty": 0, "nosuperlot": 1,
                "incomplete": 0, "showempty": 1, "rpp": 5, "pi": 1, "ci": 0,
            },
            referer="https://www.bricklink.com/v2/search.page",
        )

        items = [
            it
            for tl in data.get("result", {}).get("typeList", [])
            if tl.get("type") == "P"
            for it in tl.get("items", [])
        ]
        if not items:
            raise BrickLinkError(f"No BrickLink part found for {q!r}")
        item = self._pick_item(items, q)

        color_id = self._resolve_color(color, item.get("strPCC"), q)
        names = color_names()
        return ResolvedPart(
            key=f"{item['strItemNo']}|{color_id if color_id is not None else 'any'}",
            query=q,
            item_id=int(item["idItem"]),
            part_no=str(item["strItemNo"]),
            name=str(item.get("strItemName") or ""),
            color_id=color_id,
            color_name=names.get(color_id) if color_id is not None else None,
            quantity=quantity,
        )

    @staticmethod
    def _pick_item(items: list[dict], query: str) -> dict:
        """Choose the right search hit.

        BrickLink ranks by relevance, not exactness -- searching "2456" returns
        the Braille brick "65549pb01" above the actual part 2456. Prefer an exact
        part-number match, then an exact element-ID (PCC) match, then give up and
        take BrickLink's first choice.
        """
        q = query.strip().lower()
        for it in items:
            if str(it.get("strItemNo", "")).strip().lower() == q:
                return it
        if q.isdigit():
            for it in items:
                if any(code == q for code, _ in _PCC_RE.findall(it.get("strPCC") or "")):
                    return it
        return items[0]

    def _resolve_color(self, color: str | None, pcc_field: str | None, query: str) -> int | None:
        if color and color.strip():
            c = color.strip()
            if c.isdigit():
                cid = int(c)
                if cid not in color_names():
                    raise BrickLinkError(f"Unknown BrickLink color id {cid}")
                return cid
            cid = color_id_by_name().get(c.lower())
            if cid is None:
                raise BrickLinkError(f"Unknown color name {color!r}")
            return cid

        # No explicit color: if the user typed an element ID, BrickLink echoes it
        # back as "300121(5)" where 5 is the color id.
        if pcc_field and query.isdigit():
            for code, cid in _PCC_RE.findall(pcc_field):
                if code == query:
                    return int(cid)
        return None

    # ---- items for sale --------------------------------------------------

    def fetch_lots(
        self,
        part: ResolvedPart,
        condition: str = "N",
        only_countries: list[str] | None = None,
        max_pages: int = 1,
    ) -> list[Lot]:
        """Fetch lots for sale, cheapest first.

        BrickLink returns these already sorted by price ascending, so one page of
        500 is far more than any sane optimizer needs for a single part.
        """
        params: dict[str, Any] = {"itemid": part.item_id, "rpp": LOTS_PER_PAGE, "pi": 1}
        if part.color_id is not None:
            params["color"] = part.color_id
        if condition in ("N", "U"):
            params["cond"] = condition
        # BrickLink's `loc` filter only accepts a single country; anything more
        # complex is filtered client-side.
        if only_countries and len(only_countries) == 1:
            params["loc"] = only_countries[0].upper()

        referer = f"https://www.bricklink.com/v2/catalog/catalogitem.page?P={part.part_no}"
        names = color_names()
        lots: list[Lot] = []

        for page in range(1, max_pages + 1):
            params["pi"] = page
            data = self._get_json(IFS_URL, dict(params), referer)
            if data.get("returnCode") not in (0, "0", None):
                raise BrickLinkError(f"BrickLink error: {data.get('returnMessage')}")
            batch = data.get("list") or []
            for raw in batch:
                try:
                    lots.append(self._to_lot(raw, part, names))
                except (KeyError, ValueError, TypeError):
                    continue
            if len(batch) < LOTS_PER_PAGE:
                break

        lots.sort(key=lambda l: l.unit_price)
        return lots

    @staticmethod
    def _to_lot(raw: dict, part: ResolvedPart, names: dict[int, str]) -> Lot:
        color_id = int(raw.get("idColor") or 0)
        return Lot(
            lot_id=int(raw["idInv"]),
            part_key=part.key,
            part_no=part.part_no,
            store_name=str(raw.get("strStorename") or raw.get("strSellerUsername") or "?"),
            seller=str(raw["strSellerUsername"]),
            country=str(raw.get("strSellerCountryCode") or "??").upper(),
            feedback=int(raw.get("n4SellerFeedbackScore") or 0),
            unit_price=parse_price(raw.get("mDisplaySalePrice")),
            quantity=int(raw.get("n4Qty") or 0),
            min_buy=parse_price(raw.get("mMinBuy")),
            color_id=color_id,
            color_name=str(raw.get("strColor") or names.get(color_id, "?")),
            condition=str(raw.get("codeNew") or "N"),
        )
