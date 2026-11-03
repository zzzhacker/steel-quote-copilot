from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from steelquote.agent.nodes import Agent, State


def build_graph(agent: Agent, checkpointer=None):
    g = StateGraph(State)
    for name in ("extract", "normalize", "resolve", "validate", "price", "human_review", "revise", "write_draft"):
        g.add_node(name, getattr(agent, name))
    g.add_edge(START, "extract")
    g.add_edge("extract", "normalize")
    g.add_edge("normalize", "resolve")
    g.add_edge("resolve", "validate")
    # a failed line gets one more pass through resolve with relaxed search; validate empties `todo` after that
    g.add_conditional_edges("validate", lambda s: "resolve" if s["todo"] else "price", ["resolve", "price"])
    g.add_edge("price", "human_review")
    g.add_conditional_edges(
        "human_review", lambda s: "write_draft" if s["decision"]["action"] == "approve" else "revise",
        ["write_draft", "revise"])
    g.add_edge("revise", "normalize")
    g.add_edge("write_draft", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
