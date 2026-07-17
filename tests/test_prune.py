"""Automatic per-tile spatial pruning (app/prune.py) + {{bbox}} strip."""

from __future__ import annotations

import duckdb
import pytest

from app.prune import covering_cells, inject_prune
from app.tiles import strip_bbox_placeholder

PTABLES = {"occ_h3": "hex_prune", "idx_h3": "hex_prune"}
COVER = (100, 200, 300)


# --- strip_bbox_placeholder (backward compat) ----------------------------

def test_strip_bbox_placeholder():
    assert strip_bbox_placeholder("... WHERE res = 5 {{bbox}}") == "... WHERE res = 5 "
    assert strip_bbox_placeholder("SELECT 1") == "SELECT 1"


# --- inject_prune --------------------------------------------------------

def test_inject_prune_adds_predicate_to_spatial_scan():
    sql = "SELECT cell_id, n AS value, n FROM idx_h3 WHERE res = 5"
    out, injected = inject_prune(sql, PTABLES, COVER)
    assert injected and "hex_prune IN (100, 200, 300)" in out.replace('"', "")


def test_inject_prune_targets_only_spatial_tables():
    sql = (
        "WITH RECURSIVE taxon_tree AS ("
        "  SELECT taxonID, parentNameUsageID FROM taxon WHERE taxonID IN (1) "
        "  UNION ALL SELECT t.taxonID, t.parentNameUsageID FROM taxon t "
        "  JOIN taxon_tree tt ON t.parentNameUsageID = tt.taxonID), "
        "src AS (SELECT cell_id, SUM(records) AS ni FROM occ_h3 "
        "  WHERE res = 7 AND aphiaid IN (SELECT taxonID FROM taxon_tree) GROUP BY 1) "
        "SELECT cell_id, ni AS value, ni AS n FROM src"
    )
    out, injected = inject_prune(sql, PTABLES, COVER)
    flat = out.replace('"', "")
    assert injected and flat.count("hex_prune IN") == 1 and "taxon.hex_prune" not in flat


def test_inject_prune_noops():
    sql = "SELECT cell_id, n AS value FROM idx_h3 WHERE res = 5"
    assert inject_prune(sql, PTABLES, ()) == (sql, False)
    assert inject_prune(sql, {}, COVER) == (sql, False)
    tsql = "SELECT cell_id, value, n FROM idx_h3_taxon WHERE rank = 'class'"
    assert inject_prune(tsql, PTABLES, COVER) == (tsql, False)


# --- covering_cells (needs duckdb h3) ------------------------------------

def _h3_ok():
    try:
        duckdb.connect().execute("INSTALL h3 FROM community; LOAD h3;")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _h3_ok(), reason="duckdb h3 extension unavailable")
def test_covering_cells():
    cov = covering_cells(3, -122.5, -121.5, 36.0, 37.0)
    assert len(cov) > 0 and all(0 < c < 2**63 for c in cov)
    assert covering_cells(3, 179.5, 179.9, 0.0, 1.0) == ()  # antimeridian skip
