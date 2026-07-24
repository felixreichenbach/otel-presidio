#!/usr/bin/env python3
"""Load-test the Presidio redaction gateway: latency + CPU/memory pressure.

Drives OTLP log batches at the gateway (gRPC or HTTP), records client-side
latency, and -- optionally -- samples per-container CPU/memory via
``docker stats`` and scrapes the gateway's Prometheus ``/metrics`` for the
server-side view.

Examples
--------
# 30s closed-loop, 10 concurrent gRPC senders, mixed PII payload, with stats
python scripts/loadtest.py --protocol grpc --concurrency 10 --duration 30 --stats

# open-loop at 200 req/s for 60s over HTTP with a JSON-heavy payload
python scripts/loadtest.py --protocol http --rate 200 --duration 60 --profile json

# compare a clean (short-circuit) vs dirty (2 calls/string) workload
python scripts/loadtest.py --profile clean
python scripts/loadtest.py --profile dirty

Notes
-----
* Wait for readiness before measuring; a warm-up window is excluded from stats.
* CPU% from `docker stats` is per-core and can exceed 100%; mem includes cache.
* The server histogram is an average (sum/count), not a percentile -- percentiles
  come from client timings below.
* With FAIL_MODE=reject an overloaded Presidio returns 503/UNAVAILABLE; error
  latencies are reported separately from successes.
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import grpc
import httpx
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

# Payload profiles -- latency scales with the number of *strings*, so a JSON
# body with N fields costs ~2N Presidio calls while a clean line costs 1.
PROFILES = {
    "dirty": [
        "User John Smith logged in from 192.168.1.24 using john.smith@example.com",
        "Payment processed for card 4111111111111111 by customer Maria Garcia",
        "Support call from +1 (212) 555-0182 regarding account of David Lee",
    ],
    "clean": [
        "Health check ok, latency=12ms, status=200",
        "cache hit ratio 0.98, qps=1042, region=eu-west-2",
    ],
    "json": [
        '{"event":"login","user":"alice@corp.io","ip":"10.0.4.9",'
        '"name":"Alice Nguyen","note":"call +1 (212) 555-0182"}',
    ],
    "mixed": [
        "User John Smith logged in from 192.168.1.24 using john.smith@example.com",
        "Health check ok, latency=12ms, status=200",
        '{"event":"login","user":"alice@corp.io","ip":"10.0.4.9","name":"Alice Nguyen"}',
        "Payment processed for card 4111111111111111 by customer Maria Garcia",
    ],
}


def build_request(profile: str, batch_size: int) -> logs_service_pb2.ExportLogsServiceRequest:
    bodies = PROFILES[profile]
    now = int(time.time() * 1e9)
    svc = common_pb2.KeyValue(
        key="service.name", value=common_pb2.AnyValue(string_value="loadtest")
    )
    records = [
        logs_pb2.LogRecord(
            time_unix_nano=now + i,
            severity_number=logs_pb2.SEVERITY_NUMBER_INFO,
            severity_text="INFO",
            body=common_pb2.AnyValue(string_value=bodies[i % len(bodies)]),
            attributes=[svc],
        )
        for i in range(batch_size)
    ]
    return logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                resource=resource_pb2.Resource(attributes=[svc]),
                scope_logs=[logs_pb2.ScopeLogs(log_records=records)],
            )
        ]
    )


# --- senders (one shared instance; httpx.Client and grpc stubs are thread-safe)


class HttpSender:
    def __init__(self, url: str, timeout: float, req) -> None:
        self.url = url
        self.client = httpx.Client(
            timeout=timeout,
            headers={"content-type": "application/x-protobuf"},
            limits=httpx.Limits(max_connections=1024, max_keepalive_connections=1024),
        )
        self.payload = req.SerializeToString()

    def send(self):
        r = self.client.post(self.url, content=self.payload)
        return r.status_code == 200, str(r.status_code)


class GrpcSender:
    def __init__(self, target: str, timeout: float, req) -> None:
        self.channel = grpc.insecure_channel(target)
        self.stub = logs_service_pb2_grpc.LogsServiceStub(self.channel)
        self.timeout = timeout
        self.req = req

    def send(self):
        try:
            self.stub.Export(self.req, timeout=self.timeout)
            return True, "OK"
        except grpc.RpcError as exc:
            return False, str(exc.code())


# --- load models ----------------------------------------------------------


def closed_loop(sender, concurrency, duration):
    """N workers, each sends the next request as soon as the last returns."""
    stop_at = time.perf_counter() + duration
    results = []
    lock = threading.Lock()

    def worker():
        local = []
        while time.perf_counter() < stop_at:
            t0 = time.perf_counter()
            ok, code = sender.send()
            local.append((time.perf_counter() - t0, None, ok, code))
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, time.perf_counter() - t0


def open_loop(sender, rate, duration, max_workers):
    """Fixed arrival rate; latency measured from the *scheduled* time too, so
    queueing under saturation shows up (coordinated-omission-aware)."""
    interval = 1.0 / rate
    n = int(rate * duration)
    results = []
    lock = threading.Lock()
    ex = ThreadPoolExecutor(max_workers=max_workers)

    def task(sched):
        t0 = time.perf_counter()
        ok, code = sender.send()
        t1 = time.perf_counter()
        with lock:
            results.append((t1 - t0, t1 - sched, ok, code))

    start = time.perf_counter()
    for i in range(n):
        sched = start + i * interval
        delay = sched - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        ex.submit(task, sched)
    ex.shutdown(wait=True)
    return results, time.perf_counter() - start


# --- helpers --------------------------------------------------------------


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def wait_ready(ready_url, timeout=60.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if httpx.get(ready_url, timeout=3.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def to_mib(s: str) -> float:
    m = re.findall(r"[0-9.]+", s)
    if not m:
        return 0.0
    n = float(m[0])
    if "GiB" in s or "GB" in s:
        return n * 1024
    if "KiB" in s or "kB" in s:
        return n / 1024
    if "MiB" in s or "MB" in s:
        return n
    return n / 1024 / 1024  # bare bytes


def find_containers(tokens):
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    return [n for n in out.stdout.split() if any(tok in n for tok in tokens)]


def sample_stats(names, interval, stop, samples):
    fmt = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
    while not stop.is_set():
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", fmt, *names],
            capture_output=True,
            text=True,
        )
        for line in out.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                name, cpu, mem = parts[0], parts[1], parts[2]
                samples.setdefault(name, []).append(
                    (float(cpu.strip().rstrip("%")), to_mib(mem.split("/")[0]))
                )
        stop.wait(interval)


def scrape_metrics(url):
    try:
        txt = httpx.get(url, timeout=5.0).text
    except httpx.HTTPError:
        return {}
    vals = {}
    for line in txt.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        key, _, val = line.rpartition(" ")
        try:
            v = float(val)
        except ValueError:
            continue
        name = key.split("{", 1)[0]
        vals[name] = vals.get(name, 0.0) + v
    return vals


# --- reporting ------------------------------------------------------------


def report(results, elapsed, batch_size, open_mode, metrics_delta, stats):
    total = len(results)
    ok = [r for r in results if r[2]]
    errs = [r for r in results if not r[2]]
    svc = sorted(r[0] * 1000 for r in ok)  # ms

    print("\n=== throughput ===")
    print(f"requests: {total} ({len(ok)} ok, {len(errs)} error) in {elapsed:.1f}s")
    if elapsed > 0:
        print(f"req/s   : {len(ok) / elapsed:8.1f}")
        print(f"records/s: {len(ok) * batch_size / elapsed:8.1f}")
    if errs:
        codes = {}
        for r in errs:
            codes[r[3]] = codes.get(r[3], 0) + 1
        print(f"errors by code: {codes}")

    if svc:
        print("\n=== response latency (ms, successful requests) ===")
        for label, p in [("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99)]:
            print(f"{label}: {pct(svc, p):8.1f}")
        print(f"max: {svc[-1]:8.1f}   mean: {statistics.fmean(svc):8.1f}")

    if open_mode and ok:
        sched = sorted(r[1] * 1000 for r in ok if r[1] is not None)
        if sched:
            print("\n=== latency since scheduled send (ms) -- includes queueing ===")
            for label, p in [("p50", .5), ("p95", .95), ("p99", .99)]:
                print(f"{label}: {pct(sched, p):8.1f}")
            print(f"max: {sched[-1]:8.1f}")

    if metrics_delta:
        d = metrics_delta
        print("\n=== gateway /metrics (delta over run) ===")
        for k in ("gateway_log_records_received_total",
                  "gateway_log_records_forwarded_total",
                  "gateway_log_records_dropped_total",
                  "gateway_entities_redacted_total",
                  "gateway_presidio_errors_total",
                  "gateway_export_errors_total"):
            if k in d:
                print(f"{k}: {d[k]:.0f}")
        cnt = d.get("gateway_process_seconds_count", 0)
        s = d.get("gateway_process_seconds_sum", 0)
        if cnt:
            print(f"server mean process time: {s / cnt * 1000:.1f} ms  ({cnt:.0f} batches)")

    if stats:
        print("\n=== container CPU / memory (from docker stats) ===")
        print(f"{'container':40} {'cpu% mean/max':>18} {'mem MiB mean/max':>20}")
        for name, xs in stats.items():
            cpus = [c for c, _ in xs]
            mems = [m for _, m in xs]
            print(f"{name:40} {statistics.fmean(cpus):8.1f}/{max(cpus):<8.1f} "
                  f"{statistics.fmean(mems):9.0f}/{max(mems):<9.0f}")


# --- main -----------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--protocol", choices=["grpc", "http"], default="grpc")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--grpc-port", type=int, default=4317)
    ap.add_argument("--http-port", type=int, default=4318)
    ap.add_argument("--profile", choices=list(PROFILES), default="mixed")
    ap.add_argument("--batch-size", type=int, default=5, help="log records per request")
    ap.add_argument("--concurrency", type=int, default=10, help="closed-loop workers")
    ap.add_argument("--rate", type=float, default=0.0, help="open-loop req/s (0 = closed loop)")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--warmup", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--stats", action="store_true", help="sample docker stats")
    ap.add_argument("--stats-interval", type=float, default=1.0)
    ap.add_argument("--containers", default="gateway,analyzer,anonymizer",
                    help="comma-separated name substrings to sample")
    args = ap.parse_args()

    grpc_target = f"{args.host}:{args.grpc_port}"
    http_url = f"http://{args.host}:{args.http_port}/v1/logs"
    ready_url = f"http://{args.host}:{args.http_port}/readyz"
    metrics_url = f"http://{args.host}:{args.http_port}/metrics"

    print(f"waiting for {ready_url} ...")
    if not wait_ready(ready_url):
        print("gateway not ready; aborting")
        raise SystemExit(1)

    req = build_request(args.profile, args.batch_size)
    if args.protocol == "grpc":
        sender = GrpcSender(grpc_target, args.timeout, req)
        target = grpc_target
    else:
        sender = HttpSender(http_url, args.timeout, req)
        target = http_url

    open_mode = args.rate > 0
    mode = f"open-loop {args.rate} req/s" if open_mode else f"closed-loop x{args.concurrency}"
    print(f"target={target} protocol={args.protocol} profile={args.profile} "
          f"batch={args.batch_size} mode={mode} duration={args.duration}s")

    # Warm-up (unmeasured): loads the spaCy model, opens connections.
    if args.warmup > 0:
        print(f"warming up {args.warmup}s ...")
        closed_loop(sender, min(4, args.concurrency), args.warmup)

    # Optional container sampling + server metrics snapshot.
    stop = threading.Event()
    samples = {}
    sampler = None
    if args.stats:
        names = find_containers([t.strip() for t in args.containers.split(",")])
        if names:
            print(f"sampling containers: {names}")
            sampler = threading.Thread(
                target=sample_stats, args=(names, args.stats_interval, stop, samples)
            )
            sampler.start()
        else:
            print("no matching containers for docker stats")
    before = scrape_metrics(metrics_url)

    if open_mode:
        results, elapsed = open_loop(sender, args.rate, args.duration,
                                     max_workers=max(args.concurrency, 64))
    else:
        results, elapsed = closed_loop(sender, args.concurrency, args.duration)

    after = scrape_metrics(metrics_url)
    if sampler:
        stop.set()
        sampler.join()

    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after} if before else after
    report(results, elapsed, args.batch_size, open_mode, delta, samples)


if __name__ == "__main__":
    main()
