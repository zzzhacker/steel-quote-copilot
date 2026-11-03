from decimal import Decimal as D

from steelquote.domain.pricing import build_quote, price_line
from steelquote.domain.schemas import Candidate, NormLine, ResolvedLine, StockAlloc


def resolved(grade, form, thk, qty, stock=(48.0, 96.0), alloc_qty=None, remnant=False, status="ok",
             cut=(None, None)) -> ResolvedLine:
    norm = NormLine(line_no=1, raw_text="", qty=qty, grade=grade, form=form, thk_in=thk, w_in=stock[0],
                    l_in=stock[1], cut_w_in=cut[0], cut_l_in=cut[1])
    cand = Candidate(product_id=1, default_code="P", name="P", grade=grade, form=form, thk_in=thk,
                     w_in=stock[0], l_in=stock[1])
    allocs = [StockAlloc(lot_id=1, heat="H1", qty=alloc_qty or qty, is_remnant=remnant, location="A1",
                         w_in=stock[0], l_in=stock[1])]
    return ResolvedLine(norm=norm, candidate=cand, allocs=allocs, status=status)


def test_hero_stainless_plate_with_certs_and_tier():
    p = price_line(resolved("304", "plate", 0.25, 2), "A", certs_required=True)
    assert p.weight_lb == 665.86
    assert p.breakdown == {
        "material": D("652.54"),  # 665.86 lb * $0.98/lb
        "surcharge": D("627.71"),  # 665.86 lb * $0.9427/lb
        "cut": D("0.00"),
        "cert": D("25.00"),
        "remnant_discount": D("0.00"),
        "tier_discount": D("-52.20"),  # 8% of material
    }
    assert p.total == D("1253.05")


def test_cut_pieces_priced_with_saw_charge_and_tier():
    p = price_line(resolved("A36", "plate", 0.5, 4, cut=(12.0, 24.0)), "B", certs_required=False)
    assert p.weight_lb == 163.35  # four 12x24x0.5 pieces, not the 48x96 stock
    assert p.breakdown["material"] == D("102.91")
    assert p.breakdown["cut"] == D("64.00")  # 2 saw cuts x 4 pieces x $8
    assert p.breakdown["surcharge"] == D("0.00")
    assert p.breakdown["tier_discount"] == D("-6.68")
    assert p.total == D("160.23")


def test_sheet_uses_shear_rate():
    p = price_line(resolved("304", "sheet", 0.0478, 1, cut=(None, 48.0)), None, certs_required=False)
    assert p.breakdown["cut"] == D("5.00")


def test_remnant_discount_on_material_only():
    p = price_line(resolved("6061", "plate", 0.25, 1, stock=(12.0, 12.0), remnant=True), None, False)
    assert p.weight_lb == 3.51
    assert p.breakdown["material"] == D("7.37")
    assert p.breakdown["remnant_discount"] == D("-0.74")
    assert p.total == D("6.63")


def test_short_line_prices_only_allocated_qty():
    p = price_line(resolved("304", "sheet", 0.1406, 5, alloc_qty=3, status="short"), None, False)
    assert p.weight_lb == 561.72
    assert p.breakdown["material"] == D("578.57")  # (98 + 5 light-section extra) $/cwt
    assert p.breakdown["surcharge"] == D("529.53")
    assert p.total == D("1108.10")


def test_needs_clarification_prices_to_zero():
    p = price_line(resolved("304", "plate", 0.25, 1, status="needs_clarification"), "A", True)
    assert p.breakdown == {} and p.total == D(0) and p.weight_lb == 0.0


def test_build_quote_totals_lines():
    lines = [resolved("304", "plate", 0.25, 2), resolved("6061", "plate", 0.25, 1, stock=(12.0, 12.0)),
             resolved("A36", "plate", 0.25, 1, status="needs_clarification")]
    q = build_quote(lines, partner_id=7, customer_name="Acme", tier=None, certs_required=False, ship_by="2026-11-10")
    assert (q.partner_id, q.customer_name, q.ship_by) == (7, "Acme", "2026-11-10")
    assert q.total == D("652.54") + D("627.71") + D("7.37") == sum(p.total for p in q.lines)
