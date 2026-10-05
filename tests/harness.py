"""Test harness: a scripted fake LLM + a tiny interpreter for the workflow tables.

The interpreter walks app.workflow.NODES/EDGES/CONDITIONAL exactly like LangGraph would, merging
`history` with list concatenation (the operator.add reducer) and keeping per-session state (the
checkpointer). This lets the whole workflow run in CI without LangGraph or an API key.
"""
from app import llm, workflow
from app.state import AgentState

_REDUCED = {"history"}


class FakeLLM:
    """Replace llm.call_json / llm.call_text. `script` maps a task to a callable or a value."""

    def __init__(self):
        self.intents = []      # queue of intent dicts
        self.sqls = []         # queue of generation dicts
        self.calls = []        # (kind, system[:40])

    def call_json(self, system, human):
        self.calls.append(("json", system[:30]))
        queue = self.intents if "intent classifier" in system else self.sqls
        item = queue.pop(0)
        self.last_human = human
        return item

    def call_text(self, system, human):
        self.calls.append(("text", system[:30]))
        return "Plain-English explanation."


def install(fake):
    llm.call_json, llm.call_text = fake.call_json, fake.call_text


class Session:
    def __init__(self):
        self.state = {}

    def turn(self, message, dialect="sqlite", execute=True):
        state = dict(self.state)
        state.update(user_input=message, dialect=dialect, execute=execute)
        node, path = workflow.ENTRY, []
        while True:
            path.append(node)
            update = workflow.NODES[node](state) or {}
            for k, v in update.items():
                state[k] = (state.get(k, []) + v) if k in _REDUCED else v
            if node == workflow.FINISH:
                break
            if node in workflow.CONDITIONAL:
                node = workflow.CONDITIONAL[node][0](state)
            else:
                node = next(d for s, d in workflow.EDGES if s == node)
        self.state = state
        state["_path"] = path
        return state["response"], path
