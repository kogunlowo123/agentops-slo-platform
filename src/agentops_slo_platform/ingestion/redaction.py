"""Scrub credentials and personal data from error text before it is stored.

Error messages are the one free-text field the platform keeps (they drive
failure classification). Agents routinely echo inputs into exceptions, so
messages are passed through the agentic-core secret scanner and PII detector
and truncated before persistence.
"""

from __future__ import annotations

from agentic_core.security import PiiDetector, SecretScanner
from agentic_core.security.patterns import redact


class Scrubber:
    def __init__(
        self,
        *,
        secrets: SecretScanner | None = None,
        pii: PiiDetector | None = None,
        max_length: int = 512,
    ) -> None:
        self._secrets = secrets or SecretScanner()
        self._pii = pii or PiiDetector()
        self._max = max_length

    def scrub(self, text: str | None) -> tuple[str | None, int]:
        """Redacted, truncated text and the number of findings removed."""
        if not text:
            return None, 0
        findings = [*self._secrets.detect(text), *self._pii.detect(text)]
        cleaned = redact(text, findings) if findings else text
        return cleaned[: self._max], len(findings)
