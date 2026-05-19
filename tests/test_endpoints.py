"""FastAPI endpoint tests with mocked DuckDB layer."""

from __future__ import annotations

import pytest


VALID_SQL = "SELECT hex_h3res5 AS cell_id, AVG(x) AS value FROM t GROUP BY 1"
VALID_SQL_WITH_N = (
    "SELECT hex_h3res5 AS cell_id, AVG(x) AS value, COUNT(*) AS n FROM t GROUP BY 1"
)


# --- health --------------------------------------------------------------

def test_health(app_client):
    r = app_client.get("/h3t/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["default_db"] == "default"
    assert set(body["dbs"].keys()) == {"default", "wrangling"}


# --- meta ----------------------------------------------------------------

def test_meta_default_db(app_client):
    r = app_client.get("/h3t/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "default"
    assert body["tables"] == ["t1", "t2"]
    assert body["available_dbs"] == ["default", "wrangling"]
    assert body["h3_columns_per_row"] == [f"hex_h3res{r}" for r in range(1, 11)]


def test_meta_with_db_param(app_client):
    r = app_client.get("/h3t/meta?db=wrangling")
    assert r.status_code == 200
    assert r.json()["db"] == "wrangling"


def test_meta_unknown_db(app_client):
    r = app_client.get("/h3t/meta?db=bogus")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "bad_request"
    assert "unknown db" in body["reason"]


# --- tile ----------------------------------------------------------------

def test_tile_empty_result(app_client, b64_sql):
    r = app_client.get(f"/h3t/5/3/12.h3t?q={b64_sql(VALID_SQL)}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"cells": []}
    assert r.headers["ETag"].startswith('W/"')
    assert r.headers["Cache-Control"] == "public, max-age=600"
    assert r.headers["Vary"] == "Accept-Encoding"


def test_tile_with_cells(app_client, b64_sql):
    app_client.app.state.canned["rows"] = (
        ["h3id", "value", "n"],
        [("85283473fffffff", 1.5, 10),
         ("85283447fffffff", 2.0, None)],
    )
    r = app_client.get(f"/h3t/5/3/12.h3t?q={b64_sql(VALID_SQL_WITH_N)}")
    assert r.status_code == 200
    cells = r.json()["cells"]
    assert cells[0] == {"h3id": "85283473fffffff", "value": 1.5, "n": 10}
    assert cells[1] == {"h3id": "85283447fffffff", "value": 2.0}


def test_tile_etag_changes_with_db(app_client, b64_sql):
    q = b64_sql(VALID_SQL)
    e_default = app_client.get(f"/h3t/5/3/12.h3t?q={q}").headers["ETag"]
    e_other   = app_client.get(f"/h3t/5/3/12.h3t?q={q}&db=wrangling").headers["ETag"]
    assert e_default != e_other


def test_tile_etag_stable(app_client, b64_sql):
    q = b64_sql(VALID_SQL)
    e1 = app_client.get(f"/h3t/5/3/12.h3t?q={q}").headers["ETag"]
    e2 = app_client.get(f"/h3t/5/3/12.h3t?q={q}").headers["ETag"]
    assert e1 == e2


def test_tile_unknown_db_400(app_client, b64_sql):
    r = app_client.get(f"/h3t/5/3/12.h3t?q={b64_sql(VALID_SQL)}&db=bogus")
    assert r.status_code == 400


def test_tile_bad_xyz_400(app_client, b64_sql):
    r = app_client.get(f"/h3t/5/100/0.h3t?q={b64_sql(VALID_SQL)}")
    assert r.status_code == 400


def test_tile_bad_base64_400(app_client):
    r = app_client.get("/h3t/5/3/12.h3t?q=!!!notb64")
    assert r.status_code == 400


def test_tile_missing_q_422(app_client):
    # request validation → 400 with {error, reason} per the R parity handler
    r = app_client.get("/h3t/5/3/12.h3t")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_tile_invalid_sql_400(app_client, b64_sql):
    r = app_client.get(f"/h3t/5/3/12.h3t?q={b64_sql('DROP TABLE x')}")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "bad_request"
    assert "select" in body["reason"].lower()


def test_tile_res_h3_out_of_range(app_client, b64_sql):
    r = app_client.get(f"/h3t/5/3/12.h3t?q={b64_sql(VALID_SQL)}&res_h3=11")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_tile_substitutes_res_placeholder(app_client, b64_sql):
    sql = "SELECT hex_h3res{{res}} AS cell_id, AVG(x) AS value FROM t GROUP BY 1"
    app_client.get(f"/h3t/5/3/12.h3t?q={b64_sql(sql)}&res_h3=7")
    captured = app_client.app.state.canned["last_sql"]
    assert "hex_h3res7" in captured
    assert "{{res}}" not in captured


# --- stats ---------------------------------------------------------------

def test_stats_with_row(app_client, b64_sql):
    app_client.app.state.canned["one"] = (
        ["min", "max", "p02", "p98", "n"],
        (0.5, 99.5, 1.0, 99.0, 1234),
    )
    r = app_client.get(f"/h3t/stats?q={b64_sql(VALID_SQL)}&release=v1")
    assert r.status_code == 200
    body = r.json()
    assert body["min"] == 0.5
    assert body["max"] == 99.5
    assert body["n"]   == 1234
    assert body["release"] == "v1"


def test_stats_db_param_routes(app_client, b64_sql):
    app_client.app.state.canned["one"] = (
        ["min", "max", "p02", "p98", "n"],
        (1.0, 2.0, 1.1, 1.9, 10),
    )
    r = app_client.get(f"/h3t/stats?q={b64_sql(VALID_SQL)}&db=wrangling")
    assert r.status_code == 200
    # mocked DB layer captures which connection was used
    assert app_client.app.state.canned["last_con"].name == "wrangling"


def test_stats_res_h3_out_of_range(app_client, b64_sql):
    r = app_client.get(f"/h3t/stats?q={b64_sql(VALID_SQL)}&res_h3=99")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


# --- CORS ----------------------------------------------------------------

def test_cors_preflight(app_client):
    r = app_client.options(
        "/h3t/5/3/12.h3t",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # FastAPI's CORSMiddleware returns 200 with the headers set
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-methods")
