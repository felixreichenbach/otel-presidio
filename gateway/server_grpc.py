"""OTLP/gRPC log ingestion server."""

from __future__ import annotations

import logging
from concurrent import futures

import grpc
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)

from .config import Config
from .pipeline import Pipeline, PresidioUnavailable
from .forwarder import ExportError

log = logging.getLogger("gateway.grpc")


class _LogsService(logs_service_pb2_grpc.LogsServiceServicer):
    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    def Export(self, request, context):
        try:
            self.pipeline.process(request)
        except PresidioUnavailable as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, f"presidio unavailable: {exc}")
        except ExportError as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, f"downstream export failed: {exc}")
        return logs_service_pb2.ExportLogsServiceResponse()


def start_grpc_server(cfg: Config, pipeline: Pipeline) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=cfg.grpc_max_workers))
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(
        _LogsService(pipeline), server
    )
    bind = f"{cfg.grpc_host}:{cfg.grpc_port}"
    server.add_insecure_port(bind)
    server.start()
    log.info("OTLP/gRPC ingestion listening on %s", bind)
    return server
