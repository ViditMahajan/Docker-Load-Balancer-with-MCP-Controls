"""Docker load balancer controller.

The state_lock protects short, atomic state changes. The operation_lock serializes
operator actions (start/stop/split/rollout), but is NOT the state lock and is not
used by the health monitor. In particular, we never hold state_lock while waiting
for requests to drain.
"""
from __future__ import annotations

import asyncio
import os
import random
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import docker
import httpx
from docker.errors import APIError, DockerException, NotFound
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_NAMES = ("a", "b")
CONTAINER_NAMES = {
    "a": os.getenv("APP_A_CONTAINER", "app-a"),
    "b": os.getenv("APP_B_CONTAINER", "app-b"),
}
APP_URLS = {"a": os.getenv("APP_A_URL", "http://app-a:8000"), "b": os.getenv("APP_B_URL", "http://app-b:8000")}
HEALTH_INTERVAL = float(os.getenv("HEALTH_INTERVAL_SECONDS", "2"))
DEFAULT_DRAIN_TIMEOUT = float(os.getenv("DRAIN_TIMEOUT_SECONDS", "10"))
DEFAULT_HEALTH_TIMEOUT = float(os.getenv("HEALTH_TIMEOUT_SECONDS", "15"))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_name(name: str) -> str:
    name = name.strip().lower().replace("app-", "").replace("container ", "")
    if name not in APP_NAMES:
        raise ValueError("Container must be 'a' or 'b'.")
    return name


def validate_split(a: int, b: int) -> None:
    if a < 0 or b < 0 or a > 100 or b > 100:
        raise ValueError("Traffic percentages must each be between 0 and 100.")
    if a + b != 100:
        raise ValueError("Traffic percentages must add up to exactly 100.")


class SplitBody(BaseModel):
    # Range and sum validation live in Controller.set_split so rejected MCP/API
    # actions get a controller event-log entry instead of failing before the controller.
    a: int
    b: int


class ContainerBody(BaseModel):
    container: str


class UnhealthyBody(BaseModel):
    enabled: bool = True


class Controller:
    def __init__(self) -> None:
        self.state_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.docker_client: docker.DockerClient | None = None
        self.containers: dict[str, dict[str, Any]] = {
            name: {
                "name": name.upper(), "container_name": CONTAINER_NAMES[name],
                "runtime": "unknown", "health": "unknown", "routing": "enabled",
                "in_flight": 0, "last_health_check": None, "last_error": None,
            }
            for name in APP_NAMES
        }
        self.configured_split = {"a": 50, "b": 50}
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.metrics = {"total": 0, "success": 0, "failure": 0, "by_container": {"a": 0, "b": 0}}
        self.rollout: dict[str, Any] | None = None
        self.pending_stop: dict[str, Any] | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self.http: httpx.AsyncClient | None = None  # short-timeout health/admin client
        self.proxy_http: httpx.AsyncClient | None = None  # no short timeout: /delay must be able to finish

    def docker(self) -> docker.DockerClient:
        if self.docker_client is None:
            self.docker_client = docker.from_env(timeout=4)
        return self.docker_client

    def log(self, action: str, source: str, result: str, details: str = "") -> None:
        # append is synchronous: no await point can interleave while a record is added.
        self.events.appendleft({"timestamp": now_iso(), "action": action, "source": source, "result": result, "details": details})

    def effective_split_unlocked(self) -> dict[str, int]:
        eligible = [n for n in APP_NAMES if self._eligible_unlocked(n)]
        if not eligible:
            return {"a": 0, "b": 0}
        if len(eligible) == 1:
            return {"a": 100 if eligible[0] == "a" else 0, "b": 100 if eligible[0] == "b" else 0}
        active_weights = {n: self.configured_split[n] for n in eligible}
        total = sum(active_weights.values())
        if total <= 0:
            return {"a": 50, "b": 50}
        a = round(100 * active_weights["a"] / total) if "a" in active_weights else 0
        return {"a": a, "b": 100 - a}

    def _eligible_unlocked(self, name: str) -> bool:
        c = self.containers[name]
        return c["runtime"] == "running" and c["health"] == "healthy" and c["routing"] == "enabled"

    def _select_container_unlocked(self) -> str | None:
        eligible = [n for n in APP_NAMES if self._eligible_unlocked(n)]
        if not eligible:
            return None
        # Failover rule: if only one healthy, running, enabled container remains,
        # send 100% to it even when its configured weight is zero.
        if len(eligible) == 1:
            return eligible[0]
        choices = [n for n in eligible if self.configured_split[n] > 0]
        if not choices:
            return None
        weights = [self.configured_split[n] for n in choices]
        return random.choices(choices, weights=weights, k=1)[0]

    async def status(self) -> dict[str, Any]:
        async with self.state_lock:
            split = self.effective_split_unlocked()
            return {
                "containers": {n: {**self.containers[n]} for n in APP_NAMES},
                "configured_split": dict(self.configured_split), "effective_split": split,
                "metrics": {**self.metrics, "by_container": dict(self.metrics["by_container"])},
                "rollout": dict(self.rollout) if self.rollout else None,
                "operation_busy": self.operation_lock.locked(),
            }

    async def _docker_runtime(self, name: str) -> tuple[str, str | None]:
        try:
            def inspect() -> tuple[str, str | None]:
                container = self.docker().containers.get(CONTAINER_NAMES[name])
                container.reload()
                runtime = "running" if container.status == "running" else "stopped"
                error = None
                if container.status not in ("running", "created", "exited", "stopped"):
                    error = f"Docker reports status: {container.status}"
                return runtime, error
            return await asyncio.to_thread(inspect)
        except NotFound:
            return "stopped", "Container does not exist in Docker."
        except (DockerException, APIError, Exception) as exc:
            # Keep the app alive so the dashboard can explain the Docker setup problem.
            return "unknown", f"Docker inspection failed: {exc}"

    async def _probe_health(self, name: str) -> tuple[str, str | None]:
        try:
            if self.http is None:
                self.http = httpx.AsyncClient(timeout=1.5)
            response = await self.http.get(f"{APP_URLS[name]}/health")
            if response.status_code == 200:
                return "healthy", None
            return "unhealthy", f"Health endpoint returned HTTP {response.status_code}."
        except httpx.HTTPError as exc:
            return "unknown", f"Health probe failed: {exc.__class__.__name__}"

    async def refresh_one(self, name: str) -> None:
        runtime, runtime_error = await self._docker_runtime(name)
        if runtime == "running":
            health, health_error = await self._probe_health(name)
        elif runtime == "stopped":
            health, health_error = "unknown", None
        else:
            health, health_error = "unknown", runtime_error
        async with self.state_lock:
            current = self.containers[name]
            # Avoid overwriting a deliberate stop transition mid-way through Docker's stop call.
            if current["runtime"] != "stopping":
                current["runtime"] = runtime
            current["health"] = health if current["runtime"] != "stopping" else current["health"]
            current["last_health_check"] = now_iso()
            current["last_error"] = health_error or runtime_error

            # If the replacement fails while the old container is draining, recover the old
            # container immediately when it is still alive and healthy.
            if self.rollout and self.rollout.get("phase") in ("draining_old", "stopping_old"):
                old = self.rollout.get("old")
                target = self.rollout.get("target")
                if old and target and name == target and health != "healthy":
                    old_state = self.containers[old]
                    if old_state["runtime"] == "running" and old_state["health"] == "healthy":
                        old_state["routing"] = "enabled"
                        self.configured_split = {"a": 100 if old == "a" else 0, "b": 100 if old == "b" else 0}
                        self.rollout["failed_reason"] = f"Target {target.upper()} became unhealthy; traffic returned to {old.upper()}."
                        self.rollout["phase"] = "rolled_back"
                        self.log("rollout_rollback", "health-monitor", "recovered", self.rollout["failed_reason"])
            if self.pending_stop and self.pending_stop.get("replacement") == name and self.pending_stop.get("target"):
                target = self.pending_stop["target"]
                if health != "healthy" and self.containers[target]["health"] == "healthy":
                    target_state = self.containers[target]
                    if target_state["runtime"] == "running" and target_state["health"] == "healthy":
                        target_state["routing"] = "enabled"
                        self.configured_split = {"a": 100 if target == "a" else 0, "b": 100 if target == "b" else 0}
                        self.pending_stop["abort_reason"] = f"Replacement {name.upper()} became unhealthy; stop cancelled."
                        self.log("stop_container", "health-monitor", "cancelled", self.pending_stop["abort_reason"])

    async def refresh_all(self) -> None:
        await asyncio.gather(*(self.refresh_one(n) for n in APP_NAMES))

    async def monitor(self) -> None:
        while True:
            try:
                await self.refresh_all()
            except Exception as exc:  # keep the monitor alive and report the failure
                self.log("health_monitor", "system", "error", str(exc))
            await asyncio.sleep(HEALTH_INTERVAL)

    @asynccontextmanager
    async def exclusive_operation(self, action: str, source: str) -> AsyncIterator[None]:
        # No waiting queue: conflicting control requests get an immediate, logged 409.
        if self.operation_lock.locked():
            self.log(action, source, "rejected", "Another control operation is in progress.")
            raise HTTPException(status_code=409, detail="Another control operation is in progress; try again after it completes.")
        await self.operation_lock.acquire()
        try:
            yield
        finally:
            self.operation_lock.release()

    async def set_split(self, a: int, b: int, source: str) -> dict[str, Any]:
        try:
            validate_split(a, b)
        except ValueError as exc:
            self.log("set_traffic_split", source, "rejected", str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with self.exclusive_operation("set_traffic_split", source):
            async with self.state_lock:
                proposed = {"a": a, "b": b}
                for name, weight in proposed.items():
                    c = self.containers[name]
                    if weight > 0 and not self._eligible_unlocked(name):
                        detail = f"Cannot assign {weight}% to {name.upper()}: it must be running, healthy, and not draining."
                        self.log("set_traffic_split", source, "rejected", detail)
                        raise HTTPException(status_code=409, detail=detail)
                self.configured_split = proposed
                self.log("set_traffic_split", source, "success", f"Configured split set to A={a}%, B={b}%.")
        return await self.status()

    async def _wait_healthy(self, name: str, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await self.refresh_one(name)
            async with self.state_lock:
                if self.containers[name]["runtime"] == "running" and self.containers[name]["health"] == "healthy":
                    return True
            await asyncio.sleep(0.4)
        return False

    async def _docker_start(self, name: str) -> None:
        def start() -> None:
            container = self.docker().containers.get(CONTAINER_NAMES[name])
            container.reload()
            if container.status != "running":
                container.start()
        await asyncio.to_thread(start)

    async def _docker_stop(self, name: str, timeout: float) -> None:
        def stop() -> None:
            container = self.docker().containers.get(CONTAINER_NAMES[name])
            container.reload()
            if container.status == "running":
                container.stop(timeout=max(1, int(timeout)))
        await asyncio.to_thread(stop)

    async def start_container(self, name: str, source: str, health_timeout: float = DEFAULT_HEALTH_TIMEOUT) -> dict[str, Any]:
        try:
            name = normalize_name(name)
        except ValueError as exc:
            self.log("start_container", source, "rejected", str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with self.exclusive_operation("start_container", source):
            async with self.state_lock:
                self.containers[name]["routing"] = "disabled"
                self.containers[name]["runtime"] = "starting"
            try:
                await self._docker_start(name)
            except Exception as exc:
                async with self.state_lock:
                    self.containers[name]["runtime"] = "stopped"
                    self.containers[name]["last_error"] = str(exc)
                self.log("start_container", source, "error", f"Docker start failed: {exc}")
                raise HTTPException(status_code=502, detail=f"Docker could not start {name.upper()}: {exc}") from exc
            healthy = await self._wait_healthy(name, health_timeout)
            async with self.state_lock:
                if healthy:
                    self.containers[name]["routing"] = "enabled"
                    self.log("start_container", source, "success", f"{name.upper()} is running and healthy.")
                else:
                    self.containers[name]["routing"] = "disabled"
                    self.log("start_container", source, "error", f"{name.upper()} did not become healthy within {health_timeout}s; it remains unrouted.")
            if not healthy:
                raise HTTPException(status_code=504, detail=f"{name.upper()} did not become healthy within {health_timeout}s; it remains unrouted.")
        return await self.status()

    async def _wait_drained(self, name: str, timeout: float, abort_key: str | None = None) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with self.state_lock:
                if abort_key == "rollout" and self.rollout and self.rollout.get("failed_reason"):
                    return False
                if abort_key == "stop" and self.pending_stop and self.pending_stop.get("abort_reason"):
                    return False
                if self.containers[name]["in_flight"] == 0:
                    return True
            await asyncio.sleep(0.1)
        return False

    async def stop_container(self, name: str, source: str, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> dict[str, Any]:
        try:
            name = normalize_name(name)
        except ValueError as exc:
            self.log("stop_container", source, "rejected", str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with self.exclusive_operation("stop_container", source):
            await self.refresh_all()
            replacement = "b" if name == "a" else "a"
            async with self.state_lock:
                c = self.containers[name]
                other = self.containers[replacement]
                if c["runtime"] != "running":
                    detail = f"{name.upper()} is not running."
                    self.log("stop_container", source, "rejected", detail)
                    raise HTTPException(status_code=409, detail=detail)
                if other["runtime"] != "running" or other["health"] != "healthy" or other["routing"] != "enabled":
                    detail = "Cannot stop this container because no other healthy, running, routable container is available."
                    self.log("stop_container", source, "rejected", detail)
                    raise HTTPException(status_code=409, detail=detail)
                # Remove from new-request routing before waiting for in-flight work.
                c["routing"] = "draining"
                self.configured_split = {"a": 100 if replacement == "a" else 0, "b": 100 if replacement == "b" else 0}
                self.pending_stop = {"target": name, "replacement": replacement, "abort_reason": None}
                self.log("drain_start", source, "success", f"{name.upper()} removed from routing; waiting for active requests.")

            drained = await self._wait_drained(name, drain_timeout, "stop")
            async with self.state_lock:
                c = self.containers[name]
                if self.pending_stop and self.pending_stop.get("abort_reason"):
                    reason = self.pending_stop["abort_reason"]
                    self.pending_stop = None
                    c["routing"] = "enabled"
                    self.log("stop_container", source, "cancelled", reason)
                    raise HTTPException(status_code=409, detail=reason)
                other = self.containers[replacement]
                if not drained:
                    c["routing"] = "disabled"  # keep running, but do not send it new traffic
                    self.pending_stop = None
                    detail = f"Drain timeout: {name.upper()} remains running but unrouted."
                    self.log("stop_container", source, "error", detail)
                    raise HTTPException(status_code=408, detail=detail)
                if other["runtime"] != "running" or other["health"] != "healthy" or other["routing"] != "enabled":
                    c["routing"] = "enabled"
                    self.configured_split = {"a": 100 if name == "a" else 0, "b": 100 if name == "b" else 0}
                    self.pending_stop = None
                    detail = "Stop cancelled because the replacement is no longer healthy."
                    self.log("stop_container", source, "cancelled", detail)
                    raise HTTPException(status_code=409, detail=detail)
                c["runtime"] = "stopping"
                c["routing"] = "disabled"

            try:
                await self._docker_stop(name, drain_timeout)
            except Exception as exc:
                async with self.state_lock:
                    self.containers[name]["runtime"] = "running"
                    self.containers[name]["routing"] = "disabled"
                    self.containers[name]["last_error"] = str(exc)
                    self.pending_stop = None
                self.log("stop_container", source, "error", f"Docker stop failed: {exc}")
                raise HTTPException(status_code=502, detail=f"Docker could not stop {name.upper()}: {exc}") from exc
            async with self.state_lock:
                self.containers[name]["runtime"] = "stopped"
                self.containers[name]["health"] = "unknown"
                self.containers[name]["routing"] = "disabled"
                self.pending_stop = None
                self.log("stop_container", source, "success", f"{name.upper()} stopped after draining.")
        return await self.status()

    async def safe_rollout(self, target: str, source: str, health_timeout: float = DEFAULT_HEALTH_TIMEOUT, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> dict[str, Any]:
        try:
            target = normalize_name(target)
        except ValueError as exc:
            self.log("safe_rollout", source, "rejected", str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with self.exclusive_operation("safe_rollout", source):
            old = "b" if target == "a" else "a"
            await self.refresh_all()
            async with self.state_lock:
                old_state = self.containers[old]
                if old_state["runtime"] != "running" or old_state["health"] != "healthy" or old_state["routing"] != "enabled":
                    detail = f"Cannot roll out to {target.upper()}: {old.upper()} is not healthy and serving as the rollback target."
                    self.log("safe_rollout", source, "rejected", detail)
                    raise HTTPException(status_code=409, detail=detail)
                previous_split = dict(self.configured_split)
                self.rollout = {"target": target, "old": old, "phase": "starting_target", "failed_reason": None, "started_at": now_iso()}
                self.log("safe_rollout", source, "started", f"Starting safe rollout from {old.upper()} to {target.upper()}.")
                self.containers[target]["routing"] = "disabled"
                self.containers[target]["runtime"] = "starting"
            try:
                await self._docker_start(target)
            except Exception as exc:
                async with self.state_lock:
                    self.containers[target]["runtime"] = "stopped"
                    self.rollout = {"target": target, "old": old, "phase": "aborted", "failed_reason": str(exc), "started_at": now_iso()}
                    self.log("safe_rollout", source, "aborted", f"Could not start target: {exc}. {old.upper()} continues serving.")
                raise HTTPException(status_code=502, detail=f"Could not start {target.upper()}; {old.upper()} remains serving: {exc}") from exc

            healthy = await self._wait_healthy(target, health_timeout)
            if not healthy:
                async with self.state_lock:
                    self.containers[target]["routing"] = "disabled"
                    self.rollout = {"target": target, "old": old, "phase": "aborted", "failed_reason": "Target health timeout", "started_at": now_iso()}
                    self.log("safe_rollout", source, "aborted", f"{target.upper()} did not become healthy; {old.upper()} remains serving.")
                raise HTTPException(status_code=504, detail=f"{target.upper()} did not become healthy within {health_timeout}s; {old.upper()} remains serving.")

            async with self.state_lock:
                if self.rollout and self.rollout.get("failed_reason"):
                    reason = self.rollout["failed_reason"]
                    raise HTTPException(status_code=409, detail=f"Rollout aborted: {reason}")
                self.containers[target]["routing"] = "enabled"
                self.configured_split = {"a": 100 if target == "a" else 0, "b": 100 if target == "b" else 0}
                self.containers[old]["routing"] = "draining"
                self.rollout["phase"] = "draining_old"
                self.log("rollout_switch", source, "success", f"New traffic moved to {target.upper()}; draining {old.upper()}.")

            drained = await self._wait_drained(old, drain_timeout, "rollout")
            async with self.state_lock:
                reason = self.rollout.get("failed_reason") if self.rollout else None
                if reason:
                    # The health monitor already restored old routing when it was safe.
                    self.log("safe_rollout", source, "rolled_back", reason)
                    raise HTTPException(status_code=409, detail=reason)
                if not drained:
                    self.containers[old]["routing"] = "disabled"
                    self.rollout["phase"] = "drain_timeout"
                    detail = f"Drain timeout: {old.upper()} stays running but unrouted; {target.upper()} continues serving."
                    self.log("safe_rollout", source, "error", detail)
                    raise HTTPException(status_code=408, detail=detail)
                self.rollout["phase"] = "stopping_old"

            # Final fresh health probe immediately before stopping the old container.
            await self.refresh_one(target)
            async with self.state_lock:
                if self.containers[target]["health"] != "healthy":
                    old_state = self.containers[old]
                    if old_state["runtime"] == "running" and old_state["health"] == "healthy":
                        old_state["routing"] = "enabled"
                        self.configured_split = {"a": 100 if old == "a" else 0, "b": 100 if old == "b" else 0}
                        self.rollout["phase"] = "rolled_back"
                        self.rollout["failed_reason"] = f"Target {target.upper()} failed its final pre-stop health check."
                    detail = self.rollout.get("failed_reason") or "Target failed health and no safe rollback target is available."
                    self.log("safe_rollout", source, "rolled_back", detail)
                    raise HTTPException(status_code=409, detail=detail)
                self.containers[old]["runtime"] = "stopping"
                self.containers[old]["routing"] = "disabled"
            try:
                await self._docker_stop(old, drain_timeout)
            except Exception as exc:
                async with self.state_lock:
                    self.containers[old]["runtime"] = "running"
                    self.containers[old]["routing"] = "disabled"
                    self.rollout["phase"] = "stop_error"
                    self.rollout["failed_reason"] = f"Could not stop {old.upper()}: {exc}"
                    self.log("safe_rollout", source, "error", self.rollout["failed_reason"])
                raise HTTPException(status_code=502, detail=self.rollout["failed_reason"]) from exc
            async with self.state_lock:
                self.containers[old]["runtime"] = "stopped"
                self.containers[old]["health"] = "unknown"
                self.containers[old]["routing"] = "disabled"
                self.rollout["phase"] = "completed"
                self.log("safe_rollout", source, "success", f"Rollout complete: {target.upper()} serves; {old.upper()} stopped.")
        return await self.status()

    async def set_unhealthy(self, name: str, enabled: bool, source: str) -> dict[str, Any]:
        try:
            name = normalize_name(name)
        except ValueError as exc:
            self.log("force_unhealthy", source, "rejected", str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with self.exclusive_operation("force_unhealthy", source):
            try:
                if self.http is None:
                    self.http = httpx.AsyncClient(timeout=2)
                response = await self.http.post(f"{APP_URLS[name]}/admin/unhealthy", json={"enabled": enabled})
                if response.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"Container returned HTTP {response.status_code}.")
            except HTTPException as exc:
                self.log("force_unhealthy", source, "error", str(exc.detail))
                raise
            except httpx.HTTPError as exc:
                self.log("force_unhealthy", source, "error", f"Could not reach {name.upper()}: {exc}")
                raise HTTPException(status_code=502, detail=f"Could not reach {name.upper()}: {exc}") from exc
            await self.refresh_one(name)
            self.log("force_unhealthy", source, "success", f"Forced unhealthy={enabled} for {name.upper()}.")
        return await self.status()

    async def recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.events)[:max(1, min(limit, 200))]


controller = Controller()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    controller.monitor_task = asyncio.create_task(controller.monitor())
    yield
    if controller.monitor_task:
        controller.monitor_task.cancel()
        try:
            await controller.monitor_task
        except asyncio.CancelledError:
            pass
    if controller.http:
        await controller.http.aclose()
    if controller.proxy_http:
        await controller.proxy_http.aclose()
    if controller.docker_client:
        controller.docker_client.close()


app = FastAPI(title="Docker Load Balancer Controller", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def request_source(x_source: str | None) -> str:
    return x_source if x_source in {"dashboard", "mcp", "api", "traffic-generator"} else "api"


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/healthz")
async def controller_health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
async def get_status(x_source: str | None = Header(default=None)) -> dict[str, Any]:
    source = request_source(x_source)
    if source == "mcp":
        controller.log("get_status", source, "success", "Status queried via MCP.")
    return await controller.status()


@app.get("/api/logs")
async def get_logs(limit: int = Query(50, ge=1, le=200), x_source: str | None = Header(default=None)) -> list[dict[str, Any]]:
    source = request_source(x_source)
    if source == "mcp":
        controller.log("get_recent_logs", source, "success", f"Requested newest {limit} event(s) via MCP.")
    return await controller.recent_logs(limit)


@app.post("/api/split")
async def traffic_split(body: SplitBody, x_source: str | None = Header(default=None)) -> dict[str, Any]:
    return await controller.set_split(body.a, body.b, request_source(x_source))


@app.post("/api/containers/{name}/start")
async def start_container(name: str, x_source: str | None = Header(default=None)) -> dict[str, Any]:
    return await controller.start_container(name, request_source(x_source))


@app.post("/api/containers/{name}/stop")
async def stop_container(name: str, x_source: str | None = Header(default=None), drain_timeout: float = Query(DEFAULT_DRAIN_TIMEOUT, gt=0, le=120)) -> dict[str, Any]:
    return await controller.stop_container(name, request_source(x_source), drain_timeout)


@app.post("/api/rollout/{target}")
async def safe_rollout(target: str, x_source: str | None = Header(default=None), health_timeout: float = Query(DEFAULT_HEALTH_TIMEOUT, gt=0, le=120), drain_timeout: float = Query(DEFAULT_DRAIN_TIMEOUT, gt=0, le=120)) -> dict[str, Any]:
    return await controller.safe_rollout(target, request_source(x_source), health_timeout, drain_timeout)


@app.post("/api/containers/{name}/unhealthy")
async def force_unhealthy(name: str, body: UnhealthyBody, x_source: str | None = Header(default=None)) -> dict[str, Any]:
    return await controller.set_unhealthy(name, body.enabled, request_source(x_source))


@app.api_route("/proxy", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str = "") -> Response:
    async with controller.state_lock:
        selected = controller._select_container_unlocked()
        if selected is None:
            controller.metrics["total"] += 1
            controller.metrics["failure"] += 1
            controller.log("proxy_request", "traffic-generator", "failed", "No healthy, running, non-draining backend; returned 503.")
            return JSONResponse(status_code=503, content={"error": "No healthy backend available."})
        controller.containers[selected]["in_flight"] += 1
        controller.metrics["total"] += 1
        controller.metrics["by_container"][selected] += 1
    try:
        body = await request.body()
        url = f"{APP_URLS[selected]}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length", "connection"}}
        if controller.proxy_http is None:
            controller.proxy_http = httpx.AsyncClient(timeout=None)
        upstream = await controller.proxy_http.request(request.method, url, content=body, headers=headers)
        ok = upstream.status_code < 400
        async with controller.state_lock:
            if ok:
                controller.metrics["success"] += 1
            else:
                controller.metrics["failure"] += 1
        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in {"content-length", "transfer-encoding", "connection", "content-encoding"}}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))
    except httpx.HTTPError as exc:
        async with controller.state_lock:
            controller.metrics["failure"] += 1
        controller.log("proxy_request", "traffic-generator", "failed", f"Backend {selected.upper()} request failed: {exc.__class__.__name__}")
        return JSONResponse(status_code=502, content={"error": f"Backend {selected.upper()} is not reachable."})
    finally:
        async with controller.state_lock:
            controller.containers[selected]["in_flight"] = max(0, controller.containers[selected]["in_flight"] - 1)
