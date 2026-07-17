"""Automatic per-tile spatial pruning.

The tile's bounding box is fully determined by its `z/x/y` (the Mercator
quadtree), so the client never states it. This module derives the tile's
covering coarse H3 cells from that bbox and rewrites the user SQL to add
`<table>.hex_prune IN (<covering cells>)` to any scan of a table that carries a
`hex_prune` column (the coarse-parent prune key materialized by
obisindicators::build_obis_h3_duckdb at `H3T_PRUNE_RES`). DuckDB then prunes row
groups to the tile instead of aggregating the whole layer per tile.

Covering cells are computed with a dedicated in-memory DuckDB that loads the
same `h3` community extension the store was built with, so the covering ids
match the stored `hex_prune` exactly (H3 indices are canonical, and
duckdb-python returns BIGINT as exact Python ints — no precision drift). The
rewrite is best-effort: any parse/rewrite failure falls back to the unpruned SQL
(correctness is still guaranteed by the outer centroid filter in
`wrap_tile_sql`; only the speed-up is lost).
"""

from __future__ import annotations

import functools
import math
import threading

import duckdb
import sqlglot
from sqlglot import exp

_cover_con: duckdb.DuckDBPyConnection | None = None
_cover_lock = threading.Lock()


def _con() -> duckdb.DuckDBPyConnection:
    global _cover_con
    if _cover_con is None:
        c = duckdb.connect()
        c.execute("INSTALL h3 FROM community; LOAD h3;")
        _cover_con = c
    return _cover_con


def _edge_deg(r: int) -> float:
    # average H3 cell edge length in degrees at resolution r (≈ res-0 1106.54 km,
    # shrinking by 1/sqrt(7) per level; 1° ≈ 111.32 km at the equator).
    return 1106.54 / (math.sqrt(7) ** int(r)) / 111.32


@functools.lru_cache(maxsize=8192)
def covering_cells(
    prune_res: int,
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
) -> tuple[int, ...]:
    """Covering res-`prune_res` H3 cell ids (BIGINT) for a bbox.

    Buffered by ~2 cell edges so the set is a SUPERSET of the coarse parent of
    any base/display cell whose centroid lands in the (unbuffered) tile — the
    prune then never drops or under-counts a cell the outer filter would keep.
    Returns an empty tuple (→ caller skips injection) if the buffered box would
    cross the antimeridian, so those rare tiles fall back to a whole scan rather
    than risk dropping wrapped cells. Cached: covering is deterministic per bbox.
    """
    b = _edge_deg(prune_res) * 2.0
    lm, lM = lon_min - b, lon_max + b
    am, aM = max(-89.9, lat_min - b), min(89.9, lat_max + b)
    if lm < -180.0 or lM > 180.0:
        return ()  # antimeridian-crossing buffered box → skip (rare)
    wkt = (f"POLYGON(({lm:.8f} {am:.8f},{lM:.8f} {am:.8f},"
           f"{lM:.8f} {aM:.8f},{lm:.8f} {aM:.8f},{lm:.8f} {am:.8f}))")
    with _cover_lock:
        cur = _con().cursor()
        try:
            rows = cur.execute(
                "SELECT UNNEST(h3_polygon_wkt_to_cells(?, ?))::BIGINT",
                [wkt, int(prune_res)],
            ).fetchall()
        finally:
            cur.close()
    return tuple(r[0] for r in rows)


def inject_prune(
    sql: str,
    prune_tables: dict[str, str],
    cover_ints: tuple[int, ...],
) -> tuple[str, bool]:
    """Add `<t>.<col> IN (<cover_ints>)` to each scan of a table in prune_tables.

    `prune_tables` maps lower-cased table name → prune column (e.g.
    {"occ_h3": "hex_prune"}). Returns (sql, injected). Best-effort: returns the
    original sql unchanged on empty inputs or any parse/rewrite error.
    """
    if not cover_ints or not prune_tables:
        return sql, False
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        return sql, False
    in_list = "(" + ",".join(str(int(i)) for i in cover_ints) + ")"
    injected = False
    for tbl in tree.find_all(exp.Table):
        col = prune_tables.get((tbl.name or "").lower())
        if not col:
            continue
        sel = tbl.find_ancestor(exp.Select)
        if sel is None:
            continue
        qual = tbl.alias_or_name  # qualify by alias if the scan is aliased
        try:
            sel.where(f"{qual}.{col} IN {in_list}", append=True, copy=False,
                      dialect="duckdb")
            injected = True
        except Exception:
            continue
    if not injected:
        return sql, False
    try:
        return tree.sql(dialect="duckdb"), True
    except Exception:
        return sql, False
