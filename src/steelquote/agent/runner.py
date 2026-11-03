"""Entry points shared by the CLI, the Streamlit app and the evals."""
import os
import uuid
from typing import Any

from dotenv import load_dotenv
from langgraph.types import Command

from steelquote.adapters.odoo import Odoo
from steelquote.agent.graph import build_graph
from steelquote.agent.nodes import Agent
from steelquote.config import get_settings

load_dotenv()


def make_graph(checkpointer=None):
    s = get_settings()
    return build_graph(Agent(s, Odoo(s)), checkpointer)


def tracing() -> list:
    """Langfuse callback handler when keys are configured, else no tracing."""
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return []
    from langfuse.langchain import CallbackHandler
    return [CallbackHandler()]


def run_config(thread_id: str | None = None, **flags: Any) -> dict:
    return {"configurable": {"thread_id": thread_id or uuid.uuid4().hex, **flags}, "callbacks": tracing()}


async def start(graph, email: str, config: dict) -> dict:
    return await graph.ainvoke({"email": email}, config)


async def resume(graph, decision: dict, config: dict) -> dict:
    return await graph.ainvoke(Command(resume=decision), config)
