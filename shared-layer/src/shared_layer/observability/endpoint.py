"""Metrics Endpoint Aggregation - /metrics HTTP handler.

Provides a standardized /metrics endpoint that aggregates metrics from all
registered components in Prometheus exposition format.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional
from threading import Thread

from .metrics import MetricsCollector, get_metrics_collector
from .tracing import get_correlation_id, inject_correlation_headers
from .health import HealthChecker, HealthStatus


@dataclass
class MetricsEndpointConfig:
    """Configuration for the metrics endpoint."""

    host: str = "127.0.0.1"
    port: int = 9090
    path: str = "/metrics"
    health_path: str = "/health"
    readiness_path: str = "/ready"
    enable_cors: bool = True
    cors_origins: list[str] = None  # type: ignore

    def __post_init__(self):
        if self.cors_origins is None:
            self.cors_origins = ["*"]


class MetricsEndpoint:
    """HTTP endpoint for metrics and health exposure."""

    def __init__(
        self,
        config: MetricsEndpointConfig | None = None,
        collector: MetricsCollector | None = None,
        health_checkers: dict[str, HealthChecker] | None = None,
    ) -> None:
        self.config = config or MetricsEndpointConfig()
        self.collector = collector or get_metrics_collector()
        self.health_checkers = health_checkers or {}
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[Thread] = None
        self._start_time = time.time()

    def add_health_checker(self, name: str, checker: HealthChecker) -> None:
        """Register a health checker."""
        self.health_checkers[name] = checker

    def remove_health_checker(self, name: str) -> bool:
        """Unregister a health checker."""
        return self.health_checkers.pop(name, None) is not None

    def start(self) -> None:
        """Start the HTTP server in a background thread."""
        if self._server is not None:
            return

        class Handler(BaseHTTPRequestHandler):
            endpoint: MetricsEndpoint = None  # type: ignore

            def do_GET(self) -> None:
                self._handle_request("GET")

            def do_HEAD(self) -> None:
                self._handle_request("HEAD")

            def do_OPTIONS(self) -> None:
                self._send_cors()
                self.send_response(204)
                self.end_headers()

            def _handle_request(self, method: str) -> None:
                path = self.path.split("?")[0]
                correlation_id = get_correlation_id() or self.headers.get("x-correlation-id", "")

                if path == self.endpoint.config.path:
                    self._serve_metrics(method)
                elif path == self.endpoint.config.health_path:
                    self._serve_health(method)
                elif path == self.endpoint.config.readiness_path:
                    self._serve_readiness(method)
                else:
                    self.send_response(404)
                    self.end_headers()

            def _serve_metrics(self, method: str) -> None:
                accept = self.headers.get("accept", "")
                if "application/json" in accept:
                    self._send_json(self.endpoint.collector.export_json(), method)
                else:
                    self._send_prometheus(self.endpoint.collector.export_prometheus(), method)

            def _serve_health(self, method: str) -> None:
                # Synchronous health check for endpoint
                results = {}
                overall = HealthStatus.HEALTHY
                for name, checker in self.endpoint.health_checkers.items():
                    # Run sync-compatible checks only
                    try:
                        import asyncio
                        loop = asyncio.new_event_loop()
                        check_results = loop.run_until_complete(checker.check())
                        loop.close()
                    except Exception:
                        check_results = []
                    results[name] = [r.to_dict() for r in check_results]
                    for r in check_results:
                        if r.critical and r.status == HealthStatus.UNHEALTHY:
                            overall = HealthStatus.UNHEALTHY
                        elif r.status == HealthStatus.DEGRADED and overall != HealthStatus.UNHEALTHY:
                            overall = HealthStatus.DEGRADED

                payload = {
                    "status": overall.value,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "uptime_seconds": round(time.time() - self.endpoint._start_time, 1),
                    "components": results,
                }
                self._send_json(payload, method)

            def _serve_readiness(self, method: str) -> None:
                # Readiness: only critical checks must pass
                ready = True
                for checker in self.endpoint.health_checkers.values():
                    try:
                        import asyncio
                        loop = asyncio.new_event_loop()
                        check_results = loop.run_until_complete(checker.check())
                        loop.close()
                        for r in check_results:
                            if r.critical and r.status != HealthStatus.HEALTHY:
                                ready = False
                                break
                    except Exception:
                        ready = False
                    if not ready:
                        break

                payload = {
                    "ready": ready,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                status = 200 if ready else 503
                self.send_response(status)
                self._send_json(payload, method)

            def _send_prometheus(self, data: str, method: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self._send_cors()
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data.encode("utf-8"))

            def _send_json(self, data: dict[str, Any], method: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors()
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

            def _send_cors(self) -> None:
                if self.endpoint.config.enable_cors:
                    origin = self.headers.get("Origin", "*")
                    if "*" in self.endpoint.config.cors_origins or origin in self.endpoint.config.cors_origins:
                        self.send_header("Access-Control-Allow-Origin", origin)
                        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-correlation-id, x-trace-id, x-span-id, x-parent-id, x-baggage-*")
                        self.send_header("Access-Control-Max-Age", "86400")

            def log_message(self, format: str, *args: Any) -> None:
                # Suppress default logging
                pass

        Handler.endpoint = self

        self._server = HTTPServer((self.config.host, self.config.port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True, name="metrics-endpoint")
        self._thread.start()

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}{self.config.path}"

    @property
    def health_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}{self.config.health_path}"

    @property
    def readiness_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}{self.config.readiness_path}"


# Convenience function for quick setup
def create_metrics_endpoint(
    port: int = 9090,
    health_checkers: dict[str, HealthChecker] | None = None,
) -> MetricsEndpoint:
    """Create and start a metrics endpoint with default config."""
    config = MetricsEndpointConfig(port=port)
    endpoint = MetricsEndpoint(config, health_checkers=health_checkers)
    endpoint.start()
    return endpoint