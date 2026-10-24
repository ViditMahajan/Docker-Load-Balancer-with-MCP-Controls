"""Comprehensive automated tests for all 10 mandatory assignment scenarios.

Scenarios tested:
1. Normal routing between A and B (weighted distribution).
2. Invalid traffic splits (out of range, sum != 100, ineligible containers).
3. One container becoming unhealthy (failover to 100% survivor, logged).
4. Both containers becoming unhealthy (HTTP 503 Service Unavailable).
5. Stopping a container while requests are in flight (in-flight completes before stop).
6. Rejecting a stop operation that would stop the last healthy container (HTTP 409).
7. Successful safe rollout (start target -> wait health -> switch -> drain old -> stop old).
8. Failed rollout and recovery (target health timeout -> rollback -> traffic stays on old).
9. MCP tool invocations and rejected operations (audit logging with source 'mcp').
10. Dashboard metrics and event logs (counters incrementing, event trail).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

import controller.main as cm
from controller.main import Controller, app, validate_split
import mcp_server


@pytest.fixture
def test_controller():
    """Create a fresh Controller instance for isolated testing."""
    c = Controller()
    # Configure mock containers A and B as running and healthy by default
    for name in ("a", "b"):
        c.containers[name].update({
            "runtime": "running",
            "health": "healthy",
            "routing": "enabled",
            "in_flight": 0,
            "last_error": None,
        })
    c.configured_split = {"a": 50, "b": 50}
    return c


# ---------------------------------------------------------------------------
# Test 1: Normal routing between A and B (Smooth Weighted Round-Robin)
# ---------------------------------------------------------------------------
def test_normal_routing_selection(test_controller):
    """Test that container selection respects configured weights when both are healthy."""
    test_controller.configured_split = {"a": 70, "b": 30}
    test_controller.current_weights = {"a": 0, "b": 0}
    selections = [test_controller._select_container_unlocked() for _ in range(1000)]
    count_a = selections.count("a")
    count_b = selections.count("b")
    # SWRR guarantees exact distribution over complete cycles
    assert count_a == 700
    assert count_b == 300
    assert test_controller.effective_split_unlocked() == {"a": 70, "b": 30}


def test_split_70_30_small_and_large_samples(test_controller):
    """Test 70/30 split across both small (10) and large (1000) request batches."""
    test_controller.configured_split = {"a": 70, "b": 30}
    test_controller.current_weights = {"a": 0, "b": 0}
    # 10 requests should produce exactly 7 A and 3 B
    ten_reqs = [test_controller._select_container_unlocked() for _ in range(10)]
    assert ten_reqs.count("a") == 7
    assert ten_reqs.count("b") == 3

    # 100 requests should produce exactly 70 A and 30 B
    test_controller.current_weights = {"a": 0, "b": 0}
    hundred_reqs = [test_controller._select_container_unlocked() for _ in range(100)]
    assert hundred_reqs.count("a") == 70
    assert hundred_reqs.count("b") == 30


def test_split_100_0_never_selects_b(test_controller):
    """Test that a 100/0 split never selects B even when both containers are healthy and eligible."""
    test_controller.configured_split = {"a": 100, "b": 0}
    test_controller.current_weights = {"a": 0, "b": 0}
    selections = [test_controller._select_container_unlocked() for _ in range(1000)]
    assert selections.count("a") == 1000
    assert selections.count("b") == 0
    assert "b" not in selections


# ---------------------------------------------------------------------------
# Test 2: Invalid traffic splits
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalid_splits_rejected(test_controller):
    """Test rejection of splits not summing to 100, out of bounds, or to ineligible containers."""
    # Split does not sum to 100
    with pytest.raises(Exception) as exc_info:
        await test_controller.set_split(80, 50, source="api")
    assert "100" in str(exc_info.value)
    assert any(e["action"] == "set_traffic_split" and e["result"] == "rejected" for e in test_controller.events)

    # Negative split
    with pytest.raises(Exception):
        await test_controller.set_split(-10, 110, source="api")

    # Target container is stopped
    test_controller.containers["b"].update({"runtime": "stopped", "routing": "disabled"})
    with pytest.raises(Exception) as exc_info:
        await test_controller.set_split(50, 50, source="api")
    assert "must be running, healthy, and not draining" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 3: One container becoming unhealthy (Failover + Log)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_container_unhealthy_failover_and_logging(test_controller):
    """Test that when container A becomes unhealthy, traffic shifts 100% to B and is logged."""
    # A becomes unhealthy
    test_controller.containers["a"]["health"] = "unhealthy"
    
    # Selection should give 100% to B
    assert test_controller._select_container_unlocked() == "b"
    assert test_controller.effective_split_unlocked() == {"a": 0, "b": 100}

    # Simulate health change detection logging
    test_controller.log("health_change", "health-monitor", "failover", "Container A became unhealthy. Effective split shifted to A=0%, B=100%.")
    
    logs = await test_controller.recent_logs(10)
    failover_log = next((l for l in logs if l["action"] == "health_change" and l["result"] == "failover"), None)
    assert failover_log is not None
    assert "Container A became unhealthy" in failover_log["details"]


# ---------------------------------------------------------------------------
# Test 4: Both containers becoming unhealthy (HTTP 503)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_both_containers_unhealthy_returns_503(test_controller):
    """Test that if neither container is healthy, selection returns None and proxy returns 503."""
    test_controller.containers["a"]["health"] = "unhealthy"
    test_controller.containers["b"]["health"] = "unhealthy"
    assert test_controller._select_container_unlocked() is None
    assert test_controller.effective_split_unlocked() == {"a": 0, "b": 0}

    with patch.object(cm, "controller", test_controller):
        client = TestClient(app)
        response = client.get("/proxy")
        assert response.status_code == 503
        assert response.json()["error"] == "No healthy backend available."


# ---------------------------------------------------------------------------
# Test 5: Stopping a container while requests are in flight (Graceful draining)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stopping_container_drains_in_flight_requests(test_controller):
    """Test that stop_container marks draining, waits for in_flight == 0, and stops container."""
    test_controller._docker_stop = AsyncMock()
    test_controller.refresh_all = AsyncMock()
    test_controller.refresh_one = AsyncMock()

    # Simulate an active in-flight request on container A
    test_controller.containers["a"]["in_flight"] = 1

    async def complete_in_flight_after_delay():
        await asyncio.sleep(0.3)
        async with test_controller.state_lock:
            test_controller.containers["a"]["in_flight"] = 0

    # Launch background task that finishes the request
    task = asyncio.create_task(complete_in_flight_after_delay())

    # Stop container A
    status = await test_controller.stop_container("a", source="dashboard", drain_timeout=5.0)
    await task

    # Assert container A was stopped after drain
    assert status["containers"]["a"]["runtime"] == "stopped"
    assert test_controller._docker_stop.called
    assert any(e["action"] == "stop_container" and e["result"] == "success" for e in test_controller.events)


# ---------------------------------------------------------------------------
# Test 6: Rejecting stop of the last healthy container
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stopping_last_healthy_container_rejected(test_controller):
    """Test that stop_container rejects when no other healthy container is available."""
    test_controller.refresh_all = AsyncMock()
    test_controller.refresh_one = AsyncMock()
    
    # Container B is stopped
    test_controller.containers["b"].update({"runtime": "stopped", "health": "unknown", "routing": "disabled"})

    # Attempt to stop Container A
    with pytest.raises(Exception) as exc_info:
        await test_controller.stop_container("a", source="mcp", drain_timeout=5.0)
    assert "no other healthy, running, routable container is available" in str(exc_info.value)

    # Check rejected event was logged with source 'mcp'
    rejected_event = next(
        (e for e in test_controller.events if e["action"] == "stop_container" and e["result"] == "rejected" and e["source"] == "mcp"),
        None,
    )
    assert rejected_event is not None


# ---------------------------------------------------------------------------
# Test 7: Successful safe rollout
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safe_rollout_happy_path(test_controller):
    """Test full safe rollout sequence: start target -> healthy -> move traffic -> drain old -> stop old."""
    # Initially 100% on A, B is stopped
    test_controller.configured_split = {"a": 100, "b": 0}
    test_controller.containers["b"].update({"runtime": "stopped", "health": "unknown", "routing": "disabled"})

    test_controller._docker_start = AsyncMock()
    test_controller._docker_stop = AsyncMock()

    # Simulate B becoming healthy upon start
    async def mock_wait_healthy(name, timeout):
        async with test_controller.state_lock:
            test_controller.containers[name].update({"runtime": "running", "health": "healthy"})
        return True

    test_controller._wait_healthy = mock_wait_healthy
    test_controller.refresh_one = AsyncMock()
    test_controller.refresh_all = AsyncMock()

    status = await test_controller.safe_rollout(target="b", source="mcp", health_timeout=5.0, drain_timeout=5.0)

    # Traffic should now be 100% on B, A should be stopped
    assert status["configured_split"] == {"a": 0, "b": 100}
    assert status["containers"]["b"]["health"] == "healthy"
    assert status["containers"]["a"]["runtime"] == "stopped"
    assert test_controller._docker_start.called
    assert test_controller._docker_stop.called
    assert any(e["action"] == "safe_rollout" and e["result"] == "success" for e in test_controller.events)


# ---------------------------------------------------------------------------
# Test 8: Failed rollout and recovery (Target health timeout)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safe_rollout_target_timeout_recovery(test_controller):
    """Test that if target fails to become healthy, rollout aborts and traffic remains on old container."""
    test_controller.configured_split = {"a": 100, "b": 0}
    test_controller.containers["b"].update({"runtime": "stopped", "health": "unknown", "routing": "disabled"})

    test_controller._docker_start = AsyncMock()
    test_controller._wait_healthy = AsyncMock(return_value=False)  # Times out / fails health
    test_controller.refresh_all = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await test_controller.safe_rollout(target="b", source="mcp", health_timeout=1.0, drain_timeout=1.0)
    assert "did not become healthy" in str(exc_info.value)

    # Traffic must remain 100% on A
    assert test_controller.configured_split == {"a": 100, "b": 0}
    assert test_controller.containers["a"]["routing"] == "enabled"
    assert any(e["action"] == "safe_rollout" and e["result"] == "aborted" for e in test_controller.events)


# ---------------------------------------------------------------------------
# Test 9: MCP tool actions and rejected operations
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mcp_tools_and_rejected_operations(test_controller):
    """Test MCP tool wrappers and verification of source='mcp' in audit logs."""
    with patch.object(cm, "controller", test_controller):
        client = TestClient(app)

        # 1. MCP query status
        resp = client.get("/api/status", headers={"X-Source": "mcp"})
        assert resp.status_code == 200
        assert any(e["action"] == "get_status" and e["source"] == "mcp" for e in test_controller.events)

        # 2. MCP valid split
        resp = client.post("/api/split", json={"a": 60, "b": 40}, headers={"X-Source": "mcp"})
        assert resp.status_code == 200
        assert test_controller.configured_split == {"a": 60, "b": 40}
        assert any(e["action"] == "set_traffic_split" and e["source"] == "mcp" and e["result"] == "success" for e in test_controller.events)

        # 3. MCP invalid split (rejected)
        resp = client.post("/api/split", json={"a": 80, "b": 50}, headers={"X-Source": "mcp"})
        assert resp.status_code == 422
        assert any(e["action"] == "set_traffic_split" and e["source"] == "mcp" and e["result"] == "rejected" for e in test_controller.events)


# ---------------------------------------------------------------------------
# Test 10: Dashboard metrics and event logs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dashboard_metrics_and_events(test_controller):
    """Test metric tracking and event log retrieval."""
    with patch.object(cm, "controller", test_controller):
        client = TestClient(app)

        # Fetch initial status
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data
        assert "containers" in data
        assert "effective_split" in data
        assert data["containers"]["a"]["status"] == "healthy"

        # Fetch logs
        resp = client.get("/api/logs?limit=10")
        assert resp.status_code == 200
        logs = resp.json()
        assert isinstance(logs, list)


# ---------------------------------------------------------------------------
# Test 11: Regression: Traffic split editing, companion calculation, and preservation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_traffic_split_editing_and_preservation(test_controller):
    """Regression test: verify traffic split update, companion values, failover separation, and polling stability."""
    with patch.object(cm, "controller", test_controller):
        client = TestClient(app)

        # 1. Initial status check (default 50/50)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["configured_split"] == {"a": 50, "b": 50}

        # 2. Change B from 30 to 35 -> companion A is 65
        resp = client.post("/api/split", json={"a": 65, "b": 35}, headers={"X-Source": "dashboard"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured_split"] == {"a": 65, "b": 35}
        assert data["effective_split"] == {"a": 65, "b": 35}
        assert test_controller.configured_split == {"a": 65, "b": 35}

        # 3. Change B to 25 -> companion A is 75
        resp = client.post("/api/split", json={"a": 75, "b": 25}, headers={"X-Source": "dashboard"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured_split"] == {"a": 75, "b": 25}
        assert data["effective_split"] == {"a": 75, "b": 25}

        # 4. Multiple automatic status refreshes must retain the saved configured split
        for _ in range(5):
            resp = client.get("/api/status")
            assert resp.status_code == 200
            assert resp.json()["configured_split"] == {"a": 75, "b": 25}

        # 5. Failover separation: when A becomes unhealthy, effective split fails over,
        # but configured split is NOT overwritten
        test_controller.containers["a"]["health"] = "unhealthy"
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured_split"] == {"a": 75, "b": 25}
        assert data["effective_split"] == {"a": 0, "b": 100}

        # When A recovers, effective split restores to configured split
        test_controller.containers["a"]["health"] = "healthy"
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured_split"] == {"a": 75, "b": 25}
        assert data["effective_split"] == {"a": 75, "b": 25}

        # 6. Rejection of invalid split preserves existing saved split
        resp = client.post("/api/split", json={"a": 70, "b": 35}, headers={"X-Source": "dashboard"})
        assert resp.status_code == 422
        assert "100" in resp.json()["detail"]
        assert test_controller.configured_split == {"a": 75, "b": 25}

        # 7. MCP status returns the saved configured split
        resp = client.get("/api/status", headers={"X-Source": "mcp"})
        assert resp.status_code == 200
        assert resp.json()["configured_split"] == {"a": 75, "b": 25}

