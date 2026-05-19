"""Per-request helpers: base64 SQL decode, {{res}} substitution, h3j payload
assembly, ETag, cache headers."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Iterable

from fastapi import Response


_RES_PLACEHOLDER = re.compile(r"\{\{\s*res\s*\}\}")


def decode_sql(q: str | None) -> str | None:
    """Base64-decode a SQL query. Returns None on missing/invalid input."""
    if not q:
        return None
    try:
        return base64.b64decode(q, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def substitute_res(sql: str, res_h3: int) -> str:
    """Replace `{{res}}` placeholders with an integer H3 resolution.

    Lets clients bake `hex_h3res{{res}}` into a single base64 query that
    covers every zoom level — the server fills in the actual resolution.
    """
    return _RES_PLACEHOLDER.sub(str(int(res_h3)), sql)


def compute_etag(
    db_name: str,
    q: str,
    z: int,
    x: int,
    y: int,
    res_h3: int,
    release: str,
    db_mtime: str,
) -> str:
    """Stable SHA-256 over a delimited string of cache-relevant inputs.

    The R service hashes the same logical tuple via digest::digest() on an
    R list, which uses R's internal serialization. We adopt a stable
    string-based scheme that both services can match (R update is part of
    the cutover); see plan §5.
    """
    payload = f"{db_name}|{q}|{z}|{x}|{y}|{res_h3}|{release}|{db_mtime}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_stats_etag(db_name: str, q: str, release: str, db_mtime: str) -> str:
    payload = f"stats|{db_name}|{q}|{release}|{db_mtime}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def set_cache_headers(
    response: Response,
    etag: str,
    release: str = "",
    db_mtime: str = "",
    max_age: int = 600,
) -> None:
    response.headers["Cache-Control"] = f"public, max-age={int(max_age)}"
    response.headers["ETag"] = f'W/"{etag}"'
    # Vary on Accept-Encoding so Varnish caches gzip and non-gzip variants
    # separately when it does the compression upstream.
    response.headers["Vary"] = "Accept-Encoding"
    if release:
        response.headers["X-Calcofi-Release"] = release
    if db_mtime:
        response.headers["X-Calcofi-Db-Mtime"] = db_mtime


def build_cells(
    columns: list[str], rows: Iterable[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    """Convert wrapped-query rows into the h3j `cells` payload.

    Expects columns ['h3id', 'value', 'n']. Drops 'n' entries that are None
    so the JSON omits the field rather than emitting `null` (matches the
    R behavior of skipping NA n values).
    """
    try:
        i_h3id = columns.index("h3id")
        i_value = columns.index("value")
        i_n = columns.index("n")
    except ValueError as e:
        raise RuntimeError(
            f"wrapped query did not project expected columns; got {columns}"
        ) from e

    out: list[dict[str, Any]] = []
    for r in rows:
        cell: dict[str, Any] = {"h3id": r[i_h3id], "value": r[i_value]}
        n = r[i_n]
        if n is not None:
            cell["n"] = int(n)
        out.append(cell)
    return out
