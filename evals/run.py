"""Compare model routing and grounding choices on the RFQ eval set.

    uv run python -m evals.run --all            # every config, results -> evals/results/
    uv run python -m evals.run --config lite --limit 5
Needs a Gemini key (GOOGLE_API_KEY) and the ER server. With LANGFUSE_* keys set, each config is also a Langfuse
experiment run on dataset `rfq-eval-v1` (trace per item, scores per metric).
"""
import argparse
import asyncio
import json
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver

from evals.metrics import score
from steelquote.agent import runner

DATASET = "rfq-eval-v1"
RESULTS = Path("evals/results")
# $/1M tokens in/out at paid list prices. The free tier is what actually runs, so this is the "what it would
# cost at volume" number that justifies model routing, not real spend.
PRICES = {"lite": (0.30, 2.50), "flash": (0.75, 3.75)}  # $/1M in/out, list prices (3.8 Flash promo rate to Dec 31 2026)
CONFIGS = {
    "lite": {},  # baseline: 3.5 Flash-Lite for every step, hybrid retrieval, validate + retry
    "flash": {"model_extract": "gemini-3.8-flash", "model_disambiguate": "gemini-3.8-flash"},
    "ungrounded": {"grounded": False},  # the model writes the SKU itself, no ERP lookup
    "keyword": {"er_mode": "keyword"},
    "vector": {"er_mode": "vector"},
    "no_retry": {"retry": False},
}
METRICS = ["line_f1", "sku_acc", "abstain_correct", "price_err"]


def cost_usd(usage: dict) -> float:
    total = 0.0
    for model, u in usage.items():
        tier = "lite" if "lite" in model else "flash"
        total += (u["input_tokens"] * PRICES[tier][0] + u["output_tokens"] * PRICES[tier][1]) / 1e6
    return total


def load_items() -> list[dict]:
    return [json.loads(line) for line in Path("evals/rfqs.jsonl").read_text().splitlines()]


async def run_item(graph, email: str, flags: dict) -> dict:
    usage = UsageMetadataCallbackHandler()
    config = runner.run_config(auto_approve=True, skip_write=True, **flags)
    config["callbacks"] = config["callbacks"] + [usage]
    start = time.perf_counter()
    try:
        state = await runner.start(graph, email, config)
        q = state["quote"]
        pred = {"lines": [{"sku": pl.line.candidate.default_code if pl.line.candidate else None,
                           "qty": pl.line.norm.qty} for pl in q.lines], "total": float(q.total)}
        error = None
    except Exception as e:  # a crash is a wrong answer, not a reason to drop the item
        pred, error = {"lines": [], "total": 0.0}, f"{type(e).__name__}: {str(e)[:200]}"
    return {"pred": pred, "error": error, "latency_s": time.perf_counter() - start,
            "cost_usd": cost_usd(usage.usage_metadata)}


def run_config(name: str, items: list[dict], limit: int | None) -> list[dict]:
    flags = CONFIGS[name]
    graph = runner.make_graph(InMemorySaver())
    rows: list[dict] = []

    async def task(*, item, **_):
        inp = item["input"] if isinstance(item, dict) else item.input
        gold = item["expected_output"] if isinstance(item, dict) else item.expected_output
        out = await run_item(graph, inp, flags)
        out["id"] = item.get("id") if isinstance(item, dict) else item.id
        out["metrics"] = score(out["pred"], gold)
        rows.append(out)
        return out

    if runner.tracing():
        from langfuse import Evaluation, get_client
        lf = get_client()
        ds = lf.get_dataset(DATASET)
        ds.items = ds.items[:limit] if limit else ds.items

        def evaluator(*, output, **_):
            return [Evaluation(name=k, value=v) for k, v in output["metrics"].items() if v is not None] + [
                Evaluation(name="cost_usd", value=output["cost_usd"]),
                Evaluation(name="latency_s", value=output["latency_s"])]

        ds.run_experiment(name=f"{name}", run_name=f"{name}-{datetime.now():%m%d-%H%M}", task=task,
                          evaluators=[evaluator], max_concurrency=2, metadata={k: str(v) for k, v in flags.items()})
        lf.flush()
    else:
        async def go():
            sem = asyncio.Semaphore(2)

            async def one(it):
                async with sem:
                    await task(item={"id": it["id"], "input": it["email"], "expected_output": it["gold"]})
            await asyncio.gather(*(one(it) for it in (items[:limit] if limit else items)))
        asyncio.run(go())
    return rows


def summarize(name: str, rows: list[dict]) -> dict:
    out: dict = {"config": name, "n": len(rows), "errors": sum(1 for r in rows if r["error"])}
    for m in METRICS:
        vals = [r["metrics"][m] for r in rows if r["metrics"][m] is not None]
        out[m] = round(statistics.mean(vals), 3) if vals else None
    out["cost_per_rfq"] = round(statistics.mean(r["cost_usd"] for r in rows), 5)
    out["p50_latency_s"] = round(statistics.median(r["latency_s"] for r in rows), 1)
    return out


def upload_dataset(items: list[dict]) -> None:
    from langfuse import get_client
    lf = get_client()
    lf.create_dataset(name=DATASET, description="Synthetic steel RFQs with gold SKUs (gold-first)")
    for it in items:
        lf.create_dataset_item(dataset_name=DATASET, id=it["id"], input=it["email"], expected_output=it["gold"],
                               metadata={"category": it["category"]})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", choices=list(CONFIGS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    names = list(CONFIGS) if args.all else (args.config or ["lite"])
    items = load_items()
    if runner.tracing():
        upload_dataset(items)
    RESULTS.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS / "summary.json"
    summary = {r["config"]: r for r in json.loads(summary_path.read_text())} if summary_path.exists() else {}
    for name in names:
        rows = run_config(name, items, args.limit)
        summary[name] = summarize(name, rows)
        (RESULTS / f"{name}.json").write_text(json.dumps(rows, indent=1))
        print(summary[name])
    summary_path.write_text(json.dumps(list(summary.values()), indent=1))
    lines = ["| config | line F1 | SKU acc | abstain | price err | $/RFQ | p50 s | errors |", "|---|---|---|---|---|---|---|---|"]
    for r in summary.values():
        lines.append(f"| {r['config']} | {r['line_f1']} | {r['sku_acc']} | {r['abstain_correct']} | {r['price_err']} "
                     f"| ${r['cost_per_rfq']} | {r['p50_latency_s']} | {r['errors']} |")
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
