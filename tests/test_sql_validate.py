"""SQL validator tests — the security-critical surface.

The validator is a verbatim copy of api-h3t/sql_validate.py. These tests
codify the existing contract.
"""

from __future__ import annotations

import pytest

from app.sql_validate import MAX_AST_NODES, MAX_SQL_BYTES, validate


# --- happy paths ---------------------------------------------------------

def test_minimal_select_passes():
    v = validate("SELECT 1 AS cell_id, 2 AS value")
    assert v["ok"], v
    assert v["has_n"] is False
    assert "cell_id" in v["normalized"].lower()


def test_select_with_n_marks_has_n():
    v = validate("SELECT hex_h3res5 AS cell_id, AVG(x) AS value, COUNT(*) AS n FROM t GROUP BY 1")
    assert v["ok"], v
    assert v["has_n"] is True


def test_cte_passes():
    v = validate(
        "WITH t AS (SELECT 1 AS a) "
        "SELECT a AS cell_id, a AS value FROM t"
    )
    assert v["ok"], v


# --- rejected statements -------------------------------------------------

def test_empty_rejected():
    assert validate("")["ok"] is False
    assert validate(None)["ok"] is False  # type: ignore[arg-type]


def test_oversize_rejected():
    huge = "SELECT " + ",".join(f"{i} AS c{i}" for i in range(2000)) + ", 1 AS cell_id, 2 AS value"
    if len(huge.encode()) > MAX_SQL_BYTES:
        assert validate(huge)["ok"] is False


def test_insert_rejected():
    v = validate("INSERT INTO t VALUES (1)")
    assert v["ok"] is False
    assert "select" in v["reason"].lower()


def test_drop_rejected():
    v = validate("DROP TABLE t")
    assert v["ok"] is False


def test_attach_rejected():
    v = validate("ATTACH 'other.db' AS o")
    assert v["ok"] is False


def test_two_statements_rejected():
    v = validate("SELECT 1 AS cell_id, 2 AS value; SELECT 3")
    assert v["ok"] is False


def test_missing_required_projection_rejected():
    v = validate("SELECT 1 AS cell_id")
    assert v["ok"] is False
    assert "value" in v["reason"]


def test_select_star_rejected():
    v = validate("SELECT * FROM t")
    assert v["ok"] is False


def test_extra_projection_rejected():
    v = validate("SELECT 1 AS cell_id, 2 AS value, 3 AS surprise")
    assert v["ok"] is False
    assert "surprise" in v["reason"]


def test_read_csv_rejected():
    v = validate("SELECT cell_id, value FROM read_csv('evil.csv')")
    assert v["ok"] is False
    assert "read_csv" in v["reason"]


def test_postgres_schema_rejected():
    v = validate("SELECT cell_id, value FROM postgres.public.t")
    assert v["ok"] is False
    assert "postgres" in v["reason"]
