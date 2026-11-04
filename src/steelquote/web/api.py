"""python -m steelquote.web.api  ->  chat UI + JSON API on http://0.0.0.0:8501

The page streams each LangGraph node as it finishes (server-sent events), so the pipeline is visible while it runs.
"""
import json
import re
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from steelquote.agent import runner
from steelquote.config import get_settings

ROOT = Path(__file__).resolve().parents[3]
STATIC = Path(__file__).parent / "static"
SCENARIOS = {  # category -> (title, what it shows)
    "clean": ("Clean order", "Straightforward lines that should price end to end."),
    "alias_units": ("Aliases and units", "Grade aliases, UNS numbers, abbreviations and unusual units."),
    "ambiguous": ("Ambiguous request", "Gauge vs fraction: the agent should flag it, not guess."),
    "out_of_stock": ("Out of stock", "Stock is short; the shortage is flagged, never hidden."),
    "missing_dims": ("Missing dimensions", "Not enough to identify an item; the agent should ask."),
    "chatty": ("Chatty email", "Noise, small talk and several lines in one message."),
}
NODES = ("extract", "normalize", "resolve", "validate", "price", "human_review", "revise", "write_draft")

app = FastAPI(title="Steel RFQ Copilot")
graph = runner.make_graph(InMemorySaver())
configs: dict[str, dict] = {}  # thread id -> run config, so a chat edit resumes the same checkpointed run


class RunBody(BaseModel):
    thread: str
    email: str


class EditBody(BaseModel):
    thread: str
    text: str


class ApproveBody(BaseModel):
    thread: str


def config_for(thread: str) -> dict:
    return configs.setdefault(thread, runner.run_config(thread_id=thread))


def known(thread: str) -> None:
    # checkpoints are in memory, so a server restart orphans open browser tabs
    if thread not in configs:
        raise HTTPException(409, "This session was lost when the server restarted. Run the request again.")


def encode(model: BaseModel | None) -> dict | None:
    """Money goes out as JSON numbers (pydantic's own JSON mode would turn Decimal into strings)."""
    return jsonable_encoder(model.model_dump(), custom_encoder={Decimal: float}) if model else None


def detail(node: str, update: dict) -> str | None:
    if node == "extract":
        return f"{len(update['rfq'].lines)} lines read"
    if node == "validate":
        return f"{len(update['todo'])} sent back to resolve" if update.get("todo") else "all lines checked live"
    return None


async def stream(input_, thread: str):
    """Yield one `step` event per finished node, then `result` with the quote (or `error`)."""
    config, last = config_for(thread), time.perf_counter()
    try:
        async for chunk in graph.astream(input_, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node not in NODES:
                    continue
                now = time.perf_counter()
                yield {"event": "step", "data": json.dumps(
                    {"node": node, "ms": round((now - last) * 1000), "detail": detail(node, update or {})})}
                last = now
        values = (await graph.aget_state(config)).values
        yield {"event": "result", "data": json.dumps({"quote": encode(values.get("quote"))})}
    except Exception as e:  # shown in the UI instead of a silent hang
        yield {"event": "error", "data": json.dumps({"message": f"{type(e).__name__}: {str(e)[:300]}"})}


@app.post("/api/run")
async def run(body: RunBody):
    configs.pop(body.thread, None)
    return EventSourceResponse(stream({"email": body.email}, body.thread))


@app.post("/api/edit")
async def edit(body: EditBody):
    known(body.thread)
    return EventSourceResponse(stream(Command(resume={"action": "edit", "text": body.text}), body.thread))


@app.post("/api/approve")
async def approve(body: ApproveBody, request: Request):
    known(body.thread)
    state = await runner.resume(graph, {"action": "approve"}, config_for(body.thread))
    draft = dict(state["draft"])
    port = urlsplit(get_settings().odoo_url).port or 8069
    # the browser may be on another machine: point the link at whatever host it used to reach us
    draft["url"] = urlsplit(draft["url"])._replace(netloc=f"{request.url.hostname}:{port}").geturl()
    return {"draft": draft}


@app.get("/api/samples")
def samples():
    out = [{"id": "hero", "scenario": "Mixed order", "label": "Remnant pick, short 316L, certs",
            "about": "Remnant-first cutting, a stock shortage and certs in one email.",
            "email": (ROOT / "evals/hero.txt").read_text()}]
    seen: dict[str, int] = {}
    for line in (ROOT / "evals/rfqs.jsonl").read_text().splitlines():
        item = json.loads(line)
        title, about = SCENARIOS[item["category"]]
        seen[item["category"]] = seen.get(item["category"], 0) + 1
        if seen[item["category"]] > 2:
            continue
        first = re.search(r"^\s*1\)\s*(.+)$", item["email"], re.M)
        out.append({"id": item["id"], "scenario": title, "about": about,
                    "label": first.group(1).strip() if first else item["id"], "email": item["email"]})
    return out


@app.get("/api/evals")
def evals():
    path = ROOT / "evals/results/summary.json"
    return json.loads(path.read_text()) if path.exists() else []


@app.get("/api/info")
def info():
    s = get_settings()
    return {"models": {"extract": s.model_extract, "disambiguate": s.model_disambiguate}}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8501)
