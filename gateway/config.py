"""Runtime configuration, sourced entirely from environment variables.

Every knob the MVP exposes is documented here so behaviour is fully
externalized (no hardcoded endpoints or secrets in the image).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _get_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return float(val)


def _get_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return list(default or [])
    return [item.strip() for item in val.split(",") if item.strip()]


def _get_kv(name: str) -> Dict[str, str]:
    """Parse ``k1=v1,k2=v2`` into a dict (used for export auth headers)."""
    out: Dict[str, str] = {}
    for pair in _get_list(name):
        if "=" in pair:
            key, _, value = pair.partition("=")
            out[key.strip()] = value.strip()
    return out


@dataclass
class ExportTarget:
    endpoint: str
    protocol: str  # "grpc" | "http"
    insecure: bool
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    # --- Ingestion -------------------------------------------------------
    grpc_enabled: bool = True
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 4317
    http_enabled: bool = True
    http_host: str = "0.0.0.0"
    http_port: int = 4318

    # --- Presidio --------------------------------------------------------
    analyzer_url: str = "http://presidio-analyzer:3000"
    anonymizer_url: str = "http://presidio-anonymizer:3000"
    language: str = "en"
    entities: List[str] = field(default_factory=list)  # empty => all supported
    score_threshold: float = 0.0
    presidio_timeout: float = 5.0

    # --- Anonymization ---------------------------------------------------
    # operator: replace | mask | hash | redact | placeholder
    operator: str = "replace"
    placeholder: str = "<REDACTED>"
    masking_char: str = "*"
    hash_type: str = "sha256"

    # --- Body / attribute handling --------------------------------------
    scan_json: bool = True
    json_fields: List[str] = field(default_factory=list)  # empty => all string fields
    redact_attributes: bool = False
    redact_attribute_keys: List[str] = field(default_factory=list)  # empty+enabled => all

    # --- Export ----------------------------------------------------------
    export_protocol: str = "grpc"
    export_endpoints: List[str] = field(default_factory=list)
    export_insecure: bool = True
    export_headers: Dict[str, str] = field(default_factory=dict)
    export_timeout: float = 10.0
    export_retries: int = 3
    export_backoff: float = 0.5  # seconds, exponential base

    # --- Failure handling ------------------------------------------------
    # reject: return an error to the sender (they retry, no data loss/leak)
    # drop: silently drop the batch
    # passthrough: forward the ORIGINAL (unredacted) batch -- use with care
    fail_mode: str = "reject"

    # --- Misc ------------------------------------------------------------
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            grpc_enabled=_get_bool("OTLP_GRPC_ENABLED", True),
            grpc_host=_get("OTLP_GRPC_HOST", "0.0.0.0"),
            grpc_port=_get_int("OTLP_GRPC_PORT", 4317),
            http_enabled=_get_bool("OTLP_HTTP_ENABLED", True),
            http_host=_get("OTLP_HTTP_HOST", "0.0.0.0"),
            http_port=_get_int("OTLP_HTTP_PORT", 4318),
            analyzer_url=_get("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:3000"),
            anonymizer_url=_get("PRESIDIO_ANONYMIZER_URL", "http://presidio-anonymizer:3000"),
            language=_get("PRESIDIO_LANGUAGE", "en"),
            entities=_get_list("REDACT_ENTITIES"),
            score_threshold=_get_float("PRESIDIO_SCORE_THRESHOLD", 0.0),
            presidio_timeout=_get_float("PRESIDIO_TIMEOUT", 5.0),
            operator=_get("ANONYMIZE_OPERATOR", "replace").lower(),
            placeholder=_get("ANONYMIZE_PLACEHOLDER", "<REDACTED>"),
            masking_char=_get("ANONYMIZE_MASKING_CHAR", "*"),
            hash_type=_get("ANONYMIZE_HASH_TYPE", "sha256"),
            scan_json=_get_bool("SCAN_JSON", True),
            json_fields=_get_list("SCAN_JSON_FIELDS"),
            redact_attributes=_get_bool("REDACT_ATTRIBUTES", False),
            redact_attribute_keys=_get_list("REDACT_ATTRIBUTE_KEYS"),
            export_protocol=_get("EXPORT_PROTOCOL", "grpc").lower(),
            export_endpoints=_get_list("EXPORT_ENDPOINTS"),
            export_insecure=_get_bool("EXPORT_INSECURE", True),
            export_headers=_get_kv("EXPORT_HEADERS"),
            export_timeout=_get_float("EXPORT_TIMEOUT", 10.0),
            export_retries=_get_int("EXPORT_RETRIES", 3),
            export_backoff=_get_float("EXPORT_BACKOFF", 0.5),
            fail_mode=_get("FAIL_MODE", "reject").lower(),
            log_level=_get("LOG_LEVEL", "INFO").upper(),
        )

    def export_targets(self) -> List[ExportTarget]:
        return [
            ExportTarget(
                endpoint=ep,
                protocol=self.export_protocol,
                insecure=self.export_insecure,
                headers=self.export_headers,
            )
            for ep in self.export_endpoints
        ]
