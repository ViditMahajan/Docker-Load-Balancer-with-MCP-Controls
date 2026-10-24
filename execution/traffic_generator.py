"""Deterministic CLI Traffic Generator for DockRoute Load Balancer.

Layer 3 Execution Script (AGENTS.md).
Sends paced traffic through the load balancer /proxy endpoint and tracks metrics.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from typing import Any

import httpx


class TrafficStats:
    def __init__(self) -> None:
        self.total = 0
        self.success = 0
        self.failed = 0
        self.by_container = {"A": 0, "B": 0, "UNKNOWN": 0}
        self.start_time = time.time()

    def record(self, status_code: int, container: str | None) -> None:
        self.total += 1
        if 200 <= status_code < 400:
            self.success += 1
            cont_key = (container or "UNKNOWN").upper()
            self.by_container[cont_key] = self.by_container.get(cont_key, 0) + 1
        else:
            self.failed += 1

    def summary(self) -> dict[str, Any]:
        elapsed = max(0.001, time.time() - self.start_time)
        return {
            "elapsed_seconds": round(elapsed, 2),
            "total_requests": self.total,
            "success": self.success,
            "failed": self.failed,
            "handled_by_a": self.by_container.get("A", 0),
            "handled_by_b": self.by_container.get("B", 0),
            "actual_rpm": round((self.total / elapsed) * 60, 1),
        }

    def print_line(self) -> None:
        a_count = self.by_container.get("A", 0)
        b_count = self.by_container.get("B", 0)
        total_healthy = a_count + b_count
        ratio = f"{round(100 * a_count / total_healthy)}% A / {round(100 * b_count / total_healthy)}% B" if total_healthy > 0 else "N/A"
        sys.stdout.write(
            f"\r[Traffic] Sent: {self.total:<5} | OK: {self.success:<5} | Fail: {self.failed:<3} | "
            f"A: {a_count:<4} | B: {b_count:<4} | Ratio: {ratio:<15}"
        )
        sys.stdout.flush()


async def run_traffic_generator(
    url: str = "http://localhost:8000",
    rpm: int = 60,
    delay: float = 0.0,
    duration: float | None = None,
    stop_event: asyncio.Event | None = None,
) -> TrafficStats:
    """Run an async traffic generator against the load balancer proxy endpoint."""
    stats = TrafficStats()
    interval = 60.0 / max(1, min(rpm, 1200))
    target_url = f"{url.rstrip('/')}/proxy"
    if delay > 0:
        target_url += f"/delay?seconds={delay}"

    if stop_event is None:
        stop_event = asyncio.Event()

    deadline = time.time() + duration if duration and duration > 0 else None

    async with httpx.AsyncClient(timeout=120.0) as client:
        while not stop_event.is_set():
            if deadline and time.time() >= deadline:
                break

            loop_start = time.time()
            try:
                resp = await client.get(target_url, headers={"X-Source": "traffic-generator"})
                raw = resp.headers.get("X-Handled-By") or resp.headers.get("X-Container") or ""
                handled_by = raw.split(",")[0].strip().upper() if raw else None
                if not handled_by:
                    try:
                        data = resp.json()
                        handled_by = str(data.get("container", "")).strip().upper() or None
                    except Exception:
                        handled_by = None
                stats.record(resp.status_code, handled_by)
            except Exception:
                stats.record(500, None)

            stats.print_line()

            elapsed = time.time() - loop_start
            sleep_time = max(0.0, interval - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass

    print()  # newline after carriage return
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic CLI Traffic Generator for DockRoute")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of the controller (default: http://localhost:8000)")
    parser.add_argument("--rpm", type=int, default=60, help="Requests per minute (default: 60)")
    parser.add_argument("--delay", type=float, default=0.0, help="Optional delay seconds per request (default: 0)")
    parser.add_argument("--duration", type=float, default=None, help="Duration in seconds to run (default: continuous until Ctrl+C)")
    args = parser.parse_args()

    stop_event = asyncio.Event()

    def handle_signal(*_: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"Starting traffic generator -> {args.url}/proxy (RPM: {args.rpm}, Delay: {args.delay}s, Duration: {args.duration or 'indefinite'}s)")
    print("Press Ctrl+C to stop.")

    try:
        stats = asyncio.run(
            run_traffic_generator(
                url=args.url,
                rpm=args.rpm,
                delay=args.delay,
                duration=args.duration,
                stop_event=stop_event,
            )
        )
    except KeyboardInterrupt:
        pass

    print("\n" + "=" * 45)
    print("TRAFFIC GENERATOR SUMMARY")
    print("=" * 45)
    summary = stats.summary()
    for k, v in summary.items():
        print(f"  {k:<20}: {v}")
    print("=" * 45)


if __name__ == "__main__":
    main()
