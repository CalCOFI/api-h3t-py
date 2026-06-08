# Deploy api-h3t-py to the CalCOFI server

**Status: cutover complete (2026-06-08).** `h3t.calcofi.io` is served by the
FastAPI port (`CalCOFI/api-h3t-py`, container `h3t_api_py`). The R Plumber
service (`CalCOFI/api-h3t`, container `h3t_api`) has been decommissioned.

```
   client → Caddy → Varnish ──→ h3t_api_py   FastAPI   (live)

   (h3t_api / R Plumber — removed)
```

The wire protocol matches the old R service: same h3j cells JSON, same SQL
contract, same `{error, reason}` 4xx shape. Two intentional differences from
R are called out under [Breaking changes from R](#breaking-changes-from-r).

## Current deployment

`docker-compose.yml` (`CalCOFI/server`) defines a single h3t service:

```yaml
  h3t_api_py:
    container_name: h3t_api_py
    build: /share/github/CalCOFI/api-h3t-py
    restart: unless-stopped
    environment:
      # legacy single-DB mode → registers as db name "default".
      DUCKDB_PATH: /data/calcofi_latest.duckdb
      H3T_PORT: 8889
      H3T_HOST: 0.0.0.0
      # Varnish strips Accept-Encoding before the cache lookup and Caddy gzips
      # at the edge, so the backend always serves plain JSON.
      H3T_APP_GZIP: "false"
    volumes:
      # calcofi_latest.duckdb is a symlink to the current versioned release.
      - /share/github/CalCOFI/int-app/data:/data:ro
    expose:
      - "8889"
```

Varnish (`varnish/default.vcl`) points its backend at this container:

```vcl
backend default {
    .host = "h3t_api_py";
    .port = "8889";
    ...
}
```

> The R service used `H3T_DB_NAME` to control the db name; **the Python config
> does not read that var** — legacy `DUCKDB_PATH` mode always registers the db
> as `default`. Use `H3T_DBS`/`H3T_DEFAULT_DB` for multi-db (below).

### Multi-database (optional)

To serve more than one DuckDB release from the same endpoint, swap
`DUCKDB_PATH` for the registry form:

```yaml
      H3T_DBS: "default:/data/calcofi_latest.duckdb,prev:/data/calcofi_v2026.04.08.duckdb"
      H3T_DEFAULT_DB: default
```

Clients then pass `?db=prev` to switch. Without `?db=`, behaviour is unchanged.

## How int-app consumes it

`int-app` (`https://app.calcofi.io/int/`) is wired to the public endpoint in
`app/global.R` (set via `Sys.setenv`, **not** `.Renviron` — a comment there
notes `.Renviron` wasn't taking effect):

```r
Sys.setenv(
  H3T_USE      = TRUE,
  H3T_BASE_URL = "https://h3t.calcofi.io/h3t",
  H3T_RELEASE  = "v2026.04.08")
```

It only calls `/h3t/stats` and the tile endpoint, so the cutover was
transparent — no int-app code change was needed. The client (`mapgl` /
`h3t_b64`) sends **URL-safe base64 with `=` padding stripped**; the service
accepts that (and plain standard base64) — see the base64 note under
[History](#history--gotchas).

## Operations

### Release invalidation (flip to a new DuckDB release)

```bash
# 1. flip the symlink to the new release
sudo ln -sfn calcofi_v2026.MM.DD.duckdb \
  /share/github/CalCOFI/int-app/data/calcofi_latest.duckdb

# 2. bounce the API so it reopens the DuckDB file.
#    `restart` reuses the SAME container (IP unchanged) → Varnish keeps
#    resolving it fine. (A `compose up -d` that RECREATES the container gives
#    it a NEW IP and needs the Varnish step in the next section.)
cd /share/github/CalCOFI/server
sudo docker compose restart h3t_api_py

# 3. flush varnish (release-tagged URLs change anyway, but this kills any
#    stale entries explicitly)
sudo docker exec varnish varnishadm ban 'obj.http.X-Url ~ "^/h3t/"'

# 4. point int-app at the new release: edit app/global.R H3T_RELEASE, then
sudo sed -i 's/H3T_RELEASE  = "v2026[0-9.]*"/H3T_RELEASE  = "v2026.MM.DD"/' \
  /share/github/CalCOFI/int-app/app/global.R
sudo touch /share/github/CalCOFI/int-app/app/restart.txt
```

### Rebuilding / recreating the container

After editing `docker-compose.yml` or the code, a rebuild recreates the
container with a **new docker network IP**. Varnish resolves its backend at
VCL-compile time, so it must be restarted to pick up the new IP — otherwise
`h3t.calcofi.io` returns `503 Backend fetch failed`:

```bash
cd /share/github/CalCOFI/server
sudo docker compose build h3t_api_py
sudo docker compose up -d h3t_api_py          # recreates → new IP
docker restart varnish                        # re-resolve backend DNS
```

> Use plain `docker restart varnish`, not `docker compose restart varnish`:
> the latter can fail with `no such service: h3t_api` from stale compose
> metadata left on the running container.

### Smoke test (from inside the docker network)

`h3t_api_py` is not on the host port map; reach it from a container on the
network. It ships `curl` (for its healthcheck), so use it as the jump host —
Varnish has no curl:

```bash
cd /share/github/CalCOFI/server
docker exec h3t_api_py curl -s http://h3t_api_py:8889/h3t/health
docker exec h3t_api_py curl -s http://h3t_api_py:8889/h3t/meta

# a real tile (note: the `base64` CLI emits PADDED standard base64; the real
# client sends url-safe + unpadded — both are accepted)
SQL='SELECT hex_h3res{{res}} AS cell_id, AVG(std_tally) AS value, COUNT(*) AS n
     FROM bio_obs WHERE scientific_name = '"'"'Sardinops sagax'"'"' GROUP BY 1'
Q=$(printf '%s' "$SQL" | base64 | tr -d '\n')
docker exec h3t_api_py \
  curl -sI "http://h3t_api_py:8889/h3t/4/3/6.h3t?q=$Q&release=v2026.04.08" \
  | grep -Ei '^(HTTP|ETag|Cache-Control|X-Calcofi-Release|Vary)'
```

### End-to-end + monitoring

```bash
curl -s https://h3t.calcofi.io/h3t/health | jq .
# {"ok":true, "default_db":"default", "dbs":{...}}

curl -sI "https://h3t.calcofi.io/h3t/4/3/6.h3t?q=$Q&release=v2026.04.08" \
  | grep -Ei '^(HTTP|X-Cache|ETag|Cache-Control|X-Calcofi-Release)'
# first hit X-Cache: MISS; repeat: HIT

cd /share/github/CalCOFI/server
sudo docker compose logs --tail=200 -f h3t_api_py
docker exec varnish varnishstat -1 -f MAIN.cache_hit -f MAIN.cache_miss
```

### Troubleshooting

```bash
cd /share/github/CalCOFI/server
sudo docker compose ps h3t_api_py
docker exec h3t_api_py curl -fsS http://localhost:8889/h3t/health
docker exec h3t_api_py sh -c 'env | grep -E "H3T_|DUCKDB"'

# 503 from h3t.calcofi.io after a rebuild → Varnish has the old backend IP:
docker restart varnish

# 400 "q is required and must be valid base64" on real client traffic →
# decode is rejecting the client's base64. The service handles url-safe +
# unpadded; if this recurs, decode the q to inspect the SQL:
echo "$Q" | tr '_-' '/+' | base64 -d
```

## Rollback

The R container is **gone**, so rollback is no longer a VCL one-liner. To
revert to R you must rebuild it:

```bash
# re-add the h3t_api service block to docker-compose.yml (see git history:
#   CalCOFI/server before commit "Cut over h3t.calcofi.io to FastAPI …"),
# then:
cd /share/github/CalCOFI/server
sudo docker compose up -d --build h3t_api
# point varnish/default.vcl backend .host back to "h3t_api", then:
docker restart varnish
```

Given the Python service has been stable, prefer fixing forward over rollback.

## History & gotchas

The cutover ran as a parallel deploy (R and Python side by side), validated
for body parity from inside the docker network, then flipped Varnish. Notes
worth keeping:

- **Parity was on response *bodies*, not ETags.** The R service hashed an R
  `list()` via `digest::digest()`, which is not portable; this service hashes
  a delimited string. ETags never matched and were never expected to — the
  Varnish cache was flushed (`ban 'obj.http.X-Url ~ "^/h3t/"'`) at cutover.
  Tile `cells` are byte-identical; `stats` `p02`/`p98` differ slightly because
  DuckDB's `approx_quantile` (t-digest) is non-deterministic under
  multithreading on both services — it only affects colour-ramp clamp bounds.
- **`sed -i` breaks a single-file bind mount.** `varnish/default.vcl` is
  bind-mounted by path; `sed -i` replaces the inode, so the container keeps
  seeing the old file and `vcl.load` compiles stale content. Edit with an
  inode-preserving tool (or `cp` over it) and **restart Varnish** to be sure.
- **Client base64 is url-safe + unpadded.** Early curl tests passed because
  the `base64` CLI emits padded standard base64; the real browser client sent
  url-safe, padding-stripped `q`, which the strict decoder rejected (400, no
  hexagons). Fixed in `app/tiles.py::decode_sql` (restore `-_`→`+/`, re-pad).
- **Repo layout.** CalCOFI repos were consolidated under
  `/share/github/CalCOFI/` (int-app, workflows, server, api-h3t-py, …). The
  DuckDB mount and shiny-server symlinks point at the new paths.

## Breaking changes from R

Intentional differences. Clients other than `int-app` should be audited:

1. **`/h3t/health` response shape** — R returned
   `{"ok":true, "db":"...", "db_mtime":"..."}`. Python returns
   `{"ok":true, "default_db":"...", "dbs":{name:{path, mtime}, ...}}`.
   The `ok` key is preserved; nothing else. (int-app does not parse health.)
2. **`/h3t/meta` response** — gains `available_dbs`, `default_db`, `db`,
   `db_mtime` keys. `tables`, `h3_columns_per_row`, `default_zoom_breaks`
   are unchanged.
3. **New `?db=` query param** on all endpoints. Defaults preserve old
   behaviour — only matters if you use the registry (multi-db) mode.
4. **`Vary: Accept-Encoding`** header is added (no behaviour change under the
   current Varnish VCL, which strips `Accept-Encoding`).
