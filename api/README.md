# API contract

Shared contract between `server/` and `ios/`.

- `openapi.yaml` — HTTP endpoints (to be written in Phase 1).
- `spectrum-format.md` — binary layout of `/spectrum` and `/block` responses (Phase 1).

Phase 0 only exposes `/healthz`, `/spike/ping`, and the guarded `/spike/*` diagnostics in `server/app/spike.py`; those are not part of the stable contract.
