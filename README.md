# Presidio Log Redaction Gateway

An OTLP-in / OTLP-out gateway that strips PII and other sensitive entities from
log records using [Microsoft Presidio](https://presidio.dataprivacystack.org/)
before forwarding them to any OTLP-compatible backend (Grafana Alloy, OTEL
Collector, Grafana Cloud, …).

```
                           ┌────────────────────────────────┐
                           │ Presidio Analyzer + Anonymizer │
                           └────────────────────────────────┘
                                 ▲                     │
                     HTTP request│                     │ HTTP response
                     (log bodies)│                     ▼ (redacted text)
  ┌──────────────┐ OTLP    ┌────────────────────────────────┐ OTLP    ┌───────────────────────┐
  │ Alloy / OTEL │────────▶│            Gateway             │────────▶│ downstream target(s)  │
  │ (front door) │ gRPC    │      detect + redact PII       │ fan-out │ (e.g. OTEL Collector) │
  └──────────────┘         └────────────────────────────────┘         └───────────────────────┘
         ▲                                                                   │
   OTLP  │  Loki push                                                        ├──▶ Grafana Cloud
  clients┘  (via Alloy)                                                      ├──▶ stdout
                                                                             └──▶ file
```

The gateway is a **single container**. Presidio runs as its own two services
(Analyzer + Anonymizer), reached over HTTP at configurable endpoints.

## What it does

- Ingests logs over **OTLP/gRPC** (`:4317`) and **OTLP/HTTP** (`:4318`).
- Detects entities with the Presidio **Analyzer** and redacts them with the
  Presidio **Anonymizer**.
- Preserves all non-sensitive metadata — timestamps, severity, resource/scope
  attributes, and trace/span IDs are never touched.
- Redacts plain-text bodies and structured JSON bodies (optionally scoped to
  named fields), plus explicitly configured attributes.
- Forwards sanitized logs over OTLP to one or more downstream targets
  (fan-out), with per-target auth headers and retry/backoff.
- Exposes `/healthz`, `/readyz`, and Prometheus `/metrics`.

## Quick start (Docker Compose)

Brings up Presidio (analyzer + anonymizer), the gateway, and a downstream OTEL
Collector that echoes what it receives to stdout and to
`output/received-logs.json`, and (when configured) forwards it to Grafana Cloud.

```bash
docker compose up --build
```

Once healthy, send sample PII logs through the gateway:

```bash
pip install -r requirements.txt        # for the sender script
python scripts/send_logs.py            # OTLP/HTTP -> http://localhost:4318/v1/logs
```

Inspect the downstream collector's output — in `docker compose logs
downstream-collector` or `output/received-logs.json` — names, emails, IPs,
phone numbers, credit-card numbers and SSNs will be replaced with
`<ENTITY_TYPE>` tags, while timestamps, severity and attributes remain intact.

### Forwarding to Grafana Cloud

The downstream collector can ship the already-redacted logs to Grafana Cloud
over OTLP/HTTP. Copy the example env file and fill in your stack's OTLP
credentials (Grafana Cloud Portal → your stack → **OTLP**):

```bash
cp .env.example .env
# edit .env: GRAFANA_CLOUD_OTLP_ENDPOINT, GRAFANA_CLOUD_INSTANCE_ID, GRAFANA_CLOUD_API_TOKEN
docker compose up -d --build downstream-collector
```

The instance ID is the basic-auth username and a Cloud Access Policy token
(scope `logs:write`) is the password. `.env` is gitignored, so credentials stay
out of the repo. View the redacted logs in Grafana Cloud via Explore → your Loki
data source.

To route through a real **Grafana Alloy** front door — Alloy receives the logs
(over OTLP *or* the Loki push API) and forwards them to the gateway over
OTLP/gRPC:

```bash
docker compose --profile alloy up --build

# via OTLP/HTTP
python scripts/send_logs.py http://localhost:4328/v1/logs   # -> Alloy -> gateway -> collector

# via the Loki push API
curl -H "Content-Type: application/json" http://localhost:3100/loki/api/v1/push \
  -d '{"streams":[{"stream":{"service_name":"demo"},"values":[["'"$(date +%s)"'000000000","User John Smith john.smith@example.com from 192.168.1.24"]]}]}'
```

Alloy listens for OTLP on host ports `4327` (gRPC) and `4328` (HTTP), for Loki
push on `3100`, and its UI is at `http://localhost:12345`.

> With no argument `send_logs.py` targets the gateway directly (`:4318`),
> bypassing Alloy; pass the `:4328` endpoint to route through Alloy.

## Configuration

Everything is set via environment variables (no hardcoded endpoints/secrets).

| Variable | Default | Description |
|---|---|---|
| `OTLP_GRPC_ENABLED` / `OTLP_GRPC_HOST` / `OTLP_GRPC_PORT` | `true` / `0.0.0.0` / `4317` | gRPC ingestion |
| `OTLP_HTTP_ENABLED` / `OTLP_HTTP_HOST` / `OTLP_HTTP_PORT` | `true` / `0.0.0.0` / `4318` | HTTP ingestion + health/metrics |
| `PRESIDIO_ANALYZER_URL` | `http://presidio-analyzer:3000` | Analyzer endpoint |
| `PRESIDIO_ANONYMIZER_URL` | `http://presidio-anonymizer:3000` | Anonymizer endpoint |
| `PRESIDIO_LANGUAGE` | `en` | Analysis language |
| `PRESIDIO_TIMEOUT` | `10` | Analyzer/anonymizer HTTP request timeout (s) |
| `REDACT_ENTITIES` | *(all)* | Comma list, e.g. `PERSON,EMAIL_ADDRESS,PHONE_NUMBER,CREDIT_CARD,IP_ADDRESS,US_SSN` |
| `PRESIDIO_SCORE_THRESHOLD` | `0.0` | Minimum detection confidence |
| `ANONYMIZE_OPERATOR` | `replace` | `replace` \| `mask` \| `hash` \| `redact` \| `placeholder` |
| `ANONYMIZE_PLACEHOLDER` | `<REDACTED>` | Value for the `placeholder` operator |
| `ANONYMIZE_MASKING_CHAR` / `ANONYMIZE_HASH_TYPE` | `*` / `sha256` | Options for `mask` / `hash` |
| `SCAN_JSON` / `SCAN_JSON_FIELDS` | `true` / *(all)* | Parse JSON bodies; optionally limit to named fields |
| `REDACT_ATTRIBUTES` / `REDACT_ATTRIBUTE_KEYS` | `false` / *(all)* | Also redact selected attribute values |
| `EXPORT_PROTOCOL` | `grpc` | `grpc` \| `http` |
| `EXPORT_ENDPOINTS` | *(none)* | Comma list of downstream targets (fan-out) |
| `EXPORT_INSECURE` | `true` | Disable TLS for gRPC export |
| `EXPORT_HEADERS` | *(none)* | Auth headers, `k=v,k2=v2` (supply via secret) |
| `EXPORT_TIMEOUT` / `EXPORT_RETRIES` / `EXPORT_BACKOFF` | `10` / `3` / `0.5` | Export timeout, retry count, backoff base (s) |
| `FAIL_MODE` | `reject` | Behaviour when Presidio is down: `reject` (sender retries) \| `drop` \| `passthrough` |
| `LOG_LEVEL` | `INFO` | Log verbosity |

### Failure handling

When Presidio is unavailable, `FAIL_MODE` decides what happens to a batch:

- `reject` *(default, safest)* — return `UNAVAILABLE`/`503` so the upstream
  sender buffers and retries. No data loss, no unredacted data leaked.
- `drop` — discard the batch (counted in `gateway_log_records_dropped_total`).
- `passthrough` — forward the **original, unredacted** batch. Only for
  environments where availability outranks redaction.

Error logs never include original or redacted payloads.

## Kubernetes

Manifests are in [deploy/k8s/](deploy/k8s/): a Deployment with liveness/readiness
probes and resource requests/limits, a Service exposing both OTLP ports, a
ConfigMap for non-secret config, and a Secret for downstream auth headers.

```bash
kubectl apply -f deploy/k8s/
```

## Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Unit tests (no network / no Presidio required) cover env parsing, anonymizer
operator mapping, body/JSON/attribute redaction, metadata preservation, and the
three failure modes. Integration behaviour (Alloy → gateway → collector,
Presidio-down, downstream-down) is exercised through the Compose stack above.

## Project layout

```
gateway/            # the container's application code
  config.py         # env-var configuration
  presidio_client.py# Analyzer + Anonymizer REST client
  redactor.py       # walks OTLP records, redacts bodies/attributes
  forwarder.py      # OTLP export with fan-out + retry/backoff
  pipeline.py       # receive -> redact -> forward + failure handling
  metrics.py        # Prometheus metric definitions
  server_grpc.py    # OTLP/gRPC ingestion
  server_http.py    # OTLP/HTTP ingestion + health + metrics
  main.py           # entrypoint
config/             # compose-stack configs (collector, alloy, sample logs)
deploy/k8s/         # Kubernetes manifests
scripts/send_logs.py# OTLP load generator for quick validation
tests/              # unit tests
.env.example        # Grafana Cloud OTLP credentials template (copy to .env)
```

## Scope

This is an MVP targeting the acceptance criteria in
[requirements.md](requirements.md): OTLP ingest, Presidio redaction of
configured entities, OTLP forwarding to ≥1 downstream, health + metrics, and
fully externalized configuration. Persistent buffering/queueing, inbound TLS,
mTLS, and per-tenant policy are noted as future enhancements.

## License

Licensed under the [Apache License 2.0](LICENSE).
