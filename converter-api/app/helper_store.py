"""Private helper queue: device registry and job state machine.

This module owns the persistent state behind the Windows helper protocol:

* ``helper_devices`` -- registered helper hosts and their revocable tokens.
* ``helper_jobs`` -- the queue with a lease-based, exactly-once completion
  contract.

The state machine is expressed as a small set of *atomic primitives*.  Both the
in-memory store (used by tests and by a database-less deployment) and the MySQL
store implement the same primitives, because atomicity has to live in the
primitive: that is where the database's row locking enforces it.

Job lifecycle::

    queued --claim--> leased --complete--> succeeded   (terminal)
                        |
                        +--fail--------> failed        (terminal)
                        +--timeout-----> timeout       (terminal)
                        +--release-----> queued        (retry, attempt spent)
                        +--lease expiry-> queued       (while attempts remain)
                                       \\-> timeout    (attempts exhausted)

Exactly-once completion
-----------------------
Every lease carries a random ``lease_token``.  ``complete_job`` performs a
conditional update guarded by ``status='leased' AND lease_token=<token>``:

* one row updated -> the caller won the race and this is the first completion;
* zero rows and the job is already ``succeeded`` -> ``duplicate``; the caller
  must return the *existing* result instead of creating a second one;
* zero rows and the job is ``leased`` under a different token -> ``stale``; a
  helper whose lease expired and was reassigned cannot overwrite the new
  attempt's result;
* zero rows and the job is terminal otherwise -> ``terminal``.

A download token is therefore minted at most once per job.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

# Job states.
QUEUED = "queued"
LEASED = "leased"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, TIMEOUT, CANCELLED})

# Primitive return codes.
COMPLETED = "completed"
DUPLICATE = "duplicate"
STALE = "stale"
NOT_FOUND = "not_found"
REJECTED = "rejected"

# Device states.
DEVICE_ACTIVE = "active"
DEVICE_REVOKED = "revoked"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_HELPER_TOKEN_TTL_DAYS = 90
DEFAULT_LEASE_SECONDS = 900

HELPER_DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS helper_devices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '数据库内部编号',
    device_id VARCHAR(64) NOT NULL UNIQUE COMMENT 'helper 设备标识',
    device_name VARCHAR(128) NOT NULL COMMENT '设备显示名',
    platform VARCHAR(64) NULL COMMENT '操作系统描述',
    helper_version VARCHAR(32) NULL COMMENT 'helper 版本号',
    capabilities JSON NULL COMMENT 'helper 声明的能力',
    token_hash CHAR(64) NOT NULL COMMENT 'helper 令牌的 SHA-256，不保存明文',
    status VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active 或 revoked',
    created_at DATETIME(6) NOT NULL COMMENT '注册时间',
    last_seen_at DATETIME(6) NULL COMMENT '最近一次设备心跳',
    token_expires_at DATETIME(6) NULL COMMENT '令牌过期时间',
    revoked_at DATETIME(6) NULL COMMENT '撤销时间',
    INDEX idx_helper_device_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

HELPER_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS helper_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '数据库内部编号',
    job_key VARCHAR(64) NULL UNIQUE COMMENT '幂等键，通常为源文件指纹',
    client_id VARCHAR(128) NOT NULL COMMENT '发起转换的设备标识',
    status VARCHAR(16) NOT NULL COMMENT 'queued、leased、succeeded、failed、timeout 或 cancelled',
    source_filename VARCHAR(255) NOT NULL COMMENT '源 MFLAC 文件名',
    source_path VARCHAR(512) NOT NULL COMMENT '服务端暂存的源文件路径',
    source_size_bytes BIGINT UNSIGNED NULL COMMENT '源文件大小',
    target_format VARCHAR(16) NOT NULL DEFAULT 'flac' COMMENT '目标格式',
    reason VARCHAR(64) NULL COMMENT '进入队列的原因，例如 missing_ekey',
    attempts INT NOT NULL DEFAULT 0 COMMENT '已领取次数',
    max_attempts INT NOT NULL DEFAULT 3 COMMENT '最大领取次数',
    lease_token VARCHAR(64) NULL COMMENT '当前租约令牌，回报时必须匹配',
    lease_owner VARCHAR(64) NULL COMMENT '当前持有租约的 helper 设备',
    lease_expires_at DATETIME(6) NULL COMMENT '租约过期时间',
    result_token VARCHAR(64) NULL UNIQUE COMMENT '结果下载令牌，每个任务最多生成一次',
    result_filename VARCHAR(255) NULL COMMENT '结果文件名',
    result_media_type VARCHAR(128) NULL COMMENT '结果 MIME 类型',
    result_size_bytes BIGINT UNSIGNED NULL COMMENT '结果大小',
    error_message TEXT NULL COMMENT '失败或超时原因',
    created_at DATETIME(6) NOT NULL COMMENT '入队时间',
    started_at DATETIME(6) NULL COMMENT '首次领取时间',
    completed_at DATETIME(6) NULL COMMENT '终态时间',
    updated_at DATETIME(6) NOT NULL COMMENT '最近变更时间',
    INDEX idx_helper_job_status_created (status, created_at),
    INDEX idx_helper_job_client (client_id, created_at),
    INDEX idx_helper_job_lease (status, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def utcnow() -> datetime:
    """Return naive UTC, matching the DATETIME(6) columns used elsewhere."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(token: str) -> str:
    """Hash a high-entropy helper token for storage.

    The token is 32 random bytes, so a single SHA-256 pass is sufficient: there
    is no low-entropy secret to brute force, and the helper must present the
    real token on every call.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def new_lease_token() -> str:
    return secrets.token_urlsafe(24)


def new_device_id() -> str:
    return f"dev_{secrets.token_hex(8)}"


def job_key_for(*parts: object) -> str:
    """Derive a stable idempotency key from source identity."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class HelperStore(Protocol):
    """Atomic primitives shared by the memory and MySQL stores."""

    def ensure_schema(self) -> None: ...

    def register_device(
        self,
        *,
        device_id: str,
        device_name: str,
        token_hash: str,
        platform: str | None = None,
        helper_version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        token_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]: ...

    def get_device(self, device_id: str) -> dict[str, Any] | None: ...

    def list_devices(self, *, limit: int = 200) -> list[dict[str, Any]]: ...

    def touch_device(self, device_id: str, now: datetime | None = None) -> bool: ...

    def revoke_device(self, device_id: str, now: datetime | None = None) -> bool: ...

    def create_job(
        self,
        *,
        client_id: str,
        source_filename: str,
        source_path: str,
        target_format: str = "flac",
        source_size_bytes: int | None = None,
        reason: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        job_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    def get_job(self, job_id: int) -> dict[str, Any] | None: ...

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...

    def queue_stats(self) -> dict[str, int]: ...

    def reconcile_expired(self, now: datetime | None = None) -> tuple[int, int]: ...

    def claim_job(
        self,
        *,
        device_id: str,
        lease_token: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any] | None: ...

    def extend_lease(
        self,
        job_id: int,
        lease_token: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool: ...

    def complete_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        result_token: str,
        result_filename: str,
        result_media_type: str,
        result_size_bytes: int,
        now: datetime | None = None,
    ) -> str: ...

    def fail_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        status: str = FAILED,
        error_message: str = "",
        now: datetime | None = None,
    ) -> str: ...

    def release_job(self, job_id: int, lease_token: str, *, now: datetime | None = None) -> str: ...


class MemoryHelperStore:
    """In-process store used by tests and by a database-less deployment.

    Primitive bodies run under one lock, which is what makes a claim or a
    completion a single atomic step here.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._jobs: dict[int, dict[str, Any]] = {}
        self._job_keys: dict[str, int] = {}
        self._result_tokens: set[str] = set()
        self._next_job_id = 1

    def ensure_schema(self) -> None:
        return None

    # -- devices ---------------------------------------------------------

    def register_device(
        self,
        *,
        device_id: str,
        device_name: str,
        token_hash: str,
        platform: str | None = None,
        helper_version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        token_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        stamp = now or utcnow()
        with self._lock:
            if device_id in self._devices:
                raise ValueError(f"device already registered: {device_id}")
            device = {
                "device_id": device_id,
                "device_name": device_name,
                "platform": platform,
                "helper_version": helper_version,
                "capabilities": dict(capabilities or {}),
                "token_hash": token_hash,
                "status": DEVICE_ACTIVE,
                "created_at": stamp,
                "last_seen_at": None,
                "token_expires_at": token_expires_at,
                "revoked_at": None,
            }
            self._devices[device_id] = device
            return dict(device)

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            device = self._devices.get(device_id)
            return dict(device) if device else None

    def list_devices(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            devices = [dict(device) for device in self._devices.values()]
        devices.sort(key=lambda device: device["created_at"])
        return devices[:limit]

    def touch_device(self, device_id: str, now: datetime | None = None) -> bool:
        stamp = now or utcnow()
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            device["last_seen_at"] = stamp
            return True

    def revoke_device(self, device_id: str, now: datetime | None = None) -> bool:
        stamp = now or utcnow()
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            # Idempotent: revoking twice keeps the original timestamp.
            if device["status"] != DEVICE_REVOKED:
                device["status"] = DEVICE_REVOKED
                device["revoked_at"] = stamp
            return True

    # -- jobs ------------------------------------------------------------

    def create_job(
        self,
        *,
        client_id: str,
        source_filename: str,
        source_path: str,
        target_format: str = "flac",
        source_size_bytes: int | None = None,
        reason: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        job_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        stamp = now or utcnow()
        with self._lock:
            if job_key is not None and job_key in self._job_keys:
                return dict(self._jobs[self._job_keys[job_key]]), False
            job_id = self._next_job_id
            self._next_job_id += 1
            job = {
                "id": job_id,
                "job_key": job_key,
                "client_id": client_id,
                "status": QUEUED,
                "source_filename": source_filename,
                "source_path": source_path,
                "source_size_bytes": source_size_bytes,
                "target_format": target_format,
                "reason": reason,
                "attempts": 0,
                "max_attempts": max_attempts,
                "lease_token": None,
                "lease_owner": None,
                "lease_expires_at": None,
                "result_token": None,
                "result_filename": None,
                "result_media_type": None,
                "result_size_bytes": None,
                "error_message": None,
                "created_at": stamp,
                "started_at": None,
                "completed_at": None,
                "updated_at": stamp,
            }
            self._jobs[job_id] = job
            if job_key is not None:
                self._job_keys[job_key] = job_id
            return dict(job), True

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [dict(job) for job in self._jobs.values() if status is None or job["status"] == status]
        jobs.sort(key=lambda job: (job["created_at"], job["id"]))
        return jobs[:limit]

    def queue_stats(self) -> dict[str, int]:
        with self._lock:
            stats: dict[str, int] = {}
            for job in self._jobs.values():
                stats[job["status"]] = stats.get(job["status"], 0) + 1
        return stats

    def reconcile_expired(self, now: datetime | None = None) -> tuple[int, int]:
        stamp = now or utcnow()
        requeued = timed_out = 0
        with self._lock:
            for job in self._jobs.values():
                if job["status"] != LEASED:
                    continue
                expires_at = job["lease_expires_at"]
                if expires_at is None or expires_at >= stamp:
                    continue
                if job["attempts"] < job["max_attempts"]:
                    job["status"] = QUEUED
                    requeued += 1
                else:
                    job["status"] = TIMEOUT
                    job["completed_at"] = stamp
                    job["error_message"] = job["error_message"] or (
                        "helper lease expired before completion"
                    )
                    timed_out += 1
                job["lease_token"] = None
                job["lease_owner"] = None
                job["lease_expires_at"] = None
                job["updated_at"] = stamp
        return requeued, timed_out

    def claim_job(
        self,
        *,
        device_id: str,
        lease_token: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        stamp = now or utcnow()
        with self._lock:
            candidates = [
                job for job in self._jobs.values() if job["status"] == QUEUED
            ]
            if not candidates:
                return None
            job = min(candidates, key=lambda item: (item["created_at"], item["id"]))
            job["status"] = LEASED
            job["lease_token"] = lease_token
            job["lease_owner"] = device_id
            job["lease_expires_at"] = stamp + timedelta(seconds=lease_seconds)
            job["attempts"] += 1
            job["started_at"] = job["started_at"] or stamp
            job["updated_at"] = stamp
            return dict(job)

    def extend_lease(
        self,
        job_id: int,
        lease_token: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        stamp = now or utcnow()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != LEASED or job["lease_token"] != lease_token:
                return False
            job["lease_expires_at"] = stamp + timedelta(seconds=lease_seconds)
            job["updated_at"] = stamp
            return True

    def complete_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        result_token: str,
        result_filename: str,
        result_media_type: str,
        result_size_bytes: int,
        now: datetime | None = None,
    ) -> str:
        stamp = now or utcnow()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return NOT_FOUND
            if job["status"] == SUCCEEDED:
                # A repeat report must not mint a second result token.
                return DUPLICATE
            if job["status"] != LEASED:
                return REJECTED
            if job["lease_token"] != lease_token:
                # The lease expired and was reassigned to another attempt.
                return STALE
            if result_token in self._result_tokens:
                raise ValueError("result token collision")
            self._result_tokens.add(result_token)
            job["status"] = SUCCEEDED
            job["result_token"] = result_token
            job["result_filename"] = result_filename
            job["result_media_type"] = result_media_type
            job["result_size_bytes"] = result_size_bytes
            job["completed_at"] = stamp
            job["updated_at"] = stamp
            job["error_message"] = None
            job["lease_token"] = None
            job["lease_owner"] = None
            job["lease_expires_at"] = None
            return COMPLETED

    def fail_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        status: str = FAILED,
        error_message: str = "",
        now: datetime | None = None,
    ) -> str:
        stamp = now or utcnow()
        if status not in {FAILED, TIMEOUT}:
            raise ValueError(f"fail_job cannot move a job to {status!r}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return NOT_FOUND
            if job["status"] in TERMINAL_STATES:
                return DUPLICATE if job["status"] == status else REJECTED
            if job["status"] != LEASED:
                return REJECTED
            if job["lease_token"] != lease_token:
                return STALE
            job["status"] = status
            job["error_message"] = error_message[:2000]
            job["completed_at"] = stamp
            job["updated_at"] = stamp
            job["lease_token"] = None
            job["lease_owner"] = None
            job["lease_expires_at"] = None
            return COMPLETED

    def release_job(self, job_id: int, lease_token: str, *, now: datetime | None = None) -> str:
        stamp = now or utcnow()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return NOT_FOUND
            if job["status"] in TERMINAL_STATES:
                return REJECTED
            if job["status"] != LEASED:
                return REJECTED
            if job["lease_token"] != lease_token:
                return STALE
            # The attempt was already counted at claim time; releasing only
            # returns the job to the queue while attempts remain.
            job["status"] = QUEUED if job["attempts"] < job["max_attempts"] else TIMEOUT
            if job["status"] == TIMEOUT:
                job["completed_at"] = stamp
                job["error_message"] = job["error_message"] or "helper released the job after the last attempt"
            job["lease_token"] = None
            job["lease_owner"] = None
            job["lease_expires_at"] = None
            job["updated_at"] = stamp
            return COMPLETED


class MySqlHelperStore:
    """MySQL-backed store using conditional updates for atomicity."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def ensure_schema(self) -> None:
        connection = self._connect()
        if connection is None:
            raise RuntimeError("MySQL is not configured")
        try:
            with connection.cursor() as cursor:
                cursor.execute(HELPER_DEVICE_SCHEMA)
                cursor.execute(HELPER_JOB_SCHEMA)
        finally:
            connection.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        connection = self._connect()
        if connection is None:
            raise RuntimeError("MySQL is not configured")
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return int(cursor.rowcount)
        finally:
            connection.close()

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        connection = self._connect()
        if connection is None:
            raise RuntimeError("MySQL is not configured")
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()
        finally:
            connection.close()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        connection = self._connect()
        if connection is None:
            raise RuntimeError("MySQL is not configured")
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        finally:
            connection.close()

    _DEVICE_COLUMNS = (
        "device_id, device_name, platform, helper_version, capabilities, token_hash, "
        "status, created_at, last_seen_at, token_expires_at, revoked_at"
    )
    _JOB_COLUMNS = (
        "id, job_key, client_id, status, source_filename, source_path, source_size_bytes, "
        "target_format, reason, attempts, max_attempts, lease_token, lease_owner, "
        "lease_expires_at, result_token, result_filename, result_media_type, "
        "result_size_bytes, error_message, created_at, started_at, completed_at, updated_at"
    )

    # -- devices ---------------------------------------------------------

    def register_device(
        self,
        *,
        device_id: str,
        device_name: str,
        token_hash: str,
        platform: str | None = None,
        helper_version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        token_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        stamp = now or utcnow()
        self._execute(
            "INSERT INTO helper_devices "
            "(device_id, device_name, platform, helper_version, capabilities, token_hash, status, created_at, token_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                device_id,
                device_name,
                platform,
                helper_version,
                _json_or_none(capabilities),
                token_hash,
                DEVICE_ACTIVE,
                stamp,
                token_expires_at,
            ),
        )
        device = self.get_device(device_id)
        if device is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError("device registration could not be read back")
        return device

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            f"SELECT {self._DEVICE_COLUMNS} FROM helper_devices WHERE device_id=%s LIMIT 1",
            (device_id,),
        )

    def list_devices(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetchall(
            f"SELECT {self._DEVICE_COLUMNS} FROM helper_devices ORDER BY created_at ASC LIMIT %s",
            (limit,),
        )

    def touch_device(self, device_id: str, now: datetime | None = None) -> bool:
        stamp = now or utcnow()
        return self._execute(
            "UPDATE helper_devices SET last_seen_at=%s WHERE device_id=%s",
            (stamp, device_id),
        ) == 1

    def revoke_device(self, device_id: str, now: datetime | None = None) -> bool:
        stamp = now or utcnow()
        # Keep the first revocation timestamp when called again.
        self._execute(
            "UPDATE helper_devices SET status=%s, revoked_at=COALESCE(revoked_at, %s) WHERE device_id=%s",
            (DEVICE_REVOKED, stamp, device_id),
        )
        return self.get_device(device_id) is not None

    # -- jobs ------------------------------------------------------------

    def create_job(
        self,
        *,
        client_id: str,
        source_filename: str,
        source_path: str,
        target_format: str = "flac",
        source_size_bytes: int | None = None,
        reason: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        job_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        stamp = now or utcnow()
        if job_key is not None:
            existing = self._fetchone(
                f"SELECT {self._JOB_COLUMNS} FROM helper_jobs WHERE job_key=%s LIMIT 1",
                (job_key,),
            )
            if existing is not None:
                return existing, False
        self._execute(
            "INSERT INTO helper_jobs "
            "(job_key, client_id, status, source_filename, source_path, source_size_bytes, target_format, "
            "reason, attempts, max_attempts, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)",
            (
                job_key,
                client_id,
                QUEUED,
                source_filename,
                source_path,
                source_size_bytes,
                target_format,
                reason,
                max_attempts,
                stamp,
                stamp,
            ),
        )
        if job_key is not None:
            job = self._fetchone(
                f"SELECT {self._JOB_COLUMNS} FROM helper_jobs WHERE job_key=%s LIMIT 1",
                (job_key,),
            )
        else:
            job = self._fetchone(
                f"SELECT {self._JOB_COLUMNS} FROM helper_jobs WHERE client_id=%s ORDER BY id DESC LIMIT 1",
                (client_id,),
            )
        if job is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError("job insert could not be read back")
        return job, True

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self._fetchone(
            f"SELECT {self._JOB_COLUMNS} FROM helper_jobs WHERE id=%s LIMIT 1",
            (job_id,),
        )

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status is None:
            return self._fetchall(
                f"SELECT {self._JOB_COLUMNS} FROM helper_jobs ORDER BY created_at ASC, id ASC LIMIT %s",
                (limit,),
            )
        return self._fetchall(
            f"SELECT {self._JOB_COLUMNS} FROM helper_jobs WHERE status=%s ORDER BY created_at ASC, id ASC LIMIT %s",
            (status, limit),
        )

    def queue_stats(self) -> dict[str, int]:
        rows = self._fetchall("SELECT status, COUNT(*) AS total FROM helper_jobs GROUP BY status")
        return {str(row["status"]): int(row["total"]) for row in rows}

    def reconcile_expired(self, now: datetime | None = None) -> tuple[int, int]:
        stamp = now or utcnow()
        requeued = self._execute(
            "UPDATE helper_jobs SET status=%s, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=%s "
            "WHERE status=%s AND lease_expires_at IS NOT NULL AND lease_expires_at < %s AND attempts < max_attempts",
            (QUEUED, stamp, LEASED, stamp),
        )
        timed_out = self._execute(
            "UPDATE helper_jobs SET status=%s, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL, "
            "completed_at=%s, updated_at=%s, "
            "error_message=COALESCE(error_message, 'helper lease expired before completion') "
            "WHERE status=%s AND lease_expires_at IS NOT NULL AND lease_expires_at < %s AND attempts >= max_attempts",
            (TIMEOUT, stamp, stamp, LEASED, stamp),
        )
        return requeued, timed_out

    def claim_job(
        self,
        *,
        device_id: str,
        lease_token: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        stamp = now or utcnow()
        # One conditional UPDATE picks and leases the oldest queued job; the row
        # lock taken here is what makes concurrent claims mutually exclusive.
        updated = self._execute(
            "UPDATE helper_jobs SET status=%s, lease_token=%s, lease_owner=%s, lease_expires_at=%s, "
            "attempts=attempts+1, started_at=COALESCE(started_at, %s), updated_at=%s "
            "WHERE status=%s ORDER BY created_at ASC, id ASC LIMIT 1",
            (
                LEASED,
                lease_token,
                device_id,
                stamp + timedelta(seconds=lease_seconds),
                stamp,
                stamp,
                QUEUED,
            ),
        )
        if updated != 1:
            return None
        return self._fetchone(
            f"SELECT {self._JOB_COLUMNS} FROM helper_jobs WHERE lease_token=%s LIMIT 1",
            (lease_token,),
        )

    def extend_lease(
        self,
        job_id: int,
        lease_token: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        stamp = now or utcnow()
        return self._execute(
            "UPDATE helper_jobs SET lease_expires_at=%s, updated_at=%s "
            "WHERE id=%s AND status=%s AND lease_token=%s",
            (stamp + timedelta(seconds=lease_seconds), stamp, job_id, LEASED, lease_token),
        ) == 1

    def complete_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        result_token: str,
        result_filename: str,
        result_media_type: str,
        result_size_bytes: int,
        now: datetime | None = None,
    ) -> str:
        stamp = now or utcnow()
        updated = self._execute(
            "UPDATE helper_jobs SET status=%s, result_token=%s, result_filename=%s, result_media_type=%s, "
            "result_size_bytes=%s, completed_at=%s, updated_at=%s, error_message=NULL, "
            "lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=%s AND status=%s AND lease_token=%s",
            (
                SUCCEEDED,
                result_token,
                result_filename,
                result_media_type,
                result_size_bytes,
                stamp,
                stamp,
                job_id,
                LEASED,
                lease_token,
            ),
        )
        if updated == 1:
            return COMPLETED
        return self._classify_lost_lease(job_id, SUCCEEDED)

    def fail_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        status: str = FAILED,
        error_message: str = "",
        now: datetime | None = None,
    ) -> str:
        stamp = now or utcnow()
        if status not in {FAILED, TIMEOUT}:
            raise ValueError(f"fail_job cannot move a job to {status!r}")
        updated = self._execute(
            "UPDATE helper_jobs SET status=%s, error_message=%s, completed_at=%s, updated_at=%s, "
            "lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=%s AND status=%s AND lease_token=%s",
            (status, error_message[:2000], stamp, stamp, job_id, LEASED, lease_token),
        )
        if updated == 1:
            return COMPLETED
        return self._classify_lost_lease(job_id, status)

    def release_job(self, job_id: int, lease_token: str, *, now: datetime | None = None) -> str:
        stamp = now or utcnow()
        # Requeue while attempts remain, otherwise the job is a timeout.
        requeued = self._execute(
            "UPDATE helper_jobs SET status=%s, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=%s "
            "WHERE id=%s AND status=%s AND lease_token=%s AND attempts < max_attempts",
            (QUEUED, stamp, job_id, LEASED, lease_token),
        )
        if requeued == 1:
            return COMPLETED
        exhausted = self._execute(
            "UPDATE helper_jobs SET status=%s, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL, "
            "completed_at=%s, updated_at=%s, "
            "error_message=COALESCE(error_message, 'helper released the job after the last attempt') "
            "WHERE id=%s AND status=%s AND lease_token=%s AND attempts >= max_attempts",
            (TIMEOUT, stamp, stamp, job_id, LEASED, lease_token),
        )
        if exhausted == 1:
            return COMPLETED
        return self._classify_lost_lease(job_id, QUEUED)

    def _classify_lost_lease(self, job_id: int, intended_status: str) -> str:
        """Explain why a conditional report matched no row."""
        job = self.get_job(job_id)
        if job is None:
            return NOT_FOUND
        status = str(job["status"])
        if status == intended_status:
            return DUPLICATE
        if status == LEASED:
            return STALE
        if status in TERMINAL_STATES:
            return REJECTED
        return REJECTED


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)
