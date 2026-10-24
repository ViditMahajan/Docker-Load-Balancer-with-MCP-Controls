# DockRoute — Docker Load Balancer with MCP Controls

A local educational implementation of a health-aware load balancer and container controller. It demonstrates weighted routing, health failover, in-flight request draining, safe rollout, an event-log dashboard, and MCP tools. It is intended for a local demo, not production deployment.

## What is included

- **`app/`** — tiny FastAPI application image used twice, labelled A and B. `/` identifies which container answered, `/health` and `/ready` report health, `/delay?seconds=5` keeps a request in flight, and `/admin/unhealthy` forces/restores health for a demo.
- **`controller/main.py`** — controller API and reverse proxy. It tracks runtime, health, routing eligibility, in-flight counts, split, request metrics, rollout state, and events.
- **`static/index.html`** — live dashboard with controls, effective/configured split, traffic generator, health toggles, and recent events.
- **`mcp_server.py`** — MCP stdio server. It delegates to the controller API with `X-Source: mcp`, rather than keeping a second copy of the business rules.
- **`tests/`** — unit tests for percentage validation and routing policy.

## Architecture

```text
                 ┌──────────────────────────┐
Browser /        │ Controller + reverse     │──────────────┐
Traffic generator│ proxy :8000               │              │
      ──────────>│ validation + routing      │              │
                 │ health monitor + draining │              │
                 │ rollout + event log       │              │
                 └────────────┬──────────────┘              │
                              │ Docker Engine API            │
                              │ (mounted Docker socket)       │
                       ┌──────┴──────┐                       │
                       ▼             ▼                       │
                  ┌────────┐    ┌────────┐    requests       │
                  │ App A  │    │ App B  │<──────────────────┘
                  │ :8000  │    │ :8000  │
                  └────────┘    └────────┘

        MCP client ──stdio──> mcp_server.py ──HTTP──> controller :8000
```

The dashboard and MCP server are clients of the same controller API. Only the controller implements validation and state transitions. The state lock protects short atomic changes; it is **not held while waiting for requests to drain**, so the health monitor can react during a rollout. A separate operation lock makes conflicting control actions fail promptly with HTTP 409.

## Requirements

1. Docker Desktop installed and running (Linux containers enabled).
2. Python 3.11 or 3.12 on Windows for local MCP tools and tests.
3. Git/VS Code/Antigravity are optional; no cloud service is required.

> **Docker socket warning:** The controller mounts `/var/run/docker.sock` so it can start and stop containers. Access to this socket is highly privileged. Keep this project local; do not expose the controller port to the public internet or deploy this exact configuration to a shared host.

## Run the application

Open PowerShell in the extracted project folder:

```powershell
docker compose up --build
```

Wait about 10–20 seconds for the images, controller health monitor, and app containers to start. Open **http://localhost:8000**. The controller's liveness endpoint is **http://localhost:8000/healthz**.

Check which app responds directly through the load balancer:

```powershell
Invoke-RestMethod http://localhost:8000/proxy
Invoke-RestMethod "http://localhost:8000/proxy/delay?seconds=5"
```

The `container` field in the JSON identifies A or B. To see logs:

```powershell
docker compose logs -f controller
```

Stop the stack with `Ctrl+C`, then in another terminal run `docker compose down` if needed.

### If startup fails

- Confirm Docker Desktop is running and set to Linux containers.
- Run `docker compose ps` and `docker compose logs controller app-a app-b`.
- If Docker socket access fails, the controller will report `unknown` runtime and a Docker inspection error in the dashboard/event log. Do not remove the socket mount and expect lifecycle controls to work; the controller needs Docker Engine access to start/stop app containers.
- If port 8000 is already in use, change the left side of `"8000:8000"` in `docker-compose.yml`, then use that port for browser, MCP `CONTROLLER_URL`, and test commands.

## Run tests

Run pytest in PowerShell (20 automated unit and scenario tests covering splits, failovers, 503 errors, in-flight draining, last healthy container rejection, safe rollouts, rollback aborts, and MCP audit logging):

```powershell
python -m pytest tests/ -v
```

Or using an explicit virtual environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt pytest-asyncio
python -m pytest tests/ -v
```

## CLI Traffic Generator

In addition to the Web Dashboard controls, a deterministic Layer 3 CLI traffic generator is available in `execution/traffic_generator.py`:

```powershell
# Send 60 requests per minute with live stats
python execution/traffic_generator.py --url http://localhost:8000 --rpm 60

# Send requests with 2-second delay for 20 seconds
python execution/traffic_generator.py --url http://localhost:8000 --rpm 30 --delay 2 --duration 20
```

## MCP setup for Antigravity / Claude / Cursor

The main Docker stack exposes the controller at `http://localhost:8000`. Run the MCP server as a local stdio process. Add an MCP server entry to your MCP configuration:

```json
{
  "mcpServers": {
    "dockroute": {
      "command": "python",
      "args": ["C:\\Users\\vidit\\Desktop\\docker-load-balancer-mcp\\mcp_server.py"],
      "env": { "CONTROLLER_URL": "http://localhost:8000" }
    }
  }
}
```

Adjust the path if you extract the folder elsewhere, then restart/reload the MCP client. Available tools:

| MCP tool | Alias | What it does |
|---|---|---|
| `get_status` | `status` | Read runtime/health/routing/split/metrics/rollout |
| `set_traffic_split` | `traffic_split` | Set A/B percentages with validation |
| `start_container` | — | Start one container and wait for health |
| `stop_container` | — | Drain and stop one container safely |
| `safe_rollout` | — | Start target → healthy → shift → drain old → stop old |
| `get_recent_logs` | `recent_logs` | Read recent event-log entries |
| `force_unhealthy` | — | Demo health failure and recovery |

Every MCP tool delegates to the same controller API and marks its source as `mcp`. Invalid requests made through the controller API are recorded as rejected/error events.

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/status` | Full controller state and metrics |
| `GET` | `/api/logs?limit=50` | Newest events first |
| `POST` | `/api/split` | Body `{"a":70,"b":30}` |
| `POST` | `/api/containers/a/start` | Start A and wait for health |
| `POST` | `/api/containers/a/stop?drain_timeout=10` | Drain then stop A |
| `POST` | `/api/rollout/b?health_timeout=15&drain_timeout=10` | Safe rollout to B |
| `POST` | `/api/containers/b/unhealthy` | Body `{"enabled":true}` for demo failover |
| `GET` | `/proxy` or `/proxy/delay?seconds=5` | Route a request through the controller |

## Suggested 3–5 minute demo video

1. **0:00–0:30 — Overview.** Show the dashboard and explain app A/B, controller, health monitor, and MCP bridge.
2. **0:30–1:05 — Weighted routing.** Set A/B to 70/30, send traffic, and show per-container request counts.
3. **1:05–1:45 — Health failover.** Force A unhealthy. Show effective traffic move to B; restore A after health returns.
4. **1:45–2:40 — Drain and rollout.** Start a long request using `/proxy/delay?seconds=8`, then trigger a safe rollout to the other container. Show new traffic shift and the in-flight request finish before old-container stop.
5. **2:40–3:30 — Validation.** Try an invalid split (e.g. 80/50), then try to assign positive weight to a stopped/unhealthy target. Show the rejected event and error message.
6. **3:30–4:15 — MCP.** Use `get_status`, `set_traffic_split`, and `get_recent_logs` from Gemini/your MCP client. Point out the event source `mcp`.
7. **4:15–4:45 — Tests and wrap-up.** Run `python -m pytest -q`, then recap key safety rules.

Don't rush the drain demo: a delayed request should be started **before** triggering the stop/rollout so it is visibly in flight.

## Design decisions and limitations

- **FastAPI** provides typed HTTP endpoints and a straightforward async proxy.
- **Docker Compose** starts the two identical demo apps and controller together.
- **Plain HTML/CSS/JavaScript** keeps the dashboard lightweight and avoids a separate frontend build.
- **Python MCP SDK** exposes the controller functions through MCP stdio.
- **In-memory event log/metrics/state** make the assignment easy to run locally. They reset when the controller restarts and are not a persistent database.
- **One controller process** is assumed. An in-memory lock cannot coordinate multiple controller replicas; a production multi-replica design would need shared state and distributed coordination.
- **Simple weighted random selection** demonstrates percentage routing over many requests; exact percentages are statistical, not guaranteed for each small batch.
- **No blind proxy retries** are performed, because automatically repeating non-idempotent requests could duplicate side effects.
- Health is based on the application health endpoint and Docker runtime. Production designs would require stronger health semantics, authentication, audit persistence, request limits, graceful shutdown, and hardened Docker access.

## Submission checklist

- [ ] `docker compose up --build` starts all three containers.
- [ ] Dashboard loads and status/health fields update.
- [ ] `/proxy` returns a container identity.
- [ ] Valid A/B splits work and invalid splits are rejected and logged.
- [ ] Unhealthy/stopped/draining containers do not receive new requests.
- [ ] No healthy backend gives HTTP 503.
- [ ] Drain waits for an in-flight request; drain timeout leaves the old container running but unrouted.
- [ ] Safe rollout waits for target health before switching and preserves the old service on target health timeout.
- [ ] MCP tools work and events show `source: mcp`.
- [ ] Unit tests pass; README and demo video are ready.
- [ ] Add your own screenshots, run results, and video link before final submission.
