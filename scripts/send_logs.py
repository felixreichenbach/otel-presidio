#!/usr/bin/env python3
"""Send sample PII log records to the gateway over OTLP/HTTP (protobuf).

Usage:
    python scripts/send_logs.py [endpoint]

Default endpoint: http://localhost:4318/v1/logs
"""

import sys
import time

import httpx
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

SAMPLES = [
    "User John Smith logged in from 192.168.1.24 using john.smith@example.com",
    "Payment processed for card 4111111111111111 by customer Maria Garcia",
    "Support call from +1 (212) 555-0182 regarding account of David Lee",
    '{"event":"login","user":"alice@corp.io","ip":"10.0.4.9","name":"Alice Nguyen"}',
    "Health check ok, latency=12ms, status=200",
]


def build_request() -> logs_service_pb2.ExportLogsServiceRequest:
    records = []
    now = int(time.time() * 1e9)
    for i, body in enumerate(SAMPLES):
        records.append(
            logs_pb2.LogRecord(
                time_unix_nano=now + i,
                severity_number=logs_pb2.SEVERITY_NUMBER_INFO,
                severity_text="INFO",
                body=common_pb2.AnyValue(string_value=body),
                attributes=[
                    common_pb2.KeyValue(
                        key="service.name",
                        value=common_pb2.AnyValue(string_value="demo"),
                    )
                ],
            )
        )
    return logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(
                            key="service.name",
                            value=common_pb2.AnyValue(string_value="demo"),
                        )
                    ]
                ),
                scope_logs=[logs_pb2.ScopeLogs(log_records=records)],
            )
        ]
    )


def main() -> None:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:4318/v1/logs"
    req = build_request()
    resp = httpx.post(
        endpoint,
        content=req.SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
        timeout=30.0,
    )
    print(f"POST {endpoint} -> {resp.status_code}")
    if resp.status_code >= 400:
        print(resp.text)
        sys.exit(1)
    print(f"Sent {len(SAMPLES)} log records. Check the downstream collector output.")


if __name__ == "__main__":
    main()
