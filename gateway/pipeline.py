"""Glue: receive -> redact -> forward, with configurable failure handling."""

from __future__ import annotations

import logging

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

from .config import Config
from .forwarder import ExportError, Forwarder
from .metrics import Metrics
from .presidio_client import PresidioError
from .redactor import Redactor

log = logging.getLogger("gateway.pipeline")


class PresidioUnavailable(Exception):
    """Signals the caller (OTLP sender) to retry later."""


def _count_records(request: logs_service_pb2.ExportLogsServiceRequest) -> int:
    return sum(
        len(scope_logs.log_records)
        for resource_logs in request.resource_logs
        for scope_logs in resource_logs.scope_logs
    )


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        redactor: Redactor,
        forwarder: Forwarder,
        metrics: Metrics,
    ) -> None:
        self.cfg = cfg
        self.redactor = redactor
        self.forwarder = forwarder
        self.metrics = metrics

    def process(self, request: logs_service_pb2.ExportLogsServiceRequest) -> None:
        count = _count_records(request)
        self.metrics.records_received.inc(count)
        with self.metrics.process_latency.time():
            redacted_ok = self._redact(request, count)
            if not redacted_ok:
                return
            try:
                self.forwarder.forward(request)
            except ExportError:
                # Never log the payload -- avoid leaking redacted/original data.
                self.metrics.records_dropped.inc(count)
                raise
            self.metrics.records_forwarded.inc(count)

    def _redact(
        self, request: logs_service_pb2.ExportLogsServiceRequest, count: int
    ) -> bool:
        """Returns True if processing should continue to forwarding."""
        try:
            entities = self.redactor.redact_request(request)
            self.metrics.entities_redacted.inc(entities)
            return True
        except PresidioError as exc:
            self.metrics.presidio_errors.inc()
            mode = self.cfg.fail_mode
            if mode == "passthrough":
                log.warning("presidio unavailable; forwarding UNREDACTED (fail_mode=passthrough)")
                return True
            if mode == "drop":
                log.warning("presidio unavailable; dropping batch (fail_mode=drop)")
                self.metrics.records_dropped.inc(count)
                return False
            # default "reject"
            log.warning("presidio unavailable; rejecting batch for sender retry: %s", exc)
            raise PresidioUnavailable(str(exc)) from exc
