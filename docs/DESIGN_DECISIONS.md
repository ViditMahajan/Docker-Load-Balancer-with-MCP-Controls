# DockRoute: Design Decisions & Trade-Offs

This document captures the engineering rationale, design decisions, and architectural trade-offs made during the development of **DockRoute**.

---

## 1. Why FastAPI for Both Proxy and Control Plane?

### Decision:
Implement the load balancer, reverse proxy, and control plane API in a single FastAPI application (`controller/main.py`), rather than running an external reverse proxy (like NGINX or Envoy) sidecar.

### Rationale:
- **Zero-Divergence State**: In dynamic canary and rollout workflows, the proxy must react within sub-milliseconds to container health state changes, in-flight request completion, and weight adjustments. Running the proxy inside the Python async event loop allows direct, lock-protected memory access to `state_lock` without requiring Redis or Consul KV polling.
- **Portability**: The entire stack runs out-of-the-box with standard Python and Docker, without requiring complex Lua scripting (NGINX) or dynamic control-plane gRPC configurations (Envoy).
- **Trade-off**: Python's raw single-thread proxy throughput (approx. 2,000–5,000 req/sec) is lower than compiled C/C++ proxies like NGINX or Envoy (50,000+ req/sec). However, for a local orchestration demonstration, edge computing nodes, or educational testbed, determinism and code clarity are prioritized over raw microsecond throughput.

---

## 2. Why Smooth Weighted Round-Robin (SWRR) over Random or Least-Connections?

### Decision:
Implement SWRR for candidate selection over Weighted Random or Least-Connections.

### Comparison Matrix:

| Strategy | Micro-Batch Predictability | Concurrency Awareness | Starvation Prevention | Implementation Complexity |
| :--- | :---: | :---: | :---: | :---: |
| **Weighted Random** | Poor (Poisson clustering) | None | Medium | Low ($O(1)$) |
| **Least Connections** | Poor (does not honor exact splits) | High | Poor for asymmetric backends | Low ($O(N)$) |
| **Smooth Weighted Round-Robin** | **Exact / Optimal ($O(1)$ variance)** | Moderate | **High (optimal interleaving)** | Medium ($O(N)$) |

### Rationale:
Automated testing and canary rollouts require deterministic guarantees. A 70/30 split must produce exactly 7 A and 3 B across 10 requests. Least-connections would distort the desired percentage if one backend had slightly higher latency, and random routing caused test flakiness.

---

## 3. Two-Tier Concurrency Architecture vs. Global Locking

### Decision:
Separate `state_lock` (memory changes) from `operation_lock` (workflow serialization).

### Rationale:
Holding a single lock across an entire rollout or request drain would stall the background health monitor and block the reverse proxy from forwarding active traffic. By restricting `state_lock` to sub-millisecond memory updates and using `operation_lock` strictly to reject concurrent operator mutations with HTTP 409, the data plane remains highly responsive during administrative actions.

---

## 4. MCP Delegating Bridge Pattern vs. Embedded Logic

### Decision:
Implement the MCP server (`mcp_server.py`) as a thin HTTP bridge that forwards tool requests to the controller with `X-Source: mcp`.

### Rationale:
- **Avoid Dual Maintenance**: If validation rules change (e.g., introducing drain timeout limits or new split constraints), modifying the controller immediately updates both the Web Dashboard and the AI Agent tools.
- **Unified Security & Auditing**: The controller's event ring buffer captures both human dashboard actions and AI tool invocations with clear provenance, enabling forensic review of autonomous agent behavior.
