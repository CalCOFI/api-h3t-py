"""Shared test fixtures.

Endpoint tests don't need a real DuckDB — they exercise routing, validation,
ETag, CORS, and response shaping. We populate the db module's internal
registry with sentinel connection objects and monkey-patch the execute
functions to return canned data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pytest
from fastapi.testclient import TestClient


class _FakeCon:
    """Sentinel passed through as a connection."""
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture
def app_client(monkeypatch):
    """TestClient with the db layer fully mocked.

    `app.state.canned_rows` and `app.state.canned_one` let individual tests
    set the response from the next DuckDB call.
    """
    from app import db as db_mod
    from app.main import app

    # populate registry with fakes
    monkeypatch.setattr(db_mod, "_CONS",   {"default": _FakeCon("default"),
                                            "wrangling": _FakeCon("wrangling")})
    monkeypatch.setattr(db_mod, "_PATHS",  {"default": Path("/fake/default.duckdb"),
                                            "wrangling": Path("/fake/wrangling.duckdb")})
    monkeypatch.setattr(db_mod, "_MTIMES", {"default": "1700000000.0",
                                            "wrangling": "1700000001.0"})

    # canned responses
    canned: dict[str, Any] = {
        "rows": (["h3id", "value", "n"], []),
        "one":  (["min", "max", "p02", "p98", "n"], None),
        "tables": ["t1", "t2"],
    }

    def fake_init(_dbs):
        return None

    def fake_execute_query(con, sql):
        canned["last_sql"] = sql
        canned["last_con"] = con
        return canned["rows"]

    def fake_execute_query_one(con, sql):
        canned["last_sql"] = sql
        canned["last_con"] = con
        return canned["one"]

    def fake_list_tables(_con):
        return canned["tables"]

    monkeypatch.setattr(db_mod, "init_connections", fake_init)
    monkeypatch.setattr(db_mod, "execute_query", fake_execute_query)
    monkeypatch.setattr(db_mod, "execute_query_one", fake_execute_query_one)
    monkeypatch.setattr(db_mod, "list_tables", fake_list_tables)

    # bypass config.load_db_paths in lifespan — the default_db is set
    # via app.state below; init_connections is a no-op now anyway.
    from app import config as config_mod
    monkeypatch.setattr(config_mod, "load_db_paths",
                        lambda: ({"default": Path("/fake/default.duckdb"),
                                  "wrangling": Path("/fake/wrangling.duckdb")},
                                 "default"))

    with TestClient(app) as client:
        client.app.state.canned = canned  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def b64_sql():
    """Base64-encode a SQL string for use as `?q=`."""
    import base64
    def _enc(sql: str) -> str:
        return base64.b64encode(sql.encode("utf-8")).decode("ascii")
    return _enc
