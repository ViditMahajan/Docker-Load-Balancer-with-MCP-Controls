"""Tiny demo application container used to make routing and draining visible."""
import asyncio
import os
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

NAME = os.getenv("CONTAINER_LABEL", "A")
app = FastAPI(title=f"Demo App {NAME}")
_forced_unhealthy = False

class UnhealthyRequest(BaseModel):
    enabled: bool = True

@app.get("/")
async def home() -> JSONResponse:
    content = {"container": NAME, "message": f"Hello from container {NAME}", "timestamp": datetime.now(timezone.utc).isoformat()}
    return JSONResponse(content=content, headers={"X-Handled-By": NAME, "X-Container": NAME})

@app.get("/health")
async def health():
    if _forced_unhealthy:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "container": NAME}, headers={"X-Handled-By": NAME, "X-Container": NAME})
    return JSONResponse(content={"status": "healthy", "container": NAME}, headers={"X-Handled-By": NAME, "X-Container": NAME})

@app.get("/ready")
async def ready():
    return await health()

@app.get("/delay")
async def delay(seconds: float = 5.0):
    """Artificially slow endpoint for demonstrating in-flight draining."""
    seconds = max(0.0, min(seconds, 60.0))
    started = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(seconds)
    content = {"container": NAME, "requested_delay_seconds": seconds, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat()}
    return JSONResponse(content=content, headers={"X-Handled-By": NAME, "X-Container": NAME})

@app.post("/admin/unhealthy")
async def set_unhealthy(body: UnhealthyRequest):
    global _forced_unhealthy
    _forced_unhealthy = body.enabled
    return {"container": NAME, "forced_unhealthy": _forced_unhealthy}
