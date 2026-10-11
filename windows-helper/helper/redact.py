"""Log redaction for the Windows MFLAC helper.

The helper handles two things that must not reach a log file: the revocable
device/lease tokens, and the EKey it uses for one decode.  ``docs/decisions/
001-encrypted-audio.md`` requires that an EKey never lands in ordinary log
messages, and the helper README requires user media paths to be redacted too.

Redaction is defence in depth: the code paths that handle an EKey do not log it
in the first place, and the filter below removes it if one ever slips through.
"""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "<redacted>"

# ``ekey: <value>`` / ``eKey=<value>`` and similar assignments.
_EKEY_ASSIGNMENT = re.compile(r"(?i)\b(e[-_]?key)\b\s*[:=]\s*\S+")
# A long base64url blob is either a token or a key; neither belongs in a log.
# 40 characters is above every non-secret identifier the protocol uses.
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")
# C:\Users\<name>\... leaks the operator's account name.
_USER_PATH = re.compile(r"(?i)\b([a-z]:\\users\\)[^\\\s\"']+")
# A bare 32-byte hex blob could be a derived key.
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")


class Redactor:
    """Scrubs secrets and private paths from arbitrary text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._literals: set[str] = set()
        self._paths: list[str] = []

    def add_secret(self, value: str | None) -> None:
        """Register an exact secret value to remove from any later message."""
        if not value:
            return
        text = str(value)
        if len(text) < 8:
            # Too short to redact reliably without mangling ordinary text.
            return
        with self._lock:
            self._literals.add(text)

    def add_path(self, value: object | None) -> None:
        """Register a private filesystem prefix to mask."""
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        with self._lock:
            if text not in self._paths:
                self._paths.append(text)

    def scrub(self, message: object) -> str:
        text = str(message)
        with self._lock:
            literals = sorted(self._literals, key=len, reverse=True)
            paths = sorted(self._paths, key=len, reverse=True)

        for literal in literals:
            if literal in text:
                text = text.replace(literal, REDACTED)
        for path in paths:
            if path in text:
                text = text.replace(path, "<local-path>")

        text = _EKEY_ASSIGNMENT.sub(r"\1=" + REDACTED, text)
        text = _USER_PATH.sub(r"\1<user>", text)
        text = _LONG_TOKEN.sub(REDACTED, text)
        text = _LONG_HEX.sub(REDACTED, text)
        return text


class RedactingFilter(logging.Filter):
    """Applies a :class:`Redactor` to every log record."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            record.msg = self._redactor.scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: self._redactor.scrub(value) for key, value in record.args.items()
                    }
                else:
                    record.args = tuple(self._redactor.scrub(value) for value in record.args)
            if record.exc_text:
                record.exc_text = self._redactor.scrub(record.exc_text)
        except Exception:  # pragma: no cover - logging must never raise
            record.msg = "<redaction failure>"
            record.args = ()
        return True


def install_redaction(redactor: Redactor, logger: logging.Logger | None = None) -> None:
    """Attach the redacting filter to a logger and its handlers."""
    target = logger or logging.getLogger()
    redacting = RedactingFilter(redactor)
    target.addFilter(redacting)
    for handler in target.handlers:
        handler.addFilter(redacting)
