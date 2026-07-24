"""Prometheus metrics for gateway self-observability."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class Metrics:
    def __init__(self) -> None:
        # A dedicated registry keeps instances independent (and avoids global
        # duplicate-registration errors when constructed more than once).
        self.registry = CollectorRegistry()
        self.records_received = Counter(
            "gateway_log_records_received_total",
            "Log records received for redaction.",
            registry=self.registry,
        )
        self.records_forwarded = Counter(
            "gateway_log_records_forwarded_total",
            "Log records successfully forwarded downstream.",
            registry=self.registry,
        )
        self.records_dropped = Counter(
            "gateway_log_records_dropped_total",
            "Log records dropped due to failures.",
            registry=self.registry,
        )
        self.entities_redacted = Counter(
            "gateway_entities_redacted_total",
            "Sensitive entities redacted by Presidio.",
            registry=self.registry,
        )
        self.presidio_errors = Counter(
            "gateway_presidio_errors_total",
            "Errors calling the Presidio Analyzer/Anonymizer.",
            registry=self.registry,
        )
        self.export_errors = Counter(
            "gateway_export_errors_total",
            "Errors exporting to downstream targets.",
            ["endpoint"],
            registry=self.registry,
        )
        self.process_latency = Histogram(
            "gateway_process_seconds",
            "End-to-end processing latency per received batch.",
            registry=self.registry,
        )
        self.ready = Gauge(
            "gateway_ready",
            "1 when Presidio is reachable and the gateway is ready.",
            registry=self.registry,
        )

    def render(self):
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
