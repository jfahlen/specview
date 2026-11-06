# SpecView server

FastAPI app deployed to Render (Oregon) via `../render.yaml`.

## Local dev

```sh
mamba create -n specview python=3.12   # matches PYTHON_VERSION on Render
mamba run -n specview pip install -r requirements.txt
mamba run -n specview uvicorn app.main:app --reload
```

For the EDL-backed endpoints locally, export `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`
(S3 direct access only works from us-west-2, so those calls will fail off-AWS).

## Phase 0 spike

Endpoints in `app/spike.py`; all except `/spike/ping` need `Authorization: Bearer $SPIKE_TOKEN`.

1. Create the Render Blueprint from this repo (it reads `render.yaml`).
2. In the dashboard set `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` (service account; LP DAAC +
   ORNL DAAC EULAs accepted) and `SPIKE_TOKEN` (e.g. `openssl rand -hex 32`).
3. `BASE=https://<service>.onrender.com SPIKE_TOKEN=... scripts/spike.sh [lat lon]`
4. Phone RTT: open `https://<service>.onrender.com/spike/ping` repeatedly from Safari on cellular/Wi-Fi,
   or run the same curl loop from a laptop tethered to the phone.

Record results in `../PLAN.md` under "Phase 0 findings".
