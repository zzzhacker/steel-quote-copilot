"""Smoke test: talk to the running ER MCP server the way the agent will (via langchain-mcp-adapters)."""
import asyncio
import json

from langchain_mcp_adapters.client import MultiServerMCPClient

from steelquote.config import get_settings


def show(title: str, res) -> None:
    # tool results come back as content blocks; the JSON payload is in block["text"]
    blocks = res if isinstance(res, list) else [res]
    payload = [json.loads(b["text"]) if isinstance(b, dict) and "text" in b else b for b in blocks]
    print(f"\n== {title}")
    for p in payload:
        print(json.dumps(p, default=str)[:700])


async def main() -> None:
    client = MultiServerMCPClient({"er": {"transport": "streamable_http", "url": get_settings().er_mcp_url}})
    tools = {t.name: t for t in await client.get_tools()}
    print("tools:", sorted(tools))
    call = lambda name, **kw: tools[name].ainvoke(kw)  # noqa: E731

    items = await call("resolve_item", text="18-8 1/4 plt", grade="304")
    show("resolve_item('18-8 1/4 plt', grade='304')", items)
    show("resolve_item('18-8 1/4 plt') unstructured", await call("resolve_item", text="18-8 1/4 plt", k=3))
    show("resolve_customer('Kessler Fab')", await call("resolve_customer", text="Kessler Fab"))
    exact = await call("resolve_item", text="304 plate", grade="304", form="plate", thk_in=0.25, w_in=48, l_in=96, k=1)
    pid = json.loads(exact[0]["text"])["product_id"]
    show("resolve_item('304 plate', 0.25, 48x96)", exact)
    show("find_stock 24x24 cut", await call("find_stock", product_id=pid, qty=3, cut_w_in=24, cut_l_in=24))
    show("find_stock 48x96 full sheet x2", await call("find_stock", product_id=pid, qty=2))
    show("validate_entities", await call("validate_entities", product_ids=[pid, 999999]))


asyncio.run(main())
