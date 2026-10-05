"""Deterministic input guardrails that run BEFORE any LLM call.

Layer 1 of the guardrail stack:
  1. guard.py        - regex checks on the raw user message (this file)
  2. LLM intent      - scope classification (prompts.INTENT_SYSTEM)
  3. validator.py    - every SQL string is validated before it is returned
  4. db.py           - read-only connection + SQLite authorizer at execution time
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_INPUT_CHARS = 4000

# ---- prompt-injection / jailbreak patterns ------------------------------------------------
_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier|system) (instructions?|prompts?|rules?)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|system) (instructions?|prompts?|rules?)",
    r"forget (all |your |the )?(previous |prior )?(instructions?|rules?|guidelines?)",
    r"(reveal|show|print|repeat|output|leak|display) (me )?(your|the) (system |hidden |initial |original )?(prompt|instructions?|rules)",
    r"what (is|are) your (system |hidden |initial )?(prompt|instructions)",
    r"you are now\b",
    r"\bact as (if you (are|were)|a|an)\b.*\b(no|without) (rules|restrictions|limits)",
    r"\b(dan|developer|jailbreak|god) mode\b",
    r"pretend (that )?(you|to) (are|be|have)\b",
    r"override (your|the) (safety|guard|rules|instructions)",
    r"</?\s*(system|assistant|instructions?)\s*>",
    r"\bbypass (the )?(validation|guardrails?|filters?|safety)",
]
_INJECTION_RE = [re.compile(p, re.I | re.S) for p in _INJECTION]

# ---- destructive SQL appearing anywhere in the message --------------------------------------
_DESTRUCTIVE_SQL_RE = re.compile(
    r"\b(delete\s+from|drop\s+(table|database|schema|index|view|column)|truncate\s+(table\s+)?\w+|"
    r"alter\s+table|insert\s+into|update\s+[\w\"`\[\]\.]+\s+set|replace\s+into|"
    r"create\s+(table|index|view|trigger|database)|attach\s+database|pragma\s+\w+\s*=)\b",
    re.I,
)

# ---- destructive intent phrased in natural language (anchored to the start of the message) --
_NL_STRONG = re.compile(
    r"^\s*(please\s+|pls\s+|can you\s+|could you\s+|would you\s+|i (want|need) (you )?to\s+|just\s+)?"
    r"(delete|drop|truncate|erase|wipe|destroy|insert|alter|purge)\b",
    re.I,
)
_DATA_NOUNS = (
    r"(all|every|the|my|those|these|that|this|any)?\s*"
    r"(employees?|customers?|orders?|products?|departments?|rows?|records?|tables?|data|database|"
    r"salar(y|ies)|prices?|stock|entries|items?|users?)"
)
_NL_WEAK = re.compile(
    r"^\s*(please\s+|pls\s+|can you\s+|could you\s+|would you\s+|i (want|need) (you )?to\s+)?"
    r"(remove|update|modify|change|set|add|rename|clear|reset)\s+" + _DATA_NOUNS,
    re.I,
)


@dataclass
class GuardResult:
    kind: str  # "ok" | "injection" | "destructive" | "empty" | "too_long"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def check_input(text: str) -> GuardResult:
    text = (text or "").strip()
    if not text:
        return GuardResult("empty", "Empty message")
    if len(text) > MAX_INPUT_CHARS:
        return GuardResult("too_long", f"Message longer than {MAX_INPUT_CHARS} characters")
    for rx in _INJECTION_RE:
        if rx.search(text):
            return GuardResult("injection", "Prompt-injection pattern detected")
    if _DESTRUCTIVE_SQL_RE.search(text) or _NL_STRONG.search(text) or _NL_WEAK.search(text):
        return GuardResult("destructive", "Request would modify data or schema")
    return GuardResult("ok")


def fence_user_text(text: str) -> str:
    """Wrap untrusted text so the LLM treats it as data, and neutralise fake delimiters."""
    cleaned = text.replace("<<<", "«").replace(">>>", "»")
    return f"<<<USER_MESSAGE\n{cleaned}\nUSER_MESSAGE>>>"
