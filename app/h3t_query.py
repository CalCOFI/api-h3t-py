"""Pure helpers: tile bbox, zoom → H3 resolution, SQL wrappers.

Port of api-h3t/h3t_query.R. No DB access here — easy to unit-test against
R-generated golden values to guarantee byte-for-byte parity.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass


# --- zoom / resolution ---------------------------------------------------

# mirrors int-app/app/global.R:175-181 (and api-h3t/h3t_query.R:7-13)
def _make_zoom_breaks() -> list[float]:
    min_res, max_res = 1, 10
    n_breaks = (max_res - min_res + 1) + 1  # 11
    # match R's seq(1, 13, length.out=11). Multiply before divide to avoid
    # the (12/10)*9 → 10.799999… float drift; (12*9)/10 → 10.8 exactly.
    b = [1.0 + (13 - 1) * i / (n_breaks - 1) for i in range(n_breaks)]
    b[0] = 0.0
    b[-1] = 22.0
    return b


h3t_zoom_breaks: list[float] = _make_zoom_breaks()


def zoom_to_res(z: float) -> int:
    """Map a web-mercator zoom to an H3 resolution in [1, 10].

    Equivalent to R's findInterval(z, breaks, rightmost.closed=TRUE,
    all.inside=TRUE).
    """
    # bisect_right(breaks, z) returns the 1-indexed-in-R bin index for the
    # left-closed-right-open intervals defined by `breaks`. Clamping to
    # [1, 10] absorbs both edge cases (z below the first break and z at or
    # above the last break).
    return max(1, min(10, bisect_right(h3t_zoom_breaks, float(z))))


# --- H3 cell geometry ----------------------------------------------------

# Average H3 cell edge length (degrees). At each resolution the edge shrinks
# by 1/sqrt(7). Res-0 edge ≈ 1106.54 km; 1° ≈ 111.32 km at the equator.
# Used to buffer tile bboxes so cells with centroids just outside a tile
# (but geometry overlapping it) are still returned — prevents seams.
def h3_edge_length_deg(r: int) -> float:
    return 1106.54 / (math.sqrt(7) ** int(r)) / 111.32


# --- tile bbox -----------------------------------------------------------

@dataclass(frozen=True)
class TileBBox:
    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float


def tile_bbox(z: int, x: int, y: int) -> TileBBox:
    """Web Mercator XYZ → lon/lat bbox. y=0 is the north edge."""
    z = int(z); x = int(x); y = int(y)
    n = 2 ** z
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError(f"x/y out of range for z={z}: x={x}, y={y}")
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.atan(math.sinh(math.pi * (1 - 2 * y / n))) * 180.0 / math.pi
    lat_min = math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))) * 180.0 / math.pi
    return TileBBox(lon_min, lon_max, lat_min, lat_max)


# --- SQL wrapping --------------------------------------------------------

# wrap a validated user SELECT as an inner subquery, projecting h3id (hex
# string), value, and optional n, with a bbox filter on the cell centroid
# and a row cap. `has_n` decides whether the inner query emits n.
_TILE_TEMPLATE = (
    "WITH user_q AS (\n"
    "{user_sql}\n"
    "),\n"
    "cells AS (\n"
    "  SELECT\n"
    "    CAST(cell_id AS BIGINT)                 AS _cell,\n"
    "    value::DOUBLE                           AS value,\n"
    "    {n_select}\n"
    "  FROM user_q\n"
    "  WHERE value IS NOT NULL\n"
    "    AND NOT isnan(value::DOUBLE)\n"
    "    AND isfinite(value::DOUBLE)\n"
    ")\n"
    "SELECT\n"
    "  h3_h3_to_string(_cell) AS h3id,\n"
    "  value,\n"
    "  n\n"
    "FROM cells\n"
    # antimeridian-aware: a cell straddling +/-180 must be returned to BOTH
    # edge tiles (its centroid is on one side, but its geometry overhangs the
    # other), so also match the +/-360 wrapped longitude against the (buffered)
    # tile bbox. the client then places each cell on the side of the tile it
    # is rendering.
    "WHERE (h3_cell_to_lng(_cell)       BETWEEN {lm:.10f} AND {lM:.10f}\n"
    "    OR h3_cell_to_lng(_cell) + 360 BETWEEN {lm:.10f} AND {lM:.10f}\n"
    "    OR h3_cell_to_lng(_cell) - 360 BETWEEN {lm:.10f} AND {lM:.10f})\n"
    "  AND h3_cell_to_lat(_cell) BETWEEN {am:.10f} AND {aM:.10f}\n"
    "LIMIT {max_rows:d}"
)


def wrap_tile_sql(
    user_sql: str,
    bbox: TileBBox,
    has_n: bool = False,
    max_rows: int = 50_000,
    buffer_deg: float = 0.0,
) -> str:
    if not isinstance(user_sql, str) or not user_sql:
        raise ValueError("user_sql must be a non-empty string")
    lm = bbox.lon_min - buffer_deg
    lM = bbox.lon_max + buffer_deg
    aM = bbox.lat_max + buffer_deg
    am = bbox.lat_min - buffer_deg
    n_select = "TRY_CAST(n AS BIGINT) AS n" if has_n else "NULL::BIGINT AS n"
    return _TILE_TEMPLATE.format(
        user_sql=user_sql, n_select=n_select,
        lm=lm, lM=lM, am=am, aM=aM,
        max_rows=int(max_rows),
    )


_STATS_TEMPLATE = (
    "WITH user_q AS (\n"
    "{user_sql}\n"
    ")\n"
    "SELECT\n"
    "  MIN(value::DOUBLE)                           AS min,\n"
    "  MAX(value::DOUBLE)                           AS max,\n"
    "  approx_quantile(value::DOUBLE, 0.02)         AS p02,\n"
    "  approx_quantile(value::DOUBLE, 0.98)         AS p98,\n"
    "  COUNT(*)                                     AS n\n"
    "FROM user_q\n"
    "WHERE value IS NOT NULL\n"
    "  AND NOT isnan(value::DOUBLE)\n"
    "  AND isfinite(value::DOUBLE)"
)


def wrap_stats_sql(user_sql: str) -> str:
    if not isinstance(user_sql, str) or not user_sql:
        raise ValueError("user_sql must be a non-empty string")
    return _STATS_TEMPLATE.format(user_sql=user_sql)
