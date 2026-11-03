"""Integration tests: need Odoo (seeded) + the Postgres `er` DB; skipped otherwise."""
import pytest

from steelquote.adapters.odoo import Odoo
from steelquote.adapters.retriever import Retriever, build_index


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    try:
        odoo = Odoo()
        if not odoo.search_read("product.product", [["x_grade", "!=", False]], ["id"], limit=1):
            pytest.skip("Odoo not seeded")
        build_index(odoo)
        return Retriever(odoo)
    except Exception as e:  # unreachable services or missing custom fields
        pytest.skip(f"Odoo/Postgres unavailable: {e}")


# alias query -> (canonical grade, form) the top hits should be about
ALIAS_QUERIES = [("18-8 1/4 plt", "304", "plate"), ("S31603 .250 plt", "316L", "plate"), ("6061 flat bar 1/4", "6061", "flat_bar")]


def _hits(retriever: Retriever, mode: str) -> int:
    n = 0
    for text, grade, form in ALIAS_QUERIES:
        top = retriever.search_items(text, k=5, mode=mode)
        n += sum(c.grade == grade and c.form == form for c in top)
    return n


def test_hybrid_at_least_keyword(retriever: Retriever) -> None:
    assert _hits(retriever, "hybrid") >= _hits(retriever, "keyword")


def test_validate_rejects_fake_id(retriever: Retriever) -> None:
    from steelquote.er_server.server import validate_entities

    real = retriever.search_items("304 plate", k=1)[0].product_id
    res = validate_entities([real, 999999])
    assert res["products"][real]["exists"] and not res["products"][999999]["exists"]
