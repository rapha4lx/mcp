from __future__ import annotations

import json
import os
import re
import secrets
import sys
import subprocess
import importlib
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from threading import Lock
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

DRIVER_MAP = {
    "postgresql": "psycopg[binary]",
    "postgres": "psycopg[binary]",
    "postgresql+psycopg": "psycopg[binary]",
    "postgresql+psycopg2": "psycopg2-binary",
    "mysql": "pymysql",
    "mysql+pymysql": "pymysql",
    "mssql+pyodbc": "pyodbc",
}

@dataclass(frozen=True)
class Settings:
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
    permissions: dict[str, bool]

@dataclass(frozen=True)
class SessionEntry:
    token: str
    label: str
    database_url: str
    schema: str
    statement_timeout_ms: int
    max_rows: int
    permissions: dict[str, bool]
    created_at: datetime
    expires_at: datetime

    def to_public_payload(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "label": self.label,
            "schema": self.schema,
            "statement_timeout_ms": self.statement_timeout_ms,
            "max_rows": self.max_rows,
            "permissions": self.permissions,
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
            permissions=context.permissions,
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
        default_schema=os.environ.get("PG_SCHEMA", "public"),
        statement_timeout_ms=int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "10000")),
        max_rows=int(os.environ.get("PG_MAX_ROWS", "200")),
        session_ttl_hours=int(os.environ.get("PG_SESSION_TTL_HOURS", "24")),
    )


SETTINGS = load_settings()
SESSION_STORE = SessionStore(SETTINGS.session_ttl_hours)
mcp = FastMCP(
    "sql-tools",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "3005")),
    stateless_http=True,
    json_response=True,
)

_engines: dict[str, Engine] = {}
_engines_lock = Lock()


def _ensure_driver(database_url: str) -> None:
    if not database_url:
        return
    parsed = urlparse(database_url.strip())
    scheme = parsed.scheme.lower()
    
    try:
        temp_engine = create_engine(database_url)
        _ = temp_engine.dialect.dbapi
    except Exception:
        pkg = DRIVER_MAP.get(scheme)
        if pkg:
            print(f"Driver for {scheme} not found. Dynamically installing {pkg}...", file=sys.stderr)
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
            importlib.invalidate_caches()
        else:
            print(f"No auto-install package mapped for scheme '{scheme}'. Attempting to proceed anyway...", file=sys.stderr)


def _get_engine(context: RequestContext) -> Engine:
    _ensure_driver(context.database_url)
    with _engines_lock:
        if context.database_url not in _engines:
            _engines[context.database_url] = create_engine(context.database_url)
        return _engines[context.database_url]


def _connect(context: RequestContext):
    engine = _get_engine(context)
    return engine.connect()


def _resolve_request_context(
    session_token: str | None = None,
    database_url: str | None = None,
    schema: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows: int | None = None,
    fallback_permissions: dict[str, bool] | None = None,
) -> RequestContext:
    session_entry = SESSION_STORE.get(session_token) if session_token else None
    
    resolved_database_url = (database_url or (session_entry.database_url if session_entry else None) or "").strip()
    if not resolved_database_url:
        raise ValueError("A database_url (during create_session) or session_token is strictly required.")

    if resolved_database_url.startswith("postgres://"):
        resolved_database_url = resolved_database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif resolved_database_url.startswith("postgresql://"):
        resolved_database_url = resolved_database_url.replace("postgresql://", "postgresql+psycopg://", 1)

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

    resolved_permissions = session_entry.permissions if session_entry else (fallback_permissions or {})

    return RequestContext(
        database_url=resolved_database_url,
        schema=resolved_schema,
        statement_timeout_ms=resolved_timeout,
        max_rows=resolved_max_rows,
        permissions=resolved_permissions,
    )


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
    database_name = "unknown"
    current_user = "unknown"
    with _connect(context) as conn:
        engine = _get_engine(context)
        if "postgres" in engine.name:
            res = conn.exec_driver_sql("select current_database(), current_user").fetchone()
            if res:
                database_name, current_user = res
        elif "mysql" in engine.name:
            res = conn.exec_driver_sql("select database(), current_user()").fetchone()
            if res:
                database_name, current_user = res

    return {
        "database_name": database_name,
        "current_user": current_user,
    }


def _normalize_sql(sql: str) -> str:
    normalized = sql.strip()
    if not normalized:
        raise ValueError("SQL query cannot be empty.")
    if normalized.count(";") > 1 or (";" in normalized[:-1]):
        raise ValueError("Only a single SQL statement is allowed.")
    return normalized[:-1].strip() if normalized.endswith(";") else normalized


def _validate_sql_permissions(sql: str, permissions: dict[str, bool]) -> str:
    normalized = _normalize_sql(sql)
    lowered = normalized.lower()

    if re.search(r"\binsert\b", lowered) and not permissions.get("allow_insert"):
        raise ValueError("Query rejected: 'allow_insert' permission is required for INSERT operations.")

    if re.search(r"\bupdate\b", lowered) and not permissions.get("allow_update"):
        raise ValueError("Query rejected: 'allow_update' permission is required for UPDATE operations.")

    if re.search(r"\bdelete\b", lowered) and not permissions.get("allow_delete"):
        raise ValueError("Query rejected: 'allow_delete' permission is required for DELETE operations.")

    if re.search(r"\bcreate\b", lowered) and not permissions.get("allow_create"):
        raise ValueError("Query rejected: 'allow_create' permission is required for CREATE operations.")

    if re.search(r"\b(?:drop|alter|truncate)\b", lowered) and not permissions.get("allow_drop"):
        raise ValueError("Query rejected: 'allow_drop' permission is required for DROP/ALTER/TRUNCATE operations.")

    if not permissions.get("allow_read") and re.search(r"\b(?:select|show|with)\b", lowered):
        raise ValueError("Query rejected: 'allow_read' permission is required for SELECT/SHOW/WITH operations.")

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


@mcp.tool()
@_safe_tool
def create_session(
    database_url: str,
    schema: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows: int | None = None,
    label: str | None = None,
    allow_read: bool = True,
    allow_insert: bool = False,
    allow_update: bool = False,
    allow_delete: bool = False,
    allow_create: bool = False,
    allow_drop: bool = False,
) -> dict[str, Any]:
    """Create a temporary session token for a database connection with granular permissions."""
    permissions = {
        "allow_read": allow_read,
        "allow_insert": allow_insert,
        "allow_update": allow_update,
        "allow_delete": allow_delete,
        "allow_create": allow_create,
        "allow_drop": allow_drop,
    }

    context = _resolve_request_context(
        database_url=database_url,
        schema=schema,
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
        fallback_permissions=permissions,
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
def list_config_databases() -> dict[str, Any]:
    """List databases defined in mcp-config.json in the current working directory."""
    config_path = os.path.join(os.getcwd(), "mcp-config.json")
    if not os.path.exists(config_path):
        return {
            "ok": False,
            "error": "mcp-config.json not found in current directory.",
            "cwd": os.getcwd()
        }
    
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
        
        databases = config.get("databases", [])
        # Return a sanitized list for the agent to choose from
        sanitized = []
        for db in databases:
            sanitized.append({
                "name": db.get("name"),
                "description": db.get("description"),
                "has_url": bool(db.get("database_url"))
            })
        
        return {"ok": True, "databases": sanitized}
    except Exception as e:
        return {"ok": False, "error": f"Failed to read config: {str(e)}"}


@mcp.tool()
@_safe_tool
def connect_to_config_database(
    name: str,
    schema: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows: int | None = None,
    label: str | None = None,
    allow_read: bool = True,
    allow_insert: bool = False,
    allow_update: bool = False,
    allow_delete: bool = False,
    allow_create: bool = False,
    allow_drop: bool = False,
) -> dict[str, Any]:
    """Connect to a database defined in mcp-config.json by its name."""
    config_path = os.path.join(os.getcwd(), "mcp-config.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"mcp-config.json not found in {os.getcwd()}")
    
    with open(config_path, "r") as f:
        config = json.load(f)
    
    target = next((db for db in config.get("databases", []) if db.get("name") == name), None)
    if not target:
        raise ValueError(f"Database '{name}' not found in mcp-config.json")
    
    db_url = target.get("database_url")
    if not db_url:
        raise ValueError(f"Database '{name}' has no database_url defined")

    # Use permissions from config if not explicitly overridden by tool call
    # Defaulting to what's in the config OR the tool arguments
    p_read = target.get("allow_read", allow_read)
    p_insert = target.get("allow_insert", allow_insert)
    p_update = target.get("allow_update", allow_update)
    p_delete = target.get("allow_delete", allow_delete)
    p_create = target.get("allow_create", allow_create)
    p_drop = target.get("allow_drop", allow_drop)

    return create_session(
        database_url=db_url,
        schema=schema or target.get("schema"),
        statement_timeout_ms=statement_timeout_ms or target.get("statement_timeout_ms"),
        max_rows=max_rows or target.get("max_rows"),
        label=label or name,
        allow_read=p_read,
        allow_insert=p_insert,
        allow_update=p_update,
        allow_delete=p_delete,
        allow_create=p_create,
        allow_drop=p_drop,
    )


@mcp.tool()
@_safe_tool
def get_session_info(session_token: str) -> dict[str, Any]:
    """Check the details and active permissions of a specific session token."""
    session = SESSION_STORE.get(session_token)
    return {"ok": True, "session": session.to_public_payload()}


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
    engine = _get_engine(context)
    inspector = inspect(engine)
    try:
        tables = inspector.get_table_names(schema=context.schema)
        rows = [{"table_schema": context.schema, "table_name": t, "table_type": "BASE TABLE"} for t in tables]
    except Exception:
        rows = []

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
    engine = _get_engine(context)
    inspector = inspect(engine)
    try:
        views = inspector.get_view_names(schema=context.schema)
        rows = [{"table_schema": context.schema, "table_name": v} for v in views]
    except Exception:
        rows = []

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
        where routine_schema = :schema
          and routine_type = 'FUNCTION'
    """
    try:
        with _connect(context) as conn:
            result = conn.execute(text(sql), {"schema": context.schema})
            rows = [dict(r._mapping) for r in result.fetchall()]
    except Exception:
        rows = []

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
    engine = _get_engine(context)
    inspector = inspect(engine)
    fks = inspector.get_foreign_keys(table_name, schema=schema_name)
    rows = []
    for fk in fks:
        for idx, col in enumerate(fk.get("constrained_columns", [])):
            if idx < len(fk.get("referred_columns", [])):
                rel_col = fk["referred_columns"][idx]
                rows.append({
                    "relation_direction": "outgoing",
                    "table_schema": schema_name,
                    "table_name": table_name,
                    "column_name": col,
                    "related_table_schema": fk.get("referred_schema") or schema_name,
                    "related_table_name": fk.get("referred_table"),
                    "related_column_name": rel_col,
                    "constraint_name": fk.get("name")
                })

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
    engine = _get_engine(context)
    inspector = inspect(engine)
    
    rows = []
    try:
        all_tables = inspector.get_table_names(schema=schema_name)
        for other_table in all_tables:
            fks = inspector.get_foreign_keys(other_table, schema=schema_name)
            for fk in fks:
                if fk.get("referred_table") == table_name:
                    for idx, col in enumerate(fk.get("constrained_columns", [])):
                        if idx < len(fk.get("referred_columns", [])):
                            rel_col = fk["referred_columns"][idx]
                            rows.append({
                                "relation_direction": "incoming",
                                "table_schema": schema_name,
                                "table_name": other_table,
                                "column_name": col,
                                "related_table_schema": fk.get("referred_schema") or schema_name,
                                "related_table_name": table_name,
                                "related_column_name": rel_col,
                                "constraint_name": fk.get("name")
                            })
    except Exception:
        pass

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
    referenced = list_referenced_tables(table, schema, session_token)
    referencing = list_referencing_tables(table, schema, session_token)
    
    referenced_rows = [{"relation_direction": "outgoing", "related_table_schema": r["related_table_schema"], "related_table_name": r["related_table_name"]} for r in referenced.get("referenced_tables", [])]
    referencing_rows = [{"relation_direction": "incoming", "related_table_schema": r["table_schema"], "related_table_name": r["table_name"]} for r in referencing.get("referencing_tables", [])]
    
    schema_name, table_name = _qualified_name(schema or "public", table)

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
    referenced = list_referenced_tables(table, schema, session_token)
    referencing = list_referencing_tables(table, schema, session_token)
    schema_name, table_name = _qualified_name(schema or "public", table)

    return {
        "schema": schema_name,
        "table": table_name,
        "referenced_count": referenced.get("count", 0),
        "referencing_count": referencing.get("count", 0),
        "referenced_tables": referenced.get("referenced_tables", []),
        "referencing_tables": referencing.get("referencing_tables", []),
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
    engine = _get_engine(context)
    inspector = inspect(engine)
    
    try:
        columns = inspector.get_columns(table_name, schema=schema_name)
        rows = [
            {
                "column_name": col["name"],
                "data_type": str(col["type"]),
                "is_nullable": "YES" if col.get("nullable", True) else "NO",
                "column_default": col.get("default")
            }
            for col in columns
        ]
    except Exception:
        rows = []

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
    """Run an SQL query against the database, respecting the session's active permissions."""
    context = _resolve_request_context(
        session_token=session_token,
        schema=schema,
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
    )
    safe_sql = _validate_sql_permissions(sql, context.permissions)
    params = _parse_params(params_json)
    
    row_limit = min(max_rows or context.max_rows, context.max_rows)

    with _connect(context) as conn:
        with conn.begin(): # Use explicit transaction so writings are auto-committed.
            result = conn.exec_driver_sql(safe_sql, tuple(params) if params else None)
            column_names = list(result.keys()) if result.returns_rows else []
            rows = []
            if result.returns_rows:
                fetched = result.fetchmany(row_limit)
                rows = [tuple(r) for r in fetched]

    return {
        "row_count": len(rows) if result.returns_rows else result.rowcount,
        "truncated": len(rows) == row_limit if result.returns_rows else False,
        "max_rows": row_limit,
        "columns": column_names,
        "rows": rows,
    }


def main() -> None:
    # If standard output is not a TTY and no transport is specified, 
    # we might be running as a subprocess in an IDE, default to stdio.
    default_transport = "streamable-http"
    if not sys.stdin.isatty() and "MCP_TRANSPORT" not in os.environ:
        default_transport = "stdio"

    transport = os.environ.get("MCP_TRANSPORT", default_transport)

    if transport == "both":
        import threading
        print("Starting MCP server in BOTH stdio and http modes...", file=sys.stderr)
        print(f"HTTP mode listening on port {os.environ.get('MCP_PORT', '3005')}...", file=sys.stderr)
        
        # Run HTTP in a background thread
        http_thread = threading.Thread(
            target=mcp.run, 
            kwargs={"transport": "streamable-http"}, 
            daemon=True
        )
        http_thread.start()
        
        # Run stdio in the main thread (blocking)
        try:
            mcp.run(transport="stdio")
        except Exception as exc:
            print(f"Stdio transport failed: {exc}", file=sys.stderr)
    elif transport == "stdio":
        print("Starting MCP server in stdio mode...", file=sys.stderr)
        try:
            mcp.run(transport=transport)
        except Exception as exc:
            print(f"Stdio transport failed: {exc}", file=sys.stderr)
    else:
        print(f"Starting MCP server in {transport} mode on port {os.environ.get('MCP_PORT', '3005')}...", file=sys.stderr)
        try:
            mcp.run(transport=transport)
        except Exception as exc:
            print(f"HTTP transport failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
