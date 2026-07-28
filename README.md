# Presidio OTEL (logs for now) Redaction Gateway

An OTLP-in / OTLP-out gateway that strips PII and other sensitive entities from
log records using [Microsoft Presidio](https://presidio.dataprivacystack.org/)
before forwarding them to any OTLP-compatible backend (Grafana Alloy, OTEL
Collector, Grafana Cloud, …).

```
                             ┌──────────────┐      ┌────────────────┐
                             │   Analyzer   │      │   Anonymizer   │
                             └──────────────┘      └────────────────┘
                                ▲         │           ▲         │
                              1 │         │ 2       3 │         │ 4
                                │         ▼           │         ▼
                           ┌────────────────────────────────────────┐
        OTLP ─────────────▶│                Gateway                 │─────────────▶ redacted OTLP
                           │          detect + redact PII           │
                           └────────────────────────────────────────┘
```

The gateway is a **single container**. Presidio runs as its own two services,
each reached over HTTP at a configurable endpoint: the gateway calls the
**Analyzer** to locate sensitive spans (1 request → 2 entities) and then the
**Anonymizer** to rewrite them (3 request → 4 redacted text). See
[How redaction works](#how-redaction-works) for the payloads.

## What it does

- Ingests logs over **OTLP/gRPC** (`:4317`) and **OTLP/HTTP** (`:4318`); the
  HTTP path accepts both protobuf and JSON payloads.
- Detects sensitive entities with the Presidio **Analyzer** and rewrites them
  with the Presidio **Anonymizer** — `replace` (default, `<ENTITY_TYPE>`),
  `mask`, `hash`, `redact`, or a fixed placeholder.
- Redacts string log bodies and structured JSON bodies (optionally limited to
  named fields), recursing into nested map/array values, plus explicitly
  selected attribute values.
- Preserves all non-sensitive metadata — timestamps, severity, resource/scope
  attributes, and trace/span IDs are never touched.
- Forwards sanitized logs over **OTLP/gRPC or OTLP/HTTP** to one or more
  downstream targets (fan-out) — e.g. an OTEL Collector or Grafana Cloud —
  each with its own auth headers, timeout, and retry/backoff.
- Fails safe: rejects a batch with `503`/`UNAVAILABLE` so the sender retries
  when Presidio is unavailable (configurable via `FAIL_MODE`, see below) or
  when every downstream target fails.
- Exposes `/healthz` (liveness), `/readyz` (Presidio reachability), and
  Prometheus `/metrics`.

## How redaction works

The gateway is the orchestrator. For every string it extracts from a log body
or attribute it makes two REST calls — first to the Presidio **Analyzer** to
locate sensitive spans, then to the **Anonymizer** to rewrite them. The two
Presidio services never talk to each other; the gateway carries the analyzer's
results into the anonymizer request.

```
For each string value in a log body / attribute:

  Gateway ──① POST /analyze  {text, language, entities?} ──────▶  Analyzer  (spaCy NLP)
  Gateway ◀── ② [{entity_type, start, end, score}, ...] ────────  Analyzer

     └─ no entities found → keep the text unchanged, skip ③–④  (1 call total)

  Gateway ──③ POST /anonymize  {text, anonymizers, analyzer_results} ─▶  Anonymizer
  Gateway ◀── ④ {text: "…redacted…"} ─────────────────────────────────  Anonymizer
```

**① Analyze** — find *where* the entities are:

```jsonc
POST http://presidio-analyzer:3000/analyze
{
  "text": "User John Smith from john@example.com",
  "language": "en",
  "entities": ["PERSON", "EMAIL_ADDRESS", ...],   // only when REDACT_ENTITIES is set
  "score_threshold": 0.5                           // only when > 0
}
→ [ {"entity_type": "PERSON",        "start": 5,  "end": 15, "score": 0.85},
    {"entity_type": "EMAIL_ADDRESS", "start": 21, "end": 37, "score": 1.0 } ]
```

**② Anonymize** — rewrite *those* spans. The gateway forwards the analyzer
results verbatim plus the operator map, built once at startup from
`ANONYMIZE_OPERATOR` (the `DEFAULT` key applies to every entity type):

```jsonc
POST http://presidio-anonymizer:3000/anonymize
{
  "text": "User John Smith from john@example.com",
  "anonymizers": { "DEFAULT": { "type": "replace" } },
  "analyzer_results": [ {entity_type, start, end, score}, ... ]
}
→ { "text": "User <PERSON> from <EMAIL_ADDRESS>" }
```

Key points:

- **Endpoints** are `PRESIDIO_ANALYZER_URL` / `PRESIDIO_ANONYMIZER_URL`
  (Docker-DNS service names by default), reached over HTTP with a shared,
  connection-pooled client bounded by `PRESIDIO_TIMEOUT`.
- **Short-circuit:** if the analyzer finds nothing, the anonymize call is
  skipped and the text is returned unchanged — clean strings cost one call,
  strings with PII cost two. Empty/whitespace strings cost none.
- **Stateless:** the anonymizer does no detection of its own; it relies on the
  byte offsets from the analyzer, which is why the gateway passes them through.
- **Two calls per string, sequential** — and the redactor runs this once per
  string field, so a JSON body with K string fields makes up to 2K sequential
  calls. Redaction latency scales with the number of strings, not records.
- **Errors** (network or non-2xx) raise a Presidio error handled per
  `FAIL_MODE` (reject / drop / passthrough). A separate `GET /health` against
  both services backs `/readyz`.

## Quick start (Docker Compose)

Brings up Presidio (analyzer + anonymizer) and the gateway. Configure the
downstream target via `EXPORT_ENDPOINTS` in
[docker-compose.yml](docker-compose.yml).

```bash
docker compose up --build
```

Once healthy, send sample PII logs through the gateway:

```bash
pip install -r requirements.txt        # for the sender script
python scripts/send_logs.py            # OTLP/HTTP -> http://localhost:4318/v1/logs
```

Redacted logs are forwarded to `EXPORT_ENDPOINTS` — names, emails, IPs,
phone numbers, credit-card numbers and SSNs will be replaced with
`<ENTITY_TYPE>` tags, while timestamps, severity and attributes remain intact.

## Configuration

Everything is set via environment variables (no hardcoded endpoints/secrets).

| Variable | Default | Description |
|---|---|---|
| `OTLP_GRPC_ENABLED` / `OTLP_GRPC_HOST` / `OTLP_GRPC_PORT` | `true` / `0.0.0.0` / `4317` | gRPC ingestion |
| `OTLP_GRPC_MAX_WORKERS` | `64` | Max concurrent gRPC `Export` handlers (thread pool) |
| `OTLP_HTTP_ENABLED` / `OTLP_HTTP_HOST` / `OTLP_HTTP_PORT` | `true` / `0.0.0.0` / `4318` | HTTP ingestion + health/metrics |
| `PRESIDIO_ANALYZER_URL` | `http://presidio-analyzer:3000` | Analyzer endpoint |
| `PRESIDIO_ANONYMIZER_URL` | `http://presidio-anonymizer:3000` | Anonymizer endpoint |
| `PRESIDIO_LANGUAGE` | `en` | Analysis language |
| `PRESIDIO_TIMEOUT` | `5` | Analyzer/anonymizer HTTP request timeout (s) |
| `PRESIDIO_MAX_CONNECTIONS` | `100` | httpx connection-pool size for concurrent Presidio calls |
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

## Observability

The gateway exposes three HTTP endpoints on the same port as OTLP/HTTP ingestion (default `4318`):

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness — returns `200 OK` when the process is running |
| `GET /readyz` | Readiness — returns `200 OK` only when Presidio (both Analyzer and Anonymizer) is reachable |
| `GET /metrics` | Prometheus metrics in text format |

### Prometheus metrics

```bash
curl http://localhost:4318/metrics
```

| Metric | Type | Description |
|---|---|---|
| `gateway_log_records_received_total` | Counter | Log records received for redaction |
| `gateway_log_records_forwarded_total` | Counter | Log records successfully forwarded downstream |
| `gateway_log_records_dropped_total` | Counter | Log records dropped due to failures |
| `gateway_entities_redacted_total` | Counter | Sensitive entities redacted by Presidio |
| `gateway_presidio_errors_total` | Counter | Errors calling the Presidio Analyzer/Anonymizer |
| `gateway_export_errors_total` | Counter | Errors exporting to a downstream target (labelled by `endpoint`) |
| `gateway_process_seconds` | Histogram | End-to-end processing latency per received batch |
| `gateway_ready` | Gauge | `1` when Presidio is reachable; `0` otherwise |

Point a Prometheus scrape config at `http://<gateway-host>:4318/metrics`. In Kubernetes the same port is exposed by the gateway Service — no separate metrics port needed.

### Failure handling

When Presidio is unavailable, `FAIL_MODE` decides what happens to a batch:

- `reject` *(default, safest)* — return `UNAVAILABLE`/`503` so the upstream
  sender buffers and retries. No data loss, no unredacted data leaked.
- `drop` — discard the batch (counted in `gateway_log_records_dropped_total`).
- `passthrough` — forward the **original, unredacted** batch. Only for
  environments where availability outranks redaction.

Error logs never include original or redacted payloads.

## Kubernetes

Manifests are in [deploy/k8s/](deploy/k8s/) — a kustomize overlay deploying the
gateway and both Presidio services, each as a Deployment + Service +
HorizontalPodAutoscaler, plus a ConfigMap for non-secret config and a Secret for
downstream auth headers. The gateway carries liveness/readiness probes and
resource requests/limits.

```bash
kubectl apply -k deploy/k8s
```

No downstream is bundled: set `EXPORT_ENDPOINTS` in the ConfigMap to your own
OTLP target (an OTEL Collector / Alloy Service, or a vendor OTLP endpoint). See
[deploy/k8s/README.md](deploy/k8s/README.md) for scaling, load-balancing, and
image-build notes.

## Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Unit tests (no network / no Presidio required) cover env parsing, anonymizer
operator mapping, body/JSON/attribute redaction, metadata preservation, and the
three failure modes. Integration behaviour (Presidio-down, downstream-down) is
exercised through the Compose stack above.

## Load testing

[scripts/loadtest.py](scripts/loadtest.py) drives OTLP batches at a running
stack and reports latency percentiles, throughput, and — with `--stats` —
per-container CPU/memory (`docker stats`) plus the gateway's `/metrics` deltas.

```bash
docker compose up -d --build          # stack must be running

# closed-loop: 10 concurrent gRPC senders for 30s, with container stats
python scripts/loadtest.py --protocol grpc --concurrency 10 --duration 30 --stats

# open-loop at a fixed rate (reveals queueing / the saturation knee)
python scripts/loadtest.py --rate 300 --duration 60 --stats

# capacity search: ramp the rate until an SLO breaks, report the max sustainable
python scripts/loadtest.py --protocol http --ramp --profile json --stats
```

`--ramp` steps the arrival rate up (`--ramp-start` × `--ramp-factor` per step,
up to `--ramp-max`) and stops at the first step that breaches `--slo-p99`
(default 500 ms) or `--slo-error-rate` (default 1%), or that can't keep up with
the offered rate — then prints the highest sustainable rate. Prefer it over
guessing a single `--rate`. Open-loop runs are time-bounded: at the deadline the
queued backlog is dropped (reported as **shed**) rather than draining for
minutes, so an over-target run finishes on time instead of hanging.

Things to keep in mind when interpreting results:

- **The Analyzer (spaCy) is the first bottleneck** — sample `gateway`,
  `presidio-analyzer`, and `presidio-anonymizer` separately; the analyzer is
  CPU-bound and defaults to a single worker. Raising `WORKERS` is the main lever
  (see [Scaling & tuning](#scaling--tuning)).
- **Payload shape drives cost** — `--profile clean` short-circuits to one
  Presidio call, `dirty` costs two per string, and `json` costs ~2× the field
  count. Latency scales with the number of strings, not records.
- **Protocol** — both paths are now concurrent (gRPC thread pool
  `OTLP_GRPC_MAX_WORKERS`; the OTLP/HTTP handler offloads blocking redaction to a
  thread pool instead of blocking the event loop). Compare both and sweep
  `--concurrency` / `--rate`.
- **Warm up and stay in steady state** — the tool waits for `/readyz` and runs
  an unmeasured `--warmup` window; use `≥10s` warmup so every analyzer worker
  loads its model, and pin container CPU/mem limits for reproducibility.
- **Don't let the client saturate first** — watch client CPU at high rates; if
  the generator is the limit, the numbers describe it, not the gateway.

## Scaling & tuning

Throughput is gated by a chain of concurrency limits. Raise them in order — each
fix simply moves the bottleneck to the next stage, so re-measure with `--ramp`
after every change and watch which container's CPU is actually pegged.

### The bottleneck chain

1. **Presidio Analyzer (usually first).** spaCy NER is CPU-bound and the stock
   image runs **one** gunicorn worker (~1 core). This is the default ceiling.
   - `WORKERS` (env on the `presidio-analyzer` service) — gunicorn workers.
     Each loads its **own ~750 MiB model**, so size to
     `min(cores to spend, RAM ÷ ~0.8 GiB)`. It's set to `6` in
     [docker-compose.yml](docker-compose.yml).
   - For more than one host, run multiple analyzer **replicas** behind a load
     balancer / Service instead of piling workers onto one box.
2. **The gateway's downstream concurrency** — how many redactions it runs at once.
   - `OTLP_GRPC_MAX_WORKERS` (default `64`) — concurrent gRPC `Export` handlers.
   - The OTLP/HTTP handler offloads blocking redaction to a thread pool (so it no
     longer serializes on the event loop).
   - `PRESIDIO_MAX_CONNECTIONS` (default `100`) — httpx pool to Presidio; keep it
     ≥ the effective in-flight concurrency so connections don't queue.
3. **The gateway process itself (GIL).** The gateway is a single Python process,
   so JSON/proto parsing and redaction bookkeeping are GIL-bound and top out
   around ~1–2 cores no matter how high the pools go. To scale past that, run
   **multiple gateway replicas** (Compose `deploy.replicas` / a K8s Deployment
   with `replicas > 1`) behind the OTLP load balancer, rather than one big pod.
4. **Anonymizer** — lightweight string substitution, rarely the limit; a small
   `WORKERS` (set to `2`) is plenty.

### Payload & batching

- **Payload shape** dominates cost: latency scales with the number of *strings*,
  not records. A `json` body costs ~2 Presidio calls per field; a `clean` line
  costs one. Narrow the work with `REDACT_ENTITIES`, `SCAN_JSON_FIELDS`, or a
  higher `PRESIDIO_SCORE_THRESHOLD`.
- **Batch size** (sender-side) amortizes per-request overhead but raises
  per-batch latency and memory; tune it together with the arrival rate.

### Measured example

Single-host stack (10 cores, 7.75 GiB), `--profile json`, `batch=5`, p99 ≤ 500 ms
SLO, found with `--ramp`:

| Change | Max sustainable | p99 at load | Bottleneck |
|---|---|---|---|
| Baseline (HTTP serialized, analyzer `WORKERS=1`) | ~10 req/s | 5 s+ | Gateway event loop |
| + gateway HTTP/gRPC/pool concurrency | ~20 req/s | ~100 ms | Analyzer (1 core) |
| + analyzer `WORKERS=6` | ~31 req/s | ~200 ms | Gateway process (GIL) |

Next lever for this workload is running multiple gateway replicas (step 3).
Numbers are illustrative — re-run `--ramp` on your own hardware and payload.

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
deploy/k8s/         # Kubernetes manifests
scripts/send_logs.py# OTLP load generator for quick validation
scripts/loadtest.py # latency + CPU/memory load test
tests/              # unit tests
```

## Scope

This is an MVP targeting the acceptance criteria in
[requirements.md](requirements.md): OTLP ingest, Presidio redaction of
configured entities, OTLP forwarding to ≥1 downstream, health + metrics, and
fully externalized configuration. See the Roadmap for what's next.

## Roadmap

Implemented today: OTLP **logs** ingest → Presidio redaction → OTLP fan-out to
one or more downstream targets. Because the API is OTLP, the same architecture
generalizes to other telemetry signals — those, plus hardening and performance
work, are tracked below.

- [ ] **OTLP traces** — ingest `/v1/traces` + gRPC `TraceService`, redact span
      names, span/event attributes (extends the logs-only redactor)
- [ ] **OTLP metrics** — redact sensitive metric attribute values
- [ ] **Concurrent Presidio calls** — batch/parallelize per-field `analyze` to
      cut JSON-body latency (currently one sequential call per string field)
- [ ] **Non-blocking HTTP ingest** — run redaction off the event loop (uvicorn
      workers / threadpool) so the OTLP/HTTP path stops serializing
- [ ] **Persistent buffering / queueing** — ride out downstream outages without
      relying on upstream sender retries
- [ ] **Inbound TLS + mTLS** on the OTLP receivers
- [ ] **Kubernetes deployment review** — validate the manifests end-to-end on a
      real cluster: image publishing/pull policy, resource requests vs the
      analyzer's per-worker model footprint, HPA bounds, Secret handling
- [ ] **Per-tenant redaction policy** — entities and operators configurable per
      tenant

## License

Licensed under the [Apache License 2.0](LICENSE).
