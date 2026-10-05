"""SQL validation. The core checks use SQLite itself against the real schema:

  * syntax          -> SQLite parser (EXPLAIN never executes the query)
  * tables/columns  -> SQLite name resolution, with "did you mean" suggestions
  * read-only       -> keyword screen + single-statement check + SQLite authorizer
  * joins           -> (needs sqlglot, optional) cartesian joins + non foreign-key joins

For PostgreSQL / MySQL requests the query is transpiled to SQLite with sqlglot (if installed) so
the same schema checks apply.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from . import db

FORBIDDEN_KEYWORDS = [
    "delete", "update", "insert", "drop", "alter", "truncate", "create", "attach", "detach",
    "pragma", "vacuum", "reindex", "grant", "revoke", "merge", "exec", "execute", "call",
]
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b|\breplace\s+into\b", re.I)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_DIALECTS = {"sqlite", "postgres", "mysql"}


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sql: str = ""          # cleaned SQL in the requested dialect
    sqlite_sql: str = ""   # the same query as SQLite sees it (used for checks and execution)

    def as_dict(self) -> dict:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def clean_sql(sql: str) -> str:
    sql = (sql or "").strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.I).strip()
    return sql.rstrip(";").strip()


def _mask(sql: str) -> str:
    """Remove comments and string literals so keyword scans don't trip on data."""
    return _STRING_RE.sub("''", _COMMENT_RE.sub(" ", sql))


def _suggest(name: str, options: list[str]) -> str:
    match = difflib.get_close_matches(name.lower(), [o.lower() for o in options], n=1, cutoff=0.6)
    if not match:
        return ""
    original = next(o for o in options if o.lower() == match[0])
    return f" Did you mean '{original}'?"


def _translate_sqlite_error(msg: str, schema: dict) -> str:
    tables = list(schema)
    columns = sorted({c["name"] for t in schema.values() for c in t["columns"]})
    m = re.match(r"no such table: (.+)", msg)
    if m:
        t = m.group(1).split(".")[-1]
        return f"Table '{t}' does not exist in the schema.{_suggest(t, tables)} Available tables: {', '.join(tables)}."
    m = re.match(r"no such column: (.+)", msg)
    if m:
        col = m.group(1)
        return f"Column '{col}' does not exist.{_suggest(col.split('.')[-1], columns)}"
    m = re.match(r"ambiguous column name: (.+)", msg)
    if m:
        return f"Column '{m.group(1)}' is ambiguous; qualify it with a table name or alias."
    if "not authorized" in msg or "attempt to write" in msg or "readonly" in msg:
        return "The statement is not read-only. Only SELECT queries are allowed."
    m = re.match(r"no such function: (.+)", msg)
    if m:
        return f"Function '{m.group(1)}' is not available."
    if "syntax error" in msg:
        return f"SQL syntax error ({msg})."
    if "You can only execute one statement" in msg:
        return "Only a single statement is allowed."
    return f"SQL error: {msg}."


def to_sqlite(sql: str, dialect: str) -> tuple[str, str | None]:
    """Return (sqlite_sql, error). Non-SQLite dialects need sqlglot; falls back to unchanged SQL."""
    if dialect == "sqlite" or dialect not in _DIALECTS:
        return sql, None
    try:
        import sqlglot
    except ImportError:
        return sql, None
    try:
        return sqlglot.transpile(sql, read=dialect, write="sqlite")[0], None
    except Exception as exc:  # sqlglot.errors.ParseError and friends
        return sql, f"SQL syntax error for dialect '{dialect}': {str(exc).splitlines()[0]}"


def validate(sql: str, dialect: str = "sqlite", schema: dict | None = None) -> ValidationResult:
    schema = schema or db.get_schema()
    sql = clean_sql(sql)
    res = ValidationResult(valid=False, sql=sql)
    if not sql:
        res.errors.append("No SQL was produced.")
        return res

    masked = _mask(sql)

    # 1. single statement
    if ";" in masked:
        res.errors.append("Only a single statement is allowed (found ';' inside the query).")
        return res

    # 2. read-only: must start with SELECT / WITH and contain no write/DDL keywords
    first = re.match(r"\s*\(*\s*(\w+)", masked)
    if not first or first.group(1).lower() not in ("select", "with"):
        res.errors.append("Only read-only SELECT queries are allowed.")
        return res
    bad = _FORBIDDEN_RE.search(masked)
    if bad:
        res.errors.append(f"Forbidden operation '{bad.group(0).upper()}': only read-only queries are allowed.")
        return res

    # 3. dialect -> sqlite
    sqlite_sql, err = to_sqlite(sql, dialect)
    if err:
        res.errors.append(err)
        return res
    res.sqlite_sql = sqlite_sql

    # 4. SQLite resolves syntax, tables, columns and functions against the real schema
    con = db.connect_ro()
    try:
        con.execute("EXPLAIN " + sqlite_sql)
    except Exception as exc:
        res.errors.append(_translate_sqlite_error(str(exc), schema))
        return res
    finally:
        con.close()

    # 5. join analysis (optional, needs sqlglot)
    j_errors, j_warnings = analyze_joins(sql, dialect, schema)
    res.errors.extend(j_errors)
    res.warnings.extend(j_warnings)
    if dialect != "sqlite" and not _have_sqlglot():
        res.warnings.append("sqlglot is not installed: the query was checked as-is against SQLite only.")
    res.valid = not res.errors
    return res


def _have_sqlglot() -> bool:
    try:
        import sqlglot  # noqa: F401
        return True
    except ImportError:
        return False


def analyze_joins(sql: str, dialect: str, schema: dict) -> tuple[list[str], list[str]]:
    """Check join relationships. Returns (errors, warnings). Silently skipped without sqlglot."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return [], []
    try:
        tree = sqlglot.parse_one(sql, read=dialect if dialect in _DIALECTS else None)
    except Exception:
        return [], []

    errors: list[str] = []
    warnings: list[str] = []
    real = {t.lower(): t for t in schema}
    fk_pairs = set()
    for t, info in schema.items():
        for fk in info["foreign_keys"]:
            fk_pairs.add(frozenset({(t.lower(), fk["from"].lower()), (fk["table"].lower(), fk["to"].lower())}))

    try:
        for select in tree.find_all(exp.Select):
            alias_map: dict[str, str] = {}
            for tbl in select.find_all(exp.Table):
                name = tbl.name.lower()
                if name in real:
                    alias_map[(tbl.alias or tbl.name).lower()] = name
            has_where = select.args.get("where") is not None
            for join in select.args.get("joins") or []:
                on, using = join.args.get("on"), join.args.get("using")
                kind = (join.args.get("kind") or "").upper() if isinstance(join.args.get("kind"), str) else ""
                target = join.this.alias_or_name if join.this is not None else "?"
                if on is None and not using and kind != "CROSS":
                    msg = f"Join with '{target}' has no join condition"
                    if has_where:
                        warnings.append(msg + " (implicit join resolved in WHERE); prefer explicit JOIN ... ON.")
                    else:
                        errors.append(msg + ": this produces a cartesian product.")
                    continue
                if on is None:
                    continue
                for eq in on.find_all(exp.EQ):
                    left, right = eq.left, eq.right
                    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                        continue
                    lt, rt = alias_map.get(left.table.lower()), alias_map.get(right.table.lower())
                    if not lt or not rt:
                        continue
                    pair = frozenset({(lt, left.name.lower()), (rt, right.name.lower())})
                    if pair not in fk_pairs:
                        warnings.append(
                            f"Join condition {left.sql()} = {right.sql()} does not follow a declared "
                            f"foreign-key relationship ({real[lt]}.{left.name} / {real[rt]}.{right.name})."
                        )
    except Exception:
        return [], []
    return errors, warnings
