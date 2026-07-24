"""Entrypoint: wires up components and runs the gRPC + HTTP servers."""

from __future__ import annotations

import logging
import signal
import sys

import uvicorn

from .config import Config
from .forwarder import Forwarder
from .metrics import Metrics
from .pipeline import Pipeline
from .presidio_client import PresidioClient
from .redactor import Redactor
from .server_grpc import start_grpc_server
from .server_http import create_app

log = logging.getLogger("gateway")


def build(cfg: Config):
    metrics = Metrics()
    presidio = PresidioClient(cfg)
    redactor = Redactor(cfg, presidio)
    forwarder = Forwarder(cfg, metrics)
    pipeline = Pipeline(cfg, redactor, forwarder, metrics)
    return metrics, presidio, pipeline


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("starting gateway v0.1.0")
    log.info(
        "ingest grpc=%s(%s:%d) http=%s(%s:%d) presidio=%s exporters=%s protocol=%s fail_mode=%s",
        cfg.grpc_enabled, cfg.grpc_host, cfg.grpc_port,
        cfg.http_enabled, cfg.http_host, cfg.http_port,
        cfg.analyzer_url, cfg.export_endpoints, cfg.export_protocol, cfg.fail_mode,
    )

    metrics, presidio, pipeline = build(cfg)

    grpc_server = None
    if cfg.grpc_enabled:
        grpc_server = start_grpc_server(cfg, pipeline)

    def _shutdown(*_a):
        log.info("shutting down")
        if grpc_server is not None:
            grpc_server.stop(grace=5)
        presidio.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # FastAPI always runs -- it serves health/metrics even if HTTP ingest is off.
    app = create_app(cfg, pipeline, presidio, metrics)
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
