"""DuckDB connection registry and query execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

_CONS: dict[str, duckdb.DuckDBPyConnection] = {}
_MTIMES: dict[str, str] = {}
_PATHS: dict[str, Path] = {}
# per-db map {table_name_lower: prune_column} for tables carrying `hex_prune`
# — the automatic per-tile spatial prune targets (see app/prune.py).
_PRUNE: dict[str, dict[str, str]] = {}


def init_connections(dbs: dict[str, Path]) -> None:
    """Open one read-only connection per registered DB at startup.

    Fail-fast: any missing file aborts the process. Loads the h3 community
    extension and the spatial extension on each connection.
    """
    for name, path in dbs.items():
        if not path.exists():
            raise SystemExit(f"db '{name}': file not found at {path}")
        con = duckdb.connect(str(path), read_only=True)
        con.execute("INSTALL h3 FROM community; LOAD h3;")
        con.execute("INSTALL spatial; LOAD spatial;")
        _CONS[name]   = con
        _PATHS[name]  = path
        _MTIMES[name] = _mtime_str(path)
        _PRUNE[name]  = _detect_prune_tables(con)


def _detect_prune_tables(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """{table_lower: 'hex_prune'} for main-schema tables carrying it.

    These are the tables the per-tile spatial prune injects into (app/prune.py);
    introspected once at startup. Stores without hex_prune yield an empty map.
    """
    rows = con.execute(
        "SELECT lower(table_name) FROM information_schema.columns "
        "WHERE table_schema = 'main' AND lower(column_name) = 'hex_prune'"
    ).fetchall()
    return {r[0]: "hex_prune" for r in rows}


def prune_tables(name: str) -> dict[str, str]:
    """The {table: prune_col} map for a db (empty if none)."""
    return _PRUNE.get(name, {})


def _mtime_str(path: Path) -> str:
    # epoch seconds with microsecond precision. R side uses
    # sprintf("%.6f", as.numeric(file.info(path)$mtime)) so the format
    # matches byte-for-byte and the ETag is reproducible across services.
    return f"{path.stat().st_mtime:.6f}"


def get_connection(name: str) -> duckdb.DuckDBPyConnection:
    if name not in _CONS:
        # raise the same shape as FastAPI's HTTPException so the route can
        # let it bubble up. We don't import HTTPException here to keep this
        # module HTTP-agnostic.
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"unknown db {name!r}; available: {sorted(_CONS)}",
        )
    return _CONS[name]


def db_mtime(name: str) -> str:
    return _MTIMES[name]


def db_path(name: str) -> Path:
    return _PATHS[name]


def db_names() -> list[str]:
    return sorted(_CONS)


def execute_query(
    con: duckdb.DuckDBPyConnection,
    sql: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run `sql` on `con` via a fresh cursor, returning (columns, rows).

    `cursor()` is cheap; the underlying connection is shared. The route
    layer enforces a wall-clock timeout via asyncio.wait_for().
    """
    cur = con.cursor()
    try:
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return cols, rows
    finally:
        cur.close()


def execute_query_one(
    con: duckdb.DuckDBPyConnection,
    sql: str,
) -> tuple[list[str], tuple[Any, ...] | None]:
    """Variant returning the first row only (or None)."""
    cur = con.cursor()
    try:
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        row  = cur.fetchone()
        return cols, row
    finally:
        cur.close()


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()
