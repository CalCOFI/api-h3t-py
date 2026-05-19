# api-h3t (FastAPI)

A Python port of [CalCOFI/api-h3t](https://github.com/CalCOFI/api-h3t),
the **h3t tile server**: takes a base64-encoded SQL `SELECT` against a
read-only [DuckDB](https://duckdb.org) file and returns
[h3j](https://github.com/INSPIDE/h3j-h3t)-format JSON tiles suitable for the
`h3tiles://` MapLibre protocol.

Drop-in companion to [`mapgl::add_h3t_source()`][add_h3t_source]. Together they
replace *preload-every-hexagon-at-every-resolution* with
*fetch-just-the-cells-in-view*, for any DuckDB dataset that has H3 cells
per row.

This rewrite preserves the wire protocol exactly — same endpoints, same
JSON, same SQL contract — and adds:

- **`?db=` multi-database support** with backward-compatible default.
- **Async concurrency** — concurrent cache-miss tile requests no longer
  serialize per worker.
- **Native sqlglot** — no reticulate bridge, no R+Python Docker image.
- **`Vary: Accept-Encoding`** so Varnish can gzip the response.

## How it works

```
 MapLibre (h3tiles:// protocol)                         h3t tile server
     │                                                        │
     │ GET  /h3t/{z}/{x}/{y}.h3t ?q=<base64(SELECT ...)>      │
     │ ────────────────────────────────────────────────────►  │
     │                                                        │ sqlglot validate
     │                                                        │ wrap with bbox +
     │                                                        │ h3_h3_to_string +
     │                                                        │ LIMIT
     │                                                        │ run vs read-only DuckDB
     │ { "cells": [{ h3id, value, n }, …] }                   │
     │ ◄────────────────────────────────────────────────────  │
```

## The SQL contract

Your `SELECT` must project **exactly** these columns (extras are rejected):

| column    | type    | required | purpose                                                    |
|-----------|---------|----------|------------------------------------------------------------|
| `cell_id` | BIGINT  | yes      | H3 cell index (use `hex_h3resN` or `h3_latlng_to_cell`)    |
| `value`   | numeric | yes      | the value the map colorizes                                |
| `n`       | BIGINT  | optional | count / weight passed through to the client                |

Conveniences:

- `{{res}}` placeholder — substituted with the H3 resolution for each tile
  (derived from zoom). Use it once in your SELECT (e.g. `hex_h3res{{res}}`)
  and one cached query serves every zoom level.
- An outer BBox + row-cap is added automatically — you don't write
  `WHERE lon BETWEEN …` yourself.

Full SQL freedom otherwise: `JOIN`, `WITH`, window functions, subqueries.

## Endpoints

All GET, all return JSON.

| route | description |
|---|---|
| `/h3t/{z}/{x}/{y}.h3t ?q=<b64>[&res_h3=N][&release=v][&db=name]` | tile in [h3j cells format](https://github.com/INSPIDE/h3j-h3t#h3j) |
| `/h3t/stats ?q=<b64>[&res_h3=N][&release=v][&db=name]` | `{min, max, p02, p98, n}` for color-ramp construction |
| `/h3t/meta [?db=name]` | DuckDB file metadata + table list + zoom→res mapping |
| `/h3t/health` | liveness; lists registered DBs and mtimes |

### Multi-database

Pass `?db=name` on any endpoint to query a non-default database. Omit it
and the default DB is used — fully backward-compatible with the R service.

### Response headers

- `Cache-Control: public, max-age=600`
- `ETag: W/"<sha256(db|q|z|x|y|res|release|db_mtime)>"`
- `Vary: Accept-Encoding`
- `X-Calcofi-Release: <release>` — echoed
- `X-Calcofi-Db-Mtime: <db mtime>` — echoed

## Configuration

| env var | default | purpose |
|---|---|---|
| `H3T_DBS` | *(unset)* | registry of DBs: `"name1:/path/a.duckdb,name2:/path/b.duckdb"` |
| `H3T_DEFAULT_DB` | *(first entry)* | name used when `?db=` is omitted |
| `DUCKDB_PATH` | *(unset)* | legacy single-DB fallback; used only if `H3T_DBS` is unset, treated as `{"default": DUCKDB_PATH}` |
| `H3T_PORT` | `8889` | listen port |
| `H3T_HOST` | `0.0.0.0` | bind host |
| `H3T_MAX_ROWS` | `50000` | row cap per tile |
| `H3T_STMT_TIMEOUT_MS` | `3000` | per-request wall-clock timeout |
| `H3T_CORS_ORIGIN` | `*` | CORS allow-origin (set to a specific origin in production) |
| `H3T_APP_GZIP` | `false` | enable FastAPI gzip middleware (off by default; Varnish handles it upstream) |

Either `H3T_DBS` or `DUCKDB_PATH` must be set; the server fails fast on
boot otherwise.

## Quickstart — Docker

```bash
git clone https://github.com/CalCOFI/api-h3t-py.git
cd api-h3t-py

docker build -t api-h3t-py .

# legacy single-DB mode
docker run --rm -p 8889:8889 \
  -e DUCKDB_PATH=/data/my.duckdb \
  -v "$(pwd)/path/to/data:/data:ro" \
  api-h3t-py

# multi-DB registry
docker run --rm -p 8889:8889 \
  -e H3T_DBS="default:/data/release.duckdb,wrangling:/data/wrangling.duckdb" \
  -e H3T_DEFAULT_DB=default \
  -v "$(pwd)/data:/data:ro" \
  api-h3t-py
```

Make a request:

```bash
SQL="SELECT hex_h3res{{res}} AS cell_id, COUNT(*) AS value, COUNT(*) AS n
       FROM my_points GROUP BY 1"
Q=$(printf '%s' "$SQL" | base64 | tr -d '\n')

curl -s "http://localhost:8889/h3t/health"
curl -s "http://localhost:8889/h3t/stats?q=$Q" | jq .
curl -s "http://localhost:8889/h3t/4/3/6.h3t?q=$Q&db=wrangling" | jq '.cells | length'
```

## Quickstart — local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

DUCKDB_PATH=$PWD/example/my.duckdb \
  uvicorn app.main:app --port 8889 --reload
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Parity vs R is enforced by regenerated golden values:

```bash
Rscript scripts/generate_r_golden.R ../api-h3t tests/fixtures/r_golden.json
pytest tests/test_h3t_query.py
```

## Security model

Defence in depth:

1. **sqlglot AST validation** before execution (verbatim port of
   `sql_validate.py` from the R service):
   - exactly one statement; root must be `SELECT` (optionally wrapped in
     `WITH` / `WITH RECURSIVE`);
   - projections must be `{cell_id, value, [n]}` — rejects `SELECT *`;
   - denylist covers `read_csv/read_parquet/read_json/read_blob`, `attach`,
     `detach`, `load_extension`, `install_extension`, `pg_*`, `mysql_*`,
     `sqlite_scan`, `shell`, `system`, and friends;
   - external catalog refs (`postgres.`, `sqlite_*`, `pg_*`) rejected;
   - AST size cap (default 2000 nodes); raw SQL size cap (default 16 KB).
2. **DuckDB connection opened read-only** — writes fail at the driver
   layer even if a write somehow slipped past the validator.
3. **Per-request `statement_timeout`** (default 3 s, set per cursor).
4. **Tile-level row cap** (`H3T_MAX_ROWS`, default 50 000).
5. **Database name is allowlisted**, not a path — no path traversal.

## Migration from the R service

The wire protocol matches the R version with one cutover-time change:

- **ETag format**: the R service hashes an R list via `digest::digest()`,
  which is not portable. This Python service hashes a delimited string:
  `sha256("db|q|z|x|y|res|release|db_mtime")`. The R service needs the
  same change applied to `plumber.R` to keep Varnish entries valid; if
  that's deferred, plan a one-time Varnish cache flush during cutover.
- **New `?db=` query param**: optional; omitting it preserves the existing
  behavior.

## Related work

- **[INSPIDE/h3j-h3t](https://github.com/INSPIDE/h3j-h3t)** — JS client that
  registers the `h3tiles://` protocol and converts JSON cells to MVT.
- **[mapgl][add_h3t_source]** — R-side wrapper.
- **[DuckDB H3 community extension](https://community-extensions.duckdb.org/extensions/h3)**
  — supplies `h3_latlng_to_cell`, `h3_cell_to_lat`, `h3_h3_to_string`, etc.
- **[uber/h3](https://h3geo.org/)** — the H3 spec itself.

## License

MIT. See [LICENSE](LICENSE).

[add_h3t_source]: https://walker-data.com/mapgl/reference/add_h3t_source.html
