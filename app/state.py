"""Graph state. Kept free of framework imports so it can be reused by tests."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

MAX_ATTEMPTS = 3  # 1 generation + up to 2 repair rounds driven by validator feedback


class AgentState(TypedDict, total=False):
    # ---- request (set by the caller each turn)
    user_input: str
    dialect: str          # sqlite | postgres | mysql
    execute: bool

    # ---- conversation memory (persisted across turns by the LangGraph checkpointer)
    history: Annotated[list, operator.add]   # [{"role", "content", "sql"?}]
    last_sql: str                            # last SQL successfully returned to the user

    # ---- per-turn working state (reset by the guard node)
    intent: str
    in_scope: bool
    user_sql: str | None
    clarification: str | None
    relevant_tables: list
    schema_text: str
    attempts: int
    sql: str | None
    sqlite_sql: str
    notes: list
    validation: dict
    warnings: list
    optimization: dict
    result: dict | None
    explanation: str
    status: str           # ok | refused | clarify | error
    message: str
    response: dict


def per_turn_defaults() -> dict[str, Any]:
    return {
        "intent": "", "in_scope": True, "user_sql": None, "clarification": None,
        "relevant_tables": [], "schema_text": "", "attempts": 0, "sql": None, "sqlite_sql": "",
        "notes": [], "validation": {}, "warnings": [], "optimization": {}, "result": None,
        "explanation": "", "status": "", "message": "", "response": {},
    }
