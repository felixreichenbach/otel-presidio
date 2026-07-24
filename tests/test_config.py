from gateway.config import Config
from gateway.presidio_client import build_anonymizers


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("REDACT_ENTITIES", "PERSON, EMAIL_ADDRESS ,PHONE_NUMBER")
    monkeypatch.setenv("EXPORT_ENDPOINTS", "a:4317,b:4317")
    monkeypatch.setenv("EXPORT_HEADERS", "Authorization=Bearer x,X-Scope=team")
    monkeypatch.setenv("OTLP_GRPC_ENABLED", "false")
    monkeypatch.setenv("EXPORT_RETRIES", "5")

    cfg = Config.from_env()
    assert cfg.entities == ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"]
    assert cfg.export_endpoints == ["a:4317", "b:4317"]
    assert cfg.export_headers == {"Authorization": "Bearer x", "X-Scope": "team"}
    assert cfg.grpc_enabled is False
    assert cfg.export_retries == 5

    targets = cfg.export_targets()
    assert len(targets) == 2
    assert targets[0].endpoint == "a:4317"
    assert targets[0].headers["Authorization"] == "Bearer x"


def test_anonymizer_mapping():
    assert build_anonymizers(Config(operator="replace")) == {"DEFAULT": {"type": "replace"}}
    assert build_anonymizers(Config(operator="redact")) == {"DEFAULT": {"type": "redact"}}
    assert build_anonymizers(Config(operator="hash", hash_type="md5")) == {
        "DEFAULT": {"type": "hash", "hash_type": "md5"}
    }
    placeholder = build_anonymizers(Config(operator="placeholder", placeholder="XXX"))
    assert placeholder == {"DEFAULT": {"type": "replace", "new_value": "XXX"}}
    mask = build_anonymizers(Config(operator="mask", masking_char="#"))
    assert mask["DEFAULT"]["type"] == "mask"
    assert mask["DEFAULT"]["masking_char"] == "#"
