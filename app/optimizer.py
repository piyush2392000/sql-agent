"""Deterministic SQL lint + index recommendations (heuristic, stdlib only).

Runs after validation. The LLM adds its own optimisation notes when the user asks to optimise a
query; this module guarantees a baseline of explainable, reproducible suggestions.
"""
from __future__ import annotations

import re

from . import db
from .validator import _mask

_CMP = r"(?:=|<>|!=|>=|<=|>|<|\bnot\s+like\b|\blike\b|\bin\b|\bbetween\b|\bis\b)"


def _alias_map(masked: str, schema: dict) -> dict[str, str]:
    real = {t.lower(): t for t in schema}
    amap: dict[str, str] = {}
    for m in re.finditer(r"\b(?:from|join)\s+([A-Za-z_]\w*)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?", masked, re.I):
        table = real.get(m.group(1).lower())
        if not table:
            continue
        amap[table.lower()] = table
        alias = m.group(2)
        if alias and alias.lower() not in {"on", "where", "join", "left", "right", "inner", "cross", "group",
                                           "order", "limit", "using", "natural", "full", "union", "having"}:
            amap[alias.lower()] = table
    return amap


def _filter_columns(masked: str, schema: dict) -> dict[str, set[str]]:
    """table -> columns used in WHERE / JOIN ... ON comparisons."""
    amap = _alias_map(masked, schema)
    cols_by_table = {t: {c["name"].lower(): c["name"] for c in info["columns"]} for t, info in schema.items()}
    used: dict[str, set[str]] = {}

    def add(table: str, col: str) -> None:
        real = cols_by_table[table].get(col.lower())
        if real:
            used.setdefault(table, set()).add(real)

    for m in re.finditer(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*" + _CMP, masked, re.I):
        table = amap.get(m.group(1).lower())
        if table:
            add(table, m.group(2))
    # right-hand side qualified columns (a.x = b.y)
    for m in re.finditer(r"=\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)", masked):
        table = amap.get(m.group(1).lower())
        if table:
            add(table, m.group(2))
    # bare columns -> only if they exist in exactly one referenced table
    referenced = {t for t in amap.values()}
    for m in re.finditer(r"(?<![\.\w])([A-Za-z_]\w*)\s*" + _CMP, masked, re.I):
        owners = [t for t in referenced if m.group(1).lower() in cols_by_table[t]]
        if len(owners) == 1:
            add(owners[0], m.group(1))
    return used


def lint(sql: str, schema: dict | None = None) -> dict:
    """Return {"suggestions": [...], "indexes": [...], "plan": {...}} for a *validated* SELECT."""
    schema = schema or db.get_schema()
    masked = _mask(sql)
    low = re.sub(r"\s+", " ", masked.lower())
    suggestions: list[str] = []

    if re.search(r"select\s+(distinct\s+)?\*", low) or re.search(r"\.\*", low):
        suggestions.append("Select only the columns you need instead of '*' to reduce I/O.")
    if re.search(r"like\s+'?%", re.sub(r"''", "'%'", sql.lower())) or re.search(r"like\s+'%", sql.lower()):
        suggestions.append("A LIKE pattern with a leading '%' cannot use an index; anchor the pattern if possible.")
    if re.search(r"where[^;]*\b(strftime|date|substr|lower|upper|cast|coalesce|ifnull|year|month)\s*\(\s*[\w\.]+", low):
        suggestions.append("A function wraps a column in WHERE, which prevents index use; compare the raw column "
                           "against a range instead (e.g. HireDate >= '2024-01-01').")
    if re.search(r"not in\s*\(\s*select", low):
        suggestions.append("Prefer NOT EXISTS over NOT IN (subquery): it handles NULLs correctly and is usually faster.")
    if " order by " in f" {low} " and " limit " not in f" {low} " and "group by" not in low:
        suggestions.append("ORDER BY without LIMIT sorts the whole result; add LIMIT if you only need the top rows.")
    if re.search(r"select distinct", low) and "group by" in low:
        suggestions.append("DISTINCT together with GROUP BY is usually redundant.")
    if low.count(" join ") >= 3:
        suggestions.append("The query joins several tables; confirm each join contributes columns or filters.")

    try:
        plan = db.explain_plan(sql)
    except Exception:
        plan = {"steps": [], "full_scans": 0, "temp_structures": 0, "estimated_cost": "unknown"}

    indexes: list[dict] = []
    scanned = {m.group(1).lower() for s in plan["steps"] for m in [re.match(r"SCAN (?:TABLE )?(\w+)", s)] if m}
    scanned_tables = {t for t in schema if t.lower() in scanned or any(t.lower() == x for x in scanned)}
    amap = _alias_map(masked, schema)
    for alias, table in amap.items():  # resolve plan aliases back to real tables
        if alias in scanned:
            scanned_tables.add(table)
    unique_cols = {t: {c["name"] for c in info["columns"] if c["pk"]} for t, info in schema.items()}
    for table, cols in _filter_columns(masked, schema).items():
        if table not in scanned_tables:
            continue
        cols = sorted(c for c in cols if c not in unique_cols[table])
        if not cols or schema[table]["row_count"] < 1:
            continue
        name = f"idx_{table.lower()}_{'_'.join(c.lower() for c in cols)}"
        indexes.append({
            "table": table, "columns": cols,
            "ddl": f"CREATE INDEX {name} ON {table}({', '.join(cols)});",
            "reason": f"{table} is scanned in full and filtered/joined on {', '.join(cols)}.",
        })
    return {"suggestions": suggestions, "indexes": indexes, "plan": plan}
