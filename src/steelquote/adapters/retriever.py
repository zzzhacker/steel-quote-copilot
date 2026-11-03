"""Hybrid entity retrieval over the Odoo catalog: PGVector (dense) + BM25 (sparse) fused with RRF.

The index is only a *candidate generator*; the ER server re-validates every hit against Odoo.
"""
import json
import re
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_postgres import PGVector

from steelquote.adapters.odoo import Odoo
from steelquote.config import get_settings
from steelquote.domain.schemas import Candidate

Mode = Literal["hybrid", "keyword", "vector"]
THK_TOL = 0.002
GRADES_JSON = Path(__file__).resolve().parents[3] / "data" / "grades.json"
FORM_WORDS = {"plate": "plate plt", "sheet": "sheet sht", "flat_bar": "flat bar flatbar", "round_bar": "round bar rod"}
ITEM_FIELDS = ["default_code", "name", "x_grade", "x_uns", "x_form", "x_thk_in", "x_w_in", "x_l_in"]


@lru_cache
def _embeddings() -> FastEmbedEmbeddings:
    return FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9./]+", text.lower())


def _grade_table() -> dict[str, dict]:
    # written by the domain agent; absent file just means no alias expansion
    return json.loads(GRADES_JSON.read_text()).get("grades", {}) if GRADES_JSON.exists() else {}


def _thk_forms(thk: float) -> str:
    frac = Fraction(thk).limit_denominator(64)
    out = [f"{thk:.3f}", f"{thk:g}", f"{thk:.3f}".lstrip("0")]
    if abs(float(frac) - thk) < 1e-3:
        out += [str(frac), f"{frac} inch", f'{frac}"']
    return " ".join(out)


def _item_doc(p: dict, grades: dict[str, dict]) -> Document:
    grade, form = p["x_grade"], p["x_form"]
    w, l = p["x_w_in"], p["x_l_in"]
    g = grades.get(grade, {})
    text = " ".join([
        grade, g.get("family", ""), FORM_WORDS.get(form, form), p["x_uns"] or "",
        " ".join(g.get("aliases", [])), _thk_forms(p["x_thk_in"]),
        f"{w:g}x{l:g}", f"{w:g} x {l:g}", p["default_code"] or "", p["name"] or "",
    ])
    meta = {"kind": "item", "product_id": p["id"], "default_code": p["default_code"], "name": p["name"],
            "grade": grade, "form": form, "thk_in": p["x_thk_in"], "w_in": w, "l_in": l}
    return Document(page_content=re.sub(r"\s+", " ", text).strip(), metadata=meta)


def _customer_doc(c: dict) -> Document:
    meta = {"kind": "customer", "partner_id": c["id"], "name": c["name"], "tier": c["x_tier"]}
    return Document(page_content=f"{c['name']} customer tier {c['x_tier']}", metadata=meta)


def load_documents(odoo: Odoo) -> tuple[list[Document], list[Document]]:
    grades = _grade_table()
    products = odoo.search_read("product.product", [["x_grade", "!=", False]], ["id", *ITEM_FIELDS])
    partners = odoo.search_read("res.partner", [["is_company", "=", True], ["x_tier", "!=", False]],
                                ["id", "name", "x_tier"])
    return [_item_doc(p, grades) for p in products], [_customer_doc(c) for c in partners]


def build_index(odoo: Odoo) -> int:
    """(Re)build both PGVector collections from Odoo. Returns the number of documents indexed."""
    items, customers = load_documents(odoo)
    for name, docs, key in (("items", items, "product_id"), ("customers", customers, "partner_id")):
        PGVector.from_documents(docs, _embeddings(), collection_name=name, connection=get_settings().er_dsn,
                                ids=[f"{name}:{d.metadata[key]}" for d in docs], pre_delete_collection=True)
    return len(items) + len(customers)


def _store(name: str) -> PGVector:
    return PGVector(_embeddings(), collection_name=name, connection=get_settings().er_dsn)


def _filters(grade: str | None, form: str | None, thk_in: float | None) -> dict[str, Any]:
    f: dict[str, Any] = {}
    if grade:
        f["grade"] = grade
    if form:
        f["form"] = form
    if thk_in is not None:
        f["thk_in"] = {"$between": [thk_in - THK_TOL, thk_in + THK_TOL]}
    return {"$and": [{k: v} for k, v in f.items()]} if len(f) > 1 else f


def _matches(meta: dict, grade: str | None, form: str | None, thk_in: float | None) -> bool:
    return ((not grade or meta["grade"] == grade) and (not form or meta["form"] == form)
            and (thk_in is None or abs(meta["thk_in"] - thk_in) <= THK_TOL))


class Retriever:
    def __init__(self, odoo: Odoo):
        self.odoo = odoo
        self.items = _store("items")
        self.customers = _store("customers")
        self.refresh()

    def refresh(self) -> None:
        """Reload the BM25 side from Odoo (the dense side lives in Postgres)."""
        self.item_docs, self.customer_docs = load_documents(self.odoo)

    def _search(self, store: PGVector, docs: list[Document], text: str, k: int, mode: Mode,
                vfilter: dict | None = None) -> list[tuple[Document, float]]:
        """Return top-k (doc, cosine) in the order dictated by `mode`."""
        if not docs:
            return []
        pool = max(k * 4, 20)
        bm25 = BM25Retriever.from_documents(docs, preprocess_func=_tokens, k=pool)
        dense = store.as_retriever(search_kwargs={"k": pool, "filter": vfilter or None})
        ranked = {"keyword": bm25, "vector": dense,
                  "hybrid": EnsembleRetriever(retrievers=[bm25, dense], weights=[0.4, 0.6])}[mode].invoke(text)[:k]
        # same score in every mode so callers can compare: cosine(query, doc)
        q = np.array(_embeddings().embed_query(text))
        vecs = np.array(_embeddings().embed_documents([d.page_content for d in ranked]))
        return list(zip(ranked, (vecs @ q / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q))).tolist()))

    def search_items(self, text: str, grade: str | None = None, form: str | None = None,
                     thk_in: float | None = None, w_in: float | None = None, l_in: float | None = None,
                     k: int = 5, mode: Mode = "hybrid") -> list[Candidate]:
        query = f"{text} {w_in:g}x{l_in:g}" if w_in and l_in else text
        hits: list[tuple[Document, float]] = []
        if grade or form or thk_in is not None:
            subset = [d for d in self.item_docs if _matches(d.metadata, grade, form, thk_in)]
            hits = self._search(self.items, subset, query, k, mode, _filters(grade, form, thk_in))
        if not hits:  # filter matched nothing: fall back to free-text search
            hits = self._search(self.items, self.item_docs, query, k, mode)
        return [Candidate(**{f: d.metadata[f] for f in Candidate.model_fields if f != "score"}, score=round(s, 4))
                for d, s in hits]

    def search_customers(self, text: str, k: int = 3, mode: Mode = "hybrid") -> list[dict]:
        hits = self._search(self.customers, self.customer_docs, text, k, mode)
        return [{**{f: d.metadata[f] for f in ("partner_id", "name", "tier")}, "score": round(s, 4)}
                for d, s in hits]
