"""FastAPI entry point: intake endpoint and the dashboard."""

from fastapi import FastAPI

app = FastAPI(title="swe-loop")


@app.get("/health")
def health() -> dict:
    return {"ok": True}
