"""Protected on-disk state: device credentials and the in-flight job journal.

Both files hold secret material -- the revocable device token and the lease
token -- so both are written through a :class:`Protector`.  On Windows the
protector is DPAPI (per-user, machine-bound), which means a copied file is
useless to another account or machine.  Where DPAPI is unavailable the helper
falls back to a plain file with owner-only permissions and logs a warning, so
the degradation is visible rather than silent.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAGIC = b"WPH1"
PROTECTOR_PLAINTEXT = 0
PROTECTOR_DPAPI = 1
# Static application entropy: DPAPI already scopes to the user, and this keeps
# another process running as the same user from trivially reading the blob.
_ENTROPY = b"wechatplayer-helper-v1"


class StateError(Exception):
    """Raised when protected state cannot be read or written."""


class Protector:
    """Interface for a payload protector."""

    identifier = PROTECTOR_PLAINTEXT
    name = "plaintext"

    def protect(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def unprotect(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class PlaintextProtector(Protector):
    """Last-resort protector used when DPAPI is not available."""

    identifier = PROTECTOR_PLAINTEXT
    name = "plaintext"

    def protect(self, data: bytes) -> bytes:
        return data

    def unprotect(self, data: bytes) -> bytes:
        return data


if os.name == "nt":  # pragma: no cover - Windows only

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_ulong),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def _blob_from_bytes(data: bytes) -> tuple["_DataBlob", Any]:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer

    class DpapiProtector(Protector):
        """Per-user Windows DPAPI protection."""

        identifier = PROTECTOR_DPAPI
        name = "dpapi"

        def _call(self, func: Any, data: bytes) -> bytes:
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            incoming, incoming_buffer = _blob_from_bytes(data)
            entropy, entropy_buffer = _blob_from_bytes(_ENTROPY)
            outgoing = _DataBlob()
            ok = func(
                ctypes.byref(incoming),
                None,
                ctypes.byref(entropy),
                None,
                None,
                0,
                ctypes.byref(outgoing),
            )
            # Keep the source buffers alive until the call returns.
            del incoming_buffer, entropy_buffer
            if not ok:
                raise StateError(f"DPAPI call failed with error {kernel32.GetLastError()}")
            try:
                return ctypes.string_at(outgoing.pbData, outgoing.cbData)
            finally:
                kernel32.LocalFree(outgoing.pbData)

        def protect(self, data: bytes) -> bytes:
            return self._call(ctypes.windll.crypt32.CryptProtectData, data)

        def unprotect(self, data: bytes) -> bytes:
            return self._call(ctypes.windll.crypt32.CryptUnprotectData, data)


def default_protector() -> Protector:
    """Pick the strongest protector available on this host."""
    if os.name == "nt":
        try:
            protector = DpapiProtector()
            # Fail fast here rather than at first use.
            if protector.unprotect(protector.protect(b"probe")) != b"probe":
                raise StateError("DPAPI round-trip mismatch")
            return protector
        except Exception as exc:
            logger.warning("DPAPI unavailable (%s); falling back to a plain credentials file", exc)
    return PlaintextProtector()


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions where the platform supports them."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class ProtectedJsonFile:
    """A JSON document stored encrypted (or plainly) on disk."""

    def __init__(self, path: Path, protector: Protector | None = None) -> None:
        self.path = path
        self.protector = protector or default_protector()
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.path.is_file():
                return None
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise StateError(f"cannot read {self.path.name}: {exc}") from exc
            if not raw.startswith(_MAGIC):
                # A truncated or foreign file is treated as absent state.
                raise StateError(f"{self.path.name} is not a helper state file")
            identifier = raw[len(_MAGIC)]
            payload = raw[len(_MAGIC) + 1 :]
            if identifier != self.protector.identifier:
                raise StateError(
                    f"{self.path.name} was written by the {identifier} protector, "
                    f"but this host provides {self.protector.identifier}"
                )
            try:
                decoded = self.protector.unprotect(payload)
                document = json.loads(decoded.decode("utf-8"))
            except (StateError, ValueError, UnicodeDecodeError) as exc:
                raise StateError(f"cannot decode {self.path.name}: {exc}") from exc
            if not isinstance(document, dict):
                raise StateError(f"{self.path.name} does not contain an object")
            return document

    def write(self, document: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = self.protector.protect(json.dumps(document, ensure_ascii=False).encode("utf-8"))
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_bytes(_MAGIC + bytes([self.protector.identifier]) + payload)
            _restrict_permissions(temporary)
            os.replace(temporary, self.path)

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


@dataclass
class Credentials:
    """The registered device identity."""

    device_id: str
    device_token: str
    api_base_url: str = ""
    token_expires_at: str = ""
    lease_seconds: int = 900
    heartbeat_seconds: int = 60

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Credentials":
        return cls(
            device_id=str(document["device_id"]),
            device_token=str(document["device_token"]),
            api_base_url=str(document.get("api_base_url") or ""),
            token_expires_at=str(document.get("token_expires_at") or ""),
            lease_seconds=int(document.get("lease_seconds") or 900),
            heartbeat_seconds=int(document.get("heartbeat_seconds") or 60),
        )


class CredentialStore:
    """Loads and saves :class:`Credentials`, keyed to one API base URL."""

    def __init__(self, path: Path, api_base_url: str, protector: Protector | None = None) -> None:
        self._file = ProtectedJsonFile(path, protector)
        self._api_base_url = api_base_url

    def load(self) -> Credentials | None:
        try:
            document = self._file.read()
        except StateError as exc:
            logger.warning("Discarding unusable credentials: %s", exc)
            return None
        if document is None:
            return None
        stored_url = str(document.get("api_base_url") or "")
        if stored_url and stored_url != self._api_base_url:
            # Credentials belong to a different server; re-registration is required.
            logger.info("Stored credentials target a different API; re-registering")
            return None
        try:
            return Credentials.from_document(document)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Discarding malformed credentials: %s", exc)
            return None

    def save(self, credentials: Credentials) -> None:
        document = credentials.to_document()
        document["api_base_url"] = self._api_base_url
        self._file.write(document)

    def clear(self) -> None:
        self._file.clear()


@dataclass
class JournalEntry:
    """What the helper was doing when it last stopped.

    The lease token is recorded so a restarted helper can ask the server
    whether its job already finished instead of decoding it a second time.
    """

    job_id: int
    lease_token: str
    phase: str = "claimed"
    attempt: int = 1
    result_size_bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "JournalEntry":
        return cls(
            job_id=int(document["job_id"]),
            lease_token=str(document.get("lease_token") or ""),
            phase=str(document.get("phase") or "claimed"),
            attempt=int(document.get("attempt") or 1),
            result_size_bytes=int(document.get("result_size_bytes") or 0),
            extra=dict(document.get("extra") or {}),
        )


class JobJournal:
    """Single-slot journal for the job currently being worked."""

    def __init__(self, path: Path, protector: Protector | None = None) -> None:
        self._file = ProtectedJsonFile(path, protector)

    def load(self) -> JournalEntry | None:
        try:
            document = self._file.read()
        except StateError as exc:
            logger.warning("Discarding unusable journal: %s", exc)
            return None
        if not document:
            return None
        try:
            return JournalEntry.from_document(document)
        except (KeyError, TypeError, ValueError):
            return None

    def record(self, entry: JournalEntry) -> None:
        self._file.write(entry.to_document())

    def clear(self) -> None:
        self._file.clear()
