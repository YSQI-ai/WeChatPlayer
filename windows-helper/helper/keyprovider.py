"""Authorized key sources and the local decoder.

This module is the *only* place in the helper that touches an EKey, and it does
so entirely in memory.  Two rules follow from
``docs/decisions/001-encrypted-audio.md``:

* An EKey is never written to disk, a URL, a database, or a log message.  It is
  registered with the log redactor as soon as it is read, so it cannot leak
  through an incidental log line further up the stack.
* When no authorized source is configured, the helper does not guess.  It raises
  :class:`KeyUnavailable` and the job is reported failed with a diagnosable
  reason, exactly as the Linux decoder does.

Supported authorized sources are the two the project already supports: an
explicit EKey file supplied by the operator, and an exported Android
``player_process_db`` SQLite database.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class KeyUnavailable(Exception):
    """No authorized key source could produce a key for this file."""


class DecoderUnavailable(Exception):
    """The offline decoder module could not be loaded."""


class DecodeFailure(Exception):
    """The decoder rejected the input or produced no usable audio."""


@dataclass
class DecodePlan:
    """How to decode one file.  Carries a secret, so never log it."""

    ekey: str | None = None
    key_db: Path | None = None
    source_kind: str = "none"


class KeyProvider:
    """Interface for an authorized key source."""

    kind = "none"

    def plan(self, source_path: Path) -> DecodePlan:  # pragma: no cover - interface
        raise NotImplementedError

    def check(self) -> tuple[bool, str]:
        """Return ``(usable, reason)`` without touching any secret."""
        return True, "ok"


class NoKeyProvider(KeyProvider):
    """Explicitly no key source.

    Files with an embedded EKey still decode; PC MusicEx files fail with a clear
    reason instead of a guess.
    """

    kind = "none"

    def plan(self, source_path: Path) -> DecodePlan:
        return DecodePlan(source_kind="none")


class ExplicitEkeyFileProvider(KeyProvider):
    """Reads one operator-supplied EKey from a local file.

    The file must contain only the EKey.  It is the operator's authorization
    record; the helper reads it and never copies or logs it.
    """

    kind = "ekey_file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def check(self) -> tuple[bool, str]:
        if not self.path.is_file():
            return False, f"EKey file not found: {self.path.name}"
        return True, "ok"

    def plan(self, source_path: Path) -> DecodePlan:
        if not self.path.is_file():
            raise KeyUnavailable(f"EKey file is not available: {self.path.name}")
        try:
            raw = self.path.read_text(encoding="utf-8", errors="strict")
        except OSError as exc:
            raise KeyUnavailable(f"cannot read the configured EKey file: {exc}") from exc
        ekey = ""
        for line in raw.splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith("#"):
                ekey = candidate
                break
        if not ekey:
            raise KeyUnavailable("the configured EKey file contains no key")
        return DecodePlan(ekey=ekey, source_kind="ekey_file")


class PlayerProcessDbProvider(KeyProvider):
    """Reads keys from an exported Android ``player_process_db`` SQLite file."""

    kind = "player_process_db"

    def __init__(self, path: Path) -> None:
        self.path = path

    def check(self) -> tuple[bool, str]:
        if not self.path.is_file():
            return False, f"key database not found: {self.path.name}"
        return True, "ok"

    def plan(self, source_path: Path) -> DecodePlan:
        if not self.path.is_file():
            raise KeyUnavailable(f"key database is not available: {self.path.name}")
        return DecodePlan(key_db=self.path, source_kind="player_process_db")


def build_key_provider(kind: str, path: Path | None) -> KeyProvider:
    if kind == "ekey_file":
        if path is None:
            raise KeyUnavailable("key_source=ekey_file requires key_source_path")
        return ExplicitEkeyFileProvider(path)
    if kind == "player_process_db":
        if path is None:
            raise KeyUnavailable("key_source=player_process_db requires key_source_path")
        return PlayerProcessDbProvider(path)
    return NoKeyProvider()


def default_decoder_path() -> Path | None:
    """Locate the offline decoder shipped in this repository."""
    helper_dir = Path(__file__).resolve().parent.parent
    candidates = [
        helper_dir.parent / "mflac-linux-decoder" / "qmc_mflac.py",
        helper_dir / "vendor" / "qmc_mflac.py",
        helper_dir / "qmc_mflac.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_decoder_module(decoder_path: Path):
    """Import ``qmc_mflac.py`` by path so the helper needs no installation."""
    if not decoder_path.is_file():
        raise DecoderUnavailable(f"decoder module not found: {decoder_path}")
    spec = importlib.util.spec_from_file_location("qmc_mflac_helper", decoder_path)
    if spec is None or spec.loader is None:
        raise DecoderUnavailable(f"decoder module could not be loaded: {decoder_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["qmc_mflac_helper"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DecoderUnavailable(f"decoder module failed to import: {exc}") from exc
    return module


class Decoder:
    """Thin wrapper around the offline decoder with redaction-safe errors."""

    def __init__(self, decoder_path: Path, redactor=None) -> None:
        self.decoder_path = decoder_path
        self._module = None
        self._redactor = redactor

    @property
    def module(self):
        if self._module is None:
            self._module = load_decoder_module(self.decoder_path)
        return self._module

    def inspect(self, source_path: Path) -> str:
        """Return the container kind without decrypting anything."""
        module = self.module
        size = source_path.stat().st_size
        with source_path.open("rb") as handle:
            if size > 1024:
                handle.seek(-1024, 2)
            footer = module.parse_footer(handle.read(1024))
        return footer.kind if footer is not None else "unknown"

    def decode(self, source_path: Path, output_path: Path, plan: DecodePlan) -> str:
        """Decrypt ``source_path`` to ``output_path``; returns the container kind."""
        module = self.module
        if plan.ekey and self._redactor is not None:
            # Register before the decode so anything logged during it is scrubbed.
            self._redactor.add_secret(plan.ekey)
        try:
            footer = module.decrypt_file(
                source_path, output_path, plan.ekey, plan.key_db
            )
        except module.DecodeError as exc:
            # The decoder's messages never embed the key value.
            raise DecodeFailure(str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise DecodeFailure(f"decoder could not process the file: {exc}") from exc
        return getattr(footer, "kind", "unknown")

    def self_test(self) -> None:
        """Run the decoder's own smoke test."""
        self.module.self_test()
