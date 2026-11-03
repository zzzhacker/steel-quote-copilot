"""Run the hero RFQ through every non-LLM node (normalize -> resolve -> validate -> price -> write_draft)."""
import asyncio
import sys

from steelquote.adapters.odoo import Odoo
from steelquote.agent.cli import show
from steelquote.agent.nodes import Agent
from steelquote.config import get_settings
from steelquote.domain.schemas import RFQ, ExtractedLine as L

LINES = [
    L(raw_text="6 pcs 1/4 304 plt cut 24 x 24", qty=6, grade="304", form="plt", thickness="1/4", cut_width="24", cut_length="24"),
    L(raw_text="4 sheets 7ga A36 4x8", qty=4, grade="A36", form="sheet", thickness="7ga", width="4", length="8"),
    L(raw_text="2 pcs 316L 3/8 plate 48x96", qty=2, grade="316L", form="plate", thickness="3/8", width="48", length="96"),
]


async def main(write: bool) -> None:
    s = get_settings()
    agent = Agent(s, Odoo(s))
    cfg = {"configurable": {"skip_write": not write}}
    state: dict = {"rfq": RFQ(customer_text="Kessler Fabrication", certs_required=True, ship_by="Fri", lines=LINES),
                   "todo": [0, 1, 2], "resolved": [], "attempts": {}}
    state |= agent.normalize(state)
    while True:
        state |= await agent.resolve(state, cfg)
        state |= await agent.validate(state, cfg)
        if not state["todo"]:
            break
    state |= agent.price(state)
    show(state)
    if write:
        state |= await agent.write_draft(state, cfg)
        print("Draft:", state["draft"])


asyncio.run(main("--write" in sys.argv))
