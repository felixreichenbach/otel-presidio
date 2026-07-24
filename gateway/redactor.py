"""Walk an OTLP log export request and redact sensitive content in place.

Non-sensitive metadata (timestamps, severity, resource/scope attributes,
trace/span IDs) is never touched -- we only mutate log-record bodies and,
optionally, explicitly configured attribute values.
"""

from __future__ import annotations

import json
from typing import Any, Tuple

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2

from .config import Config
from .presidio_client import PresidioClient


class Redactor:
    def __init__(self, cfg: Config, presidio: PresidioClient) -> None:
        self.cfg = cfg
        self.presidio = presidio

    def redact_request(
        self, request: logs_service_pb2.ExportLogsServiceRequest
    ) -> int:
        """Redact every log record in the request. Returns entities redacted."""
        total = 0
        for resource_logs in request.resource_logs:
            for scope_logs in resource_logs.scope_logs:
                for record in scope_logs.log_records:
                    total += self._redact_record(record)
        return total

    def _redact_record(self, record) -> int:
        count = self._redact_any_value(record.body)
        if self.cfg.redact_attributes:
            keys = self.cfg.redact_attribute_keys
            for kv in record.attributes:
                if not keys or kv.key in keys:
                    count += self._redact_any_value(kv.value)
        return count

    def _redact_any_value(self, value: common_pb2.AnyValue) -> int:
        kind = value.WhichOneof("value")
        if kind == "string_value":
            new_text, count = self._redact_text(value.string_value)
            value.string_value = new_text
            return count
        if kind == "kvlist_value":
            count = 0
            for kv in value.kvlist_value.values:
                count += self._redact_any_value(kv.value)
            return count
        if kind == "array_value":
            count = 0
            for item in value.array_value.values:
                count += self._redact_any_value(item)
            return count
        # bool/int/double/bytes values are left untouched.
        return 0

    def _redact_text(self, text: str) -> Tuple[str, int]:
        if self.cfg.scan_json:
            stripped = text.lstrip()
            if stripped[:1] in ("{", "["):
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    obj = None
                if obj is not None:
                    new_obj, count = self._redact_json(obj)
                    return json.dumps(new_obj, separators=(",", ":")), count
        return self.presidio.redact(text)

    def _redact_json(self, obj: Any, key: str | None = None) -> Tuple[Any, int]:
        fields = self.cfg.json_fields
        if isinstance(obj, dict):
            count = 0
            out = {}
            for k, v in obj.items():
                new_v, c = self._redact_json(v, key=k)
                out[k] = new_v
                count += c
            return out, count
        if isinstance(obj, list):
            count = 0
            out_list = []
            for item in obj:
                new_item, c = self._redact_json(item, key=key)
                out_list.append(new_item)
                count += c
            return out_list, count
        if isinstance(obj, str):
            # When specific fields are configured, only scan those keys.
            if fields and key not in fields:
                return obj, 0
            return self.presidio.redact(obj)
        return obj, 0
