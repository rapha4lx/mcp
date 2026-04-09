from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


READ_ONLY_PREFIXES = ("select", "with", "show")
FORBIDDEN_SQL_PATTERNS = (
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\bdrop\b",
    r"\balter\b",
    r"\btruncate\b",
    r"\bcreate\b",
    r"\bgrant\b",
    r"\brevoke\b",
    r"\bcopy\b",
    r"\bcall\b",
    r"\bdo\b",
)


@dataclass(frozen=True)
class Settings:
    database_url: str
    default_schema: str
    statement_timeout_ms: int
    max_rows: int


def load_settings() -> Settings:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required in the environment or .env file.")

    return Settings(
        database_url=database_url,
        default_schema=os.environ.get("PG_SCHEMA", "public"),
        statement_timeout_ms=int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "10000")),
        max_rows=int(os.environ.get("PG_MAX_ROWS", "200")),
    )


SETTINGS = load_settings()
mcp = FastMCP(
    "postgres-tools",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "3005")),
    stateless_http=True,
    json_response=True,
)


def _connect() -> psycopg.Connection:
    conn = psycopg.connect(SETTINGS.database_url, autocommit=False)
    conn.execute("SET default_transaction_read_only = on")
    conn.execute(f"SET statement_timeout = '{SETTINGS.statement_timeout_ms}ms'")
    return conn


def _verify_database_connection() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select current_database(), current_user")
        database_name, current_user = cur.fetchone()

    print(
        f"Database connection OK: database={database_name} user={current_user}",
        file=sys.stderr,
    )


def _normalize_sql(sql: str) -> str:
    normalized = sql.strip()
    if not normalized:
        raise ValueError("SQL query cannot be empty.")
    if normalized.count(";") > 1 or (";" in normalized[:-1]):
        raise ValueError("Only a single SQL statement is allowed.")
    return normalized[:-1].strip() if normalized.endswith(";") else normalized


def _validate_read_only_sql(sql: str) -> str:
    normalized = _normalize_sql(sql)
    lowered = normalized.lower()

    if not lowered.startswith(READ_ONLY_PREFIXES):
        raise ValueError("Only read-only SELECT, WITH, or SHOW statements are allowed.")

    for pattern in FORBIDDEN_SQL_PATTERNS:
        if re.search(pattern, lowered):
            raise ValueError("Query contains non-read-only SQL and was rejected.")

    return normalized


def _parse_params(params_json: str | None) -> list[Any]:
    if not params_json:
        return []

    try:
        payload = json.loads(params_json)
    except json.JSONDecodeError as exc:
        raise ValueError("params_json must be valid JSON.") from exc

    if not isinstance(payload, list):
        raise ValueError("params_json must decode to a JSON array.")

    return payload


def _qualified_name(schema: str, table: str) -> tuple[str, str]:
    schema_name = (schema or SETTINGS.default_schema).strip()
    table_name = table.strip()
    if not schema_name or not table_name:
        raise ValueError("schema and table must be non-empty.")
    return schema_name, table_name


@mcp.tool()
def list_tables(schema: str = SETTINGS.default_schema) -> dict[str, Any]:
    """List base tables and views visible in a schema."""
    sql = """
        select
            table_schema,
            table_name,
            table_type
        from information_schema.tables
        where table_schema = %s
        order by table_name
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (schema,))
        rows = cur.fetchall()

    return {"schema": schema, "count": len(rows), "tables": rows}


@mcp.tool()
def describe_table(table: str, schema: str = SETTINGS.default_schema) -> dict[str, Any]:
    """Describe columns for a table or view."""
    schema_name, table_name = _qualified_name(schema, table)
    sql = """
        select
            column_name,
            data_type,
            is_nullable,
            column_default
        from information_schema.columns
        where table_schema = %s
          and table_name = %s
        order by ordinal_position
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (schema_name, table_name))
        rows = cur.fetchall()

    return {
        "schema": schema_name,
        "table": table_name,
        "count": len(rows),
        "columns": rows,
    }


@mcp.tool()
def query(sql: str, params_json: str | None = None, max_rows: int | None = None) -> dict[str, Any]:
    """Run a read-only SQL query against Postgres."""
    safe_sql = _validate_read_only_sql(sql)
    params = _parse_params(params_json)
    row_limit = min(max_rows or SETTINGS.max_rows, SETTINGS.max_rows)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(safe_sql, params)
        column_names = [column.name for column in cur.description] if cur.description else []
        rows = cur.fetchmany(row_limit)

    return {
        "row_count": len(rows),
        "truncated": len(rows) == row_limit,
        "max_rows": row_limit,
        "columns": column_names,
        "rows": rows,
    }


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    try:
        _verify_database_connection()
    except Exception as exc:
        print(f"Database connection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
