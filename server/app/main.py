from fastapi import FastAPI

from app import spike

app = FastAPI(title="SpecView API", version="0.0.1")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


app.include_router(spike.router)
