# Deploy api-h3t-py to the CalCOFI server

Cutover of `h3t.calcofi.io` from the R Plumber service
(`CalCOFI/api-h3t`, container `h3t_api`) to the FastAPI port
(`CalCOFI/api-h3t-py`, container `h3t_api_py`).

The wire protocol matches the R service: same h3j cells JSON, same SQL
contract, same `{error, reason}` 4xx shape, same ETag scheme (after the
`CalCOFI/api-h3t` commit that switched to the stable string hash).

## Strategy

Run the new container **alongside** the existing one on a separate port,
validate parity from inside the docker network, then flip Varnish's
backend with a single VCL edit + reload. Keep the R container available
for instant rollback for ~1 week.

```
                              ┌──────────────┐
   client → Caddy → Varnish ──┤ h3t_api      │ R Plumber  (current)
                              └──────────────┘
                              ┌──────────────┐
                              │ h3t_api_py   │ FastAPI    (new — flip to this)
                              └──────────────┘
```

## Step 1 — on the server: pull the new code

```bash
ssh <server-host>

# api-h3t-py is a new repo; clone it next to api-h3t
sudo -u bebest git -C /share/github/CalCOFI clone \
  https://github.com/CalCOFI/api-h3t-py.git

# api-h3t got a small change too (ETag scheme)
git -C /share/github/CalCOFI/api-h3t pull --ff-only
```

## Step 2 — edit `docker-compose.yml` to add the new service

In `/share/github/CalCOFI/server/docker-compose.yml`, add a sibling
`h3t_api_py` service. Both services run in parallel during validation;
Varnish keeps hitting the R service until step 6.

```yaml
  h3t_api_py:
    container_name: h3t_api_py
    build: /share/github/CalCOFI/api-h3t-py
    restart: unless-stopped
    environment:
      # legacy single-DB mode — picks up the same DuckDB as h3t_api so
      # parity tests compare apples-to-apples.
      DUCKDB_PATH: /data/calcofi_latest.duckdb
      H3T_PORT: 8889
      H3T_HOST: 0.0.0.0
      # name must match the R service's H3T_DB_NAME (default "default")
      # so the ETag matches byte-for-byte.
      H3T_DB_NAME: default
      # leave gzip off: Caddy gzips at the edge and Varnish strips
      # Accept-Encoding before the cache lookup, so the backend should
      # always serve plain JSON.
      H3T_APP_GZIP: "false"
    volumes:
      - /share/github/db-viz-hex/data:/data:ro
    expose:
      - "8889"
```

> Note: both containers `expose: "8889"` internally — that's fine because
> they're on the docker network with distinct DNS names (`h3t_api` vs
> `h3t_api_py`). Varnish resolves by hostname.

If you ever want to serve multiple DuckDB releases from the same
endpoint, swap `DUCKDB_PATH` for the registry form:

```yaml
      H3T_DBS: "default:/data/calcofi_latest.duckdb,prev:/data/calcofi_v2026.04.08.duckdb"
      H3T_DEFAULT_DB: default
```

Then clients can pass `?db=prev` to switch. Without `?db=`, behavior is
unchanged.

## Step 3 — build and launch the new container

```bash
cd /share/github/CalCOFI/server

sudo docker compose build h3t_api_py

# bring it up without touching h3t_api or varnish
sudo docker compose up -d h3t_api_py

# tail logs until it reports ready
sudo docker compose logs -f h3t_api_py
#  expect: "Uvicorn running on http://0.0.0.0:8889"
```

## Step 4 — smoke-test the new backend from inside the docker network

The new container isn't on the host port map; reach it via the docker
network (varnish is the easiest jump host since it lives there).

```bash
# health, meta, openapi docs
sudo docker compose exec varnish curl -s http://h3t_api_py:8889/h3t/health | jq .
sudo docker compose exec varnish curl -s http://h3t_api_py:8889/h3t/meta   | jq .

# a real tile (same SQL as the R service)
SQL='SELECT hex_h3res{{res}} AS cell_id, AVG(std_tally) AS value, COUNT(*) AS n
     FROM bio_obs WHERE scientific_name = '"'"'Sardinops sagax'"'"' GROUP BY 1'
Q=$(printf '%s' "$SQL" | base64 | tr -d '\n')
sudo docker compose exec varnish \
  curl -sI "http://h3t_api_py:8889/h3t/4/3/6.h3t?q=$Q&release=v2026.04.08" \
  | grep -Ei '^(HTTP|ETag|Cache-Control|X-Calcofi-Release|Vary)'
```

## Step 5 — run live parity check vs the R service

`scripts/parity_check.py` in the api-h3t-py repo hits both services and
diffs JSON bodies, headers, and ETags.

```bash
cd /share/github/CalCOFI/api-h3t-py

# from the host: forward both container ports
# (or run the script inside a temporary python container that's on the
#  docker network — see "alternative" below)

# exec into varnish (it has curl + python? probably not — use a side container)
sudo docker run --rm --network=server_default \
  -v "$PWD:/work" -w /work \
  python:3.13-slim sh -c "pip install -q httpx && python scripts/parity_check.py \
    --r-base http://h3t_api:8889 \
    --py-base http://h3t_api_py:8889 \
    --release v2026.04.08"
```

The script prints `OK: all checks passed` when bodies, headers, and
ETags match. If any divergence shows up, **do not proceed**.

> Replace `server_default` with the actual docker network name —
> `docker network ls | grep server` will reveal it
> (typically `server_default` if your compose project is in `server/`).

### Alternative — extend parity to db-viz-hex real queries

For a more thorough test, extend `scripts/parity_check.py`'s `QUERIES`
dict with the actual SQL templates from
`db-viz-hex/app/functions_h3t.R` (species + env queries with real species
ids and date ranges). Run again before flipping Varnish.

## Step 6 — flip Varnish to the new backend

Edit `/share/github/CalCOFI/server/varnish/default.vcl`:

```diff
 backend default {
-    .host = "h3t_api";
+    .host = "h3t_api_py";
     .port = "8889";
     .connect_timeout = 5s;
     .first_byte_timeout = 10s;
     .between_bytes_timeout = 5s;
 }
```

Reload Varnish without restarting the container (preserves cache):

```bash
# load the new VCL with a label
sudo docker compose exec varnish \
  varnishadm vcl.load v_py /etc/varnish/default.vcl

# activate it
sudo docker compose exec varnish varnishadm vcl.use v_py
```

Or restart the container if you'd rather (drops cache):

```bash
sudo docker compose restart varnish
```

### Cache flush (recommended after the swap)

The ETag scheme is unchanged in format but `db_mtime` is now `%.6f` epoch
seconds rather than the prior POSIXct string. Old cache entries can stick
until natural expiry (24h) but will respond with a stale `ETag` until
then. To flush cleanly:

```bash
sudo docker compose exec varnish varnishadm ban 'req.url ~ "^/h3t/"'
```

## Step 7 — verify end-to-end through Caddy

```bash
curl -s https://h3t.calcofi.io/h3t/health | jq .
# expect: {"ok":true, "default_db":"default", "dbs":{...}}
#        — note the new shape (R returned a different shape).
#        If db-viz-hex or other clients parsed /health, audit them.

curl -sI "https://h3t.calcofi.io/h3t/4/3/6.h3t?q=$Q&release=v2026.04.08" \
  | grep -Ei '^(HTTP|X-Cache|ETag|Cache-Control|X-Calcofi-Release)'
# first hit: X-Cache: MISS; repeat: X-Cache: HIT
```

Open `db-viz-hex` (`https://app.calcofi.io` or wherever it's routed) and
pan/zoom on the map. Confirm:
- Tiles render without visible seams.
- Legend populates (stats endpoint works).
- Browser network panel shows `200 OK` with `X-Cache: HIT` after the
  first interaction.

## Step 8 — monitor

```bash
# new backend logs
sudo docker compose logs --tail=200 -f h3t_api_py

# varnish hit rate
sudo docker compose exec varnish varnishstat -1 \
  -f MAIN.cache_hit -f MAIN.cache_miss -f MAIN.cache_hitpass

# any 5xx? grep caddy
sudo docker compose logs caddy | grep -E ' 5\d\d ' | tail
```

Target: hit rate within 24h matches pre-cutover baseline.

## Rollback

If anything looks wrong, point Varnish back at the R service in seconds:

```bash
# edit varnish/default.vcl to set .host = "h3t_api" again
sudo docker compose exec varnish \
  varnishadm vcl.load v_r /etc/varnish/default.vcl
sudo docker compose exec varnish varnishadm vcl.use v_r
```

The R container is still running and ready to take traffic.

## Decommission (after ~1 week of stable Python operation)

```bash
# stop and remove the old service
sudo docker compose stop  h3t_api
sudo docker compose rm -f h3t_api

# remove the h3t_api block from docker-compose.yml and commit:
git -C /share/github/CalCOFI/server commit -am \
  "remove h3t_api (R Plumber); cutover to h3t_api_py complete"

# optional: shrink h3t_api_py's container name to h3t_api so future
# changes don't need a rename — but the VCL backend hostname would
# need updating in lockstep.
```

## Ongoing — release invalidation

Same flow as the R service:

```bash
# 1. flip the symlink to the new release
sudo ln -sfn calcofi_v2026.MM.DD.duckdb \
  /share/github/db-viz-hex/data/calcofi_latest.duckdb

# 2. bounce the API so it reopens the DuckDB file
sudo docker compose restart h3t_api_py

# 3. flush varnish (URLs carry the old release param so new clients
#    won't hit them, but this kills any stale entries explicitly)
sudo docker compose exec varnish varnishadm ban 'req.url ~ "^/h3t/"'

# 4. update H3T_RELEASE in the db-viz-hex's .Renviron and restart it
echo 'H3T_RELEASE=v2026.MM.DD' \
  | sudo tee /srv/shiny-server/db-viz-hex/.Renviron > /dev/null
sudo touch /srv/shiny-server/db-viz-hex/restart.txt
```

## Troubleshooting

```bash
# is the new backend healthy?
sudo docker compose ps h3t_api_py
sudo docker compose exec varnish curl -fsS http://h3t_api_py:8889/h3t/health

# inside the container
sudo docker compose exec h3t_api_py sh -c 'env | grep -E "H3T_|DUCKDB"'

# verify ETag parity once more after restart
sudo docker compose exec varnish curl -sI \
  "http://h3t_api:8889/h3t/4/3/6.h3t?q=$Q&release=v2026.04.08" | grep -i etag
sudo docker compose exec varnish curl -sI \
  "http://h3t_api_py:8889/h3t/4/3/6.h3t?q=$Q&release=v2026.04.08" | grep -i etag
# must match
```

## Breaking-change call-outs

These are intentional changes from the R service. Clients other than
`db-viz-hex` should be audited:

1. **`/h3t/health` response shape** — R returned
   `{"ok":true, "db":"...", "db_mtime":"..."}`. Python returns
   `{"ok":true, "default_db":"...", "dbs":{name:{path, mtime}, ...}}`.
   The `ok` key is preserved; nothing else.
2. **`/h3t/meta` response** — gains `available_dbs`, `default_db`,
   `db`, `db_mtime` keys. `tables`, `h3_columns_per_row`,
   `default_zoom_breaks` are unchanged.
3. **New `?db=` query param** on all endpoints. Defaults preserve old
   behavior — only matters if you intentionally use the registry mode.
4. **`Vary: Accept-Encoding`** header is added (no behavior change with
   the current Varnish VCL since it strips `Accept-Encoding`).
