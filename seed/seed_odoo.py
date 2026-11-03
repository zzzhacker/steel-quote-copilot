"""Load the demo catalog into a running Odoo 19: `python -m seed.seed_odoo [--reset]`. Idempotent."""
import argparse
from collections.abc import Iterable

from seed import catalog
from steelquote.adapters.odoo import Odoo

CHUNK = 200
INV_CTX = {"context": {"inventory_mode": True}}  # lets stock.quant take inventory_quantity on create


def ensure(odoo: Odoo, model: str, vals: list[dict], key: tuple[str, ...], domain: list | None = None) -> dict[tuple, int]:
    """Create the records whose `key` values don't exist yet (batched); return key -> id for all."""
    def k(rec: dict) -> tuple:
        return tuple(v[0] if isinstance(v, list) else v for v in (rec[f] for f in key))

    found = odoo.search_read(model, [(key[0], "in", [v[key[0]] for v in vals]), *(domain or [])], [*key])
    ids = {k(r): r["id"] for r in found}
    todo = [v for v in vals if k(v) not in ids]
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        ids.update(zip((k(v) for v in chunk), odoo.create(model, chunk)))
    return ids


def ensure_fields(odoo: Odoo) -> None:
    for model, fields in catalog.CUSTOM_FIELDS.items():
        model_id = odoo.search_read("ir.model", [("model", "=", model)], ["id"])[0]["id"]
        vals = [{"model_id": model_id, "name": n, "field_description": label, "ttype": t, "state": "manual"}
                for n, label, t in fields]
        ensure(odoo, "ir.model.fields", vals, ("name", "model_id"), [("model_id", "=", model_id)])


def ensure_lot_tracking(odoo: Odoo) -> None:
    """Enable 'Lots & Serial Numbers' (stock.group_production_lot) the way Settings does."""
    lot_group = odoo.search_read("ir.model.data", [("module", "=", "stock"), ("name", "=", "group_production_lot")],
                                 ["res_id"])[0]["res_id"]
    admin = odoo.search_read("res.users", [("login", "=", "admin")], ["group_ids"])[0]
    if lot_group not in admin["group_ids"]:
        [wiz] = odoo.create("res.config.settings", [{"group_stock_production_lot": True}])
        odoo.call("res.config.settings", "execute", ids=[wiz])


def ensure_locations(odoo: Odoo) -> dict[str, int]:
    parent = odoo.search_read("stock.warehouse", [("code", "=", "WH")], ["lot_stock_id"])[0]["lot_stock_id"][0]
    vals = [{"name": n, "location_id": parent, "usage": "internal"} for n in catalog.RACKS]
    ids = ensure(odoo, "stock.location", vals, ("name", "location_id"), [("location_id", "=", parent)])
    return {name: i for (name, _), i in ids.items()}


def ensure_partners(odoo: Odoo) -> None:
    vals = [{"ref": p.ref, "name": p.name, "email": p.email, "city": p.city, "x_tier": p.tier,
             "is_company": True, "customer_rank": 1} for p in catalog.partners()]
    ensure(odoo, "res.partner", vals, ("ref",))


def _product_vals(p: catalog.Product) -> dict:
    return {"default_code": p.default_code, "name": p.name, "type": "consu", "is_storable": True,
            "tracking": "lot", "list_price": p.list_price, "weight": p.weight_lb, "sale_ok": True,
            "x_grade": p.grade, "x_uns": p.uns, "x_form": p.form,
            "x_thk_in": p.thk_in, "x_w_in": p.w_in, "x_l_in": p.l_in}


def ensure_products(odoo: Odoo, prods: list[catalog.Product]) -> dict[str, int]:
    services = [{"default_code": c, "name": n, "type": "service", "list_price": price} for c, n, price in catalog.SERVICES]
    vals = [_product_vals(p) for p in prods] + services
    return {k[0]: i for k, i in ensure(odoo, "product.product", vals, ("default_code",)).items()}


def ensure_lots(odoo: Odoo, lots: list[catalog.Lot], product_ids: dict[str, int]) -> dict[str, int]:
    company = odoo.search_read("res.company", [], ["id"], limit=1)[0]["id"]
    vals = [{"name": l.name, "product_id": product_ids[l.product_code], "company_id": company, "x_mill": l.mill,
             "x_is_remnant": l.is_remnant, "x_w_in": l.w_in, "x_l_in": l.l_in} for l in lots]
    ids = ensure(odoo, "stock.lot", vals, ("name", "product_id"), [("product_id", "in", sorted(set(product_ids.values())))])
    return {name: i for (name, _), i in ids.items()}


def _apply_inventory(odoo: Odoo, quant_ids: Iterable[int]) -> None:
    ids = list(quant_ids)
    for i in range(0, len(ids), CHUNK):
        odoo.call("stock.quant", "action_apply_inventory", ids=ids[i:i + CHUNK], **INV_CTX)


def ensure_quants(odoo: Odoo, lots: list[catalog.Lot], lot_ids: dict[str, int], product_ids: dict[str, int],
                  loc_ids: dict[str, int]) -> int:
    stocked = {r["lot_id"][0] for r in odoo.search_read("stock.quant", [("lot_id", "in", list(lot_ids.values())),
                                                                       ("quantity", ">", 0)], ["lot_id"])}
    vals = [{"product_id": product_ids[l.product_code], "location_id": loc_ids[l.rack], "lot_id": lot_ids[l.name],
             "inventory_quantity": l.qty} for l in lots if lot_ids[l.name] not in stocked]
    for i in range(0, len(vals), CHUNK):
        _apply_inventory(odoo, odoo.call("stock.quant", "create", vals_list=vals[i:i + CHUNK], **INV_CTX))
    return len(vals)


def reset(odoo: Odoo) -> None:
    """Zero all seeded stock; the catalog stays, so the next seed() re-stocks it without duplicating anything.

    Odoo refuses to delete records that have stock moves, so we never delete or archive-and-recreate.
    """
    codes = [p.default_code for p in catalog.products()]
    prods = odoo.search_read("product.product", [("default_code", "in", codes)], ["id"])
    quants = odoo.search_read("stock.quant", [("product_id", "in", [p["id"] for p in prods]), ("quantity", "!=", 0),
                                              ("location_id.usage", "=", "internal")], ["id"])
    qids = [q["id"] for q in quants]
    if qids:
        odoo.call("stock.quant", "write", ids=qids, vals={"inventory_quantity": 0}, **INV_CTX)
        _apply_inventory(odoo, qids)


def seed(odoo: Odoo) -> dict[str, int]:
    prods = catalog.products()
    lots = catalog.lots(prods)
    ensure_fields(odoo)
    ensure_lot_tracking(odoo)
    locs = ensure_locations(odoo)
    ensure_partners(odoo)
    product_ids = ensure_products(odoo, prods)
    lot_ids = ensure_lots(odoo, lots, product_ids)
    new_quants = ensure_quants(odoo, lots, lot_ids, product_ids, locs)
    return {"products": len(prods), "lots": len(lot_ids), "locations": len(locs), "new_quants": new_quants}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="zero seeded stock first (catalog is kept and re-stocked)")
    args = ap.parse_args()
    odoo = Odoo()
    if args.reset:
        reset(odoo)
    print(seed(odoo))


if __name__ == "__main__":
    main()
