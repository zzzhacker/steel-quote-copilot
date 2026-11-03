"""MCP server 'steel-er': hybrid entity resolution + LIVE validation against Odoo (the system of record)."""
from functools import lru_cache

from mcp.server.fastmcp import FastMCP

from steelquote.adapters.odoo import Odoo
from steelquote.adapters.retriever import Mode, Retriever, build_index
from steelquote.domain.schemas import StockAlloc

mcp = FastMCP("steel-er", host="127.0.0.1", port=8765)  # streamable HTTP at /mcp


@lru_cache
def _odoo() -> Odoo:
    return Odoo()


@lru_cache
def _retriever() -> Retriever:
    return Retriever(_odoo())


def _id(v: int | list | bool) -> int | None:
    """Odoo many2one comes back as id or [id, name] depending on the call."""
    return v[0] if isinstance(v, list) else (v or None)


def _fits(w: float, l: float, cut_w: float, cut_l: float) -> bool:
    return (w >= cut_w and l >= cut_l) or (w >= cut_l and l >= cut_w)


@mcp.tool()
def resolve_item(text: str, grade: str | None = None, form: str | None = None, thk_in: float | None = None,
                 w_in: float | None = None, l_in: float | None = None, k: int = 5,
                 mode: Mode = "hybrid") -> list[dict]:
    """Rank catalog products for free text; structured args (canonical grade/form, inches) pre-filter.
    Returns Candidate dicts (score = cosine relevance). Validate ids with validate_entities before use."""
    return [c.model_dump() for c in _retriever().search_items(text, grade, form, thk_in, w_in, l_in, k, mode)]


@mcp.tool()
def resolve_customer(text: str, k: int = 3) -> list[dict]:
    """Rank customers for a free-text name: [{partner_id, name, tier, score}]."""
    return _retriever().search_customers(text, k)


@mcp.tool()
def find_stock(product_id: int, qty: int, cut_w_in: float | None = None, cut_l_in: float | None = None) -> list[dict]:
    """LIVE stock allocation for qty pieces. With a cut size: smallest remnant that contains it first, then
    full-size lots. Without: full-size lots only. May return less than qty (short)."""
    odoo = _odoo()
    quants = odoo.search_read(
        "stock.quant", [["product_id", "=", product_id], ["location_id.usage", "=", "internal"], ["lot_id", "!=", False],
                          ["lot_id.name", "not =like", "STALE-%"]],  # leftovers of archived products
        ["lot_id", "location_id", "available_quantity"])
    quants = [q for q in quants if q["available_quantity"] > 0]
    lots = {x["id"]: x for x in odoo.search_read(
        "stock.lot", [["id", "in", [_id(q["lot_id"]) for q in quants]]], ["name", "x_is_remnant", "x_w_in", "x_l_in"])}
    locs = {x["id"]: x["complete_name"] for x in odoo.search_read(
        "stock.location", [["id", "in", [_id(q["location_id"]) for q in quants]]], ["complete_name"])}
    cut = cut_w_in is not None and cut_l_in is not None

    rows = []  # (sort key, alloc fields, available)
    for q in quants:
        lot = lots[_id(q["lot_id"])]
        w, l = lot["x_w_in"], lot["x_l_in"]
        if cut and not _fits(w, l, cut_w_in, cut_l_in) or not cut and lot["x_is_remnant"]:
            continue
        key = (not lot["x_is_remnant"], w * l if lot["x_is_remnant"] else 0, lot["id"])
        alloc = dict(lot_id=lot["id"], heat=lot["name"], is_remnant=bool(lot["x_is_remnant"]),
                     location=locs[_id(q["location_id"])], w_in=w, l_in=l)
        rows.append((key, alloc, int(q["available_quantity"])))

    out, need = [], qty
    for _, alloc, avail in sorted(rows, key=lambda r: r[0]):
        if need <= 0:
            break
        take = min(avail, need)
        out.append(StockAlloc(**alloc, qty=take).model_dump())
        need -= take
    return out


@mcp.tool()
def validate_entities(product_ids: list[int], partner_id: int | None = None) -> dict:
    """LIVE existence check against Odoo (not the index): catches stale or hallucinated ids.
    {'products': {id: {'exists', 'on_hand'}}, 'partner': {'exists', 'name', 'tier'} | None}"""
    odoo = _odoo()
    found = {p["id"]: p for p in odoo.search_read("product.product", [["id", "in", product_ids]], ["qty_available"])}
    products = {i: {"exists": i in found, "on_hand": found[i]["qty_available"] if i in found else 0.0}
                for i in product_ids}
    partner = None
    if partner_id is not None:
        rows = odoo.search_read("res.partner", [["id", "=", partner_id]], ["name", "x_tier"])
        partner = {"exists": bool(rows), **({"name": rows[0]["name"], "tier": rows[0]["x_tier"]} if rows else {})}
    return {"products": products, "partner": partner}


@mcp.tool()
def sync_index() -> int:
    """Rebuild the vector index and BM25 from Odoo. Returns documents indexed."""
    n = build_index(_odoo())
    _retriever().refresh()
    return n


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
