import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2

from gateway.config import Config
from gateway.metrics import Metrics
from gateway.pipeline import Pipeline, PresidioUnavailable
from gateway.redactor import Redactor
from tests.fakes import FakePresidio


class RecordingForwarder:
    def __init__(self):
        self.forwarded = []

    def forward(self, request):
        self.forwarded.append(request)


def _request(text="hello John Smith"):
    return logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                scope_logs=[
                    logs_pb2.ScopeLogs(
                        log_records=[
                            logs_pb2.LogRecord(
                                body=common_pb2.AnyValue(string_value=text)
                            )
                        ]
                    )
                ]
            )
        ]
    )


def _pipeline(cfg, presidio, forwarder):
    return Pipeline(cfg, Redactor(cfg, presidio), forwarder, Metrics())


def test_happy_path_forwards_redacted():
    fwd = RecordingForwarder()
    _pipeline(Config(), FakePresidio(), fwd).process(_request())
    assert len(fwd.forwarded) == 1
    body = fwd.forwarded[0].resource_logs[0].scope_logs[0].log_records[0].body
    assert "John Smith" not in body.string_value


def test_fail_mode_reject_raises_and_does_not_forward():
    fwd = RecordingForwarder()
    pipe = _pipeline(Config(fail_mode="reject"), FakePresidio(raise_error=True), fwd)
    with pytest.raises(PresidioUnavailable):
        pipe.process(_request())
    assert fwd.forwarded == []


def test_fail_mode_drop_swallows_and_does_not_forward():
    fwd = RecordingForwarder()
    pipe = _pipeline(Config(fail_mode="drop"), FakePresidio(raise_error=True), fwd)
    pipe.process(_request())  # no exception
    assert fwd.forwarded == []


def test_fail_mode_passthrough_forwards_original():
    fwd = RecordingForwarder()
    pipe = _pipeline(Config(fail_mode="passthrough"), FakePresidio(raise_error=True), fwd)
    pipe.process(_request("hello John Smith"))
    assert len(fwd.forwarded) == 1
    body = fwd.forwarded[0].resource_logs[0].scope_logs[0].log_records[0].body
    # Unredacted, since Presidio failed and mode is passthrough.
    assert "John Smith" in body.string_value
