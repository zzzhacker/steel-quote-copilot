"""Gold-first eval set: sample real in-stock items, write the answer, then render a messy email from it.

    uv run python -m evals.build_set        # needs Odoo + the ER server running (gold totals use live stock)
"""
import asyncio
import json
import random
from collections import defaultdict
from pathlib import Path

from steelquote.adapters.odoo import Odoo
from steelquote.agent.nodes import Agent
from steelquote.config import get_settings
from steelquote.domain.ontology import normalize_line
from steelquote.domain.pricing import price_line
from steelquote.domain.schemas import Candidate, ExtractedLine, ResolvedLine

OUT = Path("evals/rfqs.jsonl")
rng = random.Random(7)
ALIASES = {g: v["aliases"] for g, v in json.loads(Path("data/grades.json").read_text())["grades"].items()}
FRAC = {0.1875: "3/16", 0.25: "1/4", 0.3125: "5/16", 0.375: "3/8", 0.5: "1/2", 0.625: "5/8", 0.75: "3/4", 1.0: "1"}
FORM_WORDS = {"plate": ["plate", "plt", "pl"], "sheet": ["sheet", "sht"], "flat_bar": ["flat bar", "flat"],
              "round_bar": ["round bar", "rd bar", "round"]}
MSG_GAUGE = {0.1793: "7ga", 0.1345: "10ga", 0.1196: "11ga", 0.1046: "12ga", 0.0747: "14ga", 0.0598: "16ga"}
GREET = ["Hi,", "Hello team,", "Good morning,", "Hey guys -", "Quote request:"]
CLOSE = ["Thx", "Thanks", "Regards", "Please advise lead time", "Need pricing asap"]


def load_catalog(odoo: Odoo) -> tuple[list[dict], dict[int, int], list[dict]]:
    products = odoo.search_read("product.product", [["x_grade", "!=", False]], [
        "default_code", "name", "x_grade", "x_form", "x_thk_in", "x_w_in", "x_l_in"])
    on_hand: dict[int, int] = defaultdict(int)
    for q in odoo.search_read("stock.quant", [["location_id.usage", "=", "internal"]],
                              ["product_id", "lot_id", "quantity"]):
        if q["lot_id"] and not q["lot_id"][1].startswith("STALE-"):
            on_hand[q["product_id"][0]] += int(q["quantity"])
    partners = odoo.search_read("res.partner", [["x_tier", "!=", False]], ["name", "email", "x_tier"])
    return products, on_hand, partners


def thk_text(p: dict, style: str) -> str:
    t = p["x_thk_in"]
    if p["x_grade"] == "A36" and p["x_form"] == "sheet" and style == "gauge" and t in MSG_GAUGE:
        return MSG_GAUGE[t]  # gauge tables only agree with the catalog for carbon steel
    if style == "mm":
        return f"{t * 25.4:.1f}mm".replace(".0mm", "mm")
    return FRAC.get(t) if style == "frac" and t in FRAC else f"{t:g}"


def line_text(p: dict, qty: int, style: str, alias: bool = False) -> str:
    grade = rng.choice(ALIASES[p["x_grade"]]) if alias else p["x_grade"]
    form = rng.choice(FORM_WORDS[p["x_form"]])
    w, l = p["x_w_in"], p["x_l_in"]
    size = {"x": f"{w:g}x{l:g}", "feet": f"{w / 12:g}x{l / 12:g}", "quote": f'{w:g}" x {l:g}"'}[
        "feet" if style == "feet" and w % 12 == 0 and l % 12 == 0 else rng.choice(["x", "quote"])]
    if p["x_form"] == "round_bar":
        return f"{qty} pcs {grade} {form} {thk_text(p, 'frac')}\" dia x {l:g}\""
    if p["x_form"] == "flat_bar":
        return f"{qty} - {grade} {form} {thk_text(p, 'frac')} x {w:g} x {l:g}"
    return rng.choice([f"{qty} pcs {thk_text(p, style)} {grade} {form} {size}",
                       f"{qty} {form} {grade} {thk_text(p, style)} {size}"])


def email(lines: list[str], partner: dict, certs: bool, chatty: bool = False) -> str:
    items = "\n".join(f"{i}) {t}" for i, t in enumerate(lines, 1))
    extras = ", certs required" if certs else ""
    body = f"{rng.choice(GREET)}\n\nneed the following{extras}, ship Fri:\n\n{items}\n\n{rng.choice(CLOSE)}\n"
    if chatty:
        body = ("Hey, hope the week is going OK. We're slammed with a job for a food plant so everything is rush. "
                "Mike said you had good pricing last time.\n\n" + body + "\nP.S. forklift access is on the east dock.\n")
    return f"From: purchasing <{partner['email']}>\n\n{body}\n{partner['name']}"


async def gold_total(agent: Agent, gold_lines: list[dict], by_sku: dict, tier: str, certs: bool) -> float:
    total = 0.0
    for g in gold_lines:
        if g["abstain"]:
            continue
        p = by_sku[g["sku"]]
        cand = Candidate(product_id=p["id"], default_code=p["default_code"], name=p["name"], grade=p["x_grade"],
                         form=p["x_form"], thk_in=p["x_thk_in"], w_in=p["x_w_in"], l_in=p["x_l_in"])
        extracted = ExtractedLine(raw_text=g["sku"], qty=g["qty"], grade=p["x_grade"], form=p["x_form"],
                                  thickness=str(p["x_thk_in"]), width=str(p["x_w_in"]), length=str(p["x_l_in"]))
        r = await agent._validate_line(ResolvedLine(norm=normalize_line(1, extracted), candidate=cand),
                                       {p["id"]: True})
        total += float(price_line(r, tier, certs).total)
    return round(total, 2)


async def main() -> None:
    s = get_settings()
    odoo = Odoo(s)
    agent = Agent(s, odoo)
    products, on_hand, partners = load_catalog(odoo)
    by_sku = {p["default_code"]: p for p in products}
    stocked = [p for p in products if on_hand[p["id"]] >= 3 and p["x_form"] != "round_bar" or
               p["x_form"] == "round_bar" and on_hand[p["id"]] >= 3]
    singles = [p for p in stocked if p["x_form"] in ("plate", "sheet")]
    items: list[dict] = []

    def add(cat: str, picks: list[tuple[dict | None, int, str | None, bool]], texts: list[str], chatty=False):
        partner, certs = rng.choice(partners), rng.random() < 0.4
        gold_lines = [{"sku": p["default_code"] if p else None, "qty": q, "abstain": ab} for p, q, _, ab in picks]
        items.append({"id": f"{cat}-{len([i for i in items if i['category'] == cat]) + 1:02d}", "category": cat,
                      "email": email(texts, partner, certs, chatty),
                      "gold": {"customer": partner["name"], "tier": partner["x_tier"], "certs": certs,
                               "lines": gold_lines}})

    for _ in range(15):  # clean
        ps = rng.sample(stocked, rng.choice([1, 1, 2, 3]))
        qs = [rng.randint(1, min(4, on_hand[p["id"]])) for p in ps]
        add("clean", [(p, q, None, False) for p, q in zip(ps, qs)],
            [line_text(p, q, rng.choice(["frac", "gauge", "dec"])) for p, q in zip(ps, qs)])
    for i in range(8):  # aliases and unit styles
        p, q = rng.choice(singles), rng.randint(1, 3)
        style = ["frac", "mm", "feet", "gauge"][i % 4]
        if style == "feet" and not (p["x_w_in"] % 12 == 0 and p["x_l_in"] % 12 == 0):
            style = "frac"
        add("alias_units", [(p, q, None, False)], [line_text(p, q, style, alias=True)])
    carbon_sheets = [p for p in stocked if p["x_grade"] == "A36" and p["x_form"] == "sheet" and p["x_thk_in"] in MSG_GAUGE]
    for i in range(6):  # ambiguous: gauge vs fraction, or grade not stated
        if i < 3:
            p = rng.choice(carbon_sheets)
            add("ambiguous", [(p, 2, None, True)], [f"2 sheets 3/16 or {MSG_GAUGE[p['x_thk_in']]} {rng.choice(['A36', 'mild steel'])} 4x8"])
        else:
            p = rng.choice([x for x in singles if x["x_grade"].startswith("3")])
            add("ambiguous", [(p, 2, None, True)],
                [f"2 pcs {thk_text(p, 'frac')} stainless {rng.choice(FORM_WORDS[p['x_form']])} {p['x_w_in']:g}x{p['x_l_in']:g}"])
    short = [p for p in singles if on_hand[p["id"]] < 3] or singles
    for i in range(5):  # out of stock / partial / not stocked
        if i < 3:
            p = rng.choice(short)
            q = on_hand[p["id"]] + rng.randint(2, 5)
            add("out_of_stock", [(p, q, None, False)], [line_text(p, q, "frac")])
        else:
            add("out_of_stock", [(None, 4, None, True)],
                [f"4 pcs 304 plt {rng.choice(['7/8', '1 3/4', '0.4', '3.5'])} x 48 x 96"])
    for _ in range(4):  # missing dimensions
        p, q = rng.choice(singles), rng.randint(2, 6)
        add("missing_dims", [(p, q, None, True)], [f"{q} pcs {p['x_grade']} {rng.choice(FORM_WORDS[p['x_form']])}"])
    for _ in range(2):  # chatty multi-line
        ps = rng.sample(stocked, 3)
        add("chatty", [(p, 2, None, False) for p in ps], [line_text(p, 2, "frac") for p in ps], chatty=True)

    partner_tier = {pt["name"]: pt["x_tier"] for pt in partners}
    for it in items:
        g = it["gold"]
        g["total"] = await gold_total(agent, g["lines"], by_sku, partner_tier[g["customer"]], g["certs"])
    OUT.write_text("\n".join(json.dumps(i) for i in items) + "\n")
    cats = defaultdict(int)
    for it in items:
        cats[it["category"]] += 1
    print(len(items), dict(cats), "->", OUT)


if __name__ == "__main__":
    asyncio.run(main())
