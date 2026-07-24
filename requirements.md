# Requirements: Presidio Log Redaction Gateway via OpenTelemetry

## 1. Project Goal
Build a containerized gateway wrapping Microsoft Presidio (https://presidio.dataprivacystack.org/) that receives log data via OTEL API, redacts sensitive data using Presidio, and forwards sanitized loglines downstream via OTEL API.
It should be a single container.

## 2. Problem Statement
Teams need to remove PII and sensitive entities from logs before forwarding to observability backends.

Current intent:
- Ingest logs from Alloy agents or OTEL collectors.
- Process each log line through Presidio for detection and anonymization.
- Forward sanitized output to:
  - Another Alloy instance, and/or Grafana Cloud (or any OTEL-compatible backend).

## 3. Scope
In scope:
- OpenTelemetry log ingestion endpoint(s).
- Presidio integration for detection and anonymization.
- OpenTelemetry log export endpoint(s).
- Containerized deployment suitable for local Docker and Kubernetes environments.

Out of scope (for first version):
- Metrics/traces transformation beyond pass-through behavior.
- Full SIEM feature set (rule engine, correlation, alerting).
- Long-term storage in the gateway.

## 4. High-Level Architecture
1. Source Alloy sends OTEL logs to the gateway.
2. Gateway receives log records via OTLP.
3. Gateway extracts log body and selected attributes.
4. Gateway sends content to Presidio Analyzer + Anonymizer.
5. Gateway reconstructs the log record with redacted content.
6. Gateway forwards sanitized logs via OTLP to downstream target(s).

## 5. Functional Requirements

### 5.1 Ingestion
- The gateway MUST support OTLP log ingestion.
- The gateway SHOULD support both protocols:
  - OTLP/gRPC (default: port 4317)
  - OTLP/HTTP (default: port 4318)

### 5.2 Redaction Pipeline
- Each incoming log record MUST be evaluated for sensitive entities.
- Presidio Analyzer MUST be used for entity detection.
- Presidio Anonymizer MUST be used for replacement/redaction.
- Redaction MUST support configurable entity types (for example: PERSON, EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, IP_ADDRESS).
- Replacement strategy MUST be configurable (mask, hash, token, static placeholder).
- The system MUST preserve non-sensitive log metadata (timestamp, severity, resource attributes, trace/span IDs).

### 5.3 Forwarding / Export
- The gateway MUST export sanitized logs via OTLP.
- The gateway MUST support configurable downstream targets:
  - Single target mode.
  - Multiple target mode (fan-out).
- The gateway SHOULD support retry with backoff on downstream failure.
- The gateway SHOULD support buffered delivery with configurable queue limits.

### 5.4 Configuration
- All runtime behavior MUST be configurable via environment variables and/or config file.
- Configurable items MUST include:
  - Ingest protocol(s) and bind addresses.
  - Presidio service endpoints.
  - Entity policy and anonymization operators.
  - Export target endpoint(s) and protocol(s).
  - TLS and authentication settings.
  - Timeouts, retries, and queue sizes.

### 5.5 Observability of the Gateway
- The gateway SHOULD emit self-observability telemetry (health, processing counts, error counts, latency).
- The gateway MUST provide health endpoints for readiness and liveness.
- Error logs MUST avoid leaking original non-redacted sensitive payloads.

## 6. Non-Functional Requirements

### 6.1 Performance
- The gateway SHOULD process logs in streaming mode with minimal added latency.
- Throughput target SHOULD be configurable by deployment size.
- Backpressure behavior MUST be defined when Presidio or downstream targets are slow.

### 6.2 Reliability
- The gateway MUST handle temporary Presidio outages gracefully.
- The gateway MUST handle temporary exporter outages with retry/buffer policies.
- Failure modes MUST be documented (drop, block, retry-until-limit).

### 6.3 Security
- TLS SHOULD be supported for inbound and outbound OTLP.
- mTLS SHOULD be supported for zero-trust environments.
- Authentication headers/tokens for downstream OTLP endpoints MUST be supported.
- Sensitive configuration values MUST be supplied securely (for example via secrets management).

### 6.4 Compatibility
- Must be compatible with Grafana Alloy as upstream sender.
- Must be compatible with OTEL Collector / Alloy / vendor OTLP endpoints as downstream receivers.
- Must not require Loki APIs for core operation.

## 7. OpenTelemetry Data Handling Requirements
- Log body redaction MUST support:
  - Plain text body.
  - Structured JSON body (with configurable fields to scan).
- Selected attributes MAY be redacted based on policy.
- Resource and instrumentation scope attributes MUST be preserved by default.
- Trace correlation fields MUST remain intact unless explicitly configured.

## 8. Deployment Requirements
- Must run as container image.
- Must support Docker Compose local setup including:
  - Alloy source
  - Presidio services
  - Gateway
  - Optional downstream Alloy
- Must support Kubernetes deployment with configurable resources, probes, and secrets.

## 9. Testing and Validation Requirements
- Unit tests for parsing, detection invocation, and anonymization mapping.
- Integration tests with:
  - Alloy -> Gateway -> OTLP receiver
  - Presidio unavailable scenarios
  - Downstream unavailable scenarios
- End-to-end validation with representative PII samples.
- Regression tests to ensure non-sensitive fields are unchanged.

## 10. Acceptance Criteria (MVP)
- Alloy can send logs to the gateway over OTLP.
- Gateway redacts configured PII entities via Presidio.
- Gateway forwards sanitized logs over OTLP to at least one downstream endpoint.
- Health checks and basic operational metrics are available.
- Configuration is fully externalized (no hardcoded endpoints/secrets).

## 11. Future Enhancements
- Attribute-level policy engine.
- Per-tenant redaction policies.
- Optional trace/span event redaction.
- Advanced audit reporting for redaction actions.
- Optional fallback redaction when Presidio is unavailable.
