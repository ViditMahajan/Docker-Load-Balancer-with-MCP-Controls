# DockRoute: REST API Reference

The **DockRoute Controller** exposes an HTTP REST API on port `8000`. This API serves both the web dashboard and the MCP stdio bridge.

---

## 1. Global Headers & Conventions

- **`X-Source` (Optional, Header)**: Identifies the originating client. Allowed values:
  - `dashboard`: Web browser UI.
  - `mcp`: AI Agent via MCP stdio bridge.
  - `traffic-generator`: Automated benchmark or test script.
  - `api`: Direct curl or custom integration (default).
- **Error Responses**: All errors return a JSON payload with either `detail` (FastAPI standard) or `error`.
  - `HTTP 409 Conflict`: Returned when an operation lock is held, when attempting to stop the last healthy container, or when assigning traffic to an unhealthy target.
  - `HTTP 422 Unprocessable Entity`: Returned when split percentages are out of range $[0, 100]$ or do not sum to 100.
  - `HTTP 503 Service Unavailable`: Returned on `/proxy` when no backends are healthy.
  - `HTTP 504 Gateway Timeout`: Returned when a rollout target fails to report healthy before timeout.

---

## 2. Telemetry & Observation Endpoints

### `GET /api/status`
Returns the authoritative system state.

- **Response `200 OK`**:
```json
{
  "containers": {
    "a": {
      "name": "A",
      "container_name": "app-a",
      "runtime": "running",
      "health": "healthy",
      "routing": "enabled",
      "in_flight": 0,
      "last_health_check": "2026-10-24T09:15:00+00:00",
      "last_error": null,
      "status": "healthy"
    },
    "b": { ... }
  },
  "configured_split": {"a": 70, "b": 30},
  "effective_split": {"a": 70, "b": 30},
  "metrics": {
    "total": 1250,
    "success": 1250,
    "failure": 0,
    "by_container": {"a": 875, "b": 375}
  },
  "rollout": null,
  "operation_busy": false
}
```

---

### `GET /api/logs?limit=50`
Returns recent structured event log entries (newest first).

- **Query Parameters**:
  - `limit` (integer, default: 50, min: 1, max: 200).
- **Response `200 OK`**:
```json
[
  {
    "timestamp": "2026-10-24T09:15:22+00:00",
    "action": "set_traffic_split",
    "source": "dashboard",
    "result": "success",
    "details": "Configured split set to A=70%, B=30%."
  }
]
```

---

### `POST /api/metrics/reset`
Resets the total request metrics and per-container request counters to zero.

- **Response `200 OK`**: Returns updated system status.

---

## 3. Traffic Management Endpoints

### `POST /api/split`
Configures the desired traffic ratio between Container A and Container B.

- **Request Body (`application/json`)**:
```json
{
  "a": 70,
  "b": 30
}
```
- **Validation Rules**:
  - $0 \le a \le 100$ and $0 \le b \le 100$.
  - $a + b = 100$.
  - If $a > 0$, Container A must be `running`, `healthy`, and `enabled`.
  - If $b > 0$, Container B must be `running`, `healthy`, and `enabled`.
- **Response `200 OK`**: Returns updated system status.
- **Response `422 Unprocessable Entity`**: If $a + b \ne 100$ or out of range.
- **Response `409 Conflict`**: If assigning weight to an ineligible container.

---

## 4. Container Lifecycle & Rollout Endpoints

### `POST /api/containers/{name}/start`
Starts container `a` or `b` via the Docker Engine API and blocks until `/health` returns 200.

- **Path Parameters**: `name`: `"a"` or `"b"`.
- **Response `200 OK`**: Returns updated system status.

---

### `POST /api/containers/{name}/stop?drain_timeout=10`
Safely stops container `a` or `b` with in-flight draining.

- **Path Parameters**: `name`: `"a"` or `"b"`.
- **Query Parameters**: `drain_timeout` (float, seconds, default: 10.0).
- **Safety Rejection (`HTTP 409`)**: Rejects immediately if the other container is not healthy, running, and routable.
- **Response `200 OK`**: Returns updated system status.

---

### `POST /api/rollout/{target}?health_timeout=15&drain_timeout=10`
Triggers an automated zero-downtime rolling update.

- **Path Parameters**: `target`: `"a"` or `"b"`.
- **Query Parameters**:
  - `health_timeout` (float, seconds, default: 15.0).
  - `drain_timeout` (float, seconds, default: 10.0).
- **Response `200 OK`**: Returns updated system status.
- **Response `409 Conflict`**: If another operation is already in progress.
- **Response `504 Gateway Timeout`**: If target container fails health check.

---

### `POST /api/containers/{name}/unhealthy`
Simulates synthetic failure by setting the mock container's health probe to 503.

- **Request Body**:
```json
{
  "enabled": true
}
```
- **Response `200 OK`**: Returns updated system status.

---

## 5. Reverse Proxy Data Plane

### `GET|POST|PUT|DELETE /proxy` & `/proxy/{path:path}`
Forwards traffic to backend containers using Smooth Weighted Round-Robin.

- **Query Parameters & Headers**: Transparently forwarded to the selected backend.
- **Response Headers**: Includes `X-Handled-By: A` or `X-Handled-By: B`.
- **Artificially Slow Delay Demo**: `GET /proxy/delay?seconds=5` sends requests to the backend's sleep endpoint to observe in-flight request draining.
- **Response `503 Service Unavailable`**: Returned if neither backend is eligible.
