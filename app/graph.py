"""Compile the workflow into a LangGraph graph with per-session memory, and run turns."""
from __future__ import annotations

import uuid
from functools import lru_cache

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from . import workflow
from .state import AgentState


@lru_cache(maxsize=1)
def get_graph():
    g = StateGraph(AgentState)
    for name, fn in workflow.NODES.items():
        g.add_node(name, fn)
    g.set_entry_point(workflow.ENTRY)
    for src, dst in workflow.EDGES:
        g.add_edge(src, dst)
    for src, (router, targets) in workflow.CONDITIONAL.items():
        g.add_conditional_edges(src, router, {t: t for t in targets})
    g.add_edge(workflow.FINISH, END)
    # MemorySaver keeps one checkpoint per thread_id = the conversation memory.
    return g.compile(checkpointer=MemorySaver())


def new_session_id() -> str:
    return uuid.uuid4().hex


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def run_turn(session_id: str, message: str, dialect: str = "sqlite", execute: bool = True) -> dict:
    out = get_graph().invoke(
        {"user_input": message, "dialect": dialect, "execute": execute}, _config(session_id))
    return out["response"]


def stream_turn(session_id: str, message: str, dialect: str = "sqlite", execute: bool = True):
    """Yield ("step", node_name) for each finished node, then ("final", response)."""
    final = None
    for chunk in get_graph().stream(
        {"user_input": message, "dialect": dialect, "execute": execute},
        _config(session_id), stream_mode="updates",
    ):
        for node, update in chunk.items():
            yield "step", node
            if node == "respond" and update and "response" in update:
                final = update["response"]
    yield "final", final


def get_history(session_id: str) -> list:
    snap = get_graph().get_state(_config(session_id))
    return (snap.values or {}).get("history", []) if snap else []
