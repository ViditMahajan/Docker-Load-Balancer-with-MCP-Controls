# DockRoute: API & MCP Reference

This document provides the complete API specification for the **FastAPI Controller** and the **Model Context Protocol (MCP)** server.

---

## 1. Global Conventions & Error Handling

- **Base URL**: `http://localhost:8000`
- **Audit Source Header (`X-Source`)**:
  - `dashboard`: Requests from the Web UI.
  - `mcp`: Requests via the FastMCP stdio bridge.
  - `traffic-generator`: Requests from automated testing/benchmarks.
  - `api`: Direct HTTP requests (default).
- **Status Codes**:
  - `HTTP 200 OK`: Request succeeded.
  - `HTTP 409 Conflict`: Returned if an operation lock is held, attempting to stop the last healthy container, or routing traffic to an unhealthy target.
  - `HTTP 422 Unprocessable Entity`: Returned if split percentages are invalid ($a, b < 0$ or $a + b \ne 100$).
  - `HTTP 503 Service Unavailable`: Returned on `/proxy` if no backends are healthy.
  - `HTTP 504 Gateway Timeout`: Returned during rollouts if the target fails health checks within timeout.

---

## 2. REST API Endpoints

### Traffic Ingress
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/proxy` | Proxies request to Container A or B according to the effective split. Returns `X-Handled-By: A` or `B`. |
| `GET` | `/proxy/delay?seconds=N` | Proxies request to a backend with an artificial delay (for testing connection draining). |

### State & Telemetry
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/status` | Returns complete cluster state, container health, configured/effective splits, and in-flight counts. |
| `GET` | `/api/logs?limit=50` | Returns recent structured audit log events (newest first, max 200). |
| `POST` | `/api/metrics/reset` | Resets total and per-container request metrics to zero. |

### Configuration & Control
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/split` | Sets desired traffic split: `{"a": 70, "b": 30}` (must sum to 100). |
| `POST` | `/api/containers/{name}/start` | Starts container (`a` or `b`) and waits for `/health` to return 200. |
| `POST` | `/api/containers/{name}/stop?drain_timeout=10` | Drains in-flight requests and stops container. Rejects if last healthy backend. |
| `POST` | `/api/containers/{name}/unhealthy` | Simulates synthetic failure: `{"enabled": true}`. |
| `POST` | `/api/rollout/{target}?health_timeout=15&drain_timeout=10` | Executes automated zero-downtime rolling update to `target`. |

---

## 3. Model Context Protocol (MCP) Tools

The MCP server runs over standard I/O (stdio) via FastMCP in [mcp_server.py](file:///C:/Users/vidit/Desktop/docker-load-balancer-mcp/mcp_server.py):

| Tool Name | Parameters | Description | Controller Endpoint |
| :--- | :--- | :--- | :--- |
| `get_status` | None | Returns cluster health, splits, metrics, and rollout phase. | `GET /api/status` |
| `set_traffic_split` | `a_percent: int, b_percent: int` | Sets traffic split percentages (sum must equal 100). | `POST /api/split` |
| `start_container` | `container: "a" \| "b"` | Starts container and waits for verified health. | `POST /api/containers/{name}/start` |
| `stop_container` | `container: "a" \| "b", drain_timeout_seconds: float` | Drains active connections and stops container. | `POST /api/containers/{name}/stop` |
| `safe_rollout` | `target: "a" \| "b", health_timeout_seconds: float, drain_timeout_seconds: float` | Executes 5-phase zero-downtime rollout. | `POST /api/rollout/{target}` |
| `force_unhealthy` | `container: "a" \| "b", enabled: bool` | Simulates container failure for chaos testing. | `POST /api/containers/{name}/unhealthy` |
| `get_recent_logs` | `limit: int` | Retrieves newest audit log entries. | `GET /api/logs?limit={limit}` |
| `reset_metrics` | None | Resets cluster request metrics. | `POST /api/metrics/reset` |

---

## 4. MCP Client Configuration

### Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "dockroute": {
      "command": "python",
      "args": [
        "C:\\Users\\vidit\\Desktop\\docker-load-balancer-mcp\\mcp_server.py"
      ],
      "env": {
        "CONTROLLER_URL": "http://localhost:8000"
      }
    }
  }
}
```

### Cursor / Antigravity (`mcp_config.json`)
```json
{
  "mcpServers": {
    "dockroute": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "${workspaceFolder}",
      "env": {
        "CONTROLLER_URL": "http://localhost:8000"
      }
    }
  }
}
```
