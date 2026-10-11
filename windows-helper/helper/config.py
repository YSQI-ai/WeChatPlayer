"""Configuration for the Windows MFLAC helper.

Format is INI (``configparser``) rather than JSON or TOML so Windows paths can
be written literally without escaping, and so the helper runs on any Python 3.9+
without ``tomllib``.

The enrollment secret is never read from the config file: it comes from the
``HELPER_ENROLLMENT_SECRET`` environment variable so it does not sit in a plain
file next to the helper.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.ini"
DEFAULT_API_BASE_URL = "https://api.yssqi.ccwu.cc"

VALID_KEY_SOURCES = ("none", "ekey_file", "player_process_db")


class ConfigError(Exception):
    """Raised when the helper configuration cannot be used."""


def _expand(value: str) -> str:
    """Expand %VARS% and ~ in a configured path."""
    return os.path.expanduser(os.path.expandvars(value.strip()))


def _as_bool(value: str, default: bool) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean value: {value!r}")


def _as_int(value: str, default: int, minimum: int = 0) -> int:
    text = (value or "").strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ConfigError(f"invalid integer value: {value!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"value must be >= {minimum}: {value!r}")
    return parsed


@dataclass
class HelperConfig:
    """Resolved helper settings."""

    api_base_url: str = DEFAULT_API_BASE_URL
    enrollment_secret: str = ""
    device_name: str = ""
    state_dir: Path = field(default_factory=lambda: Path.cwd() / "state")
    work_dir: Path | None = None

    poll_interval_seconds: int = 5
    heartbeat_seconds: int = 60
    lease_seconds: int = 900
    # 0 derives the lease-renewal interval from the server lease (lease / 3).
    lease_heartbeat_seconds: int = 0
    job_timeout_seconds: int = 1800
    request_timeout_seconds: int = 60
    download_timeout_seconds: int = 900
    upload_timeout_seconds: int = 900
    request_retries: int = 3
    retry_backoff_seconds: float = 2.0

    # Authorized key source. "none" makes the helper fail any job it cannot
    # decode, which is the correct default: it never guesses a key.
    key_source: str = "none"
    key_source_path: Path | None = None
    decoder_path: Path | None = None

    # A process that must be running before the helper claims work, e.g.
    # QQMusic.exe. Empty disables the check.
    required_process: str = ""
    min_free_disk_bytes: int = 512 * 1024 * 1024

    verify_tls: bool = True
    log_level: str = "INFO"
    max_attempts_hint: int = 3

    def __post_init__(self) -> None:
        self.api_base_url = self.api_base_url.rstrip("/")
        if self.work_dir is None:
            self.work_dir = self.state_dir / "work"
        if not self.device_name:
            self.device_name = os.environ.get("COMPUTERNAME") or "windows-helper"
        if self.key_source not in VALID_KEY_SOURCES:
            raise ConfigError(
                f"key_source must be one of {', '.join(VALID_KEY_SOURCES)}; got {self.key_source!r}"
            )
        if self.key_source != "none" and self.key_source_path is None:
            raise ConfigError(f"key_source={self.key_source} requires key_source_path")
        if not self.api_base_url:
            raise ConfigError("api_base_url is required")

    @property
    def credentials_path(self) -> Path:
        return self.state_dir / "credentials.bin"

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.bin"

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.work_dir is not None:
            self.work_dir.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """A loggable summary that contains no secret material."""
        return {
            "api_base_url": self.api_base_url,
            "device_name": self.device_name,
            "state_dir": str(self.state_dir),
            "key_source": self.key_source,
            "required_process": self.required_process or None,
            "enrollment_secret_configured": bool(self.enrollment_secret),
        }

    @classmethod
    def load(cls, config_path: Path | None = None) -> "HelperConfig":
        """Load configuration from an INI file plus environment overrides."""
        parser = configparser.ConfigParser()
        path = config_path or (Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME)
        if path.is_file():
            try:
                parser.read(path, encoding="utf-8")
            except configparser.Error as exc:
                raise ConfigError(f"cannot parse {path}: {exc}") from exc

        section = parser["helper"] if parser.has_section("helper") else {}

        def raw(key: str, default: str = "") -> str:
            return str(section.get(key, default)) if section else default

        state_dir_text = raw("state_dir") or os.environ.get(
            "LOCALAPPDATA", str(Path.home())
        ) + r"\WeChatPlayerHelper"

        config = cls(
            api_base_url=os.environ.get("HELPER_API_BASE_URL") or raw("api_base_url", DEFAULT_API_BASE_URL),
            enrollment_secret=os.environ.get("HELPER_ENROLLMENT_SECRET", "").strip(),
            device_name=os.environ.get("HELPER_DEVICE_NAME") or raw("device_name"),
            state_dir=Path(_expand(state_dir_text)),
            work_dir=Path(_expand(raw("work_dir"))) if raw("work_dir") else None,
            poll_interval_seconds=_as_int(raw("poll_interval_seconds"), 5, 1),
            heartbeat_seconds=_as_int(raw("heartbeat_seconds"), 60, 5),
            lease_seconds=_as_int(raw("lease_seconds"), 900, 30),
            lease_heartbeat_seconds=_as_int(raw("lease_heartbeat_seconds"), 0, 0),
            job_timeout_seconds=_as_int(raw("job_timeout_seconds"), 1800, 30),
            request_timeout_seconds=_as_int(raw("request_timeout_seconds"), 60, 5),
            download_timeout_seconds=_as_int(raw("download_timeout_seconds"), 900, 5),
            upload_timeout_seconds=_as_int(raw("upload_timeout_seconds"), 900, 5),
            request_retries=_as_int(raw("request_retries"), 3, 0),
            retry_backoff_seconds=float(raw("retry_backoff_seconds") or 2.0),
            key_source=(raw("key_source", "none").strip().lower() or "none"),
            key_source_path=Path(_expand(raw("key_source_path"))) if raw("key_source_path") else None,
            decoder_path=Path(_expand(raw("decoder_path"))) if raw("decoder_path") else None,
            required_process=raw("required_process").strip(),
            min_free_disk_bytes=_as_int(raw("min_free_disk_bytes"), 512 * 1024 * 1024),
            verify_tls=_as_bool(raw("verify_tls"), True),
            log_level=(raw("log_level", "INFO").strip().upper() or "INFO"),
            max_attempts_hint=_as_int(raw("max_attempts_hint"), 3, 1),
        )
        return config
