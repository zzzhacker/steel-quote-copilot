"""Shared contract between domain, ER server, agent, seed and evals. No I/O."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Form = Literal["plate", "sheet", "flat_bar", "round_bar"]
LineStatus = Literal["ok", "short", "needs_clarification"]


class ExtractedLine(BaseModel):
    """What the LLM reads off the email, kept as written (strings) so the ontology normalizes it."""

    raw_text: str
    qty: int | None = None
    grade: str | None = Field(None, description="as written, e.g. '304', 'S30400', '18-8', 'A36'")
    form: str | None = Field(None, description="as written, e.g. 'plt', 'sheet', 'flat bar'")
    thickness: str | None = Field(None, description="as written, e.g. '1/4', '7ga', '6mm', '.250'")
    width: str | None = None
    length: str | None = None
    cut_width: str | None = Field(None, description="cut-to size width if the customer wants it cut")
    cut_length: str | None = None


class RFQ(BaseModel):
    customer_text: str | None = Field(None, description="customer / company / sender as written")
    certs_required: bool = False
    ship_by: str | None = None
    lines: list[ExtractedLine]


class NormLine(BaseModel):
    """Ontology-normalized line: canonical grade, inches, issues for anything unresolved."""

    line_no: int
    raw_text: str
    qty: int | None
    grade: str | None  # canonical, e.g. '304', '316L', 'A36', '6061'
    uns: str | None = None
    form: Form | None
    thk_in: float | None
    w_in: float | None
    l_in: float | None
    cut_w_in: float | None = None
    cut_l_in: float | None = None
    issues: list[str] = []  # e.g. 'missing thickness', 'ambiguous gauge vs fraction'


class Candidate(BaseModel):
    product_id: int
    default_code: str
    name: str
    grade: str
    form: Form
    thk_in: float
    w_in: float
    l_in: float
    score: float = 0.0


class StockAlloc(BaseModel):
    lot_id: int
    heat: str
    qty: int
    is_remnant: bool
    location: str
    w_in: float
    l_in: float


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ResolvedLine(BaseModel):
    norm: NormLine
    candidate: Candidate | None = None
    alternatives: list[Candidate] = []
    allocs: list[StockAlloc] = []
    checks: list[Check] = []
    status: LineStatus = "needs_clarification"


class PricedLine(BaseModel):
    line: ResolvedLine
    weight_lb: float
    breakdown: dict[str, Decimal]  # material, surcharge, cut, cert, tier_discount ...
    total: Decimal


class Quote(BaseModel):
    partner_id: int | None
    customer_name: str | None
    tier: str | None
    lines: list[PricedLine]
    certs_required: bool = False
    ship_by: str | None = None
    total: Decimal
