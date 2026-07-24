"""Test doubles that avoid any network calls to Presidio."""

from __future__ import annotations

import re
from typing import Tuple

from gateway.presidio_client import PresidioError

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NAMES = ("John Smith", "Maria Garcia", "David Lee", "Alice Nguyen", "Priya Patel")


class FakePresidio:
    """Deterministic stand-in: masks emails and a fixed set of names."""

    def __init__(self, raise_error: bool = False) -> None:
        self.raise_error = raise_error
        self.calls = 0

    def redact(self, text: str) -> Tuple[str, int]:
        self.calls += 1
        if self.raise_error:
            raise PresidioError("boom")
        if not text or not text.strip():
            return text, 0
        count = 0
        new = text
        emails = _EMAIL.findall(new)
        for e in emails:
            new = new.replace(e, "<EMAIL_ADDRESS>")
            count += 1
        for name in _NAMES:
            if name in new:
                new = new.replace(name, "<PERSON>")
                count += 1
        return new, count

    def health(self) -> bool:
        return not self.raise_error
