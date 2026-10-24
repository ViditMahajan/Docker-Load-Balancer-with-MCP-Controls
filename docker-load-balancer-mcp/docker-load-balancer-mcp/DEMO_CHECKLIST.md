# Demo run sheet (keep this beside the screen while recording)

## Before recording
- Run `docker compose up --build` and wait until both app health indicators are healthy.
- Open `http://localhost:8000` and arrange the terminal beside the dashboard.
- Ensure MCP is connected and the MCP tool list is visible.
- Start a fresh event log view by reloading the dashboard (the log itself resets only when the controller restarts).

## Commands to prepare
```powershell
# One request through the load balancer
Invoke-RestMethod http://localhost:8000/proxy

# A long in-flight request (run this in its own terminal)
Invoke-RestMethod "http://localhost:8000/proxy/delay?seconds=8"

# Valid split
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/split -ContentType 'application/json' -Headers @{'X-Source'='api'} -Body '{"a":70,"b":30}'

# Invalid split: should return HTTP 422 and create a rejected event
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/split -ContentType 'application/json' -Body '{"a":80,"b":50}'

# Inspect recent events
Invoke-RestMethod 'http://localhost:8000/api/logs?limit=20'
```

## Capture these screens
1. Dashboard with both containers healthy and request metrics.
2. Configured split versus effective split during failover.
3. A drain/rollout with an in-flight request.
4. Rejected invalid split and corresponding event entry.
5. MCP `get_status` / `get_recent_logs` and a log with source `mcp`.
6. Passing test output.
