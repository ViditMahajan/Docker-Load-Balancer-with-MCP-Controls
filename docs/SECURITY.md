# DockRoute: Security Considerations & Threat Model

This document outlines the security architecture, threat vectors, privilege boundaries, and production deployment considerations for **DockRoute**.

---

## 1. The Docker Unix Socket Privilege Model

### The Primary Attack Vector
In `docker-compose.yml`, the controller container mounts the host's Docker daemon socket:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

### Security Implications
Access to `/var/run/docker.sock` is equivalent to **root privileges on the host operating system**. Any process capable of executing commands inside the controller container or issuing arbitrary Docker API requests can:
1. Create privileged containers with root filesystem mounts (`-v /:/host`).
2. Read, modify, or exfiltrate host data.
3. Terminate host processes or reconfigure host networking.

### DockRoute Safeguards:
- **Local-Only Deployment**: The controller service binds to `localhost` (`127.0.0.1:8000`) for development. It is **never** intended to be exposed directly to the public internet without an authenticating reverse proxy (e.g., Traefik, NGINX, Cloudflare Tunnel).
- **No Direct Shell Execution**: The controller does not execute arbitrary shell commands or pass unsanitized input to `subprocess`. All interactions with the Docker Engine use the official Python `docker` SDK targeting strict container name constants (`CONTAINER_NAMES = {"a": "app-a", "b": "app-b"}`).

---

## 2. API Input Validation & Defense in Depth

### Pydantic Strict Typing
All incoming REST payloads are parsed and validated through Pydantic models before touching internal controller state:
- Input split percentages ($a, b$) are validated to ensure they are integers in the strict range $[0, 100]$.
- Floating-point percentages, NaN values, negative numbers, and injection characters are rejected at the HTTP transport boundary with `422 Unprocessable Entity`.
- Candidate container names are passed through `normalize_name()`:
  ```python
  def normalize_name(name: str) -> str:
      name = name.strip().lower().replace("app-", "").replace("container ", "")
      if name not in ("a", "b"):
          raise ValueError("Container must be 'a' or 'b'.")
      return name
  ```
  This eliminates directory traversal or arbitrary container targeting.

---

## 3. Denial of Service (DoS) Mitigation

1. **Serialized Control Plane (`operation_lock`)**:
   Long-running workflows (draining, safe rollouts, container restarts) acquire an exclusive lock. If an attacker or compromised agent attempts to flood `/api/rollout` or `/api/containers/a/stop`, subsequent requests fail immediately with **HTTP 409 Conflict** without queueing up memory leaks or CPU thrashing.
2. **Fixed-Memory Event Ring Buffer**:
   Event logging uses Python's `collections.deque(maxlen=500)`. When 500 events are reached, older entries are evicted automatically in $O(1)$ time. This guarantees that event logging can never cause an Out-Of-Memory (OOM) crash on the host.
3. **Bounded Delay Endpoints**:
   The artificial delay endpoint (`/proxy/delay?seconds=N`) clamps requested sleep times strictly between $0.0$ and $60.0$ seconds to prevent thread starvation.

---

## 4. MCP Security & Agent Authorization

When integrating with Model Context Protocol (MCP) clients:
- **Audit Attribution**: The MCP server attaches `X-Source: mcp` to all HTTP calls. The security team or operator can inspect `/api/logs` to see the exact timeline of actions initiated by the AI agent.
- **Fail-Safe Constraints**: Even if an LLM hallucinates an instruction (e.g., "stop all containers" or "set weight to 500%"), the controller rejects the action deterministically before any Docker API call is made.

---

## 5. Production Hardening Recommendations

For teams adapting DockRoute patterns to production environments:
1. **Docker Socket Proxy**: Replace the raw `/var/run/docker.sock` mount with a read-only or restricted socket proxy (e.g., `tecnativa/docker-socket-proxy`) that grants permissions *only* to `POST /containers/{id}/start` and `POST /containers/{id}/stop`, blocking container creation and image deletion.
2. **Mutual TLS (mTLS)**: Enforce mTLS or JWT bearer token authentication on `/api/*` endpoints.
3. **Network Isolation**: Ensure backend application containers communicate with the controller over an internal Docker bridge network that does not expose backend ports directly to the host.
