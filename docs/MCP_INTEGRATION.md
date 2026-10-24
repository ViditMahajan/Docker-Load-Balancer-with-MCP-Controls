# DockRoute: Model Context Protocol (MCP) Integration

This document details the **Model Context Protocol (MCP)** server architecture, available tool interfaces, and configuration instructions for integrating **DockRoute** with AI agents (Claude Desktop, Cursor, Antigravity IDE, etc.).

---

## 1. Architectural Philosophy: The Delegating Bridge Pattern

A common anti-pattern when building MCP servers is duplicating business logic, validation routines, and state stores inside the MCP tool handlers.

DockRoute implements the **Delegating Bridge Pattern**:
- The MCP server (`mcp_server.py`) is completely stateless.
- It translates incoming JSON-RPC tool invocations into authenticated HTTP calls to the controller's internal REST API.
- It attaches an explicit request header: `X-Source: mcp`.
- **Why this matters**:
  1. **Single Source of Truth**: All validation rules (0–100 bounds, sum=100, eligibility checks, operation locking) live strictly inside `controller/main.py`.
  2. **Audit Logging**: Every action triggered by an AI model is tagged in the event log with `source: mcp`, clearly distinguishing autonomous model decisions from human dashboard actions.
  3. **Zero Divergence**: New validation rules added to the controller automatically protect MCP callers without touching `mcp_server.py`.

---

## 2. MCP Interaction Architecture (Mermaid)

```mermaid
graph LR
    subgraph HostSystem["AI Agent Host Environment"]
        Agent["Claude Desktop / Cursor / Antigravity IDE<br/>(LLM Context)"]
        Client["MCP Client Engine<br/>(JSON-RPC 2.0 protocol)"]
    end

    subgraph MCPServerProcess["DockRoute MCP Server Process (mcp_server.py)"]
        FastMCP["FastMCP Stdio Server<br/>(reads stdin, writes stdout)"]
        HTTPClient["Async HTTP Client<br/>(httpx, timeout=130s)"]
    end

    subgraph ControllerService["DockRoute Controller Service (:8000)"]
        API["FastAPI Control Endpoints<br/>(Headers: X-Source: mcp)"]
        Audit["Event Audit Ring Buffer<br/>(Deque maxlen=500)"]
        State["Authoritative State Store"]
    end

    Agent -->|Model decides to call tool| Client
    Client -->|JSON-RPC via stdio| FastMCP
    FastMCP -->|Dispatches tool handler| HTTPClient
    HTTPClient -->|REST API Request| API
    API -->|Logs action with source='mcp'| Audit
    API -->|Mutates state safely| State
    API -->>|JSON response payload| HTTPClient
    HTTPClient -->>|Formatted JSON string| FastMCP
    FastMCP -->>|JSON-RPC result| Client
    Client -->>|Injected into LLM context| Agent
```

---

## 3. Complete Reference of Available MCP Tools

All tools are implemented in [mcp_server.py](file:///C:/Users/vidit/Desktop/docker-load-balancer-mcp/mcp_server.py):

| Tool Name | Parameters | Description | Controller Endpoint |
| :--- | :--- | :--- | :--- |
| `get_status` / `status` | None | Returns complete system state: containers, health, runtime, routing eligibility, configured & effective split, in-flight counts, request metrics, and active rollout phase. | `GET /api/status` |
| `set_traffic_split` / `traffic_split` | `a_percent: int`, `b_percent: int` | Sets the desired traffic split. Requires $a, b \ge 0$, $a + b = 100$, and any target with weight $> 0$ must be running, healthy, and enabled. | `POST /api/split` |
| `start_container` | `container: str` (`"a"` or `"b"`) | Starts the specified container via Docker socket and waits for health endpoint before enabling routing. | `POST /api/containers/{name}/start` |
| `stop_container` | `container: str`, `drain_timeout_seconds: float = 10` | Removes container from routing, drains active requests, then stops it via Docker socket. Rejects with 409 if it is the last healthy container. | `POST /api/containers/{name}/stop` |
| `safe_rollout` | `target: str` (`"a"` or `"b"`), `health_timeout_seconds: float = 15`, `drain_timeout_seconds: float = 10` | Executes zero-downtime rolling update: starts target, awaits health, switches traffic, drains old container, stops old container. | `POST /api/rollout/{target}` |
| `force_unhealthy` | `container: str`, `enabled: bool = True` | Injects synthetic failure or restores health on container A or B for failover testing. | `POST /api/containers/{name}/unhealthy` |
| `get_recent_logs` | `limit: int = 50` | Retrieves recent structured event log entries (newest first). | `GET /api/logs?limit={limit}` |
| `reset_metrics` | None | Resets total request counters, success/failure counts, and per-container metrics to zero. | `POST /api/metrics/reset` |

---

## 4. MCP Configuration Examples

### Claude Desktop Configuration (`claude_desktop_config.json`)

On Windows: `%APPDATA%\Claude\claude_desktop_config.json`  
On macOS/Linux: `~/.config/Claude/claude_desktop_config.json`

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

### Cursor / Antigravity IDE Configuration (`mcp_config.json`)

```json
{
  "mcpServers": {
    "dockroute-lb": {
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

---

## 5. Sample Tool Invocation Prompts

Once configured, an LLM agent can autonomously orchestrate and inspect the cluster using natural language:

- *"Check the current health and traffic split of the DockRoute load balancer."*  
  $\rightarrow$ Invokes `get_status()`.
- *"Shift 80% of traffic to Container A and 20% to Container B."*  
  $\rightarrow$ Invokes `set_traffic_split(a_percent=80, b_percent=20)`.
- *"Perform a safe zero-downtime rollout to Container B."*  
  $\rightarrow$ Invokes `safe_rollout(target="b")`.
- *"Simulate an outage on Container A and show me the resulting event log."*  
  $\rightarrow$ Invokes `force_unhealthy(container="a", enabled=True)` then `get_recent_logs(limit=10)`.
