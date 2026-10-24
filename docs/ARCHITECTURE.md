# DockRoute: System Architecture

DockRoute is a health-aware, zero-downtime HTTP load balancer designed for containerized microservices, featuring dual control planes: a real-time web dashboard and a Model Context Protocol (MCP) server for AI agents.

---

## 1. High-Level Component Architecture

```mermaid
flowchart TD
    subgraph Clients["Clients"]
        HTTP["HTTP Traffic (/proxy, /proxy/delay)"]
        UI["Web Dashboard (:8000)"]
        MCP["MCP Client (stdio / JSON-RPC)"]
    end

    subgraph Controller["Load Balancer & Controller (:8000)"]
        API["FastAPI Control Plane (/api/split, /api/rollout)"]
        MCP_SERVER["FastMCP Server (mcp_server.py)"]
        SM["StateManager (State, In-Flight & Split)"]
        LB["LoadBalancer (Reverse Proxy & SWRR Engine)"]
        HC["HealthChecker (Polling /health every 2s)"]
        RM["RolloutManager (Safe Rollout & Draining)"]
        EL["EventLogger (Audit Ring Buffer maxlen=500)"]
        DS["DockerService (Docker Engine Socket)"]
    end

    subgraph Containers["Backends (docker-lb-net)"]
        A["Container A: app-a (:8000)"]
        B["Container B: app-b (:8000)"]
    end

    HTTP --> LB
    UI <--> API
    MCP <--> MCP_SERVER
    API --> SM
    MCP_SERVER --> API
    SM --> LB
    SM --> RM
    RM --> DS
    HC --> A
    HC --> B
    LB -->|Effective Split| A
    LB -->|Effective Split| B
    EL --> UI
```

---

## 2. Core Subsystems

1. **FastAPI Control Plane**: Exposes administrative endpoints for configuring splits, triggering rollouts, checking cluster status, and reviewing audit logs.
2. **Reverse Proxy Core**: Intercepts client traffic on `/proxy` and forwards requests to eligible backends using Smooth Weighted Round-Robin (SWRR).
3. **State & Concurrency Management**:
   - `state_lock`: Protects in-memory counters, health statuses, and metrics. Released immediately before network forwarding to ensure zero latency impact on proxying.
   - `operation_lock`: Serializes long-running workflows (rollouts, container stops) and rejects conflicting concurrent requests with `HTTP 409 Conflict`.
4. **Health Monitor Loop**: Asynchronously probes backend `/health` endpoints every 2 seconds. Automatically shifts 100% of traffic to healthy survivors if a backend fails, and restores configured ratios upon recovery.
5. **Connection Draining & Safe Rollout**:
   - Tracks active in-flight requests per container.
   - Marks target containers `draining` during rollouts or shutdowns, allowing in-flight requests to complete while routing new traffic away.
6. **FastMCP Server**: Provides standard Model Context Protocol tools over stdio, logging all agent-executed mutations with `source: mcp`.
