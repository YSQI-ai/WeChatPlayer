"""HTTP client for the private helper protocol (standard library only).

Retry policy matters for the exactly-once guarantee:

* ``complete``/``fail``/``release``/heartbeats are idempotent server-side, so a
  lost response is retried safely -- the server answers ``duplicate`` instead of
  publishing a second result.
* ``claim`` is *not* retried after a timeout.  If the request reached the server
  but the response was lost, retrying would lease a second job while the first
  sits leased until it expires.  Instead the poll simply returns no work and the
  abandoned lease is reclaimed by the server.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

USER_AGENT = "WeChatPlayerHelper/0.1 (+windows)"
UPLOAD_CHUNK_SIZE = 1024 * 1024


class HelperApiError(Exception):
    """An API call failed with a definitive (non-retryable) error."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class AuthenticationError(HelperApiError):
    """The device token is missing, wrong, expired, or revoked."""


class TransientApiError(Exception):
    """A call failed in a way that may succeed on a later attempt."""


@dataclass
class Job:
    """A leased job as described by the server."""

    job_id: int
    lease_token: str
    source_filename: str
    target_format: str
    attempt: int
    max_attempts: int
    source_size_bytes: int | None = None
    reason: str | None = None
    lease_expires_at: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Job":
        return cls(
            job_id=int(payload["job_id"]),
            lease_token=str(payload["lease_token"]),
            source_filename=str(payload.get("source_filename") or "source.mflac"),
            target_format=str(payload.get("target_format") or "flac"),
            attempt=int(payload.get("attempts") or 1),
            max_attempts=int(payload.get("max_attempts") or 3),
            source_size_bytes=payload.get("source_size_bytes"),
            reason=payload.get("reason"),
            lease_expires_at=str(payload.get("lease_expires_at") or ""),
        )


class _MultipartBody:
    """Iterable multipart body so a large result is never held in memory."""

    def __init__(self, prefix: bytes, file_path: Path, suffix: bytes) -> None:
        self.prefix = prefix
        self.file_path = file_path
        self.suffix = suffix
        self.length = len(prefix) + file_path.stat().st_size + len(suffix)

    def __iter__(self) -> Iterator[bytes]:
        yield self.prefix
        with self.file_path.open("rb") as handle:
            while True:
                chunk = handle.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        yield self.suffix


def encode_multipart(
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
    filename: str,
    content_type: str = "application/octet-stream",
) -> tuple[_MultipartBody, str]:
    """Build a streaming multipart/form-data body."""
    boundary = f"----WeChatPlayerHelper{secrets.token_hex(16)}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
                "utf-8"
            )
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode("utf-8")
    )
    prefix = b"".join(parts)
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return _MultipartBody(prefix, file_path, suffix), f"multipart/form-data; boundary={boundary}"


class HelperClient:
    """Talks to the private ``/api/v1/helper`` surface."""

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: int = 60,
        download_timeout: int = 900,
        upload_timeout: int = 900,
        retries: int = 3,
        backoff_seconds: float = 2.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.download_timeout = download_timeout
        self.upload_timeout = upload_timeout
        self.retries = max(0, retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.verify_tls = verify_tls
        self._ssl_context = (
            ssl.create_default_context() if verify_tls else ssl._create_unverified_context()  # noqa: SLF001
        )

    # -- transport -------------------------------------------------------

    def _open(
        self,
        request: urllib.request.Request,
        timeout: int,
        body: Iterable[bytes] | bytes | None,
    ) -> Any:
        return urllib.request.urlopen(request, data=body, timeout=timeout, context=self._ssl_context)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Iterable[bytes] | bytes | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int | None = None,
        retry: bool = True,
        retry_on_timeout: bool = True,
        content_length: int | None = None,
    ) -> tuple[int, bytes]:
        """Perform one API call and return ``(status, body)``."""
        url = f"{self.base_url}{path}"
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        request_headers.update(headers or {})

        payload = body
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if content_length is not None:
            request_headers["Content-Length"] = str(content_length)

        effective_timeout = timeout or self.request_timeout
        attempts = self.retries + 1 if retry else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=payload if payload is not None else None, headers=request_headers, method=method
            )
            try:
                with self._open(request, effective_timeout, payload) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                detail = _read_error_detail(exc)
                if exc.code in (401, 403):
                    raise AuthenticationError(exc.code, detail) from exc
                if exc.code >= 500 or exc.code == 429:
                    last_error = TransientApiError(f"HTTP {exc.code}: {detail}")
                else:
                    raise HelperApiError(exc.code, detail) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError) as exc:
                if isinstance(exc, (socket.timeout, TimeoutError)) and not retry_on_timeout:
                    raise TransientApiError(f"timed out after the request was sent: {exc}") from exc
                last_error = TransientApiError(str(exc))

            if attempt + 1 < attempts:
                delay = self.backoff_seconds * (2**attempt)
                logger.debug("Retrying %s %s in %.1fs after %s", method, path, delay, last_error)
                time.sleep(delay)

        raise last_error or TransientApiError("request failed")

    @staticmethod
    def _auth_headers(device_id: str, device_token: str, lease: str | None = None) -> dict[str, str]:
        headers = {"X-Helper-Device": device_id, "X-Helper-Token": device_token}
        if lease is not None:
            # A header, not a query parameter, so the lease stays out of proxy logs.
            headers["X-Helper-Lease"] = lease
        return headers

    # -- device ----------------------------------------------------------

    def register(
        self,
        *,
        enrollment_secret: str,
        device_name: str,
        platform: str,
        helper_version: str,
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, body = self._request(
            "POST",
            "/api/v1/helper/devices/register",
            headers={"X-Helper-Enroll-Secret": enrollment_secret},
            json_body={
                "device_name": device_name,
                "platform": platform,
                "helper_version": helper_version,
                "capabilities": capabilities or {},
            },
            retry_on_timeout=False,
        )
        return _decode_json(status, body)

    def device_heartbeat(self, device_id: str, device_token: str) -> dict[str, Any]:
        status, body = self._request(
            "POST",
            "/api/v1/helper/devices/heartbeat",
            headers=self._auth_headers(device_id, device_token),
        )
        return _decode_json(status, body)

    # -- jobs ------------------------------------------------------------

    def claim(self, device_id: str, device_token: str) -> Job | None:
        status, body = self._request(
            "POST",
            "/api/v1/helper/jobs/claim",
            headers=self._auth_headers(device_id, device_token),
            # Never re-issue a claim whose response may have been lost.
            retry_on_timeout=False,
        )
        payload = _decode_json(status, body)
        job = payload.get("job")
        if not job:
            return None
        return Job.from_payload(job)

    def job_heartbeat(self, device_id: str, device_token: str, job_id: int, lease: str) -> dict[str, Any]:
        status, body = self._request(
            "POST",
            f"/api/v1/helper/jobs/{job_id}/heartbeat",
            headers=self._auth_headers(device_id, device_token, lease),
        )
        return _decode_json(status, body)

    def download_source(
        self, device_id: str, device_token: str, job_id: int, lease: str, destination: Path, max_bytes: int
    ) -> int:
        """Stream the encrypted source to ``destination``; returns bytes written."""
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/helper/jobs/{job_id}/source",
            headers=self._auth_headers(device_id, device_token, lease),
            method="GET",
        )
        written = 0
        try:
            with self._open(request, self.download_timeout, None) as response:
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(UPLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise HelperApiError(413, "source file exceeds the configured limit")
                        output.write(chunk)
        except urllib.error.HTTPError as exc:
            detail = _read_error_detail(exc)
            if exc.code in (401, 403):
                raise AuthenticationError(exc.code, detail) from exc
            raise HelperApiError(exc.code, detail) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise TransientApiError(str(exc)) from exc
        return written

    def complete(
        self,
        device_id: str,
        device_token: str,
        job_id: int,
        lease: str,
        result_path: Path,
        output_name: str = "",
    ) -> dict[str, Any]:
        """Upload the decoded audio. Safe to retry: the server de-duplicates."""
        body, content_type = encode_multipart(
            {"output_name": output_name} if output_name else {},
            "file",
            result_path,
            result_path.name,
            content_type="application/octet-stream",
        )
        status, response = self._request(
            "POST",
            f"/api/v1/helper/jobs/{job_id}/complete",
            headers=self._auth_headers(device_id, device_token, lease) | {"Content-Type": content_type},
            body=body,
            timeout=self.upload_timeout,
            content_length=body.length,
        )
        return _decode_json(status, response)

    def fail(
        self, device_id: str, device_token: str, job_id: int, lease: str, reason: str, message: str
    ) -> dict[str, Any]:
        body, content_type = encode_multipart_document(
            {"reason": reason, "error_message": message[:2000]}
        )
        status, response = self._request(
            "POST",
            f"/api/v1/helper/jobs/{job_id}/fail",
            headers=self._auth_headers(device_id, device_token, lease) | {"Content-Type": content_type},
            body=body,
            content_length=len(body),
        )
        return _decode_json(status, response)

    def release(self, device_id: str, device_token: str, job_id: int, lease: str) -> dict[str, Any]:
        status, body = self._request(
            "POST",
            f"/api/v1/helper/jobs/{job_id}/release",
            headers=self._auth_headers(device_id, device_token, lease),
        )
        return _decode_json(status, body)

    def job_status(self, device_id: str, device_token: str, job_id: int) -> dict[str, Any]:
        status, body = self._request(
            "GET",
            f"/api/v1/helper/jobs/{job_id}",
            headers=self._auth_headers(device_id, device_token),
        )
        return _decode_json(status, body)


def encode_multipart_document(fields: dict[str, str]) -> tuple[bytes, str]:
    """Encode a small form body without a file part."""
    boundary = f"----WeChatPlayerHelper{secrets.token_hex(16)}"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for name, value in fields.items()
    ]
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _decode_json(status: int, body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HelperApiError(status, "server returned a non-JSON response") from exc
    if not isinstance(decoded, dict):
        raise HelperApiError(status, "server returned an unexpected payload")
    return decoded


def _read_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        if isinstance(payload, dict) and "detail" in payload:
            return str(payload["detail"])
    except Exception:
        pass
    return str(exc.reason or "request failed")


def helper_version() -> str:
    return os.environ.get("HELPER_VERSION", "0.1.0")
