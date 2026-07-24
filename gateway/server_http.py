"""OTLP/HTTP ingestion plus health and metrics endpoints (FastAPI)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from google.protobuf.json_format import MessageToDict, Parse
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

from .config import Config
from .forwarder import ExportError
from .metrics import Metrics
from .pipeline import Pipeline, PresidioUnavailable
from .presidio_client import PresidioClient

log = logging.getLogger("gateway.http")


def create_app(
    cfg: Config,
    pipeline: Pipeline,
    presidio: PresidioClient,
    metrics: Metrics,
) -> FastAPI:
    app = FastAPI(title="Presidio Log Redaction Gateway")

    @app.get("/healthz")
    def healthz() -> dict:
        # Liveness: the process is up and serving.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:
        ready = presidio.health()
        metrics.ready.set(1 if ready else 0)
        if ready:
            return Response(content='{"status":"ready"}', media_type="application/json")
        return Response(
            content='{"status":"not-ready","reason":"presidio unreachable"}',
            media_type="application/json",
            status_code=503,
        )

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    if cfg.http_enabled:

        @app.post("/v1/logs")
        async def ingest_logs(request: Request) -> Response:
            body = await request.body()
            content_type = request.headers.get("content-type", "")
            req = logs_service_pb2.ExportLogsServiceRequest()
            is_json = "application/json" in content_type
            try:
                if is_json:
                    Parse(body.decode("utf-8"), req)
                else:
                    req.ParseFromString(body)
            except Exception as exc:  # noqa: BLE001 - malformed payload
                log.warning("failed to parse OTLP request: %s", exc)
                return Response(content='{"error":"bad request"}', status_code=400,
                                media_type="application/json")

            resp = logs_service_pb2.ExportLogsServiceResponse()
            try:
                pipeline.process(req)
            except PresidioUnavailable as exc:
                return _error(503, f"presidio unavailable: {exc}", is_json)
            except ExportError as exc:
                return _error(503, f"downstream export failed: {exc}", is_json)

            if is_json:
                return Response(content=_to_json(resp), media_type="application/json")
            return Response(content=resp.SerializeToString(),
                            media_type="application/x-protobuf")

    return app


def _to_json(msg) -> str:
    import json

    return json.dumps(MessageToDict(msg))


def _error(status: int, message: str, is_json: bool) -> Response:
    import json

    return Response(
        content=json.dumps({"error": message}),
        status_code=status,
        media_type="application/json",
    )
