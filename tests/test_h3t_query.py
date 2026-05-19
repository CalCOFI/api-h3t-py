"""h3t_query parity tests vs R-generated golden values.

To regenerate fixtures: `Rscript scripts/generate_r_golden.R ../api-h3t tests/fixtures/r_golden.json`
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.h3t_query import (
    TileBBox,
    h3_edge_length_deg,
    h3t_zoom_breaks,
    tile_bbox,
    wrap_stats_sql,
    wrap_tile_sql,
    zoom_to_res,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads((FIXTURES / "r_golden.json").read_text())


# --- zoom_to_res ---------------------------------------------------------

def test_zoom_breaks_match():
    # 11 breaks: [0, 2.2, 3.4, 4.6, 5.8, 7, 8.2, 9.4, 10.6, 11.8, 22]
    assert len(h3t_zoom_breaks) == 11
    assert h3t_zoom_breaks[0]  == pytest.approx(0.0)
    assert h3t_zoom_breaks[-1] == pytest.approx(22.0)


def test_zoom_to_res_parity(golden):
    zs   = golden["zoom_to_res"]["z"]
    expected = golden["zoom_to_res"]["res"]
    for z, e in zip(zs, expected):
        assert zoom_to_res(z) == e, f"z={z}: python={zoom_to_res(z)} R={e}"


def test_zoom_to_res_clamps_below_and_above():
    assert zoom_to_res(-100) == 1
    assert zoom_to_res(1000) == 10


# --- h3_edge_length_deg --------------------------------------------------

def test_h3_edge_length_parity(golden):
    rs = golden["h3_edge_length_deg"]["r"]
    expected = golden["h3_edge_length_deg"]["deg"]
    for r, e in zip(rs, expected):
        assert h3_edge_length_deg(r) == pytest.approx(e, rel=1e-12, abs=1e-15), \
            f"r={r}"


# --- tile_bbox -----------------------------------------------------------

def test_tile_bbox_parity(golden):
    for case in golden["tile_bbox"]:
        bbox = tile_bbox(case["z"], case["x"], case["y"])
        for fld in ("lon_min", "lon_max", "lat_min", "lat_max"):
            assert getattr(bbox, fld) == pytest.approx(case[fld], abs=1e-9), \
                f"{case}: {fld}"


def test_tile_bbox_rejects_out_of_range():
    with pytest.raises(ValueError):
        tile_bbox(5, 100, 0)
    with pytest.raises(ValueError):
        tile_bbox(5, 0, -1)


def test_tile_bbox_z0_spans_world():
    b = tile_bbox(0, 0, 0)
    assert b.lon_min == -180 and b.lon_max == 180
    # Web Mercator clips latitude at ~85.05°
    assert b.lat_min == pytest.approx(-85.0511287798, abs=1e-9)
    assert b.lat_max == pytest.approx(85.0511287798, abs=1e-9)


# --- wrap_tile_sql snapshot ---------------------------------------------

def test_wrap_tile_sql_has_n_matches_r(golden):
    bbox = tile_bbox(5, 3, 12)
    out = wrap_tile_sql(
        "SELECT hex_h3res5 AS cell_id, AVG(std_tally) AS value, COUNT(*) AS n "
        "FROM bio_obs WHERE species_id = 42 GROUP BY 1",
        bbox, has_n=True, max_rows=50000,
        buffer_deg=h3_edge_length_deg(5) * 1.5,
    )
    assert out == golden["wrap_tile_sql_has_n"]


def test_wrap_tile_sql_no_n_matches_r(golden):
    bbox = tile_bbox(5, 3, 12)
    out = wrap_tile_sql(
        "SELECT hex_h3res5 AS cell_id, AVG(temperature) AS value FROM env_obs",
        bbox, has_n=False, max_rows=50000, buffer_deg=0,
    )
    assert out == golden["wrap_tile_sql_no_n"]


def test_wrap_tile_sql_rejects_empty():
    with pytest.raises(ValueError):
        wrap_tile_sql("", TileBBox(0, 1, 0, 1))


# --- wrap_stats_sql snapshot --------------------------------------------

def test_wrap_stats_sql_matches_r(golden):
    out = wrap_stats_sql(
        "SELECT hex_h3res5 AS cell_id, AVG(std_tally) AS value, COUNT(*) AS n "
        "FROM bio_obs WHERE species_id = 42 GROUP BY 1"
    )
    assert out == golden["wrap_stats_sql"]
