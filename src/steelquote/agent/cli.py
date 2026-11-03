"""python -m steelquote.agent.cli evals/hero.txt [--approve] [--edit "make line 2 3/16"]"""
import argparse
import asyncio
from pathlib import Path

from steelquote.agent import runner


def show(state: dict) -> None:
    q = state["quote"]
    print(f"\nCustomer: {q.customer_name} (tier {q.tier})")
    for pl in q.lines:
        r = pl.line
        sku = r.candidate.default_code if r.candidate else "-"
        print(f"  {r.norm.line_no}. {r.norm.raw_text[:44]:44} -> {sku:26} [{r.status}] ${pl.total}")
        for c in r.checks:
            print(f"       {'ok  ' if c.ok else 'FAIL'} {c.name}: {c.detail}")
    print(f"Total: ${q.total}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("email_file")
    ap.add_argument("--edit", action="append", default=[])
    ap.add_argument("--approve", action="store_true")
    args = ap.parse_args()
    graph, config = runner.make_graph(), runner.run_config()
    state = await runner.start(graph, Path(args.email_file).read_text(), config)
    show(state)
    for text in args.edit:
        print(f"\n> {text}")
        state = await runner.resume(graph, {"action": "edit", "text": text}, config)
        show(state)
    if args.approve:
        state = await runner.resume(graph, {"action": "approve"}, config)
        print("\nDraft:", state["draft"])


if __name__ == "__main__":
    asyncio.run(main())
