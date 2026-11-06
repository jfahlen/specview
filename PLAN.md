# SpecView — Plan

iPhone app that shows EMIT / AVIRIS RGB imagery and spectra at the user's location. Backend on Render (Oregon = AWS us-west-2) next to NASA Earthdata Cloud.

Status: Phase 0 in progress (2026-11-06). Repo scaffolded; spike server written and tested locally; not yet deployed to Render.

## Goals

1. On launch, search Earthdata (via `earthaccess`) for EMIT / AVIRIS-NG / AVIRIS-3 scenes covering the user's location.
2. User picks a scene; show its RGB on a map at the user's location.
3. Show the spectrum of the pixel nearest the user.
4. Tap / drag anywhere in the scene to show that pixel's spectrum.
5. As the user moves, update the map and "my pixel" spectrum.
6. Feel instant. Only invited people can use the server.

## Architecture

```
iPhone (SwiftUI + MapKit + Swift Charts)
   │  HTTPS/2, Bearer JWT (+ App Attest later)
   ▼
Render web service, region oregon (FastAPI/uvicorn, always-on, persistent disk)
   │  earthaccess → CMR search (public)
   │  s3credentials (service EDL account) → direct S3 reads, in-region
   ▼
NASA Earthdata Cloud (us-west-2)
   LP DAAC:   EMIT L2A RFL (NetCDF, 285 bands, 60 m, sensor geometry + GLT)
   ORNL DAAC: AVIRIS-NG / AVIRIS-3 L2 reflectance (ENVI BIL or NetCDF — verify per collection)
```

## Repo layout (single repo)

```
specview/
  render.yaml     # rootDir: server, buildFilter paths: server/**
  PLAN.md
  api/            # OpenAPI spec + binary spectrum format doc (shared contract)
  server/         # FastAPI app; only this deploys to Render
  ios/            # Xcode project
```

`render.yaml` sketch:

```yaml
services:
  - type: web
    name: specview-api
    region: oregon
    runtime: python
    rootDir: server
    buildFilter:
      paths: ["server/**"]
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Secrets only in Render env vars: service EDL credentials, EDL OAuth client secret, JWT signing key. The iOS app holds only the server URL and EDL client ID (public).

## User flow

1. App gets location (when-in-use) → `GET /v1/scenes?lat&lon`.
2. Server: `earthaccess.search_data(point=...)` across EMIT / AVIRIS-NG / AVIRIS-3 collections → instrument, datetime, cloud cover, footprint polygon, CMR browse image URL.
3. App lists scenes with browse thumbnails; user picks one.
4. `POST /v1/scenes/{id}/prepare`. The app shows the browse PNG as a map overlay right away, then switches to full-quality tiles when ready.
5. Spectrum chart for the nearest pixel (with pixel outline and distance). Tap or long-press-drag anywhere to see other spectra; pin a few to compare.
6. `CLLocationManager` `distanceFilter` ≈ half a pixel (~30 m for EMIT) updates "my pixel." Leaving the footprint → offer a new search.
7. Manual location override (drop a pin) for testing, demos, and App Review.

## Server API

| Endpoint | Purpose |
|---|---|
| `GET /v1/auth/login` → EDL, `GET /v1/auth/callback`, `POST /v1/auth/exchange`, `POST /v1/auth/refresh` | EDL OAuth login → server JWTs |
| `GET /v1/scenes?lat&lon` | Search (cached ~10 min per rounded lat/lon) |
| `POST /v1/scenes/{id}/prepare`, `GET /v1/scenes/{id}` | Prepare status, bounds, wavelengths, good-band mask, footprint |
| `GET /v1/scenes/{id}/tiles/{z}/{x}/{y}.webp` | RGB XYZ tiles |
| `GET /v1/scenes/{id}/spectrum?lat&lon` | Single spectrum (binary) |
| `GET /v1/scenes/{id}/block?lat&lon&r=` | Block of spectra (compressed binary) |
| `/admin/allow`, `/admin/revoke`, `/admin/users`, `/admin/requests` | Allowlist admin |

## Auth: Earthdata Login (EDL) OAuth

- Register an app in EDL (urs.earthdata.nasa.gov → Applications; may need the "application creator" permission from EDL support). Redirect URI: `https://<service>.onrender.com/v1/auth/callback`.
- Flow:
  1. App opens `ASWebAuthenticationSession` on EDL `/oauth/authorize` (client_id, response_type=code, redirect_uri, state).
  2. User signs in to EDL; first time they approve the app.
  3. Server `/callback` checks `state` and exchanges the code (client secret is server-side only) → EDL token + `uid`.
  4. Server checks `uid` against the allowlist (SQLite on persistent disk).
     - Allowed → short-lived one-time code → redirect `specview://auth?code=...` → app calls `POST /v1/auth/exchange` → server JWTs. Real tokens never go in a URL.
     - Not allowed → "request access" page; uid logged to `/admin/requests`.
  5. App uses server JWTs: ~15 min access, rotating refresh token in Keychain. Optional forced EDL re-login every 30–90 days.
- Revoke = remove from allowlist and kill refresh tokens.
- User EDL is for **identity only**. Data access uses one **service EDL account** on the server (`earthaccess.login(strategy="environment")`). Accept LP DAAC and ORNL DAAC EULAs on that account.
- Later: App Attest to stop allowlisted tokens being used from scripts. Per-user rate limits.

## Sharing

- TestFlight external testing via public link (requires Apple Developer Program, $99/yr). The first build goes through Beta App Review. Builds expire after 90 days.
- Onboarding: person sends their EDL username → add to allowlist → send TestFlight link. The link alone gives no data access.
- App Review: create a reviewer EDL account, allowlist it, and put its credentials plus manual-location instructions in the review notes. Check current guideline 4.8 (Sign in with Apple) wording; EDL is a specific required service account, so it should be exempt.
- Long term: Unlisted App Store distribution.

## Performance design

### Once per scene (`prepare`)
- Pull the granule over in-region S3 (EMIT ~1–2 GB → seconds to tens of seconds). Cache S3 credentials and refresh at ~50 min (they expire at 1 h).
- Write a **BIP int16 memmap** (reflectance × 10000) so one spectrum = one contiguous read (EMIT: 285 × 2 B ≈ 570 B).
- Read the file through once so it stays in the OS page cache (EMIT cube ≈ 0.9 GB). Size RAM for 1–3 hot scenes.
- Lookup index: EMIT → GLT + its geotransform. AVIRIS → ENVI `map info` affine (incl. rotation) + cached `pyproj` lat/lon→UTM transformer.
- RGB: ~650/560/470 nm, one fixed 2–98% stretch per scene, gamma, transparent no-data. Orthorectify EMIT via GLT. Build XYZ tile pyramid (or COG + rio-tiler).
- Start prepare early: begin fetching the newest/clearest result as soon as search results return.
- Only one prepare job per scene, even if several users open it; limit concurrent jobs.

### Per tap on the server (< 1 ms)
```
col = floor((lon - x0)/dx); row = floor((lat - y0)/dy)
sr, sc = glt[row, col]            # EMIT; 1-based, 0 = no data → search 3×3/5×5 for nearest valid pixel
spec = cube[sr, sc, :]            # from page cache
return header(pixel center lat/lon, row, col, distance) + raw int16
```
- `async def` handler with no awaits, uvicorn + uvloop + httptools. Raw binary ~600 B, no gzip. Wavelengths are sent once per scene, not per tap.
- Before `prepare` finishes: direct S3 reads (EMIT HDF5 chunk read; AVIRIS BIL range GET of one line) ≈ 30–100 ms.

### Network
- One `URLSession`, HTTP/2 keep-alive. Ping every ~20–30 s while a scene is open so the connection stays open and the cellular radio stays awake (waking it adds 100–300 ms).

### On the phone
- On scene open, fetch a 64×64 px block of spectra around the user (~2.3 MB, LZFSE/zstd). Fetch the next block in the background as the user moves toward the edge.
- Tap outside the cache → single spectrum first, then a 16×16 block (~150 KB) around the tap.
- Drag: at most one request in flight; send only the latest position. Crosshair moves instantly.
- One chart with no animation (Swift Charts; fall back to `Canvas` if slow). Mask water-vapor bands (~1350–1450, ~1800–1950 nm) using the good-band mask.
- Optional: PCA scene (~16 float16 components + 16×285 basis ≈ 50 MB for EMIT) on the phone → instant approximate spectrum anywhere, replaced by the exact one from the server.

### Latency targets (Phase 0 measures these)
| Case | Tap → spectrum |
|---|---|
| Inside phone cache | ~1–5 ms |
| PCA scene on phone (approx.) | ~1–5 ms, exact ~50 ms later |
| Server, warm, West Coast | ~25–60 ms |
| Server, East Coast | ~80–120 ms |
| Before prepare finishes (S3) | ~100–200 ms |

## Render setup
- Paid, always-on instance (no free-tier spin-down), 4–8 GB RAM, persistent disk (~50 GB) used as an LRU cache of converted scenes.
- A persistent disk means one instance and brief downtime on each deploy (the page cache is lost too) — that's why the build filter exists.
- Rough cost: ~$85–175+/month (check current Render pricing) + $99/yr Apple.

## Phases

- [ ] **0. Feasibility spike** (biggest risk)
  - [x] Repo layout (`render.yaml`, `server/`, `api/`, `ios/`) + spike endpoints (`server/app/spike.py`, driver `server/scripts/spike.sh`)
  - [ ] Minimal FastAPI service on Render Oregon (code ready; needs Blueprint + env vars)
  - [ ] Service EDL login; get LP DAAC s3credentials; read an EMIT granule directly from S3 (confirms Render's outbound IPs count as us-west-2)
  - [ ] Measure S3 throughput, prepare time, per-tap round-trip time from the phone
  - [ ] Find AVIRIS-NG / AVIRIS-3 collection short names, formats, projections, sizes in CMR (names + formats done, see findings; projections/sizes need file reads)
  - [ ] Confirm EMIT NetCDF layout and GLT conventions (V002 — run `/spike/emit/layout` on Render)
  - [ ] Request the EDL app-creator permission / register the OAuth app (in parallel; approval can take a while)
- [ ] **1. Server MVP, EMIT only:** search, prepare (GLT, tiles, BIP memmap), spectrum, block. Dev static token auth.
- [ ] **2. iOS MVP:** scene list, `MKMapView` with browse overlay → `MKTileOverlay` subclass with auth header, spectrum chart, location tracking, tap/drag, manual location override.
- [ ] **3. Auth:** EDL OAuth, one-time-code exchange, JWT + refresh, allowlist, admin CLI, request-access list.
- [ ] **4. AVIRIS-NG / AVIRIS-3** readers incl. range reads and progressive tiles.
- [ ] **5. Performance:** phone block cache, early prefetch, keep-alive ping, LRU disk/RAM eviction, optional PCA, App Attest.
- [ ] **6. Ship:** privacy policy, App Store privacy labels, TestFlight external link, reviewer account.

## Phase 0 findings

From public CMR queries, 2026-11-06:

| Instrument | Collection (short_name / version) | Provider | Format | Notes |
|---|---|---|---|---|
| EMIT | `EMITL2ARFL` **v002** (v001 still listed) | LPCLOUD | NetCDF-4: `*_RFL_*.nc` + `*_RFLUNCERT_*.nc` + browse `.png` | Masks moved to separate `EMITL2AMASK` v002/v003. Data `s3://lp-prod-protected/EMITL2ARFL.002/...`; browse PNG in `s3://lp-prod-public/...`. 11 granules over Pasadena, latest 2026-10-19. |
| AVIRIS-3 | `AV3_L2A_RFL_2357` v1 | ORNL_CLOUD | NetCDF-4 **orthocorrected** `*_RFL_ORT.nc`, `*_UNC_ORT.nc`, `*_QL.tif`, `*_BROWSE.jpg`, `.yaml` | 511 granules, latest 2025-01-23. `s3://ornl-cumulus-prod-protected/aviris/AV3_L2A_RFL/data/` |
| AVIRIS-5 (new) | `AV5_L2A_RFL_2484` v1 | ORNL_CLOUD | same layout as AVIRIS-3 | 6050 granules, latest 2026-06-05. Consider adding to the search. |
| AVIRIS-NG | `AVIRIS-NG_L2_Reflectance_2110` v1 | ORNL_CLOUD | ENVI `*_rfl_*_img.bin` (+ .hdr), one CMR granule per file | 10714 granules, latest 2022-09-17. No browse image in CMR → must generate thumbnails. 0 hits at Pasadena point. |

Implications:
- AVIRIS-3/5 are NetCDF and already orthorectified → can probably share most of the EMIT reader minus GLT. Only AVIRIS-NG needs the ENVI/BIL path, so Phase 4 shrinks.
- Service account needs both LP DAAC and ORNL DAAC S3 credentials (spike handles both).
- CMR `ArchiveAndDistributionInformation.Size` is empty for these → get sizes from S3 `HEAD` during the spike.
- EMIT full float32 cube ≈ 1.8 GB; the spike streams it in chunk-row blocks to an int16 memmap (~0.9 GB) so it runs on small instances.

Still to measure on Render: outbound IP / region, S3 MB/s, prepare time, cold pixel read, phone RTT.

## Gaps still to design
- **Masks/quality:** EMIT L2A mask product (clouds, bad pixels); cloud-cover filtering; EMIT uncertainty display.
- **Scene edges:** adjacent same-overpass granules — mosaic, or auto-switch scenes.
- **Long result lists:** ranking, filtering, paging (years of overpasses).
- **Spectrum UX:** units/axes, compare pins, CSV/share export.
- **Products:** reflectance only, or also radiance (EMIT L1B) / mineral products?
- **Credit:** EMIT/AVIRIS citations and DOIs in the app.
- **App states:** no data here, offline, permission denied, scene preparing, login expired, prepare failed.
- **Server ops:** recover jobs after restart, handle granules that fail to convert, logs/latency metrics/alerts.
- **Security:** JWT key storage/rotation, OAuth `state`, rate limits, admin endpoint protection.
- **Privacy:** don't log precise user coordinates.
- **Engineering:** git + GitHub, Render auto-deploy, Xcode Cloud or fastlane for TestFlight; tests for lat/lon→pixel lookups against known EMIT pixels, endpoint tests, GPX walk simulation.
- **Open decisions (user):** app name / bundle ID, Apple Developer account, Render plan, EDL app-creator access. Minimum iOS 17 assumed; foreground location only.

## Reusable code
`/Users/jfahlen/Documents/src/MyLocation/locsync/ios/LocLogger/LocationManager.swift` — `CLLocationManager` wrapper from an earlier project.
