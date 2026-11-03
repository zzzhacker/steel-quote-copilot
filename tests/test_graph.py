"""Graph wiring without a model: extraction is stubbed, everything else (MCP, Odoo, pricing, HITL) is real.
Skips unless Odoo and the ER MCP server are running."""
import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from steelquote.adapters.odoo import Odoo
from steelquote.agent import runner
from steelquote.agent.graph import build_graph
from steelquote.agent.nodes import Agent
from steelquote.config import get_settings
from steelquote.domain.schemas import RFQ, ExtractedLine as L

LINES = [
    L(raw_text="6 pcs 1/4 304 plt cut 24 x 24", qty=6, grade="304", form="plt", thickness="1/4", cut_width="24", cut_length="24"),
    L(raw_text="2 pcs 316L 3/8 plate 48x96", qty=2, grade="316L", form="plate", thickness="3/8", width="48", length="96"),
]


class StubExtract(Agent):
    async def extract(self, state, config):
        rfq = RFQ(customer_text="Kessler Fabrication", certs_required=True, lines=LINES)
        return {"rfq": rfq, "todo": [0, 1], "resolved": [], "attempts": {}}


@pytest.fixture
def graph():
    s = get_settings()
    try:
        httpx.get(s.odoo_url, timeout=2)
        httpx.get(s.er_mcp_url, timeout=2)
    except httpx.HTTPError:
        pytest.skip("Odoo / ER server not running")
    return build_graph(StubExtract(s, Odoo(s)), InMemorySaver())


@pytest.mark.asyncio
async def test_interrupt_then_approve_skips_write(graph):
    cfg = runner.run_config(skip_write=True)
    state = await runner.start(graph, "ignored", cfg)
    assert state["__interrupt__"], "graph must pause for human review"
    q = state["quote"]
    assert q.customer_name == "Kessler Fabrication" and q.tier == "A"
    assert [pl.line.status for pl in q.lines] == ["ok", "short"]
    assert any(a.is_remnant for a in q.lines[0].line.allocs), "cut job should come from a remnant first"
    done = await runner.resume(graph, {"action": "approve"}, cfg)
    assert "draft" not in done or not done["draft"]
