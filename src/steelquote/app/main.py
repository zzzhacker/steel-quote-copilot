"""streamlit run src/steelquote/app/main.py"""
import asyncio
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from langgraph.checkpoint.memory import InMemorySaver

from steelquote.agent import runner

RESULTS = Path("evals/results/summary.json")
ICON = {"ok": "🟢", "short": "🟠", "needs_clarification": "🔴"}

st.set_page_config(page_title="Steel RFQ Copilot", layout="wide")
ss = st.session_state
ss.setdefault("config", None)
ss.setdefault("state", None)
ss.setdefault("chat", [])


@st.cache_resource
def graph():
    return runner.make_graph(InMemorySaver())


def advance(text: str) -> None:
    ss.chat.append(("user", text))
    with st.spinner("extract → normalize → resolve (MCP) → validate (live ERP) → price"):
        if ss.state is None:
            ss.config = runner.run_config()
            ss.state = asyncio.run(runner.start(graph(), text, ss.config))
        else:
            ss.state = asyncio.run(runner.resume(graph(), {"action": "edit", "text": text}, ss.config))
    ss.chat.append(("assistant", "Quote updated. Tell me what to change, or approve it on the right."))


def approve() -> None:
    with st.spinner("writing draft quotation to Odoo"):
        ss.state = asyncio.run(runner.resume(graph(), {"action": "approve"}, ss.config))


def render_line(pl) -> None:
    r, n = pl.line, pl.line.norm
    with st.container(border=True):
        st.markdown(f"{ICON[r.status]} **{n.line_no}. {n.raw_text}**")
        if r.candidate:
            st.caption(f"`{r.candidate.default_code}` · {pl.weight_lb:,.0f} lb · qty {n.qty}")
        for a in r.allocs:
            kind = f"remnant {a.w_in:g}x{a.l_in:g}" if a.is_remnant else "full sheet"
            st.markdown(f"- heat `{a.heat}` ×{a.qty} · {kind} · {a.location}")
        st.markdown("  ".join(f"{'✅' if c.ok else '❌'} {c.name}" for c in r.checks))
        for c in r.checks:
            if not c.ok:
                st.caption(f"{c.name}: {c.detail}")
        st.markdown(f"**${pl.total:,.2f}**")
        if pl.breakdown:
            with st.expander("price breakdown"):
                st.table(pd.DataFrame({"$": {k: float(v) for k, v in pl.breakdown.items()}}))


def quote_panel() -> None:
    state = ss.state
    if not state or "quote" not in state:
        st.info("Paste an RFQ email on the left to start.")
        return
    q = state["quote"]
    st.subheader(f"{q.customer_name or '⚠ customer not resolved'}" + (f" · tier {q.tier}" if q.tier else ""))
    st.caption(f"certs required: {q.certs_required} · ship by: {q.ship_by or '-'}")
    for pl in q.lines:
        render_line(pl)
    st.metric("Quote total", f"${q.total:,.2f}")
    draft = state.get("draft")
    if draft:
        st.success(f"Draft created: {draft['name']}")
        st.link_button("Open in Odoo", draft["url"])
    else:
        st.button("Approve & create draft in Odoo", type="primary", on_click=approve,
                  disabled=not q.partner_id)


def evals_tab() -> None:
    if not RESULTS.exists():
        st.info("No results yet. Run `uv run python -m evals.run --all`.")
        return
    df = pd.DataFrame(json.loads(RESULTS.read_text()))
    st.dataframe(df, hide_index=True, width="stretch")
    metric = st.selectbox("Chart metric", [c for c in df.columns if c != "config"], index=1)
    st.bar_chart(df.set_index("config")[metric])


copilot, evals = st.tabs(["Copilot", "Evals"])
with copilot:
    chat_col, quote_col = st.columns([2, 3])
    with chat_col:
        hero = Path("evals/hero.txt")
        if ss.state is None and hero.exists():
            st.button("Load sample RFQ", on_click=lambda: advance(hero.read_text()))
        for role, text in ss.chat:
            with st.chat_message(role):
                st.text(text) if role == "user" else st.write(text)
        prompt = st.chat_input("Paste an RFQ email, then chat edits like “make line 2 3/16”")
        if prompt:
            advance(prompt)
            st.rerun()
    with quote_col:
        quote_panel()
with evals:
    evals_tab()
