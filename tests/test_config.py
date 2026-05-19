"""Tests for env-driven config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import _parse_db_registry, load_db_paths


def test_parse_db_registry_single():
    out = _parse_db_registry("default:/data/a.duckdb")
    assert out == {"default": Path("/data/a.duckdb")}


def test_parse_db_registry_multi():
    out = _parse_db_registry("a:/x.duckdb, b:/y.duckdb")
    assert out == {"a": Path("/x.duckdb"), "b": Path("/y.duckdb")}


def test_parse_db_registry_rejects_missing_separator():
    with pytest.raises(ValueError):
        _parse_db_registry("malformed")


def test_parse_db_registry_rejects_duplicate():
    with pytest.raises(ValueError):
        _parse_db_registry("a:/x.duckdb,a:/y.duckdb")


def test_parse_db_registry_rejects_empty():
    with pytest.raises(ValueError):
        _parse_db_registry("")


def test_load_db_paths_registry_mode(monkeypatch):
    monkeypatch.setenv("H3T_DBS", "main:/a.duckdb,other:/b.duckdb")
    monkeypatch.setenv("H3T_DEFAULT_DB", "other")
    registry, default = load_db_paths()
    assert default == "other"
    assert set(registry) == {"main", "other"}


def test_load_db_paths_default_is_first_when_unset(monkeypatch):
    monkeypatch.setenv("H3T_DBS", "first:/a.duckdb,second:/b.duckdb")
    monkeypatch.delenv("H3T_DEFAULT_DB", raising=False)
    registry, default = load_db_paths()
    assert default == "first"


def test_load_db_paths_invalid_default(monkeypatch):
    monkeypatch.setenv("H3T_DBS", "a:/x.duckdb")
    monkeypatch.setenv("H3T_DEFAULT_DB", "nonexistent")
    with pytest.raises(ValueError):
        load_db_paths()


def test_load_db_paths_legacy_mode(monkeypatch):
    monkeypatch.delenv("H3T_DBS", raising=False)
    monkeypatch.setenv("DUCKDB_PATH", "/legacy.duckdb")
    registry, default = load_db_paths()
    assert registry == {"default": Path("/legacy.duckdb")}
    assert default == "default"


def test_load_db_paths_no_env_fails(monkeypatch):
    monkeypatch.delenv("H3T_DBS", raising=False)
    monkeypatch.delenv("DUCKDB_PATH", raising=False)
    with pytest.raises(SystemExit):
        load_db_paths()
