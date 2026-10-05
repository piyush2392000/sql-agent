"""LangGraph nodes. Each node is a plain function: state -> partial state update.

Nodes never import langgraph, so the whole workflow is unit-testable with a fake LLM.
"""
from __future__ import annotations

import re

from . import db, guard, llm, optimizer, prompts, validator
from .state import MAX_ATTEMPTS, per_turn_defaults

DIALECT_NAMES = {"sqlite": "SQLite", "postgres": "PostgreSQL", "mysql": "MySQL"}
SQL_INTENTS = {"generate", "followup", "optimize", "debug", "explain"}


def _dialect(state) -> str:
    d = state.get("dialect") or "sqlite"
    return d if d in DIALECT_NAMES else "sqlite"


def _history_text(state, n: int = 6) -> str:
    items = state.get("history") or []
    lines = []
    for h in items[-n:]:
        who = "User" if h["role"] == "user" else "Assistant"
        lines.append(f"{who}: {h['content']}" + (f"\n  (SQL: {h['sql']})" if h.get("sql") else ""))
    return "\n".join(lines) or "(none)"


# ------------------------------------------------------------------------------ 1. guard
def guard_node(state):
    """Reset per-turn state and run the deterministic input guardrails."""
    update = per_turn_defaults()
    update["dialect"] = _dialect(state)
    result = guard.check_input(state.get("user_input", ""))
    if result.ok:
        return update
    messages = {
        "injection": prompts.INJECTION_MESSAGE,
        "destructive": prompts.DESTRUCTIVE_MESSAGE,
        "empty": "Please type a question about the database.",
        "too_long": "That message is too long. Please shorten your request.",
    }
    update.update(in_scope=False, status="refused", intent=result.kind, message=messages[result.kind])
    return update


# ------------------------------------------------------------------------------ 2. intent
def intent_node(state):
    schema = db.get_schema()
    system = prompts.INTENT_SYSTEM.format(schema=db.schema_to_text(schema))
    human = prompts.INTENT_HUMAN.format(
        last_sql=state.get("last_sql") or "(none)",
        history=_history_text(state),
        message=guard.fence_user_text(state["user_input"]),
    )
    data = llm.call_json(system, human)
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in SQL_INTENTS | {"info", "clarify", "destructive", "out_of_scope"}:
        intent = "clarify" if not data else "generate"
    if intent == "followup" and not state.get("last_sql"):
        intent = "generate"
    user_sql = data.get("user_sql")
    user_sql = user_sql.strip() if isinstance(user_sql, str) and user_sql.strip() else None
    if intent in {"optimize", "debug", "explain"} and not user_sql:
        intent, data["clarification"] = "clarify", "Please paste the SQL query you want me to look at."

    update = {"intent": intent, "user_sql": user_sql, "clarification": data.get("clarification")}
    if intent == "out_of_scope":
        update.update(in_scope=False, status="refused", message=prompts.REJECT_MESSAGE)
    elif intent == "destructive":
        update.update(in_scope=False, status="refused", message=prompts.DESTRUCTIVE_MESSAGE)
    elif intent == "clarify":
        update.update(status="clarify", message=data.get("clarification") or
                      "Could you tell me a bit more about what you'd like to see?")
    return update


# ------------------------------------------------------------------------------ 3. schema retrieval
_WORD = re.compile(r"[a-z0-9]+")


def _stem(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def schema_node(state):
    """Select relevant tables (keyword match + FK neighbours). Small schemas are passed whole."""
    schema = db.get_schema()
    text_src = " ".join([state.get("user_input", ""), state.get("user_sql") or "", state.get("last_sql") or ""])
    words = {_stem(w) for w in _WORD.findall(text_src.lower())}
    scores = {}
    for t, info in schema.items():
        names = {_stem(t.lower())} | {_stem(c["name"].lower()) for c in info["columns"]}
        values = {_stem(v.lower()) for c in info["columns"] for v in c.get("values", [])}
        scores[t] = 3 * (_stem(t.lower()) in words) + len(names & words) + len(values & words)
    selected = {t for t, s in scores.items() if s > 0}
    for t in list(selected):  # pull in FK neighbours so joins are possible
        for fk in schema[t]["foreign_keys"]:
            selected.add(fk["table"])
        for other, info in schema.items():
            if any(fk["table"] == t for fk in info["foreign_keys"]) and scores[other] > 0:
                selected.add(other)
    if len(schema) <= 8 or not selected:
        selected = set(schema)
    tables = sorted(selected)
    return {"relevant_tables": tables, "schema_text": db.schema_to_text(schema, tables)}


# ------------------------------------------------------------------------------ 4. generation
def generate_node(state):
    attempts = state.get("attempts", 0) + 1
    intent = state["intent"]
    if intent == "explain" and state.get("user_sql"):  # nothing to generate
        return {"attempts": attempts, "sql": validator.clean_sql(state["user_sql"]), "notes": []}

    dialect = _dialect(state)
    repair = ""
    if state.get("validation", {}).get("errors") and state.get("sql"):
        repair = prompts.REPAIR_BLOCK.format(sql=state["sql"], errors="; ".join(state["validation"]["errors"]))
    user_sql_errors = ""
    if state.get("user_sql") and intent in {"optimize", "debug"}:
        v = validator.validate(state["user_sql"], dialect)
        user_sql_errors = "; ".join(v.errors + v.warnings) or "(none)"

    system = prompts.GENERATE_SYSTEM.format(dialect_name=DIALECT_NAMES[dialect], schema=state["schema_text"])
    human = prompts.GENERATE_HUMAN.format(
        mode_instruction=prompts.GENERATE_MODES[intent],
        last_sql=state.get("last_sql") or "(none)",
        history=_history_text(state),
        user_sql=state.get("user_sql") or "(none)",
        user_sql_errors=user_sql_errors or "(none)",
        repair_block=repair,
        message=guard.fence_user_text(state["user_input"]),
    )
    data = llm.call_json(system, human)
    sql = data.get("sql") if isinstance(data.get("sql"), str) else None
    notes = [str(n) for n in data.get("notes", []) if n] if isinstance(data.get("notes"), list) else []
    update = {"attempts": attempts, "sql": sql, "notes": notes}
    if not sql:
        reason = data.get("cannot_answer") or "I couldn't build a query for that from the available tables."
        update.update(status="clarify", message=str(reason))
    return update


# ------------------------------------------------------------------------------ 5. validation
def validate_node(state):
    result = validator.validate(state["sql"], _dialect(state))
    update = {
        "validation": result.as_dict(), "warnings": result.warnings,
        "sql": result.sql, "sqlite_sql": result.sqlite_sql,
    }
    if not result.valid and state.get("attempts", 0) >= MAX_ATTEMPTS:
        update.update(status="error", message=(
            "I couldn't produce a valid query for that request. Last problem: " + "; ".join(result.errors)))
    return update


# ------------------------------------------------------------------------------ 6. optimisation lint
def optimize_node(state):
    return {"optimization": optimizer.lint(state["sqlite_sql"])}


# ------------------------------------------------------------------------------ 7. execution
def execute_node(state):
    if not state.get("execute"):
        return {}
    try:
        return {"result": db.execute_readonly(state["sqlite_sql"])}
    except Exception as exc:
        return {"result": {"error": f"Query execution failed: {exc}"}}


# ------------------------------------------------------------------------------ 8. explanation
def explain_node(state):
    dialect = _dialect(state)
    human = prompts.EXPLAIN_HUMAN.format(
        dialect_name=DIALECT_NAMES[dialect], sql=state["sql"],
        notes="\n".join(f"- {n}" for n in state.get("notes", [])) or "(none)")
    try:
        text = llm.call_text(prompts.EXPLAIN_SYSTEM, human)
    except Exception:
        text = "This query was validated against the schema, but an explanation could not be generated."
    return {"explanation": text}


# ------------------------------------------------------------------------------ info / reject
def info_node(state):
    schema = db.get_schema()
    system = prompts.INFO_SYSTEM.format(schema=db.schema_to_text(schema))
    human = f"RECENT CONVERSATION:\n{_history_text(state)}\n\nQUESTION:\n{guard.fence_user_text(state['user_input'])}"
    return {"status": "ok", "message": llm.call_text(system, human)}


# ------------------------------------------------------------------------------ 9. respond
def respond_node(state):
    status = state.get("status") or "ok"
    sql_ok = bool(state.get("sql")) and state.get("validation", {}).get("valid") and status not in {"error", "refused"}
    response = {
        "status": status,
        "intent": state.get("intent"),
        "dialect": _dialect(state),
        "message": state.get("message") or "",
        "sql": state.get("sql") if sql_ok else None,
        "explanation": state.get("explanation") or "",
        "notes": state.get("notes") or [],
        "warnings": state.get("warnings") or [],
        "validation": state.get("validation") or {},
        "optimization": state.get("optimization") or {},
        "result": state.get("result"),
        "attempts": state.get("attempts", 0),
    }
    if sql_ok and status != "error":
        response["status"] = "ok"
    reply = response["explanation"] or response["message"] or ""
    history = [{"role": "user", "content": state.get("user_input", "")},
               {"role": "assistant", "content": reply[:400], "sql": response["sql"]}]
    update = {"response": response, "history": history, "status": response["status"]}
    if sql_ok:
        update["last_sql"] = state["sql"]
    return update


# ------------------------------------------------------------------------------ routers
def route_after_guard(state) -> str:
    return "respond" if state.get("status") == "refused" else "intent"


def route_after_intent(state) -> str:
    if state.get("status") in {"refused", "clarify"}:
        return "respond"
    return "info" if state["intent"] == "info" else "schema"


def route_after_generate(state) -> str:
    return "respond" if state.get("status") == "clarify" else "validate"


def route_after_validate(state) -> str:
    v = state.get("validation", {})
    if v.get("valid"):
        return "optimize"
    return "respond" if state.get("attempts", 0) >= MAX_ATTEMPTS else "generate"
