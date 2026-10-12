"""Private HTTP protocol for the Windows MFLAC helper.

Security posture
----------------
* The whole surface is disabled unless ``HELPER_ENROLLMENT_SECRET`` is set, so a
  default deployment never exposes an unauthenticated work queue.
* Enrollment uses the shared secret; every other call needs a per-device token.
  Only the token's SHA-256 is stored, and tokens are compared in constant time.
* Lease tokens travel in the ``X-Helper-Lease`` header, not in a URL, so they do
  not reach proxy access logs.
* The helper is never given the result download token.  That capability URL goes
  to the Mini Program through its own endpoint, so a compromised helper cannot
  enumerate previously finished results.
* No EKey is accepted on this surface.  The helper decodes locally and uploads
  only the finished audio, per docs/decisions/001-encrypted-audio.md.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

try:
    from . import helper_store
    from .helper_store import (
        COMPLETED,
        DEVICE_ACTIVE,
        DUPLICATE,
        FAILED,
        LEASED,
        NOT_FOUND,
        REJECTED,
        STALE,
        SUCCEEDED,
        TERMINAL_STATES,
        TIMEOUT,
    )
except ImportError:  # pragma: no cover - supports running main.py directly
    import helper_store  # type: ignore[no-redef]
    from helper_store import (  # type: ignore[no-redef]
        COMPLETED,
        DEVICE_ACTIVE,
        DUPLICATE,
        FAILED,
        LEASED,
        NOT_FOUND,
        REJECTED,
        STALE,
        SUCCEEDED,
        TERMINAL_STATES,
        TIMEOUT,
    )

logger = logging.getLogger("wechat-converter-api.helper")

HELPER_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
DEVICE_ID_RE = re.compile(r"dev_[0-9a-f]{16}")
LEASE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32}")
# Accepted result container signatures: FLAC, Ogg (ogg/opus), RIFF (wav), ID3/mp3.
RESULT_SIGNATURES: dict[bytes, str] = {
    b"fLaC": "audio/flac",
    b"OggS": "audio/ogg",
    b"RIFF": "audio/wav",
    b"ID3": "audio/mpeg",
}
FLAC_SIGNATURE = b"fLaC"

RESULT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class HelperSettings:
    """Runtime configuration for the helper surface."""

    enrollment_secret: str
    tmp_root: Path
    lease_seconds: int = helper_store.DEFAULT_LEASE_SECONDS
    max_attempts: int = helper_store.DEFAULT_MAX_ATTEMPTS
    token_ttl_days: int = helper_store.DEFAULT_HELPER_TOKEN_TTL_DAYS
    heartbeat_seconds: int = 60
    max_source_bytes: int = 1024 * 1024 * 1024
    max_result_bytes: int = 1024 * 1024 * 1024
    source_ttl_seconds: int = 7 * 24 * 3600

    @property
    def enabled(self) -> bool:
        return bool(self.enrollment_secret)

    @classmethod
    def from_env(cls, tmp_root: Path) -> HelperSettings:
        return cls(
            enrollment_secret=os.getenv("HELPER_ENROLLMENT_SECRET", "").strip(),
            tmp_root=tmp_root,
            lease_seconds=int(os.getenv("HELPER_LEASE_SECONDS", str(helper_store.DEFAULT_LEASE_SECONDS))),
            max_attempts=int(os.getenv("HELPER_MAX_ATTEMPTS", str(helper_store.DEFAULT_MAX_ATTEMPTS))),
            token_ttl_days=int(os.getenv("HELPER_TOKEN_TTL_DAYS", str(helper_store.DEFAULT_HELPER_TOKEN_TTL_DAYS))),
            heartbeat_seconds=int(os.getenv("HELPER_HEARTBEAT_SECONDS", "60")),
            max_source_bytes=int(os.getenv("HELPER_MAX_SOURCE_BYTES", str(1024 * 1024 * 1024))),
            max_result_bytes=int(os.getenv("HELPER_MAX_RESULT_BYTES", str(1024 * 1024 * 1024))),
            source_ttl_seconds=int(os.getenv("HELPER_SOURCE_TTL_SECONDS", str(7 * 24 * 3600))),
        )


class PublishResult(Protocol):
    """Move a finished result into a servable download directory."""

    def __call__(
        self,
        *,
        job_dir: Path,
        output_path: Path,
        filename: str,
        source_filename: str,
        client_id: str,
        media_type: str,
        token: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> int: ...


def _require_enabled(settings: HelperSettings) -> None:
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="Helper protocol is not enabled on this server")


def _safe_source_path(settings: HelperSettings, job: dict[str, Any]) -> Path:
    """Resolve a job's source path, refusing anything outside the temp root."""
    raw = Path(str(job["source_path"]))
    root = settings.tmp_root.resolve()
    try:
        resolved = raw.resolve()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Job source file is unavailable") from exc
    if root not in resolved.parents or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Job source file is unavailable")
    return resolved


def _job_public_view(job: dict[str, Any], settings: HelperSettings) -> dict[str, Any]:
    """The subset of job state a helper may see."""
    return {
        "job_id": int(job["id"]),
        "status": str(job["status"]),
        "source_filename": str(job["source_filename"]),
        "source_size_bytes": job.get("source_size_bytes"),
        "target_format": str(job["target_format"]),
        "reason": job.get("reason"),
        "attempts": int(job["attempts"]),
        "max_attempts": int(job["max_attempts"]),
        "client_id": str(job["client_id"]),
        "lease_token": job.get("lease_token"),
        "lease_expires_at": _iso(job.get("lease_expires_at")),
        "created_at": _iso(job.get("created_at")),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _result_summary(job: dict[str, Any]) -> dict[str, Any]:
    """Result description without the download capability token."""
    return {
        "job_id": int(job["id"]),
        "job_status": str(job["status"]),
        "filename": job.get("result_filename"),
        "media_type": job.get("result_media_type"),
        "size_bytes": job.get("result_size_bytes"),
    }


def _client_job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Client-facing job status for the Mini Program, including the download link."""
    status = str(job["status"])
    payload: dict[str, Any] = {
        "job_id": int(job["id"]),
        "status": status,
        "source_filename": str(job["source_filename"]),
        "target_format": str(job["target_format"]),
        "created_at": _iso(job.get("created_at")),
        "completed_at": _iso(job.get("completed_at")),
        "error_message": job.get("error_message") or "",
    }
    token = job.get("result_token")
    if status == SUCCEEDED and token:
        payload["download_url"] = f"/api/v1/download/{token}"
        payload["filename"] = job.get("result_filename")
        payload["media_type"] = job.get("result_media_type")
        payload["size_bytes"] = job.get("result_size_bytes")
    return payload


def _read_result_file(
    upload: UploadFile, destination: Path, limit: int, expected_format: str
) -> tuple[int, str]:
    """Stream an uploaded result to disk and validate its audio signature."""
    total = 0
    with destination.open("wb") as output:
        while True:
            chunk = upload.file.read(RESULT_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail="Helper result is too large")
            output.write(chunk)
    if total == 0:
        raise HTTPException(status_code=422, detail="Helper result is empty")
    with destination.open("rb") as stored:
        header = stored.read(4)
    media_type = RESULT_SIGNATURES.get(header)
    if media_type is None:
        raise HTTPException(status_code=422, detail="Helper result is not a recognized audio file")
    if expected_format == "flac" and header != FLAC_SIGNATURE:
        raise HTTPException(status_code=422, detail="Helper result is not a FLAC file")
    return total, media_type


def build_router(
    *,
    store: helper_store.HelperStore,
    settings: HelperSettings,
    publish_result: PublishResult,
) -> APIRouter:
    """Build the helper router.

    ``publish_result`` is injected so this module never imports ``main``; that
    avoids a circular import and keeps the composition root in one place.
    """
    router = APIRouter(prefix="/api/v1/helper", tags=["helper"])

    # -- authentication --------------------------------------------------

    def require_enrollment(
        x_helper_enroll_secret: str | None = Header(default=None, alias="X-Helper-Enroll-Secret"),
    ) -> None:
        _require_enabled(settings)
        presented = (x_helper_enroll_secret or "").strip()
        if not presented or not secrets.compare_digest(presented, settings.enrollment_secret):
            raise HTTPException(status_code=401, detail="Invalid enrollment secret")

    def require_device(
        x_helper_device: str | None = Header(default=None, alias="X-Helper-Device"),
        x_helper_token: str | None = Header(default=None, alias="X-Helper-Token"),
    ) -> dict[str, Any]:
        _require_enabled(settings)
        device_id = (x_helper_device or "").strip()
        token = (x_helper_token or "").strip()
        if not DEVICE_ID_RE.fullmatch(device_id) or not HELPER_TOKEN_RE.fullmatch(token):
            raise HTTPException(status_code=401, detail="Helper authentication required")
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=401, detail="Unknown helper device")
        if not secrets.compare_digest(str(device["token_hash"]), helper_store.hash_token(token)):
            raise HTTPException(status_code=401, detail="Invalid helper token")
        if str(device["status"]) != DEVICE_ACTIVE:
            raise HTTPException(status_code=403, detail="Helper device is revoked")
        expires_at = device.get("token_expires_at")
        if expires_at is not None and expires_at < helper_store.utcnow():
            raise HTTPException(status_code=403, detail="Helper token has expired")
        store.touch_device(device_id)
        return device

    def require_lease(x_helper_lease: str | None = Header(default=None, alias="X-Helper-Lease")) -> str:
        lease = (x_helper_lease or "").strip()
        if not LEASE_TOKEN_RE.fullmatch(lease):
            raise HTTPException(status_code=400, detail="Missing or malformed lease token")
        return lease

    def _owned_leased_job(job_id: int, device: dict[str, Any], lease: str) -> dict[str, Any]:
        """Load a job and confirm this device still owns the given lease."""
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if str(job["status"]) in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail=f"Job is already {job['status']}")
        if str(job["status"]) != LEASED:
            raise HTTPException(status_code=409, detail="Job is not leased")
        if job.get("lease_owner") != device["device_id"]:
            raise HTTPException(status_code=409, detail="Job is leased to another helper")
        if job.get("lease_token") != lease:
            raise HTTPException(status_code=409, detail="Lease token is stale")
        return job

    # -- device lifecycle ------------------------------------------------

    @router.post("/devices/register")
    def register_device(
        payload: dict[str, Any] | None = None,
        _: None = Depends(require_enrollment),
    ) -> JSONResponse:
        body = payload or {}
        device_name = str(body.get("device_name") or "").strip()[:128] or "windows-helper"
        platform = str(body.get("platform") or "").strip()[:64] or None
        helper_version = str(body.get("helper_version") or "").strip()[:32] or None
        capabilities = body.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise HTTPException(status_code=400, detail="capabilities must be an object")

        device_id = helper_store.new_device_id()
        token = helper_store.new_device_token()
        expires_at = helper_store.utcnow() + timedelta(days=settings.token_ttl_days)
        store.register_device(
            device_id=device_id,
            device_name=device_name,
            token_hash=helper_store.hash_token(token),
            platform=platform,
            helper_version=helper_version,
            capabilities=capabilities if isinstance(capabilities, dict) else None,
            token_expires_at=expires_at,
        )
        logger.info("Registered helper device %s (%s)", device_id, device_name)
        return JSONResponse(
            {
                "device_id": device_id,
                # Returned exactly once; only the hash is stored server-side.
                "device_token": token,
                "token_expires_at": expires_at.isoformat(),
                "lease_seconds": settings.lease_seconds,
                "heartbeat_seconds": settings.heartbeat_seconds,
                "max_attempts": settings.max_attempts,
            },
            status_code=201,
        )

    @router.post("/devices/heartbeat")
    def device_heartbeat(device: dict[str, Any] = Depends(require_device)) -> dict[str, Any]:
        store.reconcile_expired()
        return {
            "status": "ok",
            "device_id": device["device_id"],
            "server_time": helper_store.utcnow().isoformat(),
            "lease_seconds": settings.lease_seconds,
            "heartbeat_seconds": settings.heartbeat_seconds,
            "queue": store.queue_stats(),
        }

    @router.post("/devices/{device_id}/revoke")
    def revoke_device(device_id: str, _: None = Depends(require_enrollment)) -> dict[str, Any]:
        if not DEVICE_ID_RE.fullmatch(device_id):
            raise HTTPException(status_code=400, detail="Malformed device id")
        if not store.revoke_device(device_id):
            raise HTTPException(status_code=404, detail="Helper device not found")
        logger.warning("Revoked helper device %s", device_id)
        return {"status": "revoked", "device_id": device_id}

    @router.get("/devices")
    def list_devices(_: None = Depends(require_enrollment)) -> dict[str, Any]:
        return {
            "devices": [
                {
                    "device_id": device["device_id"],
                    "device_name": device["device_name"],
                    "platform": device.get("platform"),
                    "helper_version": device.get("helper_version"),
                    "status": device["status"],
                    "last_seen_at": _iso(device.get("last_seen_at")),
                    "created_at": _iso(device.get("created_at")),
                }
                for device in store.list_devices()
            ]
        }

    # -- job queue: helper side ------------------------------------------

    @router.post("/jobs/claim")
    def claim_job(device: dict[str, Any] = Depends(require_device)) -> dict[str, Any]:
        # Reclaim expired leases before handing out new work so a dead helper's
        # job becomes claimable within the same call.
        requeued, timed_out = store.reconcile_expired()
        if requeued or timed_out:
            logger.info("Lease reconciliation: requeued=%s timed_out=%s", requeued, timed_out)
        lease_token = helper_store.new_lease_token()
        job = store.claim_job(
            device_id=str(device["device_id"]),
            lease_token=lease_token,
            lease_seconds=settings.lease_seconds,
        )
        if job is None:
            return {"job": None, "lease_seconds": settings.lease_seconds}
        return {"job": _job_public_view(job, settings), "lease_seconds": settings.lease_seconds}

    @router.post("/jobs/{job_id}/heartbeat")
    def job_heartbeat(
        job_id: int,
        device: dict[str, Any] = Depends(require_device),
        lease: str = Depends(require_lease),
    ) -> dict[str, Any]:
        job = _owned_leased_job(job_id, device, lease)
        if not store.extend_lease(job_id, lease, lease_seconds=settings.lease_seconds):
            raise HTTPException(status_code=409, detail="Lease was lost")
        refreshed = store.get_job(job_id) or job
        return {
            "status": "ok",
            "job_id": job_id,
            "lease_expires_at": _iso(refreshed.get("lease_expires_at")),
            "lease_seconds": settings.lease_seconds,
        }

    @router.get("/jobs/{job_id}/source")
    def download_source(
        job_id: int,
        device: dict[str, Any] = Depends(require_device),
        lease: str = Depends(require_lease),
    ) -> FileResponse:
        job = _owned_leased_job(job_id, device, lease)
        source_path = _safe_source_path(settings, job)
        return FileResponse(
            source_path,
            media_type="application/octet-stream",
            filename=str(job["source_filename"]),
        )

    @router.post("/jobs/{job_id}/complete")
    def complete_job(
        job_id: int,
        file: UploadFile = File(...),
        output_name: str = Form(""),
        device: dict[str, Any] = Depends(require_device),
        lease: str = Depends(require_lease),
    ) -> JSONResponse:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        # A repeat report for an already-finished job is answered without
        # touching the upload, so a retry can never mint a second result.
        if str(job["status"]) == SUCCEEDED:
            return JSONResponse({"status": DUPLICATE, **_result_summary(job)})
        _owned_leased_job(job_id, device, lease)

        job_dir: Path | None = None
        token: str | None = None
        try:
            job_dir = _unique_dir(_ensure_tmp(settings), f"helper-upload-{job_id}-")
            uploaded = job_dir / "result.upload"
            size_bytes, media_type = _read_result_file(
                file, uploaded, settings.max_result_bytes, str(job["target_format"])
            )
            filename = _result_filename(
                output_name, str(job["source_filename"]), str(job["target_format"])
            )

            # Publish first, then record: the database never references a
            # download token whose directory failed to materialize.
            token = secrets.token_urlsafe(32)
            published_size = publish_result(
                job_dir=job_dir,
                output_path=uploaded,
                filename=filename,
                source_filename=str(job["source_filename"]),
                client_id=str(job["client_id"]),
                media_type=media_type,
                token=token,
                extra_metadata={"helper_job_id": job_id, "helper_device": device["device_id"]},
            )
            job_dir = None  # publish_result consumed the directory

            outcome = store.complete_job(
                job_id,
                lease,
                result_token=token,
                result_filename=filename,
                result_media_type=media_type,
                result_size_bytes=published_size or size_bytes,
            )
            if outcome == COMPLETED:
                _drop_source_dir(store.get_job(job_id) or job)
                logger.info("Helper job %s completed by %s", job_id, device["device_id"])
                return JSONResponse({"status": COMPLETED, **(_result_summary(store.get_job(job_id) or job))})
            if outcome == DUPLICATE:
                # Another attempt finished first; withdraw our unreferenced copy.
                _discard_published(settings, token)
                latest = store.get_job(job_id) or job
                return JSONResponse({"status": DUPLICATE, **_result_summary(latest)})
            _discard_published(settings, token)
            if outcome == STALE:
                raise HTTPException(status_code=409, detail="Lease expired and the job was reassigned")
            if outcome == NOT_FOUND:
                raise HTTPException(status_code=404, detail="Job not found")
            raise HTTPException(status_code=409, detail=f"Job is already {store.get_job(job_id)['status']}")
        finally:
            file.file.close()
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)

    @router.post("/jobs/{job_id}/fail")
    def fail_job(
        job_id: int,
        reason: str = Form("failed"),
        error_message: str = Form(""),
        device: dict[str, Any] = Depends(require_device),
        lease: str = Depends(require_lease),
    ) -> JSONResponse:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if str(job["status"]) in {FAILED, TIMEOUT}:
            return JSONResponse({"status": DUPLICATE, **_result_summary(job)})
        _owned_leased_job(job_id, device, lease)

        status = TIMEOUT if reason == "timeout" else FAILED
        outcome = store.fail_job(job_id, lease, status=status, error_message=error_message or reason)
        if outcome == COMPLETED:
            _drop_source_dir(store.get_job(job_id) or job)
            logger.warning("Helper job %s marked %s by %s: %s", job_id, status, device["device_id"], error_message[:200])
            return JSONResponse({"status": COMPLETED, "job_id": job_id, "job_status": status})
        if outcome == DUPLICATE:
            return JSONResponse({"status": DUPLICATE, **_result_summary(store.get_job(job_id) or job)})
        if outcome == STALE:
            raise HTTPException(status_code=409, detail="Lease expired and the job was reassigned")
        raise HTTPException(status_code=409, detail=f"Job is already {store.get_job(job_id)['status']}")

    @router.post("/jobs/{job_id}/release")
    def release_job(
        job_id: int,
        device: dict[str, Any] = Depends(require_device),
        lease: str = Depends(require_lease),
    ) -> dict[str, Any]:
        """Return a claimed job to the queue when a helper shuts down cleanly."""
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        _owned_leased_job(job_id, device, lease)
        outcome = store.release_job(job_id, lease)
        if outcome != COMPLETED:
            raise HTTPException(status_code=409, detail="Lease was lost")
        latest = store.get_job(job_id) or job
        return {"status": "released", "job_id": job_id, "job_status": str(latest["status"])}

    @router.get("/jobs/{job_id}")
    def job_status(
        job_id: int,
        device: dict[str, Any] = Depends(require_device),
    ) -> dict[str, Any]:
        """Status probe so a restarted helper can reconcile an interrupted job."""
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job_id,
            "status": str(job["status"]),
            "attempts": int(job["attempts"]),
            "max_attempts": int(job["max_attempts"]),
            "lease_owner": job.get("lease_owner"),
            "completed_at": _iso(job.get("completed_at")),
        }

    # -- job queue: operator side ----------------------------------------

    @router.post("/jobs", status_code=201)
    def enqueue_job(
        file: UploadFile = File(...),
        client_id: str = Form("anonymous"),
        target_format: str = Form("flac"),
        reason: str = Form("missing_ekey"),
        output_name: str = Form(""),
        _: None = Depends(require_enrollment),
    ) -> JSONResponse:
        """Accept a source MFLAC that the server cannot decode on its own."""
        target = target_format.lower().strip().lstrip(".") or "flac"
        if target != "flac":
            raise HTTPException(status_code=400, detail="The helper queue currently supports target_format=flac")

        # The source directory is named before the job exists, so the job row is
        # inserted once with its final path and no second write is needed.
        source_dir = _unique_dir(_ensure_tmp(settings), "helper-source-")
        try:
            source_path = source_dir / "source.mflac"
            total = 0
            with source_path.open("wb") as output:
                while True:
                    chunk = file.file.read(RESULT_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > settings.max_source_bytes:
                        raise HTTPException(status_code=413, detail="Source file is too large for the helper queue")
                    output.write(chunk)
            if total == 0:
                raise HTTPException(status_code=422, detail="Source file is empty")

            with source_path.open("rb") as stored:
                if not _looks_like_qmc(stored):
                    raise HTTPException(
                        status_code=422,
                        detail="Helper queue accepts QQ Music encrypted files (.mflac/.mgg/.qmc)",
                    )

            job_key = helper_store.job_key_for(client_id, reason, total, _fingerprint(source_path))
            job, created = store.create_job(
                client_id=client_id or "anonymous",
                source_filename=output_name or file.filename or "source.mflac",
                source_path=str(source_path),
                target_format=target,
                source_size_bytes=total,
                reason=reason,
                max_attempts=settings.max_attempts,
                job_key=job_key,
            )
            if not created:
                # Same content already queued; keep the existing job and drop this copy.
                shutil.rmtree(source_dir, ignore_errors=True)
                return JSONResponse(
                    {
                        "job_id": int(job["id"]),
                        "status": str(job["status"]),
                        "created": False,
                        "status_url": f"/api/v1/jobs/{int(job['id'])}?client_id={client_id}",
                    },
                    status_code=200,
                )

            logger.info("Queued helper job %s for client %s (%s bytes)", job["id"], client_id, total)
            return JSONResponse(
                {
                    "job_id": int(job["id"]),
                    "status": str(job["status"]),
                    "created": True,
                    "source_filename": job["source_filename"],
                    "status_url": f"/api/v1/jobs/{int(job['id'])}?client_id={client_id}",
                },
                status_code=201,
            )
        finally:
            file.file.close()

    @router.get("/jobs")
    def list_jobs(
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        _: None = Depends(require_enrollment),
    ) -> dict[str, Any]:
        jobs = store.list_jobs(status=status, limit=limit)
        return {
            "jobs": [
                {
                    "job_id": int(job["id"]),
                    "status": str(job["status"]),
                    "client_id": str(job["client_id"]),
                    "source_filename": str(job["source_filename"]),
                    "attempts": int(job["attempts"]),
                    "lease_owner": job.get("lease_owner"),
                    "error_message": job.get("error_message") or "",
                    "created_at": _iso(job.get("created_at")),
                    "completed_at": _iso(job.get("completed_at")),
                }
                for job in jobs
            ],
            "stats": store.queue_stats(),
        }

    @router.post("/jobs/reconcile")
    def reconcile(_: None = Depends(require_enrollment)) -> dict[str, Any]:
        requeued, timed_out = store.reconcile_expired()
        return {"requeued": requeued, "timed_out": timed_out, "stats": store.queue_stats()}

    return router


def build_client_router(*, store: helper_store.HelperStore) -> APIRouter:
    """Mini Program facing job status endpoint (no helper credentials)."""
    router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

    @router.get("/{job_id}")
    def client_job_status(job_id: int, client_id: str = Query(..., min_length=1, max_length=128)) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None or str(job["client_id"]) != client_id:
            # Same response for missing and foreign jobs so job ids are not enumerable.
            raise HTTPException(status_code=404, detail="Job not found")
        return _client_job_view(job)

    return router


# -- helpers -------------------------------------------------------------


def _ensure_tmp(settings: HelperSettings) -> Path:
    settings.tmp_root.mkdir(parents=True, exist_ok=True)
    return settings.tmp_root


def enqueue_existing_file(
    *,
    store,
    settings: "HelperSettings",
    source_path: Path,
    source_filename: str,
    client_id: str,
    reason: str,
    target_format: str = "flac",
    output_name: str = "",
) -> dict[str, object]:
    """Queue a source file that is already on disk.

    Used by the conversion endpoint when it meets a file only an authorized
    helper can decode.  The file is *moved* into a dedicated source directory so
    the helper can download it, and the job row is inserted once with its final
    path so no second write is needed.

    Raises :class:`HelperEnqueueError` if the file cannot be queued; the caller
    still owns cleanup of its own job directory.
    """

    source_dir = _unique_dir(_ensure_tmp(settings), "helper-source-")
    try:
        stored_path = source_dir / "source.mflac"
        shutil.move(str(source_path), str(stored_path))
        total = stored_path.stat().st_size
        if total == 0:
            raise HelperEnqueueError("source file is empty")
        if total > settings.max_source_bytes:
            raise HelperEnqueueError("source file is too large for the helper queue")
        with stored_path.open("rb") as stored:
            if not _looks_like_qmc(stored):
                raise HelperEnqueueError("not a QQ Music encrypted file")

        job_key = helper_store.job_key_for(client_id, reason, total, _fingerprint(stored_path))
        job, created = store.create_job(
            client_id=client_id or "anonymous",
            source_filename=output_name or source_filename or "source.mflac",
            source_path=str(stored_path),
            target_format=target_format,
            source_size_bytes=total,
            reason=reason,
            max_attempts=settings.max_attempts,
            job_key=job_key,
        )
        if not created:
            # The same content is already queued; keep that job and drop this copy.
            shutil.rmtree(source_dir, ignore_errors=True)
        return {
            "job_id": int(job["id"]),
            "status": str(job["status"]),
            "created": created,
        }
    except Exception as exc:
        shutil.rmtree(source_dir, ignore_errors=True)
        if isinstance(exc, HelperEnqueueError):
            raise
        raise HelperEnqueueError(str(exc)) from exc


class HelperEnqueueError(Exception):
    """Raised when a source file cannot be handed to the helper queue."""


def _unique_dir(parent: Path, prefix: str) -> Path:
    """Create a uniquely named directory under ``parent``.

    Deliberately avoids ``tempfile.mkdtemp``.  On Windows that helper catches
    ``PermissionError`` and retries while ``os.access`` still reports the parent
    as writable, so a denied write spins through every candidate name instead of
    failing; a bounded loop over random names fails fast.  The directory is also
    created with the default mode: a ``0o700`` mode is a POSIX hardening measure
    that on Windows can leave the directory unwritable for its own creator.
    """
    parent.mkdir(parents=True, exist_ok=True)
    for _ in range(64):
        candidate = parent / f"{prefix}{secrets.token_hex(8)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not allocate a unique directory under {parent}")


def _discard_published(settings: HelperSettings, token: str) -> None:
    shutil.rmtree(settings.tmp_root / f"download-{token}", ignore_errors=True)


def _drop_source_dir(job: dict[str, Any]) -> None:
    """Delete a finished job's staged source file and its directory."""
    raw = job.get("source_path")
    if not raw:
        return
    directory = Path(str(raw)).parent
    shutil.rmtree(directory, ignore_errors=True)


def _result_filename(output_name: str, source_filename: str, target_format: str) -> str:
    raw = (output_name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not raw:
        raw = Path(source_filename or "converted").stem
    stem = re.sub(r"[^\w.-]+", "_", Path(raw).stem, flags=re.UNICODE).strip("._")
    return f"{(stem[:120] or 'converted')}.{target_format}"


def _looks_like_qmc(handle: Any) -> bool:
    """Detect a QQ Music container by its trailer marker or size footer."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    window = min(1024, size)
    handle.seek(size - window)
    tail = handle.read(window)
    if tail.endswith(b"musicex\x00") or tail.endswith(b"QTag") or tail.endswith(b"STag"):
        return True
    if len(tail) >= 4:
        length = int.from_bytes(tail[-4:], "little")
        if 0 < length <= 0x500 and len(tail) >= length + 4:
            return True
    return False


def _fingerprint(path: Path, limit: int = 64 * 1024) -> str:
    """Cheap content fingerprint for the job's idempotency key."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(limit))
    digest.update(str(path.stat().st_size).encode("ascii"))
    return digest.hexdigest()


def cleanup_expired_sources(store: helper_store.HelperStore, settings: HelperSettings) -> int:
    """Remove staged source files for jobs that are already finished."""
    removed = 0
    for job in store.list_jobs(limit=500):
        if str(job["status"]) not in TERMINAL_STATES:
            continue
        raw = job.get("source_path")
        if not raw:
            continue
        directory = Path(str(raw)).parent
        if directory.exists() and settings.tmp_root.resolve() in directory.resolve().parents:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed
