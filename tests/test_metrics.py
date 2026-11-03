from evals.metrics import score

GOLD = {"lines": [{"sku": "A", "qty": 2, "abstain": False}, {"sku": "B", "qty": 1, "abstain": True}], "total": 100.0}


def test_abstaining_where_gold_abstains_scores_perfectly():
    pred = {"lines": [{"sku": "A", "qty": 2}, {"sku": None, "qty": 1}], "total": 90.0}
    assert score(pred, GOLD) == {"line_f1": 1.0, "sku_acc": 1.0, "abstain_correct": 1.0, "price_err": 0.1}


def test_guessing_a_sku_where_gold_abstains_is_penalised():
    pred = {"lines": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}], "total": 100.0}
    m = score(pred, GOLD)
    assert m["abstain_correct"] == 0.0 and m["line_f1"] < 1.0
