"""Tiny FastAPI app served by uvicorn for the fastapi_* workloads."""

from fastapi import FastAPI

from bench.workloads import count_primes

app = FastAPI()


@app.get("/sync-cpu")
def sync_cpu(limit: int = 30_000) -> dict:
    # A plain ``def`` endpoint: Starlette runs it on an anyio worker thread,
    # so with the GIL these requests serialise; without it they run in parallel.
    return {"primes": count_primes(2, limit)}


@app.get("/async-json")
async def async_json() -> dict:
    # Pure event-loop work; free-threading is not expected to help here.
    return {"ok": True}
