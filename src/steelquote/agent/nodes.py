"""Graph nodes. Each does one job; the LLM never prices, and every ERP fact comes through MCP tools."""
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.types import interrupt
from pydantic import BaseModel, create_model

from steelquote.adapters.odoo import Odoo
from steelquote.config import Settings
from steelquote.domain.ontology import normalize_line
from steelquote.domain.pricing import build_quote
from steelquote.domain.schemas import (RFQ, Candidate, Check, ExtractedLine, NormLine,
                                       Quote, ResolvedLine, StockAlloc)

PROMPTS = Path(__file__).parent / "prompts"
TOL = 0.002
NO_TAX = [[6, 0, []]]  # quotes are pre-tax; tax rules are the ERP's job, not the copilot's


class State(TypedDict, total=False):
    email: str
    rfq: RFQ
    norm: list[NormLine]
    todo: list[int]  # 0-based indices to (re)resolve; revisions only re-run what changed
    resolved: list[ResolvedLine]
    attempts: dict[int, int]
    partner: dict
    quote: Quote
    decision: dict
    draft: dict


class LinePatch(BaseModel):
    line_no: int
    remove: bool = False
    qty: int | None = None
    grade: str | None = None
    form: str | None = None
    thickness: str | None = None
    width: str | None = None
    length: str | None = None
    cut_width: str | None = None
    cut_length: str | None = None


class Revision(BaseModel):
    patches: list[LinePatch] = []
    customer_text: str | None = None
    certs_required: bool | None = None


class Sku(BaseModel):
    default_code: str


FREE_TIER_RPM = {"lite": 10, "flash": 5}  # conservative free-tier requests/minute (Google publishes no number); retries absorb 429s
_LIMITERS: dict[str, InMemoryRateLimiter] = {}


def _limiter(model: str) -> InMemoryRateLimiter:
    family = next(k for k in FREE_TIER_RPM if k in model)
    return _LIMITERS.setdefault(family, InMemoryRateLimiter(
        requests_per_second=FREE_TIER_RPM[family] / 60 * 0.8, max_bucket_size=1))  # shared across steps, 20% headroom


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def _unwrap(res: Any, many: bool) -> Any:
    """MCP tool results arrive as content blocks of JSON text; flatten them back into Python."""
    if isinstance(res, tuple):
        res = res[0]
    out: list = []
    for block in res if isinstance(res, list) else [res]:
        text = block.get("text") if isinstance(block, dict) else block
        value = json.loads(text) if isinstance(text, str) else text
        out.extend(value) if isinstance(value, list) else out.append(value)
    return out if many else out[0]


def _matches(n: NormLine, c: Candidate) -> bool:
    ok = c.grade == n.grade and (n.form is None or c.form == n.form)
    ok = ok and n.thk_in is not None and abs(c.thk_in - n.thk_in) <= TOL
    for want, have in ((n.w_in, c.w_in), (n.l_in, c.l_in)):
        ok = ok and (want is None or abs(want - have) <= 0.5)
    return ok


def _fits(cut: tuple[float, float], w: float, l: float) -> bool:
    return (cut[0] <= w and cut[1] <= l) or (cut[1] <= w and cut[0] <= l)


class Agent:
    def __init__(self, settings: Settings, odoo: Odoo):
        self.s, self.odoo = settings, odoo
        self._mcp = MultiServerMCPClient(
            {"er": {"url": settings.er_mcp_url, "transport": "streamable_http"}})
        self._tools: dict[str, Any] = {}

    def _llm(self, model: str, schema: type[BaseModel]):
        """Provider is picked from the model name, so routing a step to another model is a config change."""
        if model.startswith("claude"):  # 5.x models reject temperature and forced tool use: native structured output
            llm = init_chat_model(model, max_tokens=8000, output_config={"effort": self.s.effort},
                                  api_key=self.s.anthropic_api_key or None)
            return llm.with_structured_output(schema, method="json_schema")
        llm = init_chat_model(model, model_provider="google_genai", max_retries=3, timeout=45, rate_limiter=_limiter(model),
                              api_key=self.s.google_api_key or self.s.gemini_api_key or None)
        return llm.with_structured_output(schema)

    async def _tool(self, name: str, many: bool = False, **args: Any) -> Any:
        if not self._tools:
            self._tools = {t.name: t for t in await self._mcp.get_tools()}
        return _unwrap(await self._tools[name].ainvoke(args), many)

    # --- nodes -------------------------------------------------------------------------------

    async def extract(self, state: State, config: RunnableConfig) -> dict:
        model = config["configurable"].get("model_extract", self.s.model_extract)
        rfq = await self._llm(model, RFQ).ainvoke(
            [("system", _prompt("extract")), ("human", state["email"])])
        return {"rfq": rfq, "todo": list(range(len(rfq.lines))), "resolved": [], "attempts": {}}

    def normalize(self, state: State) -> dict:
        norm = [normalize_line(i + 1, line) for i, line in enumerate(state["rfq"].lines)]
        resolved = state.get("resolved") or []
        for i, r in enumerate(resolved):  # keep untouched lines in sync after a removal/renumber
            if i not in state["todo"] and i < len(norm):
                r.norm = norm[i]
        return {"norm": norm}

    async def resolve(self, state: State, config: RunnableConfig) -> dict:
        cfg = config["configurable"]
        norm = state["norm"]
        resolved = list(state.get("resolved") or [])
        resolved += [None] * (len(norm) - len(resolved))
        partner = state.get("partner")
        if not partner and state["rfq"].customer_text and cfg.get("grounded", True):
            found = await self._tool("resolve_customer", many=True, text=state["rfq"].customer_text)
            partner = found[0] if found else {}
        for i in state["todo"]:
            retry = state.get("attempts", {}).get(i, 0) > 0
            resolved[i] = await self._resolve_line(norm[i], cfg, retry)
        return {"resolved": resolved, "partner": partner or {}}

    async def _resolve_line(self, n: NormLine, cfg: dict, retry: bool) -> ResolvedLine:
        if n.grade is None or n.thk_in is None or n.qty is None:
            gaps = "; ".join(n.issues) or "grade, thickness or quantity missing"
            return ResolvedLine(norm=n, checks=[Check(name="complete", ok=False, detail=gaps)])
        if not cfg.get("grounded", True):
            return await self._guess_sku(n, cfg)
        args: dict[str, Any] = {"text": n.raw_text, "k": 8 if retry else 5,
                                "mode": cfg.get("er_mode", "hybrid")}
        if not retry:  # a retry drops the structured filters and searches on text alone
            args |= {"grade": n.grade, "form": n.form, "thk_in": n.thk_in, "w_in": n.w_in, "l_in": n.l_in}
        cands = [Candidate(**c) for c in await self._tool("resolve_item", many=True, **args)]
        if any("ambiguous" in i for i in n.issues):
            return ResolvedLine(norm=n, alternatives=cands, checks=[
                Check(name="ambiguity", ok=False, detail="; ".join(n.issues))])
        exact = [c for c in cands if _matches(n, c)]
        if len(exact) == 1:
            chosen = exact[0]
        elif len(exact) > 1 and len({(c.grade, c.form, c.thk_in) for c in exact}) == 1:
            chosen = await self._pick_by_stock(n, exact)  # same item in several sheet sizes
        else:
            chosen = await self._choose(n, exact or cands, cfg)
        if chosen is None:
            return ResolvedLine(norm=n, alternatives=cands, checks=[
                Check(name="candidates", ok=False, detail="no candidate clearly matches")])
        return ResolvedLine(norm=n, candidate=chosen, alternatives=[c for c in cands if c != chosen])

    async def _pick_by_stock(self, n: NormLine, cands: list[Candidate]) -> Candidate:
        """Customer gave no sheet size: take the size that can fill the order, remnant first if cutting."""
        has_cut = None not in (n.cut_w_in, n.cut_l_in)
        ranked = []
        for c in cands:
            allocs = await self._tool("find_stock", many=True, product_id=c.product_id, qty=n.qty,
                                      cut_w_in=n.cut_w_in, cut_l_in=n.cut_l_in)
            fills = sum(a["qty"] for a in allocs) >= n.qty
            uses_remnant = has_cut and any(a["is_remnant"] for a in allocs)
            ranked.append(((fills, uses_remnant, -c.w_in * c.l_in), c))
        return max(ranked, key=lambda rc: rc[0])[1]

    async def _choose(self, n: NormLine, cands: list[Candidate], cfg: dict) -> Candidate | None:
        if not cands:
            return None
        options = tuple(str(c.product_id) for c in cands) + ("none",)
        # the model can only return an ID the ERP gave us, or "none": enforced by the schema
        Choice = create_model("Choice", reason=(str, ""), product_id=(Literal[options], "none"))
        model = cfg.get("model_disambiguate", self.s.model_disambiguate)
        payload = {"request": n.model_dump(exclude={"issues"}),
                   "candidates": [c.model_dump(exclude={"score"}) for c in cands]}
        pick = await self._llm(model, Choice).ainvoke(
            [("system", _prompt("disambiguate")), ("human", json.dumps(payload))])
        return next((c for c in cands if str(c.product_id) == pick.product_id), None)

    async def _guess_sku(self, n: NormLine, cfg: dict) -> ResolvedLine:
        """Ungrounded baseline for the evals: the model writes the SKU itself, no ERP lookup."""
        model = cfg.get("model_disambiguate", self.s.model_disambiguate)
        sku = await self._llm(model, Sku).ainvoke([
            ("system", "Write the inventory SKU for this line, format like SS304-PL-0.250-48x96 "
                       "(prefix SS/CS/AL, form PL/SH/FB/RB, thickness in inches, WxL in inches)."),
            ("human", n.model_dump_json())])
        cand = Candidate(product_id=0, default_code=sku.default_code, name=sku.default_code,
                         grade=n.grade or "", form=n.form or "plate", thk_in=n.thk_in or 0,
                         w_in=n.w_in or 0, l_in=n.l_in or 0)
        return ResolvedLine(norm=n, candidate=cand, status="ok")

    async def validate(self, state: State, config: RunnableConfig) -> dict:
        if not config["configurable"].get("grounded", True):
            return {"todo": []}
        cfg = config["configurable"]
        resolved, attempts = list(state["resolved"]), dict(state.get("attempts", {}))
        todo = [i for i in state["todo"] if resolved[i].candidate]
        live = await self._tool("validate_entities", product_ids=[resolved[i].candidate.product_id for i in todo],
                                partner_id=(state.get("partner") or {}).get("partner_id")) if todo else {}
        exists = {int(k): v["exists"] for k, v in live.get("products", {}).items()}
        retry: list[int] = []
        for i in todo:
            r = await self._validate_line(resolved[i], exists)
            resolved[i] = r
            if r.status == "needs_clarification" and attempts.get(i, 0) == 0 and cfg.get("retry", True):
                attempts[i] = 1
                retry.append(i)
        return {"resolved": resolved, "attempts": attempts, "todo": retry}

    async def _validate_line(self, r: ResolvedLine, exists: dict[int, bool]) -> ResolvedLine:
        n, c = r.norm, r.candidate
        cut = (n.cut_w_in, n.cut_l_in)
        has_cut = None not in cut
        r.allocs = [StockAlloc(**a) for a in await self._tool(
            "find_stock", many=True, product_id=c.product_id, qty=n.qty,
            cut_w_in=cut[0], cut_l_in=cut[1])]
        have = sum(a.qty for a in r.allocs)
        r.checks = [
            Check(name="exists_in_erp", ok=exists.get(c.product_id, False), detail=c.default_code),
            Check(name="matches_request", ok=_matches(n, c), detail=f"{c.grade} {c.form} {c.thk_in}in"),
            Check(name="cut_fits", ok=not has_cut or _fits(cut, c.w_in, c.l_in),
                  detail=f"cut {cut[0]}x{cut[1]} from {c.w_in}x{c.l_in}" if has_cut else "no cut"),
            Check(name="stock_sufficient", ok=have >= n.qty, detail=f"{have} of {n.qty} available"),
        ]
        hard = [x for x in r.checks if x.name != "stock_sufficient" and not x.ok]
        r.status = "needs_clarification" if hard else ("ok" if have >= n.qty else "short")
        return r

    def price(self, state: State) -> dict:
        p, rfq = state.get("partner") or {}, state["rfq"]
        quote = build_quote(state["resolved"], p.get("partner_id"), p.get("name"), p.get("tier"),
                            rfq.certs_required, rfq.ship_by)
        return {"quote": quote}

    def human_review(self, state: State, config: RunnableConfig) -> dict:
        if config["configurable"].get("auto_approve"):
            return {"decision": {"action": "approve"}}
        return {"decision": interrupt({"quote": state["quote"].model_dump(mode="json")})}

    async def revise(self, state: State, config: RunnableConfig) -> dict:
        model = config["configurable"].get("model_revise", self.s.model_revise)
        rfq = state["rfq"].model_copy(deep=True)
        view = [{"line_no": i + 1, **l.model_dump()} for i, l in enumerate(rfq.lines)]
        rev = await self._llm(model, Revision).ainvoke([
            ("system", _prompt("revise")),
            ("human", f"Lines: {json.dumps(view)}\nRep says: {state['decision']['text']}")])
        todo, resolved, drop = [], list(state["resolved"]), set()
        for p in rev.patches:
            i = p.line_no - 1
            if not 0 <= i < len(rfq.lines):
                continue
            if p.remove:
                drop.add(i)
                continue
            changes = p.model_dump(exclude={"line_no", "remove"}, exclude_none=True)
            rfq.lines[i] = rfq.lines[i].model_copy(update=changes)
            todo.append(i)
        if rev.certs_required is not None:
            rfq.certs_required = rev.certs_required
        keep = [i for i in range(len(rfq.lines)) if i not in drop]
        remap = {old: new for new, old in enumerate(keep)}
        rfq.lines = [rfq.lines[i] for i in keep]
        update: dict = {"rfq": rfq, "resolved": [resolved[i] for i in keep],
                        "todo": [remap[i] for i in todo if i in remap], "attempts": {}}
        if rev.customer_text:
            rfq.customer_text, update["partner"] = rev.customer_text, {}
        return update

    async def write_draft(self, state: State, config: RunnableConfig) -> dict:
        cfg = config["configurable"]
        if cfg.get("skip_write"):
            return {}
        q: Quote = state["quote"]
        if not q.partner_id:
            raise ValueError("customer not resolved: tell the agent who the customer is before approving")
        services = {p["name"]: p["id"] for p in self.odoo.search_read(
            "product.product", [["name", "in", ["Saw Cut", "Shear Cut", "MTR Cert"]]], ["name"])}
        order_lines = []
        for pl in q.lines:
            r, b = pl.line, pl.breakdown
            qty = sum(a.qty for a in r.allocs) or r.norm.qty or 0
            if not r.candidate or not qty or pl.total == 0:
                continue
            heats = ", ".join(f"{a.heat}{' (remnant)' if a.is_remnant else ''}" for a in r.allocs)
            service = b.get("cut", Decimal(0)) + b.get("cert", Decimal(0))
            order_lines.append([0, 0, {"product_id": r.candidate.product_id, "product_uom_qty": qty,
                                       "name": f"{r.candidate.name} | heat {heats}",
                                       "tax_ids": NO_TAX, "price_unit": float((pl.total - service) / qty)}])
            for key, svc in (("cut", "Saw Cut"), ("cert", "MTR Cert")):
                if b.get(key) and svc in services:
                    order_lines.append([0, 0, {"product_id": services[svc], "product_uom_qty": 1,
                                               "name": f"{svc}: {r.candidate.default_code}",
                                               "tax_ids": NO_TAX, "price_unit": float(b[key])}])
        [oid] = self.odoo.create("sale.order", [{"partner_id": q.partner_id, "order_line": order_lines,
                                                 "note": f"Ship by: {q.ship_by or 'n/a'}"}])
        name = self.odoo.search_read("sale.order", [["id", "=", oid]], ["name"])[0]["name"]
        self.odoo.message_post("sale.order", oid, self._audit_note(q, cfg))
        url = f"{self.s.odoo_url}/odoo/action-sale.action_quotations_with_onboarding/{oid}"
        return {"draft": {"id": oid, "name": name, "url": url}}

    def _audit_note(self, q: Quote, cfg: dict) -> str:
        rows = "".join(
            f"<li>{pl.line.norm.raw_text} &rarr; <b>{pl.line.candidate.default_code if pl.line.candidate else 'UNRESOLVED'}</b> "
            f"[{pl.line.status}] " + ", ".join(f"{c.name}={'ok' if c.ok else 'FAIL'}" for c in pl.line.checks) + "</li>"
            for pl in q.lines)
        models = {k: cfg.get(k, getattr(self.s, k)) for k in ("model_extract", "model_disambiguate")}
        trace = f"<p>Trace: {cfg['trace_url']}</p>" if cfg.get("trace_url") else ""
        return (f"<p><b>Drafted by steelquote agent; approved by a human.</b> Prices are computed by "
                f"deterministic code, not the model.</p><ul>{rows}</ul><p>Models: {models}</p>{trace}")
