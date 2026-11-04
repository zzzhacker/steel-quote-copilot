# Steel RFQ-to-Quote Copilot

A sales rep pastes a messy steel RFQ ("6 pcs 1/4 304 plt cut 24x24, certs required, ship Fri"). An agent extracts the lines, grounds every item against a **real ERP (Odoo 19)**, prices it deterministically, and, after a human approves, writes a **draft quotation** back into the ERP with an audit trail.

Built as a one-day prototype on synthetic inventory and public specs. The surcharges are real: the Cleveland-Cliffs stainless table for shipments Aug 30 – Oct 3, 2026.

## The principle
**The model reads and chooses. It never does math and never invents an ID.**

| The LLM does | Deterministic code does |
|---|---|
| Extract lines from the email | Normalize grades, fractions, mm, gauges (per material) into the ontology |
| Pick *from the ERP's candidate IDs*, or abstain | Retrieve candidates (BM25 + vector, RRF), validate against live stock |
| Turn a chat edit ("make line 2 3/16") into a patch | Weight, surcharge, cuts, certs, tier discounts, totals |

## Architecture
```mermaid
flowchart TB
    UI["Web UI (FastAPI + HTML/JS)"] <--> Agent
    Evals["Eval harness"] --> Agent
    subgraph Agent["LangGraph agent"]
        direction LR
        extract --> normalize --> resolve --> validate --> price --> review{{"human review<br/>interrupt()"}}
        review -- approve --> draft["write draft"]
        review -- chat edit --> revise --> normalize
    end
    Agent -. traces .-> LF["Langfuse"]
    Agent --> LLM["Chat model<br/>Gemini / Claude"]
    Agent -. imports .-> Domain["domain/<br/>ontology, pricing"]
    Agent -- "MCP (streamable HTTP)" --> ER
    subgraph ER["ER MCP server"]
        direction LR
        ri["resolve_item"] ~~~ rc["resolve_customer"] ~~~ fs["find_stock"] ~~~ ve["validate_entities"]
    end
    ER -- "live state" --> Odoo[("Odoo 19<br/>JSON-2 API")]
    ER -- "BM25 + vector, RRF" --> PG[("Postgres + pgvector")]
    draft -- "approved draft quote" --> Odoo
```
- **ER MCP server** (`er_server/`): `resolve_item`, `resolve_customer`, `find_stock` (remnant-first, live), `validate_entities` (live, catches stale/hallucinated IDs). The same grounding pattern I run in production at Blue Yonder, rebuilt for steel.
- **Agent** (`agent/`): LangGraph with a human-review `interrupt()`. Edits re-run only the changed lines. A line that fails validation gets one retry with relaxed search, then asks for clarification.
- **Web UI** (`web/`): FastAPI streams each LangGraph node over server-sent events, so the pipeline is visible while it runs. The page is plain HTML, CSS and JS with no build step: pick a sample or paste an email, watch the steps, expand a line for its checks, heat numbers and price breakdown, refine by chat, approve into Odoo.
- **Domain** (`domain/`): ontology (grade aliases and UNS, fractions, gauge tables per material, densities, weights) and pricing. Pure functions, no I/O, unit-tested.
- **Odoo**: products, lots (heat numbers), remnants, customers with tiers. Custom `x_` fields were created over the API, so no addon module is needed. The agent only writes **draft** quotes, with an audit note in the chatter. A person confirms in Odoo.

## Run it
```bash
cp .env.example .env            # add GOOGLE_API_KEY and/or OPENAI_API_KEY (+ LANGFUSE_* for tracing/evals)
docker compose up -d            # Odoo 19 + Postgres/pgvector
uv sync
uv run python -m seed.seed_odoo                     # ~250 SKUs, ~730 lots, 20 customers
uv run python -m steelquote.er_server.server &      # MCP server on :8765
uv run python -m steelquote.web.api                 # UI + API on http://localhost:8501
uv run python -m steelquote.agent.cli evals/hero.txt --approve   # or from the terminal
uv run pytest
```
The Odoo database `steel`, its API key, and the admin test login are local-dev only.

## Evals
`evals/rfqs.jsonl`: 40 synthetic RFQs built **gold-first** (sample real in-stock items, write the answer, then render a messy email): clean, aliases and units, ambiguous (gauge vs fraction, grade missing), out-of-stock, missing dimensions, chatty multi-line.

`uv run python -m evals.run --all` compares configurations (each also a Langfuse experiment run when keys are set): **ungrounded** (the model writes the SKU itself), retrieval mode (keyword / vector / hybrid), validate-retry off, and model size/routing across Gemini and OpenAI. Metrics: line F1, SKU accuracy, correct abstention, quote $ error, $/RFQ, p50 latency. The default model is `gemini-3.5-flash-lite` on the free tier; OpenAI models are selected by name (`gpt-*`).

### Results (40 synthetic RFQs, one run per config)
Grounding and retrieval ablations, all on `gemini-3.5-flash-lite` for every step:

| config | line F1 | SKU acc | abstain | price err | $/RFQ* | p50 s | errors |
|---|---|---|---|---|---|---|---|
| lite | 0.977 | 0.952 | 1.0 | 0.006 | $0.00067 | 15.5 | 0 |
| ungrounded | 0.5 | 0.387 | 0.75 | 0.093 | $0.00069 | 30.1 | 0 |
| keyword | 0.927 | 0.881 | 1.0 | 0.077 | $0.0006 | 15.0 | 1 |
| vector | 0.947 | 0.905 | 1.0 | 0.041 | $0.00061 | 15.8 | 0 |
| no_retry | 0.957 | 0.917 | 1.0 | 0.018 | $0.00056 | 15.3 | 0 |

Model size and routing, hybrid retrieval and validate-retry on. Extract and disambiguate are the two LLM steps:

| config | extract / disambiguate | line F1 | SKU acc | price err | $/RFQ* | p50 s |
|---|---|---|---|---|---|---|
| luna | gpt-6-luna / gpt-6-luna | 0.949 | 0.917 | 0.039 | $0.00019 | 3.0 |
| terra | gpt-6.1-terra / gpt-6.1-terra | 0.922 | 0.857 | 0.055 | $0.00147 | 2.9 |
| sol | gpt-6.1-sol / gpt-6.1-sol | 0.992 | 0.988 | 0.000 | $0.00274 | 2.9 |
| routed | luna / terra | 0.932 | 0.881 | 0.053 | $0.00023 | 2.8 |
| routed_sol | luna / sol | 0.944 | 0.905 | 0.040 | $0.00029 | 2.8 |
| routed_rev | sol / luna | 0.992 | 0.988 | 0.000 | $0.00266 | 3.1 |
| lite (Gemini) | flash-lite / flash-lite | 0.977 | 0.952 | 0.006 | $0.00067 | 15.5 |

\* List-price equivalent. Gemini ran on the free tier (actual spend $0, latency inflated by a deliberate rate limiter), and its rows were measured before the round-bar fix below, so compare them loosely. OpenAI prices come from third-party trackers, not OpenAI's pricing page; sources disagree on Sol, so treat the cost column as relative. `gpt-6.1-luna` does not exist for this key; `gpt-6-luna` is the cheap tier.

What the numbers say:
- **Grounding is not optional.** When the model writes the SKU itself, SKU accuracy falls from 0.95 to 0.39 and line F1 from 0.98 to 0.50. The ERP candidate list and live validation are what make the model usable.
- **Hybrid retrieval beats either half:** line F1 0.977 vs vector 0.947 vs keyword 0.927. Exact codes (S30400) favor keyword, paraphrase ("18-8 sheet") favors vectors.
- **The validate-and-retry loop** adds about 2 points (0.957 → 0.977). With 40 items that is roughly one line, so treat it as directional.
- **Quality is limited by extraction, not by disambiguation.** The strong model reading the email (`routed_rev`, sol / luna) scores exactly the same as the strong model everywhere (0.992), while the cheap model reading it and the strong one choosing (`routed_sol`, luna / sol) gains nothing over luna alone (0.944 vs 0.949). Once the ERP supplies candidates, the choice step is easy; reading a messy email is the hard part.
- **Routing here saves little money.** The email is most of the tokens, and the extraction call is the expensive one, so using the cheap model for the choice step costs almost the same as all-sol ($0.00266 vs $0.00274). The finding is where to spend, not a large saving: put the stronger model on extraction.
- **Model size is not monotonic.** Terra, the middle tier, scored below the cheapest tier (0.922 vs 0.949) at 8x the cost. Pick by measurement, not by tier name.
- Abstention scores 1.0, but that metric is lenient: it checks that at least as many lines were held back as should be, not that they are the same lines. The items are also generated, so real emails will be harder. Each config is a single run at 40 items, so differences under about 3 points are noise.

An early run of these configs had a bug: `rd bar` was not recognized as round bar, so those lines were priced $0. It is fixed and tested, and the OpenAI rows above were re-run after the fix.

Run a config with `uv run python -m evals.run --config terra`. Run configs in separate processes: several in one process reuse a closed event loop and error on the last items.

## Taking this to a real client ERP
1. Replace the seed with a read-only sync of their item master, lots and customer part-number cross-references; keep `validate_entities` hitting live state.
2. Price book freshness: surcharges and base prices load from the ERP or a dated table, not code.
3. Ingest from the quoting inbox (email, PDF); keep the human approve step until the override rate is low enough to earn autonomy.
4. Per-client config: the ontology, gauge conventions, tiers, and units live in data files; the graph and eval harness carry over unchanged.

## Roadmap
MTR (mill test report) extraction and matching to heat numbers · LoRA-tuned small extractor trained on approved corrections exported from Langfuse (decide with the same experiments) · multi-warehouse allocation · tax and freight from the ERP · email/PDF ingestion.

## Honest limits
One run per config on 40 items. Synthetic inventory, illustrative base prices (only the stainless surcharges are published figures), one warehouse, no tax, no PDF input.
