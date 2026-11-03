"""Pure scoring of a predicted quote against a gold item.

pred: {"lines": [{"sku": str | None, "qty": int | None}], "total": float}
gold: {"lines": [{"sku": str | None, "qty": int, "abstain": bool}], "total": float | None}
"""
from collections import Counter


def _pred_pairs(pred: dict) -> Counter:
    return Counter((p["sku"], p["qty"]) for p in pred["lines"] if p["sku"])


def line_f1(pred: dict, gold: dict) -> float:
    want = Counter((g["sku"], g["qty"]) for g in gold["lines"] if not g["abstain"])
    got = _pred_pairs(pred)
    if not want and not got:
        return 1.0
    hits = sum((want & got).values())
    p, r = hits / max(sum(got.values()), 1), hits / max(sum(want.values()), 1)
    return 2 * p * r / (p + r) if p + r else 0.0


def sku_acc(pred: dict, gold: dict) -> float | None:
    want = [g["sku"] for g in gold["lines"] if not g["abstain"]]
    if not want:
        return None
    got = Counter(p["sku"] for p in pred["lines"] if p["sku"])
    return sum((Counter(want) & got).values()) / len(want)


def abstain_correct(pred: dict, gold: dict) -> float | None:
    should = sum(g["abstain"] for g in gold["lines"])
    if not should:
        return None
    held_back = sum(1 for p in pred["lines"] if not p["sku"])  # no resolved SKU = asked for clarification
    return min(held_back, should) / should


def price_err(pred: dict, gold: dict) -> float | None:
    return abs(pred["total"] - gold["total"]) / gold["total"] if gold.get("total") else None


def score(pred: dict, gold: dict) -> dict[str, float | None]:
    return {"line_f1": line_f1(pred, gold), "sku_acc": sku_acc(pred, gold),
            "abstain_correct": abstain_correct(pred, gold), "price_err": price_err(pred, gold)}
