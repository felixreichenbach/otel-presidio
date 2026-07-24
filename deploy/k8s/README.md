# Kubernetes deployment

The Kubernetes translation of [`docker-compose.yml`](../../docker-compose.yml).
Every compose service becomes a **Deployment + Service + HorizontalPodAutoscaler**,
so the pipeline scales horizontally and each hop is load-balanced.

```
                             ┌─────────────────────┐     ┌───────────────────────┐
                             │  presidio-analyzer  │     │  presidio-anonymizer  │
                             │  (N) ClusterIP+HPA  │     │  (N) ClusterIP + HPA  │
                             └─────────────────────┘     └───────────────────────┘
                                 ▲            │              ▲            │
                        ① analyze│  ② results │      ③ anon. │  ④ results │
                           (HTTP)│            ▼        (HTTP)│            ▼
 ┌─────────┐   ┌────────┐   ┌──────────────────────────────────────────────────┐   redacted    ┌──────────────────────────┐
 │ senders │─▶ │  -lb   │─▶ │               presidio-gateway (N)               │──OTLP/gRPC──▶ │ downstream-collector (N) │──▶ Grafana Cloud
 │         │OTLP│  (LB)  │  │                detect + redact PII                │               │     ClusterIP + HPA      │
 └─────────┘   └────────┘   └──────────────────────────────────────────────────┘               └──────────────────────────┘
```

The gateway is the only hop that moves log data downstream. It calls the
Analyzer (① request → ② spans) and Anonymizer (③ request → ④ redacted text)
as plain request/response HTTP — those services never forward logs themselves —
then the gateway exports the redacted batch to the collector over OTLP/gRPC.

## Apply

```bash
kubectl apply -k deploy/k8s
```

Requires a running **metrics-server** for the HPAs, and (for the external
`presidio-gateway-lb`) a cloud/MetalLB LoadBalancer provider.

> **Local (laptop) clusters:** the defaults below are sized for a single node
> (~0.95 CPU / 1.5 GiB requested at rest, ~2.65 CPU / 4.1 GiB fully scaled —
> give the VM ≥4 CPU / ≥6 GiB). The `presidio-gateway-lb` LoadBalancer works
> out of the box on Docker Desktop; on minikube run `minikube tunnel`; on kind
> use `cloud-provider-kind` or just
> `kubectl port-forward svc/presidio-gateway 4318:4318`.

Before applying, edit the placeholder values:
- `downstream-collector.yaml` Secret → your Grafana Cloud OTLP endpoint / instance ID / token.
- `secret.yaml` → gateway `EXPORT_HEADERS` (only if exporting straight to Grafana Cloud instead of via the collector).
- `gateway.yaml` → the `image:` reference. It defaults to the locally-built
  `otel-presidio-gateway:0.1.0` (see "Building the gateway image" below). For a
  remote cluster, push the image to a registry and point this at it.

### Building the gateway image

The gateway is the only custom service; everything else pulls public images.
Build it from the repo root (`Dockerfile`) straight into your local cluster's
image store, then it resolves via `imagePullPolicy: IfNotPresent`:

```sh
# minikube
minikube image build -t otel-presidio-gateway:0.1.0 .

# Docker Desktop / kind (build with local docker, then load into kind)
docker build -t otel-presidio-gateway:0.1.0 .
kind load docker-image otel-presidio-gateway:0.1.0   # kind only
```

## Load balancing between services

- **HTTP hops** (gateway → analyzer, gateway → anonymizer): the ClusterIP
  Services load-balance *per request* via kube-proxy — scaling the target
  Deployment immediately spreads load. This is the important path: the analyzer
  (spaCy NER) is the CPU bottleneck, so it has the widest HPA range.
- **gRPC hops** (OTLP ingest into the gateway, gateway → collector): gRPC is a
  long-lived HTTP/2 stream, so an L4 ClusterIP pins each connection to a single
  backend pod. With multiple client *and* server replicas, connections still
  spread statistically. For strict per-request gRPC balancing, front those hops
  with a service mesh (Linkerd/Istio) or an L7 proxy (Envoy) — no app change
  needed.

## Scaling notes

- Unlike compose (which scales the analyzer via gunicorn `WORKERS`), here each
  analyzer pod runs `WORKERS=1` (~1 core, one ~750 MiB model) and the **HPA adds
  pods**. Size cluster nodes for that memory footprint — every extra analyzer
  replica loads its own model, so this component is RAM-bound as it scales.
- HPAs target 70% CPU with a 30 s scaleUp / 5-min scaleDown stabilization window
  (the latter avoids flapping). Current defaults, sized for a single-node
  cluster:

  | Component | replicas | HPA min→max |
  |---|---|---|
  | `presidio-analyzer` (bottleneck) | 1 | 1→3 |
  | `presidio-gateway` | 1 | 1→3 |
  | `presidio-anonymizer` | 1 | 1→2 |
  | `downstream-collector` | 1 | 1→2 |

- **Moving to a real cluster:** raise `minReplicas` to ≥2 for HA and lift the
  `maxReplicas` ceilings (the analyzer earns the widest range). Add a
  `PodDisruptionBudget` (`minAvailable: 1`) per Deployment for node-drain
  guarantees — but *not* on a single-node cluster, where it would block drains.
