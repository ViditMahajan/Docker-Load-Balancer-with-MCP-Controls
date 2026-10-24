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
flowchart LR
    %% COLOR & STYLE DEFINITIONS
    classDef agentStyle fill:#371b58,stroke:#8e24aa,stroke-width:2px,color:#ffffff;
    classDef clientStyle fill:#4c3575,stroke:#a29bfe,stroke-width:2px,color:#ffffff;
    classDef bridgeStyle fill:#1e3799,stroke:#4a69bd,stroke-width:2px,color:#ffffff;
    classDef httpStyle fill:#0c2461,stroke:#54a0ff,stroke-width:2px,color:#ffffff;
    classDef ctrlStyle fill:#006266,stroke:#1dd1a1,stroke-width:2px,color:#ffffff;
    classDef auditStyle fill:#3d3d3d,stroke:#7f8c8d,stroke-width:2px,color:#ffffff;

    subgraph HostSystem[" 🤖 AI Agent Host Environment "]
        Agent["🧠 Claude Desktop / Cursor / Antigravity<br/>(LLM Context & Reasoning)"]:::agentStyle
        Client["⚡ MCP Client Engine<br/>(Standard JSON-RPC 2.0 protocol)"]:::clientStyle
    end

    subgraph MCPServerProcess[" 🔌 DockRoute MCP Process (mcp_server.py) "]
        FastMCP["🚀 FastMCP Stdio Server<br/>(reads stdin, writes stdout)"]:::bridgeStyle
        HTTPClient["🌐 Async HTTP Client<br/>(httpx, timeout=130s)"]:::httpStyle
    end

    subgraph ControllerService[" ⚖️ DockRoute Controller Service (:8000) "]
        API["📡 FastAPI Control Endpoints<br/><code>(Headers: X-Source: mcp)</code>"]:::ctrlStyle
        Audit["📜 Event Audit Ring Buffer<br/>(Deque maxlen=500, Source tagged)"]:::auditStyle
        State["💾 Controller State Store<br/>(Weights, Metrics, Containers)"]:::ctrlStyle
    end

    Agent -->|"1. Model decides tool call"| Client
    Client -->|"2. JSON-RPC via stdio"| FastMCP
    FastMCP -->|"3. Invokes tool handler"| HTTPClient
    HTTPClient -->|"4. REST API Request"| API
    
    API -->|"5a. Log action with source='mcp'"| Audit
    API -->|"5b. Mutate state under lock"| State
    
    API -->>|"6. JSON response payload"| HTTPClient
    HTTPClient -->>|"7. Formatted JSON string"| FastMCP
    FastMCP -->>|"8. JSON-RPC result"| Client
    Client -->>|"9. Tool response injected"| Agent
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
