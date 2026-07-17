"""FastAPI app — h3t tile API.

Endpoints (mirror the R Plumber service):
  GET /h3t/{z}/{x}/{y}.h3t ?q=<b64>[&res_h3=N][&release=v][&db=name] → h3j tile
  GET /h3t/stats           ?q=<b64>[&res_h3=N][&release=v][&db=name] → value summary
  GET /h3t/meta                                          [&db=name]  → schema + release
  GET /h3t/health                                                    → liveness
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, db, h3t_query, prune, tiles
from .sql_validate import validate as validate_sql

log = logging.getLogger("h3t")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    registry, default_name = config.load_db_paths()
    db.init_connections(registry)
    app.state.default_db = default_name
    log.info("h3t ready: dbs=%s default=%s", db.db_names(), default_name)
    yield


app = FastAPI(title="api-h3t", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.CORS_ORIGIN],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type", "If-None-Match"],
    expose_headers=["ETag", "X-Calcofi-Release", "X-Calcofi-Db-Mtime", "X-Cache"],
    max_age=600,
)

if config.APP_GZIP:
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)


# --- error responses (parity with R service shape) -----------------------

@app.exception_handler(HTTPException)
async def _http_exception(_req: Request, exc: HTTPException) -> JSONResponse:
    # match the R service shape: {"error": "bad_request", "reason": "..."}
    # for 4xx (except 422); upgrade 422 → 400 with reason.
    code = exc.status_code
    if code == 400:
        return JSONResponse(
            status_code=400,
            content={"error": "bad_request", "reason": str(exc.detail)},
        )
    if code == 500:
        return JSONResponse(
            status_code=500,
            content={"error": "query_failed", "reason": str(exc.detail)},
        )
    return JSONResponse(status_code=code, content={"reason": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def _validation_exception(
    _req: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic validation errors → 400 with a flattened reason, matching R.
    errs = exc.errors()
    reason = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errs)
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "reason": reason},
    )


# --- helpers -------------------------------------------------------------

def _resolve_db(db_arg: str | None) -> str:
    return db_arg or app.state.default_db


def _validate_q(q: str | None, res_h3: int | None) -> dict:
    sql = tiles.decode_sql(q)
    if sql is None:
        raise HTTPException(400, "q is required and must be valid base64")
    if res_h3 is not None:
        sql = tiles.substitute_res(sql, res_h3)
    # backward-compat: strip any {{bbox}} token from cached URLs minted by the
    # earlier placeholder clients (the server now auto-injects hex_prune). Must
    # run before validate_sql, which parses + re-serializes via sqlglot.
    sql = tiles.strip_bbox_placeholder(sql)
    v = validate_sql(sql)
    if not v.get("ok"):
        raise HTTPException(400, v.get("reason") or "invalid SQL")
    return v


# --- endpoints -----------------------------------------------------------

@app.get("/h3t/health")
async def health() -> dict:
    return {
        "ok": True,
        "default_db": app.state.default_db,
        "dbs": {
            name: {"path": str(db.db_path(name)), "mtime": db.db_mtime(name)}
            for name in db.db_names()
        },
    }


@app.get("/h3t/{z}/{x}/{y}.h3t")
async def tile(
    z: int, x: int, y: int,
    response: Response,
    q: str = Query(..., description="base64-encoded user SELECT"),
    res_h3: int | None = Query(None, ge=1, le=10),
    release: str = "",
    db_name: str | None = Query(None, alias="db"),
) -> dict:
    try:
        bbox = h3t_query.tile_bbox(z, x, y)
    except ValueError as e:
        raise HTTPException(400, f"invalid z/x/y: {e}") from e

    qres = res_h3 if res_h3 is not None else h3t_query.zoom_to_res(z)
    if not 1 <= qres <= 10:
        raise HTTPException(400, "res_h3 must be in [1, 10]")

    name = _resolve_db(db_name)
    con = db.get_connection(name)
    db_mtime = db.db_mtime(name)

    v = _validate_q(q, qres)

    # automatic per-tile spatial prune: derive the tile's covering coarse H3
    # cells from z/x/y and inject `hex_prune IN (...)` into any scan of a table
    # that carries hex_prune. Skipped for tiles coarser than the prune res or
    # when the covering set is too large; correctness is always held by the outer
    # centroid filter below, so injection is a pure speed-up. Stores without a
    # hex_prune column (e.g. CalCOFI bio_obs/env_obs) are never injected.
    inner = v["normalized"]
    ptables = db.prune_tables(name)
    if ptables and qres >= config.PRUNE_RES:
        cover = prune.covering_cells(
            config.PRUNE_RES, bbox.lon_min, bbox.lon_max, bbox.lat_min, bbox.lat_max)
        if cover and len(cover) <= config.MAX_COVER_CELLS:
            inner, _ = prune.inject_prune(inner, ptables, cover)

    wrapped = h3t_query.wrap_tile_sql(
        inner, bbox, has_n=bool(v.get("has_n")),
        max_rows=config.MAX_ROWS_PER_TILE,
        buffer_deg=h3t_query.h3_edge_length_deg(qres) * 1.5,
    )

    try:
        cols, rows = await asyncio.wait_for(
            run_in_threadpool(db.execute_query, con, wrapped),
            timeout=config.STMT_TIMEOUT_MS / 1000,
        )
    except asyncio.TimeoutError:
        log.warning("tile query timeout (>%dms)", config.STMT_TIMEOUT_MS)
        raise HTTPException(504, "query timeout")
    except Exception as e:
        log.exception("tile query failed")
        raise HTTPException(500, str(e)) from e

    etag = tiles.compute_etag(name, q, z, x, y, qres, release, db_mtime)
    tiles.set_cache_headers(response, etag, release, db_mtime)
    return {"cells": tiles.build_cells(cols, rows)}


@app.get("/h3t/stats")
async def stats(
    response: Response,
    q: str = Query(...),
    release: str = "",
    res_h3: int = Query(5, ge=1, le=10),
    db_name: str | None = Query(None, alias="db"),
) -> dict:
    name = _resolve_db(db_name)
    con = db.get_connection(name)
    db_mtime = db.db_mtime(name)

    v = _validate_q(q, res_h3)
    wrapped = h3t_query.wrap_stats_sql(v["normalized"])

    try:
        cols, row = await asyncio.wait_for(
            run_in_threadpool(db.execute_query_one, con, wrapped),
            timeout=config.STMT_TIMEOUT_MS / 1000,
        )
    except asyncio.TimeoutError:
        log.warning("stats query timeout (>%dms)", config.STMT_TIMEOUT_MS)
        raise HTTPException(504, "query timeout")
    except Exception as e:
        log.exception("stats query failed")
        raise HTTPException(500, str(e)) from e

    etag = tiles.compute_stats_etag(name, q, release, db_mtime)
    tiles.set_cache_headers(response, etag, release, db_mtime)

    body: dict = dict(zip(cols, row)) if row is not None else dict.fromkeys(cols)
    body["release"] = release
    body["db_mtime"] = db_mtime
    return body


@app.get("/h3t/meta")
async def meta(
    response: Response,
    db_name: str | None = Query(None, alias="db"),
) -> dict:
    name = _resolve_db(db_name)
    con = db.get_connection(name)
    tables = await run_in_threadpool(db.list_tables, con)
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["Vary"] = "Accept-Encoding"
    return {
        "db": name,
        "db_mtime": db.db_mtime(name),
        "tables": tables,
        # per-row H3 index convention: a single res-10 `hex_id` column; derive
        # coarser cells at query time with h3_cell_to_parent(hex_id, {res}).
        # Replaces the old wide hex_h3res1..10 columns retired in the
        # core-schema consolidation (release v2026.07.15+).
        "h3_index_column": "hex_id",
        "h3_index_resolution": 10,
        "h3_parent_expr": "h3_cell_to_parent(hex_id, {{res}})",
        "default_zoom_breaks": h3t_query.h3t_zoom_breaks,
        "available_dbs": db.db_names(),
        "default_db": app.state.default_db,
    }
