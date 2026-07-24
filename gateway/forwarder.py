"""Forward sanitized OTLP log batches to one or more downstream targets.

Supports OTLP/gRPC and OTLP/HTTP (protobuf), fan-out to multiple targets,
per-target auth headers, and retry with exponential backoff.
"""

from __future__ import annotations

import logging
import time
from typing import List

import grpc
import httpx
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)

from .config import Config, ExportTarget
from .metrics import Metrics

log = logging.getLogger("gateway.forwarder")


class ExportError(Exception):
    """Raised when a batch could not be delivered to any target."""


class Forwarder:
    def __init__(self, cfg: Config, metrics: Metrics) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.targets = cfg.export_targets()
        self._grpc_stubs = {}
        self._http = httpx.Client(timeout=cfg.export_timeout)
        for target in self.targets:
            if target.protocol == "grpc":
                self._grpc_stubs[target.endpoint] = self._make_grpc_stub(target)

    def _make_grpc_stub(self, target: ExportTarget):
        if target.insecure:
            channel = grpc.insecure_channel(target.endpoint)
        else:
            channel = grpc.secure_channel(
                target.endpoint, grpc.ssl_channel_credentials()
            )
        return logs_service_pb2_grpc.LogsServiceStub(channel)

    def forward(self, request: logs_service_pb2.ExportLogsServiceRequest) -> None:
        if not self.targets:
            log.warning("no export targets configured; dropping batch")
            return
        failures: List[str] = []
        for target in self.targets:
            if not self._send_with_retry(target, request):
                failures.append(target.endpoint)
        if failures and len(failures) == len(self.targets):
            # Every target failed -> surface an error so the sender can retry.
            raise ExportError(f"all export targets failed: {failures}")

    def _send_with_retry(
        self, target: ExportTarget, request: logs_service_pb2.ExportLogsServiceRequest
    ) -> bool:
        attempts = self.cfg.export_retries + 1
        for attempt in range(attempts):
            try:
                if target.protocol == "http":
                    self._send_http(target, request)
                else:
                    self._send_grpc(target, request)
                return True
            except Exception as exc:  # noqa: BLE001 - retry any transport error
                self.metrics.export_errors.labels(endpoint=target.endpoint).inc()
                if attempt + 1 >= attempts:
                    log.error("export to %s failed permanently: %s", target.endpoint, exc)
                    return False
                backoff = self.cfg.export_backoff * (2 ** attempt)
                log.warning(
                    "export to %s failed (attempt %d/%d), retrying in %.2fs: %s",
                    target.endpoint, attempt + 1, attempts, backoff, exc,
                )
                time.sleep(backoff)
        return False

    def _send_grpc(
        self, target: ExportTarget, request: logs_service_pb2.ExportLogsServiceRequest
    ) -> None:
        stub = self._grpc_stubs[target.endpoint]
        metadata = list(target.headers.items())
        stub.Export(request, timeout=self.cfg.export_timeout, metadata=metadata)

    def _send_http(
        self, target: ExportTarget, request: logs_service_pb2.ExportLogsServiceRequest
    ) -> None:
        url = target.endpoint
        if not url.endswith("/v1/logs"):
            url = url.rstrip("/") + "/v1/logs"
        headers = {"Content-Type": "application/x-protobuf", **target.headers}
        resp = self._http.post(url, content=request.SerializeToString(), headers=headers)
        resp.raise_for_status()
