from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from threading import Lock
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
    database_url: str | None
    default_schema: str
    statement_timeout_ms: int
    max_rows: int
    session_ttl_hours: int


@dataclass(frozen=True)
class RequestContext:
    database_url: str
    schema: str
    statement_timeout_ms: int
    max_rows: int


@dataclass(frozen=True)
class SessionEntry:
    token: str
    label: str
    database_url: str
    schema: str
    statement_timeout_ms: int
    max_rows: int
    created_at: datetime
    expires_at: datetime

    def to_public_payload(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "label": self.label,
            "schema": self.schema,
            "statement_timeout_ms": self.statement_timeout_ms,
            "max_rows": self.max_rows,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class SessionStore:
    def __init__(self, ttl_hours: int) -> None:
        if ttl_hours <= 0:
            raise ValueError("PG_SESSION_TTL_HOURS must be greater than zero.")
        self._ttl = timedelta(hours=ttl_hours)
        self._sessions: dict[str, SessionEntry] = {}
        self._lock = Lock()

    def create(self, context: RequestContext, label: str | None = None) -> SessionEntry:
        now = datetime.now(UTC)
        entry = SessionEntry(
            token=secrets.token_urlsafe(24),
            label=(label or context.schema).strip() or "default",
            database_url=context.database_url,
            schema=context.schema,
            statement_timeout_ms=context.statement_timeout_ms,
            max_rows=context.max_rows,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._purge_expired_locked(now)
            self._sessions[entry.token] = entry
        return entry

    def get(self, token: str) -> SessionEntry:
        normalized = token.strip()
        if not normalized:
            raise ValueError("session_token must be non-empty.")
        now = datetime.now(UTC)
        with self._lock:
            self._purge_expired_locked(now)
            entry = self._sessions.get(normalized)
            if not entry:
                raise ValueError("session_token is invalid or expired.")
            return entry

    def list(self) -> list[SessionEntry]:
        now = datetime.now(UTC)
        with self._lock:
            self._purge_expired_locked(now)
            return sorted(self._sessions.values(), key=lambda entry: entry.created_at)

    def revoke(self, token: str) -> bool:
        normalized = token.strip()
        if not normalized:
            raise ValueError("session_token must be non-empty.")
        with self._lock:
            return self._sessions.pop(normalized, None) is not None

    def _purge_expired_locked(self, now: datetime) -> None:
        expired_tokens = [
            token for token, entry in self._sessions.items() if entry.expires_at <= now
        ]
        for token in expired_tokens:
            self._sessions.pop(token, None)


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        database_url=os.environ.get("DATABASE_URL"),
        default_schema=os.environ.get("PG_SCHEMA", "public"),
        statement_timeout_ms=int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "10000")),
        max_rows=int(os.environ.get("PG_MAX_ROWS", "200")),
        session_ttl_hours=int(os.environ.get("PG_SESSION_TTL_HOURS", "24")),
    )


SETTINGS = load_settings()
SESSION_STORE = SessionStore(SETTINGS.session_ttl_hours)
mcp = FastMCP(
    "postgres-tools",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "3005")),
    stateless_http=True,
    json_response=True,
)


def _resolve_request_context(
    session_token: str | None = None,
    database_url: str | None = None,
    schema: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows: int | None = None,
) -> RequestContext:
    session_entry = SESSION_STORE.get(session_token) if session_token else None
    resolved_database_url = (
        database_url
        or (session_entry.database_url if session_entry else None)
        or SETTINGS.database_url
        or ""
    ).strip()
    if not resolved_database_url:
        raise ValueError(
            "session_token or database_url is required when DATABASE_URL is not configured in the environment."
        )

    resolved_schema = (
        schema or (session_entry.schema if session_entry else None) or SETTINGS.default_schema
    ).strip()
    if not resolved_schema:
        raise ValueError("schema must be non-empty.")

    resolved_timeout = (
        statement_timeout_ms
        or (session_entry.statement_timeout_ms if session_entry else None)
        or SETTINGS.statement_timeout_ms
    )
    if resolved_timeout <= 0:
        raise ValueError("statement_timeout_ms must be greater than zero.")

    resolved_max_rows = (
        max_rows or (session_entry.max_rows if session_entry else None) or SETTINGS.max_rows
    )
    if resolved_max_rows <= 0:
        raise ValueError("max_rows must be greater than zero.")

    return RequestContext(
        database_url=resolved_database_url,
        schema=resolved_schema,
        statement_timeout_ms=resolved_timeout,
        max_rows=resolved_max_rows,
    )


def _connect(context: RequestContext) -> psycopg.Connection:
    conn = psycopg.connect(context.database_url, autocommit=False)
    conn.execute("SET default_transaction_read_only = on")
    conn.execute(f"SET statement_timeout = '{context.statement_timeout_ms}ms'")
    return conn


def _tool_error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _safe_tool(fn):
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            print(f"Tool request failed in {fn.__name__}: {exc}", file=sys.stderr)
            return _tool_error_payload(exc)

    return wrapped


def _validate_database_connection(context: RequestContext) -> dict[str, Any]:
    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute("select current_database(), current_user")
        database_name, current_user = cur.fetchone()
    return {
        "database_name": database_name,
        "current_user": current_user,
    }


def _verify_database_connection() -> None:
    context = _resolve_request_context()
    connection_info = _validate_database_connection(context)
    print(
        "Database connection OK: "
        f"database={connection_info['database_name']} "
        f"user={connection_info['current_user']}",
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
    schema_name = schema.strip()
    table_name = table.strip()
    if not schema_name or not table_name:
        raise ValueError("schema and table must be non-empty.")
    return schema_name, table_name


def _fetch_foreign_key_relationships(
    context: RequestContext,
    schema: str,
    table: str,
    relation_direction: str,
) -> list[tuple[Any, ...]]:
    if relation_direction not in {"incoming", "outgoing"}:
        raise ValueError("relation_direction must be 'incoming' or 'outgoing'.")

    join_column = "con.confrelid" if relation_direction == "incoming" else "con.conrelid"

    sql = f"""
        with target_table as (
            select c.oid
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s
              and c.relname = %s
        )
        select
            %s as relation_direction,
            src_ns.nspname as table_schema,
            src_cls.relname as table_name,
            src_col.attname as column_name,
            tgt_ns.nspname as related_table_schema,
            tgt_cls.relname as related_table_name,
            tgt_col.attname as related_column_name,
            con.conname as constraint_name
        from pg_constraint con
        join target_table tt on tt.oid = {join_column}
        join pg_class src_cls on src_cls.oid = con.conrelid
        join pg_namespace src_ns on src_ns.oid = src_cls.relnamespace
        join pg_class tgt_cls on tgt_cls.oid = con.confrelid
        join pg_namespace tgt_ns on tgt_ns.oid = tgt_cls.relnamespace
        join lateral unnest(con.conkey) with ordinality as src_keys(attnum, ordinality) on true
        join lateral unnest(con.confkey) with ordinality as tgt_keys(attnum, ordinality)
            on tgt_keys.ordinality = src_keys.ordinality
        join pg_attribute src_col on src_col.attrelid = src_cls.oid and src_col.attnum = src_keys.attnum
        join pg_attribute tgt_col on tgt_col.attrelid = tgt_cls.oid and tgt_col.attnum = tgt_keys.attnum
        where con.contype = 'f'
        order by table_schema, table_name, constraint_name
    """

    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute(sql, (schema, table, relation_direction))
        return cur.fetchall()


def _fetch_related_table_names(
    context: RequestContext,
    schema: str,
    table: str,
    relation_direction: str,
) -> list[tuple[Any, ...]]:
    if relation_direction not in {"incoming", "outgoing"}:
        raise ValueError("relation_direction must be 'incoming' or 'outgoing'.")

    join_column = "con.confrelid" if relation_direction == "incoming" else "con.conrelid"

    sql = f"""
        with target_table as (
            select c.oid
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s
              and c.relname = %s
        )
        select distinct
            %s as relation_direction,
            related_ns.nspname as related_table_schema,
            related_cls.relname as related_table_name
        from pg_constraint con
        join target_table tt on tt.oid = {join_column}
        join pg_class related_cls on related_cls.oid = con.conrelid
        join pg_namespace related_ns on related_ns.oid = related_cls.relnamespace
        where con.contype = 'f'
        order by related_table_schema, related_table_name
    """

    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute(sql, (schema, table, relation_direction))
        return cur.fetchall()


@mcp.tool()
@_safe_tool
def create_session(
    database_url: str,
    schema: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Create a temporary session token for a database connection."""
    context = _resolve_request_context(
        database_url=database_url,
        schema=schema,
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
    )
    connection_info = _validate_database_connection(context)
    session = SESSION_STORE.create(context, label=label)
    return {
        "ok": True,
        "session": session.to_public_payload(),
        "database_name": connection_info["database_name"],
        "current_user": connection_info["current_user"],
    }


@mcp.tool()
@_safe_tool
def list_sessions() -> dict[str, Any]:
    """List active temporary session tokens."""
    sessions = [entry.to_public_payload() for entry in SESSION_STORE.list()]
    return {"count": len(sessions), "sessions": sessions}


@mcp.tool()
@_safe_tool
def revoke_session(session_token: str) -> dict[str, Any]:
    """Revoke an active temporary session token."""
    revoked = SESSION_STORE.revoke(session_token)
    return {"ok": revoked, "revoked": revoked}


@mcp.tool()
@_safe_tool
def list_tables(
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List base tables and views visible in a schema."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    sql = """
        select
            table_schema,
            table_name,
            table_type
        from information_schema.tables
        where table_schema = %s
        order by table_name
    """
    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute(sql, (context.schema,))
        rows = cur.fetchall()

    return {"schema": context.schema, "count": len(rows), "tables": rows}


@mcp.tool()
@_safe_tool
def list_views(
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List views visible in a schema."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    sql = """
        select
            table_schema,
            table_name
        from information_schema.views
        where table_schema = %s
        order by table_name
    """
    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute(sql, (context.schema,))
        rows = cur.fetchall()

    return {"schema": context.schema, "count": len(rows), "views": rows}


@mcp.tool()
@_safe_tool
def list_functions(
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List functions visible in a schema."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    sql = """
        select
            routine_schema,
            routine_name,
            specific_name,
            data_type
        from information_schema.routines
        where routine_schema = %s
          and routine_type = 'FUNCTION'
        order by routine_name, specific_name
    """

    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute(sql, (context.schema,))
        rows = cur.fetchall()

    return {"schema": context.schema, "count": len(rows), "functions": rows}


@mcp.tool()
@_safe_tool
def list_referenced_tables(
    table: str,
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List tables referenced by foreign keys from a given table."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    schema_name, table_name = _qualified_name(context.schema, table)
    rows = _fetch_foreign_key_relationships(context, schema_name, table_name, "outgoing")

    return {
        "schema": schema_name,
        "table": table_name,
        "count": len(rows),
        "referenced_tables": rows,
    }


@mcp.tool()
@_safe_tool
def list_referencing_tables(
    table: str,
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List tables that reference a given table by foreign keys."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    schema_name, table_name = _qualified_name(context.schema, table)
    rows = _fetch_foreign_key_relationships(context, schema_name, table_name, "incoming")

    return {
        "schema": schema_name,
        "table": table_name,
        "count": len(rows),
        "referencing_tables": rows,
    }


@mcp.tool()
@_safe_tool
def list_related_tables(
    table: str,
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List related table names for a given table."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    schema_name, table_name = _qualified_name(context.schema, table)
    referenced_rows = _fetch_related_table_names(context, schema_name, table_name, "outgoing")
    referencing_rows = _fetch_related_table_names(context, schema_name, table_name, "incoming")

    return {
        "schema": schema_name,
        "table": table_name,
        "referenced_count": len(referenced_rows),
        "referencing_count": len(referencing_rows),
        "referenced_tables": referenced_rows,
        "referencing_tables": referencing_rows,
    }


@mcp.tool()
@_safe_tool
def list_related_tables_detailed(
    table: str,
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """List related tables with columns and constraints for a given table."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    schema_name, table_name = _qualified_name(context.schema, table)
    referenced_rows = _fetch_foreign_key_relationships(context, schema_name, table_name, "outgoing")
    referencing_rows = _fetch_foreign_key_relationships(context, schema_name, table_name, "incoming")

    return {
        "schema": schema_name,
        "table": table_name,
        "referenced_count": len(referenced_rows),
        "referencing_count": len(referencing_rows),
        "referenced_tables": referenced_rows,
        "referencing_tables": referencing_rows,
    }


@mcp.tool()
@_safe_tool
def describe_table(
    table: str,
    schema: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """Describe columns for a table or view."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
    )
    schema_name, table_name = _qualified_name(context.schema, table)
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
    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute(sql, (schema_name, table_name))
        rows = cur.fetchall()

    return {
        "schema": schema_name,
        "table": table_name,
        "count": len(rows),
        "columns": rows,
    }


@mcp.tool()
@_safe_tool
def query(
    sql: str,
    params_json: str | None = None,
    max_rows: int | None = None,
    session_token: str | None = None,
    schema: str | None = None,
    statement_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Run a read-only SQL query against Postgres."""
    safe_sql = _validate_read_only_sql(sql)
    params = _parse_params(params_json)
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
    )
    row_limit = min(max_rows or context.max_rows, context.max_rows)

    with _connect(context) as conn, conn.cursor() as cur:
        cur.execute("select set_config('search_path', %s, false)", (context.schema,))
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
    if SETTINGS.database_url:
        try:
            _verify_database_connection()
        except Exception as exc:
            print(f"Database connection failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    else:
        print(
            "DATABASE_URL not configured at startup; waiting for create_session or per-request database_url.",
            file=sys.stderr,
        )

    try:
        mcp.run(transport=transport)
    except Exception as exc:
        print(f"MCP server failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
