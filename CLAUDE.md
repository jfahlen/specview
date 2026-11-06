# SpecView

iPhone app + Render (Oregon) FastAPI backend for viewing EMIT / AVIRIS RGB and spectra at the user's location.

- The full plan, decisions, and phase checklist are in `PLAN.md`. Read it before starting work and keep its checkboxes up to date.
- Single repo: `server/` deploys to Render (via `render.yaml`, rootDir `server`), `ios/` is the Xcode project, `api/` holds the shared API contract.
- Auth: users sign in with NASA Earthdata Login (OAuth) and must be on the server's allowlist; the server reads data with its own service EDL account.
- Never commit secrets (EDL credentials, OAuth client secret, JWT key) — they live in Render env vars.
