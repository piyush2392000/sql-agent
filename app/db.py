"""Database layer: creation + seeding, schema introspection, safe read-only execution.

Everything here uses only the standard library (sqlite3).
"""
from __future__ import annotations

import os
import random
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "db" / "schema.sql"
DB_PATH = Path(os.environ.get("SQL_AGENT_DB", ROOT / "data" / "sample.db"))

MAX_ROWS = int(os.environ.get("SQL_AGENT_MAX_ROWS", "200"))
QUERY_TIMEOUT_S = float(os.environ.get("SQL_AGENT_QUERY_TIMEOUT", "5"))

# SQLite authorizer action codes that a read-only SELECT may trigger.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def read_only_authorizer(action, arg1, arg2, db_name, trigger):
    """Deny anything that is not a plain read. Defence in depth on top of the validator."""
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


# --------------------------------------------------------------------------- setup
_STATES = ["California", "Texas", "New York", "Florida", "Washington", "Illinois", "Ohio", "Colorado"]
_CITIES = {
    "California": ["Los Angeles", "San Francisco", "San Diego"],
    "Texas": ["Austin", "Dallas", "Houston"],
    "New York": ["New York", "Buffalo"],
    "Florida": ["Miami", "Orlando"],
    "Washington": ["Seattle", "Spokane"],
    "Illinois": ["Chicago", "Springfield"],
    "Ohio": ["Columbus", "Cleveland"],
    "Colorado": ["Denver", "Boulder"],
}
_FIRST = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Karen", "Leo",
          "Mia", "Noah", "Olivia", "Paul", "Quinn", "Ruth", "Sam", "Tina", "Uma", "Victor", "Wendy", "Xavier"]
_LAST = ["Smith", "Johnson", "Brown", "Davis", "Miller", "Wilson", "Moore", "Taylor", "Anderson", "Thomas",
         "Jackson", "White", "Harris", "Martin", "Garcia", "Clark", "Lewis", "Walker", "Hall", "Young"]
_PRODUCTS = [
    ("Laptop Pro 14", "Electronics", 1299.0), ("Wireless Mouse", "Electronics", 25.5),
    ("Mechanical Keyboard", "Electronics", 89.0), ("4K Monitor", "Electronics", 349.0),
    ("USB-C Hub", "Electronics", 45.0), ("Noise Cancelling Headphones", "Electronics", 199.0),
    ("Standing Desk", "Furniture", 420.0), ("Ergonomic Chair", "Furniture", 310.0),
    ("Bookshelf", "Furniture", 120.0), ("Desk Lamp", "Furniture", 35.0),
    ("Notebook (pack of 5)", "Stationery", 12.0), ("Gel Pens (12)", "Stationery", 9.5),
    ("Whiteboard", "Stationery", 60.0), ("Sticky Notes", "Stationery", 4.0),
    ("Coffee Beans 1kg", "Grocery", 18.0), ("Green Tea (100 bags)", "Grocery", 11.0),
    ("Protein Bars (24)", "Grocery", 29.0), ("Running Shoes", "Apparel", 95.0),
    ("Rain Jacket", "Apparel", 110.0), ("Backpack", "Apparel", 70.0),
]
_DEPTS = [("Engineering", "San Francisco"), ("Sales", "New York"), ("Marketing", "Chicago"),
          ("HR", "Austin"), ("Finance", "Seattle"), ("Support", "Denver")]


def _rand_date(rng: random.Random, start: date, end: date) -> str:
    return (start + timedelta(days=rng.randint(0, (end - start).days))).isoformat()


def init_db(force: bool = False) -> Path:
    """Create and seed the sample database if it does not exist yet (deterministic seed)."""
    if DB_PATH.exists() and not force:
        return DB_PATH
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    rng = random.Random(42)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA_FILE.read_text())

    con.executemany("INSERT INTO Departments(DepartmentName, Location) VALUES (?,?)", _DEPTS)

    employees = []
    for i in range(1, 41):
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        dept = rng.randint(1, len(_DEPTS))
        manager = None if i <= 3 else rng.randint(1, 3)
        employees.append((i, first, last, f"{first.lower()}.{last.lower()}{i}@corp.example",
                          _rand_date(rng, date(2019, 1, 1), date(2025, 9, 30)),
                          round(rng.uniform(45000, 160000), 2), dept, manager))
    con.executemany("INSERT INTO Employees VALUES (?,?,?,?,?,?,?,?)", employees)

    customers = []
    for i in range(1, 61):
        state = rng.choice(_STATES)
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        customers.append((i, first, last, f"{first.lower()}{i}@mail.example", rng.choice(_CITIES[state]),
                          state, "USA", _rand_date(rng, date(2022, 1, 1), date(2025, 9, 30))))
    con.executemany("INSERT INTO Customers VALUES (?,?,?,?,?,?,?,?)", customers)

    con.executemany(
        "INSERT INTO Products(ProductName, Category, UnitPrice, Stock) VALUES (?,?,?,?)",
        [(n, c, p, rng.randint(0, 250)) for n, c, p in _PRODUCTS],
    )

    statuses = ["Pending", "Shipped", "Delivered", "Delivered", "Delivered", "Cancelled"]
    item_id = 1
    for oid in range(1, 301):
        cust = rng.randint(1, 60)
        emp = rng.choice([None] + [e[0] for e in employees if e[6] == 2])  # Sales dept
        odate = _rand_date(rng, date(2023, 1, 1), date(2025, 10, 1))
        items, total = [], 0.0
        for _ in range(rng.randint(1, 4)):
            pid = rng.randint(1, len(_PRODUCTS))
            qty = rng.randint(1, 5)
            price = _PRODUCTS[pid - 1][2]
            total += qty * price
            items.append((item_id, oid, pid, qty, price))
            item_id += 1
        con.execute("INSERT INTO Orders VALUES (?,?,?,?,?,?)",
                    (oid, cust, emp, odate, rng.choice(statuses), round(total, 2)))
        con.executemany("INSERT INTO OrderItems VALUES (?,?,?,?,?)", items)
    con.commit()
    con.close()
    return DB_PATH


# --------------------------------------------------------------------------- connections
def connect_ro() -> sqlite3.Connection:
    """Read-only connection with an authorizer that only allows SELECT-style actions."""
    init_db()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON")
    # Treat "double quoted" unknown identifiers as errors, not string literals (Python 3.12+).
    if hasattr(con, "setconfig"):
        try:
            con.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            con.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
        except (sqlite3.Error, AttributeError):
            pass
    con.set_authorizer(read_only_authorizer)
    return con


# --------------------------------------------------------------------------- schema introspection
_SCHEMA_CACHE: dict | None = None


def get_schema(refresh: bool = False) -> dict:
    """Return {table: {columns: [...], foreign_keys: [...], row_count}} from the live DB."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None and not refresh:
        return _SCHEMA_CACHE
    init_db()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # no authorizer: PRAGMAs needed
    schema: dict = {}
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        cols = []
        for _cid, name, ctype, notnull, _dflt, pk in con.execute(f'PRAGMA table_info("{t}")'):
            col = {"name": name, "type": ctype or "TEXT", "pk": bool(pk), "notnull": bool(notnull)}
            if (ctype or "").upper() in ("TEXT", "VARCHAR") and not pk and "email" not in name.lower():
                vals = [r[0] for r in con.execute(
                    f'SELECT DISTINCT "{name}" FROM "{t}" WHERE "{name}" IS NOT NULL LIMIT 13')]
                if 0 < len(vals) <= 12:
                    col["values"] = sorted(vals)
            cols.append(col)
        fks = [{"from": r[3], "table": r[2], "to": r[4]} for r in con.execute(f'PRAGMA foreign_key_list("{t}")')]
        count = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        schema[t] = {"columns": cols, "foreign_keys": fks, "row_count": count}
    con.close()
    _SCHEMA_CACHE = schema
    return schema


def schema_to_text(schema: dict, tables: list[str] | None = None) -> str:
    """Compact, LLM-friendly description of (a subset of) the schema."""
    lines = []
    for t, info in schema.items():
        if tables is not None and t not in tables:
            continue
        cols = []
        for c in info["columns"]:
            s = f'{c["name"]} {c["type"]}'
            if c["pk"]:
                s += " PK"
            if "values" in c:
                s += " one of: " + ", ".join(repr(v) for v in c["values"])
            cols.append(s)
        lines.append(f"TABLE {t} ({'; '.join(cols)})")
        for fk in info["foreign_keys"]:
            lines.append(f"  FK {t}.{fk['from']} -> {fk['table']}.{fk['to']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- execution
def execute_readonly(sql: str, limit: int | None = None) -> dict:
    """Run a validated SELECT. Read-only connection + authorizer + row cap + timeout."""
    limit = limit or MAX_ROWS
    con = connect_ro()
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    start = time.monotonic()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
    finally:
        con.close()
    truncated = len(rows) > limit
    rows = [list(r) for r in rows[:limit]]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
    }


def explain_plan(sql: str) -> dict:
    """EXPLAIN QUERY PLAN as a coarse cost estimate (SCAN = full table scan, SEARCH = index lookup)."""
    con = connect_ro()
    try:
        steps = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        con.close()
    scans = sum(1 for s in steps if s.startswith("SCAN"))
    temp = sum(1 for s in steps if "TEMP B-TREE" in s)
    score = scans * 2 + temp
    level = "low" if score <= 1 else "medium" if score <= 4 else "high"
    return {"steps": steps, "full_scans": scans, "temp_structures": temp, "estimated_cost": level}


if __name__ == "__main__":
    p = init_db(force=True)
    print("created", p)
    print(schema_to_text(get_schema(refresh=True)))
