"""Phase 0 feasibility-spike endpoints.

Goal: prove that Render Oregon can read EMIT granules directly from LP DAAC's
in-region S3 bucket, and measure throughput / latency. Everything except
/spike/ping requires `Authorization: Bearer $SPIKE_TOKEN`.

Remove (or keep behind admin auth) once Phase 1 lands.
"""

from __future__ import annotations

import os
import secrets
import time
import urllib.request
from functools import lru_cache
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, Header, HTTPException, Query

router = APIRouter(prefix="/spike", tags=["spike"])

EMIT_SHORT_NAME = "EMITL2ARFL"
EMIT_VERSION = "002"  # V002 (2025+); masks moved to EMITL2AMASK
S3_CRED_TTL_S = 50 * 60  # LP DAAC creds expire at 1 h


def require_token(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("SPIKE_TOKEN", "")
    got = authorization.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


# --------------------------------------------------------------------------
# earthaccess / S3 helpers
# --------------------------------------------------------------------------

_login_done = False
_fs_cache: dict[str, tuple[Any, float]] = {}  # daac -> (fs, created)


def _login() -> None:
    global _login_done
    if not _login_done:
        import earthaccess

        auth = earthaccess.login(strategy="environment")
        if not auth or not auth.authenticated:
            raise HTTPException(500, "service EDL login failed (check EARTHDATA_USERNAME/PASSWORD)")
        _login_done = True


def _daac_for(s3_url: str) -> str:
    if s3_url.startswith("s3://lp-prod-"):
        return "LPDAAC"
    if s3_url.startswith("s3://ornl-cumulus-"):
        return "ORNLDAAC"
    raise HTTPException(400, f"unknown bucket for {s3_url}")


def _s3fs(daac: str = "LPDAAC"):
    """In-region s3fs filesystem per DAAC, refreshed every ~50 min."""
    import earthaccess

    now = time.time()
    hit = _fs_cache.get(daac)
    if hit is None or now - hit[1] > S3_CRED_TTL_S:
        _login()
        hit = (earthaccess.get_s3_filesystem(daac=daac), now)
        _fs_cache[daac] = hit
    return hit[0]


@lru_cache(maxsize=64)
def _search_emit(lat: float, lon: float, count: int) -> list[dict]:
    import earthaccess

    results = earthaccess.search_data(
        short_name=EMIT_SHORT_NAME, version=EMIT_VERSION, point=(lon, lat), count=count
    )
    out = []
    for g in results:
        links = g.data_links(access="direct")
        rfl = [l for l in links if l.endswith(".nc") and "_RFL_" in l]
        out.append(
            {
                "id": g["umm"]["GranuleUR"],
                "time": g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"],
                "cloud_cover": g["umm"].get("CloudCover"),
                "s3": rfl[0] if rfl else None,
                "size_mb": g.size(),
            }
        )
    return out


def _timed(fn):
    t0 = time.perf_counter()
    v = fn()
    return v, (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@router.get("/ping")
async def ping() -> dict:
    """Unauthenticated, no work: for measuring phone → server RTT."""
    return {"t": time.time()}


@router.get("/whoami", dependencies=[Depends(require_token)])
def whoami() -> dict:
    """Outbound IP + AWS region hint (does the IP fall in us-west-2?)."""
    ip = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5).read().decode().strip()
    return {"outbound_ip": ip, "render_region_env": os.environ.get("RENDER_REGION")}


@router.get("/search", dependencies=[Depends(require_token)])
def search(lat: float, lon: float, count: int = 10) -> dict:
    res, ms = _timed(lambda: _search_emit(round(lat, 3), round(lon, 3), count))
    return {"ms": ms, "granules": res}


@router.get("/emit/layout", dependencies=[Depends(require_token)])
def emit_layout(s3: str = Query(..., description="s3:// URL of an EMIT L2A RFL .nc")) -> dict:
    """Open an EMIT (or AVIRIS-3/5 NetCDF) granule directly from S3 and dump its HDF5 layout + key attrs."""
    import h5py

    fs = _s3fs(_daac_for(s3))
    t0 = time.perf_counter()
    with fs.open(s3, "rb", block_size=8 * 1024 * 1024) as f, h5py.File(f, "r") as h:
        open_ms = (time.perf_counter() - t0) * 1000.0
        items: dict[str, Any] = {}

        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                items[name] = {
                    "shape": obj.shape,
                    "dtype": str(obj.dtype),
                    "chunks": obj.chunks,
                    "compression": obj.compression,
                    "attrs": {k: _jsonable(v) for k, v in obj.attrs.items()},
                }

        h.visititems(visit)
        root_attrs = {k: _jsonable(v) for k, v in h.attrs.items()}
    return {"open_ms": open_ms, "root_attrs": root_attrs, "datasets": items}


@router.get("/emit/pixel", dependencies=[Depends(require_token)])
def emit_pixel(s3: str, row: int, col: int) -> dict:
    """Cold single-spectrum read straight from S3 (pre-`prepare` path)."""
    import h5py

    fs = _s3fs(_daac_for(s3))
    t0 = time.perf_counter()
    with fs.open(s3, "rb", block_size=1024 * 1024) as f, h5py.File(f, "r") as h:
        t_open = time.perf_counter()
        spec = h["reflectance"][row, col, :]
        t_read = time.perf_counter()
    return {
        "open_ms": (t_open - t0) * 1000.0,
        "read_ms": (t_read - t_open) * 1000.0,
        "n_bands": int(spec.shape[0]),
        "first5": spec[:5].tolist(),
    }


@router.get("/emit/throughput", dependencies=[Depends(require_token)])
def emit_throughput(s3: str, mb: int = Query(256, le=4096)) -> dict:
    """Raw sequential S3 read rate (no HDF5 decode). Works for LP DAAC and ORNL URLs."""
    fs = _s3fs(_daac_for(s3))
    chunk = 16 * 1024 * 1024
    want = mb * 1024 * 1024
    got = 0
    t0 = time.perf_counter()
    with fs.open(s3, "rb", block_size=chunk) as f:
        while got < want:
            b = f.read(min(chunk, want - got))
            if not b:
                break
            got += len(b)
    s = time.perf_counter() - t0
    return {"bytes": got, "seconds": s, "MB_per_s": got / 1e6 / s if s else None}


@router.get("/emit/full", dependencies=[Depends(require_token)])
def emit_full(s3: str) -> dict:
    """Approximate `prepare` cost: stream the reflectance cube in row blocks into an
    int16 BIP memmap on local disk (bounded RAM), then read the GLT."""
    import tempfile

    import h5py

    fs = _s3fs(_daac_for(s3))
    t0 = time.perf_counter()
    read_s = conv_s = 0.0
    with fs.open(s3, "rb", block_size=32 * 1024 * 1024, cache_type="readahead") as f, h5py.File(f, "r") as h:
        ds = h["reflectance"]
        rows, cols, bands = ds.shape
        dtype, chunks = str(ds.dtype), ds.chunks
        step = max(1, (chunks or (64,))[0])
        with tempfile.NamedTemporaryFile(suffix=".bip", delete=True) as tmp:
            bip = np.lib.format.open_memmap(tmp.name, mode="w+", dtype=np.int16, shape=(rows, cols, bands))
            for r0 in range(0, rows, step):
                ta = time.perf_counter()
                blk = ds[r0 : r0 + step]
                tb = time.perf_counter()
                bip[r0 : r0 + step] = np.clip(np.nan_to_num(blk, nan=-0.9999) * 10000, -32768, 32767)
                conv_s += time.perf_counter() - tb
                read_s += tb - ta
            bip.flush()
            int16_mb = bip.nbytes / 1e6
            del bip
        t_cube = time.perf_counter()
        glt_x = h["location/glt_x"][...]
        glt_y = h["location/glt_y"][...]
        t_glt = time.perf_counter()
    return {
        "cube_shape": [rows, cols, bands],
        "cube_dtype": dtype,
        "chunks": chunks,
        "row_step": step,
        "glt_shape": glt_x.shape,
        "glt_x_max": int(glt_x.max()),
        "glt_y_max": int(glt_y.max()),
        "cube_total_s": t_cube - t0,
        "cube_read_s": read_s,
        "to_int16_s": conv_s,
        "read_glt_s": t_glt - t_cube,
        "int16_mb": int16_mb,
    }


def _jsonable(v):
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    if isinstance(v, np.ndarray):
        return v.tolist() if v.size <= 32 else f"<array {v.shape} {v.dtype}>"
    if isinstance(v, np.generic):
        return v.item()
    return v
