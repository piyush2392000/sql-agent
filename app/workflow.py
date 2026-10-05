"""Single source of truth for the workflow topology.

graph.py compiles this into a LangGraph StateGraph; tests/conftest.py walks the same tables with a
tiny interpreter so the full workflow can be tested without LangGraph installed.
"""
from . import nodes

NODES = {
    "guard": nodes.guard_node,
    "intent": nodes.intent_node,
    "info": nodes.info_node,
    "schema": nodes.schema_node,
    "generate": nodes.generate_node,
    "validate": nodes.validate_node,
    "optimize": nodes.optimize_node,
    "execute": nodes.execute_node,
    "explain": nodes.explain_node,
    "respond": nodes.respond_node,
}
ENTRY = "guard"
FINISH = "respond"

# unconditional edges
EDGES = [
    ("info", "respond"),
    ("schema", "generate"),
    ("optimize", "execute"),
    ("execute", "explain"),
    ("explain", "respond"),
]

# conditional edges: node -> (router, possible targets)
CONDITIONAL = {
    "guard": (nodes.route_after_guard, ["intent", "respond"]),
    "intent": (nodes.route_after_intent, ["schema", "info", "respond"]),
    "generate": (nodes.route_after_generate, ["validate", "respond"]),
    "validate": (nodes.route_after_validate, ["optimize", "generate", "respond"]),
}
