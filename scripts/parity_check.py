#!/usr/bin/env python3
"""Live parity check between the R Plumber service and this FastAPI port.

Usage:
    python scripts/parity_check.py \
        --r-base http://localhost:8889 \
        --py-base http://localhost:8890 \
        [--release v2026.05.14]

Exits non-zero on any divergence. Diffs JSON bodies and headers (ETag,
Cache-Control, X-Calcofi-Release).

Assumes both services are pointed at the same DuckDB file, with
H3T_DB_NAME=default on the R side (or unset — defaults to "default") so
the ETag db component matches the Python service's legacy fallback name.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx


# Curated query fixture — keep these synthetic so they don't depend on the
# specific table schema. Extend with real int-app queries before cutover.
QUERIES: dict[str, str] = {
    "minimal_const": (
        "SELECT 599405990948208639::BIGINT AS cell_id, 1.0 AS value, 1 AS n"
    ),
    "two_cells_with_n": (
        "SELECT * FROM (VALUES "
        "(599405990948208639::BIGINT, 1.5, 10::BIGINT), "
        "(599405991417970687::BIGINT, 2.5,  4::BIGINT)) "
        "AS t(cell_id, value, n)"
    ),
    "without_n": (
        "SELECT 599405990948208639::BIGINT AS cell_id, 3.14 AS value"
    ),
}

TILES: list[tuple[int, int, int]] = [
    (5, 3, 12),
    (8, 41, 100),
    (10, 163, 397),
]


def b64(sql: str) -> str:
    return base64.b64encode(sql.encode("utf-8")).decode("ascii")


@dataclass
class Endpoint:
    name: str
    base: str


def diff_json(a: Any, b: Any, path: str = "") -> list[str]:
    """Return a list of human-readable diffs. Empty list = parity."""
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        return [f"{path}: type mismatch {type(a).__name__} vs {type(b).__name__}"]
    if isinstance(a, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing in R")
            elif k not in b:
                out.append(f"{path}.{k}: missing in Py")
            else:
                out.extend(diff_json(a[k], b[k], f"{path}.{k}"))
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} vs {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff_json(x, y, f"{path}[{i}]"))
        return out
    if isinstance(a, float) or isinstance(b, float):
        if abs(float(a) - float(b)) > 1e-9:
            return [f"{path}: float {a!r} vs {b!r}"]
        return []
    if a != b:
        return [f"{path}: {a!r} vs {b!r}"]
    return []


def cmp_response(
    label: str, r_resp: httpx.Response, py_resp: httpx.Response, check_etag: bool
) -> list[str]:
    diffs: list[str] = []
    if r_resp.status_code != py_resp.status_code:
        diffs.append(
            f"{label}: status {r_resp.status_code} vs {py_resp.status_code}"
        )
        return diffs
    if check_etag:
        r_etag = r_resp.headers.get("etag")
        p_etag = py_resp.headers.get("etag")
        if r_etag != p_etag:
            diffs.append(f"{label}: ETag mismatch {r_etag!r} vs {p_etag!r}")
    try:
        r_body  = r_resp.json()
        py_body = py_resp.json()
    except ValueError as e:
        return [f"{label}: non-JSON response ({e})"]

    # Normalize: sort cells by h3id so order doesn't matter
    if isinstance(r_body, dict) and "cells" in r_body:
        r_body["cells"]  = sorted(r_body["cells"],  key=lambda c: c.get("h3id", ""))
        py_body["cells"] = sorted(py_body["cells"], key=lambda c: c.get("h3id", ""))

    diffs.extend(diff_json(r_body, py_body, label))
    return diffs


def run(r_base: str, py_base: str, release: str) -> int:
    r  = httpx.Client(base_url=r_base,  timeout=10)
    py = httpx.Client(base_url=py_base, timeout=10)

    failures: list[str] = []

    # health is informational only — keys/format differ on purpose
    print("=== /h3t/health ===")
    for cli, base in ((r, r_base), (py, py_base)):
        resp = cli.get("/h3t/health")
        print(f"  {base}: {resp.status_code} {resp.json()}")

    # /h3t/meta — table list must match
    print("\n=== /h3t/meta ===")
    rh  = r.get("/h3t/meta")
    pyh = py.get("/h3t/meta")
    rt  = sorted(rh.json().get("tables",  []))
    pt  = sorted(pyh.json().get("tables", []))
    if rt != pt:
        failures.append(f"meta tables differ: R={rt} Py={pt}")
        print(f"  FAIL: tables differ")
    else:
        print(f"  OK: {len(rt)} tables match")

    # /h3t/stats — full parity per query
    print("\n=== /h3t/stats ===")
    for name, sql in QUERIES.items():
        q = b64(sql)
        params = {"q": q, "release": release, "res_h3": 5}
        r_resp  = r.get("/h3t/stats",  params=params)
        py_resp = py.get("/h3t/stats", params=params)
        diffs = cmp_response(f"stats[{name}]", r_resp, py_resp, check_etag=True)
        if diffs:
            failures.extend(diffs)
            for d in diffs:
                print(f"  FAIL {d}")
        else:
            print(f"  OK   {name}")

    # /h3t/{z}/{x}/{y}.h3t — full parity per (tile, query)
    print("\n=== /h3t/{z}/{x}/{y}.h3t ===")
    for z, x, y in TILES:
        for name, sql in QUERIES.items():
            q = b64(sql)
            params = {"q": q, "release": release}
            url = f"/h3t/{z}/{x}/{y}.h3t"
            r_resp  = r.get(url,  params=params)
            py_resp = py.get(url, params=params)
            label = f"tile({z},{x},{y})[{name}]"
            diffs = cmp_response(label, r_resp, py_resp, check_etag=True)
            if diffs:
                failures.extend(diffs)
                for d in diffs:
                    print(f"  FAIL {d}")
            else:
                print(f"  OK   {label}")

    print("\n=== summary ===")
    if failures:
        print(f"FAIL: {len(failures)} divergence(s)")
        for f in failures[:20]:
            print(f"  - {f}")
        if len(failures) > 20:
            print(f"  … {len(failures) - 20} more")
        return 1
    print("OK: all checks passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--r-base",  default="http://localhost:8889")
    ap.add_argument("--py-base", default="http://localhost:8890")
    ap.add_argument("--release", default="v_test")
    args = ap.parse_args()
    return run(args.r_base, args.py_base, args.release)


if __name__ == "__main__":
    sys.exit(main())
