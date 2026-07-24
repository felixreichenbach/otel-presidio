import json

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

from gateway.config import Config
from gateway.redactor import Redactor
from tests.fakes import FakePresidio


def _request(body: common_pb2.AnyValue, attributes=None):
    record = logs_pb2.LogRecord(
        time_unix_nano=123,
        severity_text="INFO",
        severity_number=logs_pb2.SEVERITY_NUMBER_INFO,
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        body=body,
        attributes=attributes or [],
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
                scope_logs=[logs_pb2.ScopeLogs(log_records=[record])],
            )
        ]
    )


def _first_record(req):
    return req.resource_logs[0].scope_logs[0].log_records[0]


def test_plain_string_body_redacted():
    req = _request(common_pb2.AnyValue(
        string_value="User John Smith email john.smith@example.com"))
    count = Redactor(Config(), FakePresidio()).redact_request(req)
    body = _first_record(req).body.string_value
    assert "John Smith" not in body
    assert "john.smith@example.com" not in body
    assert "<PERSON>" in body and "<EMAIL_ADDRESS>" in body
    assert count == 2


def test_metadata_preserved():
    req = _request(common_pb2.AnyValue(string_value="hi David Lee"))
    Redactor(Config(), FakePresidio()).redact_request(req)
    rec = _first_record(req)
    # Trace correlation + severity + timestamps must survive untouched.
    assert rec.trace_id == b"\x01" * 16
    assert rec.span_id == b"\x02" * 8
    assert rec.time_unix_nano == 123
    assert rec.severity_text == "INFO"
    # Resource attributes preserved.
    res_attrs = req.resource_logs[0].resource.attributes
    assert res_attrs[0].value.string_value == "demo"


def test_json_body_field_redaction():
    payload = json.dumps({"user": "alice@corp.io", "name": "Alice Nguyen", "status": 200})
    req = _request(common_pb2.AnyValue(string_value=payload))
    cfg = Config(scan_json=True)
    count = Redactor(cfg, FakePresidio()).redact_request(req)
    out = json.loads(_first_record(req).body.string_value)
    assert out["user"] == "<EMAIL_ADDRESS>"
    assert out["name"] == "<PERSON>"
    assert out["status"] == 200  # non-string untouched
    assert count == 2


def test_json_scoped_fields_only():
    payload = json.dumps({"user": "alice@corp.io", "note": "David Lee called"})
    req = _request(common_pb2.AnyValue(string_value=payload))
    cfg = Config(scan_json=True, json_fields=["user"])
    Redactor(cfg, FakePresidio()).redact_request(req)
    out = json.loads(_first_record(req).body.string_value)
    assert out["user"] == "<EMAIL_ADDRESS>"
    assert out["note"] == "David Lee called"  # not in scanned fields


def test_attributes_not_redacted_by_default():
    attrs = [common_pb2.KeyValue(
        key="email", value=common_pb2.AnyValue(string_value="john.smith@example.com"))]
    req = _request(common_pb2.AnyValue(string_value="ok"), attributes=attrs)
    Redactor(Config(redact_attributes=False), FakePresidio()).redact_request(req)
    assert _first_record(req).attributes[0].value.string_value == "john.smith@example.com"


def test_attributes_redacted_when_enabled():
    attrs = [common_pb2.KeyValue(
        key="email", value=common_pb2.AnyValue(string_value="john.smith@example.com"))]
    req = _request(common_pb2.AnyValue(string_value="ok"), attributes=attrs)
    cfg = Config(redact_attributes=True, redact_attribute_keys=["email"])
    Redactor(cfg, FakePresidio()).redact_request(req)
    assert _first_record(req).attributes[0].value.string_value == "<EMAIL_ADDRESS>"
