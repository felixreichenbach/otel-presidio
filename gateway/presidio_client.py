"""Thin client for the Presidio Analyzer and Anonymizer REST APIs."""

from __future__ import annotations

from typing import Dict, List, Tuple

import httpx

from .config import Config


class PresidioError(Exception):
    """Raised when Presidio cannot be reached or returns an error."""


def build_anonymizers(cfg: Config) -> Dict[str, dict]:
    """Translate the configured operator into a Presidio ``anonymizers`` map.

    The ``DEFAULT`` key applies to every detected entity type.
    """
    op = cfg.operator
    if op == "mask":
        return {
            "DEFAULT": {
                "type": "mask",
                "masking_char": cfg.masking_char,
                "chars_to_mask": 100,
                "from_end": False,
            }
        }
    if op == "hash":
        return {"DEFAULT": {"type": "hash", "hash_type": cfg.hash_type}}
    if op == "redact":
        return {"DEFAULT": {"type": "redact"}}
    if op == "placeholder":
        return {"DEFAULT": {"type": "replace", "new_value": cfg.placeholder}}
    # default: "replace" -> Presidio substitutes "<ENTITY_TYPE>"
    return {"DEFAULT": {"type": "replace"}}


class PresidioClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.analyzer_url = cfg.analyzer_url.rstrip("/")
        self.anonymizer_url = cfg.anonymizer_url.rstrip("/")
        self.entities = cfg.entities
        self.language = cfg.language
        self.score_threshold = cfg.score_threshold
        self.anonymizers = build_anonymizers(cfg)
        self._client = httpx.Client(timeout=cfg.presidio_timeout)

    def close(self) -> None:
        self._client.close()

    def analyze(self, text: str) -> List[dict]:
        payload: Dict[str, object] = {"text": text, "language": self.language}
        if self.entities:
            payload["entities"] = self.entities
        if self.score_threshold > 0:
            payload["score_threshold"] = self.score_threshold
        try:
            resp = self._client.post(f"{self.analyzer_url}/analyze", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:  # network error or non-2xx
            raise PresidioError(f"analyzer request failed: {exc}") from exc
        # Anonymizer only accepts these four fields; strip analyzer extras.
        return [
            {
                "entity_type": r["entity_type"],
                "start": r["start"],
                "end": r["end"],
                "score": r.get("score", 1.0),
            }
            for r in resp.json()
        ]

    def anonymize(self, text: str, analyzer_results: List[dict]) -> str:
        payload = {
            "text": text,
            "anonymizers": self.anonymizers,
            "analyzer_results": analyzer_results,
        }
        try:
            resp = self._client.post(f"{self.anonymizer_url}/anonymize", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PresidioError(f"anonymizer request failed: {exc}") from exc
        return resp.json()["text"]

    def redact(self, text: str) -> Tuple[str, int]:
        """Return ``(redacted_text, entities_redacted)`` for a single string."""
        if not text or not text.strip():
            return text, 0
        results = self.analyze(text)
        if not results:
            return text, 0
        return self.anonymize(text, results), len(results)

    def health(self) -> bool:
        """Best-effort readiness probe of both Presidio services."""
        try:
            for url in (self.analyzer_url, self.anonymizer_url):
                resp = self._client.get(f"{url}/health", timeout=2.0)
                if resp.status_code >= 500:
                    return False
        except httpx.HTTPError:
            return False
        return True
