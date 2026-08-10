"""Request/response shapes and the internal lot representation."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class PartRequest(BaseModel):
    """One line the user added in the UI."""

    part: str = Field(..., description="BrickLink part number, or a LEGO element ID")
    quantity: int = Field(1, ge=1)
    color: str | None = Field(None, description="Color name or BrickLink color id; blank means any")


class Filters(BaseModel):
    condition: str = Field("N", pattern="^(N|U|A)$", description="New, Used, or Any")
    exclude_countries: list[str] = Field(default_factory=list)
    only_countries: list[str] = Field(default_factory=list)
    min_feedback: int = Field(0, ge=0)
    max_stores: int | None = Field(None, ge=1)
    runner_ups: int = Field(3, ge=0, le=10)


class SolveRequest(BaseModel):
    parts: list[PartRequest]
    filters: Filters = Field(default_factory=Filters)


@dataclass(frozen=True)
class ResolvedPart:
    """A user line after part/color resolution against BrickLink's catalog."""

    key: str                 # stable id for this line, e.g. "3001|5"
    query: str               # what the user typed
    item_id: int             # BrickLink internal numeric item id
    part_no: str             # e.g. "3001"
    name: str
    color_id: int | None     # None means "any color"
    color_name: str | None
    quantity: int


@dataclass(frozen=True)
class Lot:
    """A single seller's listing of one part."""

    lot_id: int
    part_key: str
    part_no: str
    store_name: str
    seller: str
    country: str
    feedback: int
    unit_price: float        # USD
    quantity: int            # units available
    min_buy: float           # store minimum merchandise spend, USD (0 = none)
    color_id: int
    color_name: str
    condition: str           # "N" or "U"


@dataclass
class Store:
    seller: str
    name: str
    country: str
    feedback: int
    min_buy: float
    shipping: float = 0.0
    shipping_is_estimate: bool = True
    days_low: int = 0
    days_high: int = 0
    lots: list[Lot] = field(default_factory=list)
