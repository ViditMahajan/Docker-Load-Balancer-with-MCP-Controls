# DockRoute: Request Draining & Safe Rollout Protocol

This document explains the zero-downtime rolling deployment protocol, in-flight request draining mechanics, and automated rollback triggers in **DockRoute**.

---

## 1. The Core Problem: Why Naive Container Stops Cause Outages

In production microservices, suddenly issuing `docker stop` or deleting a pod drops active TCP connections and terminates in-flight operations (e.g., database writes, file uploads, payment transactions).

DockRoute implements an **active draining protocol**:
1. It immediately removes the target container from receiving **new requests**.
2. It waits for all **in-flight requests** currently being processed by the container to complete naturally.
3. Only after the active in-flight count reaches zero (or a strict safety timeout expires) is the container instructed to stop.

---

## 2. In-Flight Request Tracking Mechanism

In `controller/main.py`, tracking is managed through atomic counter increments and decrements inside `state_lock`:

```python
# 1. On incoming request dispatch:
async with self.state_lock:
    self.containers[selected]["in_flight"] += 1

try:
    # 2. Forwarding happens OUTSIDE state_lock
    response = await client.request(...)
finally:
    # 3. Always decremented in finally block
    async with self.state_lock:
        self.containers[selected]["in_flight"] -= 1
```

During a drain:
`_wait_drained(name, timeout)` polls the container's `in_flight` counter every 100 milliseconds. As soon as `in_flight == 0`, the drain resolves immediately without unnecessary waiting.

---

## 3. Safe Rollout Protocol (Blue/Green State Machine)

The `/api/rollout/{target}` workflow transitions through an explicit state machine:

```
[start_target] ---> [waiting_healthy] ---> [switching_traffic] ---> [draining_old] ---> [stopping_old] ---> [completed]
       |                    |                                             |
       v                    v                                             v
[abort / 502]        [timeout_abort / 504]                         [target_failed_recover]
                     * Traffic stays on old                        * Old container re-enabled!
                     * Rollout marked 'rolled_back'                * Rollout marked 'rolled_back'
```

### Protocol Steps:

1. **Lock Acquisition**: Acquires `operation_lock` to serialize deployments.
2. **Phase 1: `start_target`**: Calls Docker Engine API to start the replacement container.
3. **Phase 2: `waiting_healthy`**: Polls the target's `/health` endpoint until HTTP 200 is verified.
   - **Timeout Protection**: If the target does not become healthy within `health_timeout` (default: 15s), the rollout **aborts immediately**, leaves the existing container running at 100%, and returns HTTP 504.
4. **Phase 3: `switching_traffic`**: Switches `configured_split` to 100% target / 0% old. All subsequent incoming requests are routed to the new container.
5. **Phase 4: `draining_old`**: Marks the old container's routing state as `draining`. Waits for active `in_flight` requests to reach zero.
6. **Phase 5: `stopping_old`**: Issues `docker stop` to the old container via the Docker Engine API.
7. **Phase 6: `completed`**: Releases `operation_lock` and records full rollout metrics in the event log.

---

### 3.1 Rollout Lifecycle State Machine

```mermaid
flowchart LR
    %% COLOR & STYLE DEFINITIONS
    classDef startPhase fill:#1e3799,stroke:#54a0ff,stroke-width:2px,color:#ffffff;
    classDef waitPhase fill:#6c5ce7,stroke:#a29bfe,stroke-width:2px,color:#ffffff;
    classDef switchPhase fill:#006266,stroke:#1dd1a1,stroke-width:2px,color:#ffffff;
    classDef drainPhase fill:#d35400,stroke:#e67e22,stroke-width:2px,color:#ffffff;
    classDef donePhase fill:#104e32,stroke:#2ed573,stroke-width:2px,color:#ffffff;
    classDef rollPhase fill:#b71540,stroke:#eb2f06,stroke-width:2px,color:#ffffff;

    P1["🚀 1. start_target<br/>(Docker API starts target)"]:::startPhase --> P2["⏳ 2. waiting_healthy<br/>(Polls /health up to 15s)"]:::waitPhase
    
    P2 -->|"✅ Target Healthy"| P3["🔀 3. switching_traffic<br/>(Traffic to target, old marked draining)"]:::switchPhase
    P2 -->|"❌ Health Timeout"| RB["🚨 rolled_back<br/>(Abort & release lock)"]:::rollPhase

    P3 --> P4["💧 4. draining_old<br/>(Wait for in_flight == 0)"]:::drainPhase

    P4 -->|"✅ All In-Flight Done"| P5["🛑 5. stopping_old<br/>(Docker API stops old)"]:::startPhase
    P4 -->|"💥 Target Crashes Mid-Drain"| RB

    P5 --> P6["🎉 6. completed<br/>(Zero downtime achieved)"]:::donePhase
```

---

## 4. Safe Rollout Sequence (Mermaid)

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 👤 Operator / 🤖 MCP Agent
    participant Ctrl as ⚖️ Controller Core
    participant Docker as 🐳 Docker Engine Socket
    participant NewApp as 🔵 Target Container (B)
    participant OldApp as 🟢 Old Container (A)

    Operator->>Ctrl: POST /api/rollout/b
    activate Ctrl
    
    rect rgb(240, 248, 255)
        Note over Ctrl: Step 1: Concurrency Guard
        Ctrl->>Ctrl: Acquire operation_lock (rejects concurrent ops with HTTP 409)
    end

    rect rgb(238, 242, 255)
        Note over Ctrl,Docker: Step 2: Spin Up Target Container
        Ctrl->>Docker: POST /containers/app-b/start
        Ctrl->>Ctrl: Set rollout.phase = "waiting_healthy"
    end
    
    rect rgb(230, 249, 245)
        Note over Ctrl,NewApp: Step 3: Health Verification Loop (up to 15s)
        loop Health Probe (200ms intervals)
            Ctrl->>NewApp: GET /health
            NewApp-->>Ctrl: 200 OK {"status": "healthy"}
        end
    end

    rect rgb(243, 232, 255)
        Note over Ctrl: Step 4: Atomic Traffic Shift
        Ctrl->>Ctrl: Phase: switching_traffic
        Ctrl->>Ctrl: Set configured_split = {a: 0, b: 100}
        Ctrl->>Ctrl: Set OldApp.routing = "draining" (New requests rejected)
    end
    
    rect rgb(255, 248, 230)
        Note over Ctrl,OldApp: Step 5: Active Request Draining
        Ctrl->>Ctrl: Phase: draining_old
        loop Active In-Flight Poll (every 100ms)
            Ctrl->>Ctrl: Check OldApp.in_flight == 0
        end
    end
    
    rect rgb(232, 245, 233)
        Note over Ctrl,Docker: Step 6: Safe Teardown & Completion
        Ctrl->>Ctrl: Phase: stopping_old
        Ctrl->>Docker: POST /containers/app-a/stop
        Ctrl->>Ctrl: Set OldApp.runtime = "stopped"
        Ctrl->>Ctrl: Set rollout.phase = "completed"
        Ctrl->>Ctrl: Release operation_lock
    end
    
    Ctrl-->>Operator: 🚀 HTTP 200 OK {"status": "completed", "winner": "b"}
    deactivate Ctrl
```

---

## 5. Mid-Drain Self-Healing Rollback

An exceptional resilience feature implemented in DockRoute:
- If the replacement container **crashes or turns unhealthy while the old container is draining**, the background health monitor instantly detects the failure.
- It immediately cancels the stop workflow, re-enables routing on the old container (`routing = "enabled"`), returns `configured_split` to the old container, and marks the rollout as `rolled_back`.
- Zero dropped requests occur during unexpected target failure.
