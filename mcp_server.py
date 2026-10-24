"""MCP stdio bridge for the controller API.

Every tool sends X-Source: mcp. The MCP process does not implement control
logic itself; validation, state transitions, and event logging stay in controller.
"""
from __future__ import annotations
import json
import os
from typing import Any
import httpx
try:
    from mcp.server.fastmcp import FastMCP as MCPServer
except (ImportError, ModuleNotFoundError):
    from mcp.server import MCPServer

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8000").rstrip("/")
mcp = MCPServer("DockRoute Load Balancer")

async def request(method: str, path: str, body: dict[str, Any] | None = None) -> str:
    try:
        async with httpx.AsyncClient(timeout=130) as client:
            response = await client.request(method, f"{CONTROLLER_URL}{path}", json=body, headers={"X-Source": "mcp"})
        try:
            data = response.json()
        except ValueError:
            data = {"message": response.text}
        if not response.is_success:
            return json.dumps({"ok": False, "http_status": response.status_code, "error": data.get("detail", data)}, indent=2)
        return json.dumps({"ok": True, "data": data}, indent=2)
    except httpx.HTTPError as exc:
        return json.dumps({"ok": False, "error": f"Cannot reach controller at {CONTROLLER_URL}: {exc}"}, indent=2)

@mcp.tool()
async def get_status() -> str:
    """Get runtime, health, routing eligibility, configured/effective split, in-flight counts, metrics, and rollout state."""
    return await request("GET", "/api/status")

@mcp.tool()
async def status() -> str:
    """Get runtime, health, routing eligibility, configured/effective split, in-flight counts, metrics, and rollout state."""
    return await get_status()

@mcp.tool()
async def set_traffic_split(a_percent: int, b_percent: int) -> str:
    """Set desired traffic weights for A and B. Percentages must be 0–100, sum to 100, and any nonzero target must be healthy, running, and enabled."""
    return await request("POST", "/api/split", {"a": a_percent, "b": b_percent})

@mcp.tool()
async def traffic_split(a_percent: int, b_percent: int) -> str:
    """Set desired traffic weights for A and B. Percentages must be 0–100, sum to 100, and any nonzero target must be healthy, running, and enabled."""
    return await set_traffic_split(a_percent, b_percent)

@mcp.tool()
async def start_container(container: str) -> str:
    """Start A or B and wait for its health endpoint before enabling routing eligibility."""
    return await request("POST", f"/api/containers/{container}/start")

@mcp.tool()
async def stop_container(container: str, drain_timeout_seconds: float = 10) -> str:
    """Remove A or B from routing, wait for in-flight requests to finish, then stop it. A healthy alternative must remain available."""
    return await request("POST", f"/api/containers/{container}/stop?drain_timeout={drain_timeout_seconds}")

@mcp.tool()
async def safe_rollout(target_container: str, health_timeout_seconds: float = 15, drain_timeout_seconds: float = 10) -> str:
    """Safely roll out to A or B: start target, wait for health, move new traffic, drain old container, then stop old. Abort if target health times out."""
    return await request("POST", f"/api/rollout/{target_container}?health_timeout={health_timeout_seconds}&drain_timeout={drain_timeout_seconds}")

@mcp.tool()
async def get_recent_logs(limit: int = 20) -> str:
    """Get the newest controller event-log entries, including source and rejected operations."""
    return await request("GET", f"/api/logs?limit={max(1, min(int(limit), 200))}")

@mcp.tool()
async def recent_logs(limit: int = 20) -> str:
    """Get the newest controller event-log entries, including source and rejected operations."""
    return await get_recent_logs(limit)

@mcp.tool()
async def force_unhealthy(container: str, enabled: bool = True) -> str:
    """Demo/testing control: force a container health endpoint unhealthy or restore it. Health monitor then triggers failover/recovery."""
    return await request("POST", f"/api/containers/{container}/unhealthy", {"enabled": enabled})

@mcp.tool()
async def reset_metrics() -> str:
    """Reset cumulative traffic metrics counters and current weights to zero."""
    return await request("POST", "/api/metrics/reset")

if __name__ == "__main__":
    mcp.run(transport="stdio")
