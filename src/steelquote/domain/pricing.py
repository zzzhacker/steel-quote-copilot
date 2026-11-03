"""Line and quote pricing. Decimal throughout, cents rounding; rates live in data/*.json."""
import json
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from typing import NamedTuple

from steelquote.domain.ontology import DATA_DIR, weight_lb
from steelquote.domain.schemas import Form, PricedLine, Quote, ResolvedLine

CENT = Decimal("0.01")
CWT = Decimal(100)
ZERO = Decimal(0)


class Piece(NamedTuple):
    qty: int
    w_in: float
    l_in: float


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(), parse_float=Decimal, parse_int=Decimal)


@cache
def _rates() -> dict:
    return _load("pricing.json")


@cache
def _surcharges() -> dict:
    return _load("surcharges_2026_09.json")


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def _spec(line: ResolvedLine) -> tuple[Form, str, float] | None:
    src = line.candidate or line.norm
    if src.form and src.grade and src.thk_in:
        return src.form, src.grade, src.thk_in
    return None


def _stock_dims(line: ResolvedLine, form: Form) -> tuple[float, float] | None:
    src = line.allocs[0] if line.allocs else line.candidate or line.norm
    w, l = src.w_in, src.l_in
    if l is None or (w is None and form != "round_bar"):
        return None
    return w or 0.0, l


def _has_cut(line: ResolvedLine) -> bool:
    return bool(line.norm.cut_w_in or line.norm.cut_l_in)


def _pieces(line: ResolvedLine, form: Form, qty: int) -> list[Piece]:
    if line.allocs and not _has_cut(line):
        return [Piece(a.qty, a.w_in, a.l_in) for a in line.allocs]
    if (stock := _stock_dims(line, form)) is None:
        return []
    cut_w, cut_l = line.norm.cut_w_in, line.norm.cut_l_in
    return [Piece(qty, cut_w or stock[0], cut_l or stock[1])]


def _cuts_per_piece(line: ResolvedLine, form: Form) -> int:
    stock = _stock_dims(line, form)
    if stock is None:
        return 0
    n = line.norm
    return sum(1 for cut, full in ((n.cut_w_in, stock[0]), (n.cut_l_in, stock[1])) if cut and cut < full - 1e-6)


def _rate_cwt(grade: str, thk_in: float, piece: Piece) -> Decimal:
    rates = _rates()
    band = next(b for b in rates["thickness_extra_cwt"] if b["lt_in"] is None or thk_in < b["lt_in"])
    size = rates["size_extra_cwt"]
    oversize = max(piece.w_in, piece.l_in) > size["over_in"]
    return rates["base_cwt"][grade] + band["extra"] + (size["extra"] if oversize else ZERO)


def _remnant_share(line: ResolvedLine) -> Decimal:
    total = sum(a.qty for a in line.allocs)
    return Decimal(sum(a.qty for a in line.allocs if a.is_remnant)) / total if total else ZERO


def _empty(line: ResolvedLine) -> PricedLine:
    return PricedLine(line=line, weight_lb=0.0, breakdown={}, total=ZERO)


def price_line(line: ResolvedLine, tier: str | None, certs_required: bool) -> PricedLine:
    spec = _spec(line)
    if line.status == "needs_clarification" or spec is None:
        return _empty(line)
    form, grade, thk = spec
    qty = (line.norm.qty or 0) if line.status == "ok" else sum(a.qty for a in line.allocs)
    pieces = [p for p in _pieces(line, form, qty) if p.qty > 0]
    if not pieces:
        return _empty(line)

    rates = _rates()
    weights = [Decimal(str(weight_lb(form, grade, thk, p.w_in, p.l_in, p.qty))) for p in pieces]
    weight = sum(weights, ZERO)
    material = _cents(sum((w * _rate_cwt(grade, thk, p) / CWT for w, p in zip(weights, pieces)), ZERO))
    surcharge = _cents(weight * _surcharges().get(grade, ZERO))
    cut_kind = "shear" if form == "sheet" else "saw"
    cut = _cents(rates["cut_charge"][cut_kind] * _cuts_per_piece(line, form) * sum(p.qty for p in pieces))
    cert = _cents(rates["cert_fee"]) if certs_required else ZERO
    remnant = -_cents(material * rates["remnant_discount"] * _remnant_share(line))
    tier_discount = -_cents((material + remnant + cut) * rates["tier_discount"].get(tier or "", ZERO))

    breakdown = {"material": material, "surcharge": surcharge, "cut": cut, "cert": cert,
                 "remnant_discount": remnant, "tier_discount": tier_discount}
    return PricedLine(line=line, weight_lb=float(weight), breakdown=breakdown, total=sum(breakdown.values(), ZERO))


def build_quote(lines: list[ResolvedLine], partner_id: int | None, customer_name: str | None,
                tier: str | None, certs_required: bool, ship_by: str | None) -> Quote:
    priced = [price_line(line, tier, certs_required) for line in lines]
    return Quote(partner_id=partner_id, customer_name=customer_name, tier=tier, lines=priced,
                 certs_required=certs_required, ship_by=ship_by, total=sum((p.total for p in priced), ZERO))
