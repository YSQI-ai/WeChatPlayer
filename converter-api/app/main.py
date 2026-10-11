from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

try:
    from .mflac_fallback import MflacDecodeError, decode_mflac
except ImportError:  # pragma: no cover - supports running main.py directly
    from app.mflac_fallback import MflacDecodeError, decode_mflac

try:
    from . import helper_api, helper_store
except ImportError:  # pragma: no cover - supports running main.py directly
    import helper_api  # type: ignore[no-redef]
    import helper_store  # type: ignore[no-redef]

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - installed in the production virtualenv
    pymysql = None
    DictCursor = None


SERVICE_NAME = "wechat-converter-api"
SERVICE_VERSION = "1.0.0"
CHUNK_SIZE = 1024 * 1024
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
CONVERSION_TIMEOUT_SECONDS = int(os.getenv("CONVERSION_TIMEOUT_SECONDS", "600"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
DOWNLOAD_LINK_TTL_SECONDS = int(os.getenv("DOWNLOAD_LINK_TTL_SECONDS", "2592000"))
TMP_ROOT = Path(os.getenv("CONVERTER_TMP_ROOT", "/var/lib/wechat-converter/tmp"))
LIBREOFFICE_BIN = os.getenv("LIBREOFFICE_BIN", "libreoffice")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
QMC2_BIN = os.getenv("QMC2_BIN", "/opt/wechat-converter-api/bin/QMC2-decoder")
NCDUMP_BIN = os.getenv("NCDUMP_BIN", "/opt/wechat-converter-api/bin/ncmdump")
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "wechat_converter")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "wechat_converter")
MYSQL_CONNECT_TIMEOUT = int(os.getenv("MYSQL_CONNECT_TIMEOUT", "5"))

logger = logging.getLogger(SERVICE_NAME)
conversion_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
database_ready = False

# Private helper queue. The protocol stays disabled unless
# HELPER_ENROLLMENT_SECRET is configured, so a default deployment exposes no
# unauthenticated work queue. Job state prefers MySQL; the in-memory store is a
# single-process fallback for local runs only.
helper_settings = helper_api.HelperSettings.from_env(TMP_ROOT)
helper_queue_ready = False

CONVERSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversion_records (
    output_filename VARCHAR(255) NULL COMMENT '最终输出文件名',
    source_filename VARCHAR(255) NOT NULL COMMENT '原始上传文件名',
    status VARCHAR(32) NOT NULL COMMENT 'processing、success 或 failed',
    cloud_saved TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已保存到云盘',
    size_bytes BIGINT UNSIGNED NULL COMMENT '输出文件大小（字节）',
    media_type VARCHAR(128) NULL COMMENT '文件 MIME 类型',
    target_format VARCHAR(32) NULL COMMENT '目标格式，例如 pdf、mp3',
    client_id VARCHAR(128) NOT NULL COMMENT '设备标识',
    created_at DATETIME(6) NOT NULL COMMENT '开始转换时间',
    completed_at DATETIME(6) NULL COMMENT '转换完成时间',
    expires_at DATETIME(6) NULL COMMENT '临时结果过期时间',
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '数据库内部编号',
    token VARCHAR(64) NULL UNIQUE COMMENT '下载令牌',
    error_message TEXT NULL COMMENT '失败原因',
    INDEX idx_conversion_client_created (client_id, created_at),
    INDEX idx_conversion_cloud (client_id, cloud_saved)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CLOUD_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_file_events (
    action VARCHAR(16) NOT NULL COMMENT 'CREATE、READ、UPDATE 或 DELETE',
    filename VARCHAR(255) NULL COMMENT '操作时的文件名',
    client_id VARCHAR(128) NOT NULL COMMENT '设备标识',
    created_at DATETIME(6) NOT NULL COMMENT '操作时间',
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '数据库内部编号',
    token VARCHAR(64) NULL COMMENT '下载令牌',
    details JSON NULL COMMENT '操作补充信息',
    INDEX idx_cloud_event_client_created (client_id, created_at),
    INDEX idx_cloud_event_token_created (token, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DOCUMENT_EXTENSIONS = {
    "doc",
    "docx",
    "odt",
    "ott",
    "rtf",
    "txt",
}

MEDIA_PROFILES: dict[str, dict[str, object]] = {
    "mp3": {
        "args": ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"],
        "media_type": "audio/mpeg",
    },
    "wav": {
        "args": ["-vn", "-c:a", "pcm_s16le"],
        "media_type": "audio/wav",
    },
    "flac": {
        "args": ["-vn", "-c:a", "flac"],
        "media_type": "audio/flac",
    },
    "ogg": {
        "args": ["-vn", "-c:a", "libvorbis", "-q:a", "5"],
        "media_type": "audio/ogg",
    },
    "opus": {
        "args": ["-vn", "-c:a", "libopus", "-b:a", "128k"],
        "media_type": "audio/ogg",
    },
    "aac": {
        "args": ["-vn", "-c:a", "aac", "-b:a", "192k"],
        "media_type": "audio/aac",
    },
    "m4a": {
        "args": ["-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
        "media_type": "audio/mp4",
    },
    "mp4": {
        "args": [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
        ],
        "media_type": "video/mp4",
    },
    "webm": {
        "args": [
            "-c:v",
            "libvpx-vp9",
            "-crf",
            "32",
            "-b:v",
            "0",
            "-c:a",
            "libopus",
            "-b:a",
            "128k",
        ],
        "media_type": "video/webm",
    },
    "mkv": {
        "args": [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ],
        "media_type": "video/x-matroska",
    },
    "mov": {
        "args": [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
        ],
        "media_type": "video/quicktime",
    },
    "avi": {
        "args": ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "libmp3lame", "-b:a", "192k"],
        "media_type": "video/x-msvideo",
    },
}

ENCRYPTED_MEDIA_EXTENSIONS = {"mflac", "ncm"}
MEDIA_INPUT_EXTENSIONS = set(MEDIA_PROFILES) | ENCRYPTED_MEDIA_EXTENSIONS


class UploadTooLarge(Exception):
    pass


class ConversionFailed(Exception):
    pass


def _db_connect():
    if pymysql is None or not MYSQL_PASSWORD:
        return None
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        connect_timeout=MYSQL_CONNECT_TIMEOUT,
        autocommit=True,
        cursorclass=DictCursor,
    )


def _db_initialize() -> bool:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            logger.warning("MySQL is not configured; database history is disabled")
            return False
        with connection.cursor() as cursor:
            cursor.execute(CONVERSION_SCHEMA)
            cursor.execute(CLOUD_EVENT_SCHEMA)
            if TMP_ROOT.exists():
                for download_dir in TMP_ROOT.glob("download-*"):
                    metadata_path = download_dir / "metadata.json"
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        output = next(path for path in download_dir.iterdir() if path.is_file() and path.name != "metadata.json")
                        created_at = datetime.fromisoformat(str(metadata["created_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
                    except (OSError, ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError):
                        continue
                    token = download_dir.name.removeprefix("download-")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                        continue
                    filename = str(metadata.get("filename") or output.name)
                    cloud_saved = bool(metadata.get("cloud_saved", False))
                    expires_at = None if cloud_saved else created_at + timedelta(seconds=DOWNLOAD_LINK_TTL_SECONDS)
                    cursor.execute(
                        "INSERT IGNORE INTO conversion_records "
                        "(token, client_id, source_filename, output_filename, media_type, status, cloud_saved, size_bytes, created_at, completed_at, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, 'success', %s, %s, %s, %s, %s)",
                        (
                            token,
                            str(metadata.get("client_id") or "anonymous"),
                            filename,
                            filename,
                            str(metadata.get("media_type") or "application/octet-stream"),
                            int(cloud_saved),
                            int(metadata.get("size_bytes") or output.stat().st_size),
                            created_at,
                            created_at,
                            expires_at,
                        ),
                    )
        return True
    except Exception:
        logger.exception("Could not initialize MySQL schema")
        return False
    finally:
        if connection is not None:
            connection.close()


def _db_create_conversion(client_id: str, source_filename: str, target_format: str | None) -> int | None:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return None
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO conversion_records "
                "(client_id, source_filename, target_format, status, created_at) "
                "VALUES (%s, %s, %s, 'processing', %s)",
                (client_id or "anonymous", source_filename or "upload", target_format, datetime.now(timezone.utc).replace(tzinfo=None)),
            )
            return int(cursor.lastrowid)
    except Exception:
        logger.exception("Could not create conversion database record")
        return None
    finally:
        if connection is not None:
            connection.close()


def _db_complete_conversion(
    record_id: int | None,
    token: str | None,
    output_filename: str,
    media_type: str,
    size_bytes: int | None,
    cloud_saved: bool = False,
) -> None:
    if record_id is None:
        return
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_at = None if cloud_saved else now + timedelta(seconds=DOWNLOAD_LINK_TTL_SECONDS)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conversion_records SET token=%s, output_filename=%s, media_type=%s, "
                "status='success', cloud_saved=%s, size_bytes=%s, completed_at=%s, expires_at=%s WHERE id=%s",
                (token, output_filename, media_type, int(cloud_saved), size_bytes, now, expires_at, record_id),
            )
    except Exception:
        logger.exception("Could not complete conversion database record")
    finally:
        if connection is not None:
            connection.close()


def _db_fail_conversion(record_id: int | None, error_message: str) -> None:
    if record_id is None:
        return
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conversion_records SET status='failed', error_message=%s, completed_at=%s WHERE id=%s",
                (error_message[:2000], datetime.now(timezone.utc).replace(tzinfo=None), record_id),
            )
    except Exception:
        logger.exception("Could not mark conversion database record as failed")
    finally:
        if connection is not None:
            connection.close()


def _db_record_event(client_id: str, token: str | None, action: str, filename: str | None, details: dict[str, object] | None = None) -> None:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO cloud_file_events (token, client_id, action, filename, details, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (token, client_id, action, filename, json.dumps(details or {}, ensure_ascii=False), datetime.now(timezone.utc).replace(tzinfo=None)),
            )
    except Exception:
        logger.exception("Could not record cloud file event")
    finally:
        if connection is not None:
            connection.close()


def _db_history(client_id: str, cloud_only: bool = False) -> list[dict[str, object]]:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return []
        clause = "AND cloud_saved=1" if cloud_only else ""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, token, client_id, source_filename, output_filename, media_type, status, cloud_saved, size_bytes, "
                "created_at, completed_at, expires_at, error_message FROM conversion_records "
                f"WHERE client_id=%s {clause} ORDER BY created_at DESC LIMIT 100",
                (client_id,),
            )
            rows = cursor.fetchall()
        records: list[dict[str, object]] = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in rows:
            created_at = row["created_at"]
            created_text = created_at.strftime("%Y-%m-%d %H:%M") if created_at else ""
            expires_at = row["expires_at"]
            expires_in = None if row["cloud_saved"] else max(0, int((expires_at - now).total_seconds())) if expires_at else 0
            token = row["token"]
            records.append({
                "id": token or str(row["id"]),
                "record_id": row["id"],
                "filename": row["output_filename"] or row["source_filename"] or "转换失败",
                "source_filename": row["source_filename"] or "",
                "download_url": f"/api/v1/download/{token}" if token else "",
                "media_type": row["media_type"] or "application/octet-stream",
                "status": row["status"],
                "cloud_saved": bool(row["cloud_saved"]),
                "created_at": created_at.isoformat() if created_at else "",
                "created_text": created_text,
                "size_bytes": int(row["size_bytes"] or 0),
                "size_text": _size_text(int(row["size_bytes"] or 0)),
                "expires_in": expires_in,
                "error_message": row["error_message"] or "",
            })
        if cloud_only:
            for record in records:
                _db_record_event(client_id, str(record["id"]), "READ", str(record["filename"]))
        return records
    except Exception:
        logger.exception("Could not read conversion database records")
        return []
    finally:
        if connection is not None:
            connection.close()


def _db_find_record(client_id: str, token: str, cloud_only: bool = False) -> dict[str, object] | None:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return None
        clause = "AND cloud_saved=1" if cloud_only else ""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, token, output_filename, media_type, cloud_saved, size_bytes FROM conversion_records "
                f"WHERE client_id=%s AND token=%s {clause} LIMIT 1",
                (client_id, token),
            )
            return cursor.fetchone()
    except Exception:
        logger.exception("Could not find conversion database record")
        return None
    finally:
        if connection is not None:
            connection.close()


def _db_mark_cloud_saved(client_id: str, token: str, filename: str) -> bool:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return False
        with connection.cursor() as cursor:
              cursor.execute(
                  "UPDATE conversion_records SET cloud_saved=1, output_filename=%s, expires_at=NULL "
                  "WHERE client_id=%s AND token=%s AND status='success'",
                  (filename, client_id, token),
              )
              if cursor.rowcount == 1:
                  return True
              cursor.execute(
                  "SELECT 1 FROM conversion_records WHERE client_id=%s AND token=%s AND cloud_saved=1 LIMIT 1",
                  (client_id, token),
              )
              return cursor.fetchone() is not None
    except Exception:
        logger.exception("Could not save cloud file database record")
        return False
    finally:
        if connection is not None:
            connection.close()


def _db_rename_cloud(client_id: str, token: str, filename: str) -> bool:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return False
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conversion_records SET output_filename=%s WHERE client_id=%s AND token=%s AND cloud_saved=1",
                (filename, client_id, token),
            )
            return cursor.rowcount == 1
    except Exception:
        logger.exception("Could not rename cloud file database record")
        return False
    finally:
        if connection is not None:
            connection.close()


def _db_delete_cloud(client_id: str, token: str) -> bool:
    connection = None
    try:
        connection = _db_connect()
        if connection is None:
            return False
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conversion_records SET cloud_saved=0 WHERE client_id=%s AND token=%s AND cloud_saved=1",
                (client_id, token),
            )
            return cursor.rowcount == 1
    except Exception:
        logger.exception("Could not delete cloud file database record")
        return False
    finally:
        if connection is not None:
            connection.close()


def _size_text(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _safe_stem(filename: str | None) -> str:
    raw_stem = Path(filename or "upload").stem
    safe_stem = re.sub(r"[^\w.-]+", "_", raw_stem, flags=re.UNICODE).strip("._")
    return safe_stem[:120] or "converted"


def _output_name(output_name: str | None, fallback: str | None, extension: str) -> str:
    raw_name = (output_name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    raw_stem = Path(raw_name).stem if raw_name else Path(fallback or "upload").stem
    safe_stem = re.sub(r"[^\w.-]+", "_", raw_stem, flags=re.UNICODE).strip("._")
    return f"{(safe_stem[:120] or 'converted')}.{extension}"


def _extension(filename: str | None) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def _new_job_dir() -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_expired_downloads()
    return Path(tempfile.mkdtemp(prefix="job-", dir=TMP_ROOT))


def _remove_job_dir(job_dir: str | Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)


def _cleanup_expired_downloads() -> None:
    if not TMP_ROOT.exists():
        return
    expiration_time = time.time() - DOWNLOAD_LINK_TTL_SECONDS
    for download_dir in TMP_ROOT.glob("download-*"):
        try:
            if not download_dir.is_dir():
                continue
            metadata_path = download_dir / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("cloud_saved"):
                    continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            created_at = _read_created_at(metadata_path)
            last_activity = created_at if created_at is not None else download_dir.stat().st_mtime
            if last_activity < expiration_time:
                _remove_job_dir(download_dir)
        except OSError:
            logger.warning("Could not inspect temporary download %s", download_dir)


def _read_created_at(metadata_path: Path) -> float | None:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        value = metadata.get("created_at")
        if not isinstance(value, str):
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _store_download(
    output_path: Path,
    job_dir: Path,
    download_name: str,
    client_id: str,
    media_type: str,
    token: str | None = None,
    extra_metadata: dict[str, object] | None = None,
) -> str:
    # A caller may supply the token so the download directory can be created
    # before the database row that references it.
    selected_token = token or secrets.token_urlsafe(32)
    download_dir = TMP_ROOT / f"download-{selected_token}"
    # 0o700 is POSIX hardening. On Windows the mode is not a permission model,
    # and a restrictive mode can leave the directory unwritable for the very
    # process that created it, which would break the result upload.
    download_dir.mkdir(**({} if os.name == "nt" else {"mode": 0o700}))
    try:
        output_path.replace(download_dir / download_name)
        stored_output = download_dir / download_name
        metadata: dict[str, object] = {
            "client_id": client_id or "anonymous",
            "filename": download_name,
            "media_type": media_type,
            "cloud_saved": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": stored_output.stat().st_size,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        (download_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        _remove_job_dir(download_dir)
        raise
    _remove_job_dir(job_dir)
    return selected_token


def _publish_helper_result(
    *,
    job_dir: Path,
    output_path: Path,
    filename: str,
    source_filename: str,
    client_id: str,
    media_type: str,
    token: str,
    extra_metadata: dict[str, object] | None = None,
) -> int:
    """Publish a helper-produced audio file as a normal conversion result.

    Reusing the standard download directory means Mini Program history, expiry
    and cloud-save all work for helper results without a second code path.
    """
    size_bytes = output_path.stat().st_size
    record_id = _db_create_conversion(client_id, source_filename, "flac")
    _store_download(
        output_path,
        job_dir,
        filename,
        client_id,
        media_type,
        token=token,
        extra_metadata=extra_metadata,
    )
    _db_complete_conversion(record_id, token, filename, media_type, size_bytes)
    return size_bytes


def _resolve_download(token: str) -> tuple[Path, str, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(status_code=404, detail="Download link not found or expired")

    download_dir = TMP_ROOT / f"download-{token}"
    try:
        metadata_path = download_dir / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            metadata = {}
        if not metadata.get("cloud_saved") and time.time() - download_dir.stat().st_mtime > DOWNLOAD_LINK_TTL_SECONDS:
            _remove_job_dir(download_dir)
            raise HTTPException(status_code=404, detail="Download link not found or expired")
        output_files = [path for path in download_dir.iterdir() if path.is_file() and path.name != "metadata.json"]
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Download link not found or expired") from exc

    if len(output_files) != 1:
        _remove_job_dir(download_dir)
        raise HTTPException(status_code=404, detail="Download link not found or expired")

    output_path = output_files[0]
    extension = output_path.suffix.lower().lstrip(".")
    media_type = "application/pdf" if extension == "pdf" else mimetypes.guess_type(output_path.name)[0]
    if extension in MEDIA_PROFILES:
        media_type = str(MEDIA_PROFILES[extension]["media_type"])
    return output_path, media_type or "application/octet-stream", download_dir


def _save_upload(upload: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("wb") as output:
        while True:
            chunk = upload.file.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise UploadTooLarge
            output.write(chunk)
    return total


def _run_command(command: list[str], cwd: Path) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConversionFailed(f"Required converter is not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionFailed("Conversion timed out") from exc

    if result.returncode != 0:
        error_tail = (result.stderr or result.stdout or "converter failed").strip()[-2000:]
        logger.warning("Converter failed with exit code %s: %s", result.returncode, error_tail)
        raise ConversionFailed(
            f"Converter exited with code {result.returncode}: {error_tail}"
        )


def _convert_document(job_dir: Path, input_path: Path) -> tuple[Path, str]:
    output_dir = job_dir / "output"
    output_dir.mkdir()
    profile_dir = job_dir / "libreoffice-profile"
    profile_uri = profile_dir.as_uri()
    command = [
        LIBREOFFICE_BIN,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        f"-env:UserInstallation={profile_uri}",
        str(input_path),
    ]
    _run_command(command, job_dir)
    expected_output = output_dir / f"{input_path.stem}.pdf"
    if not expected_output.exists():
        candidates = list(output_dir.glob("*.pdf"))
        if len(candidates) != 1:
            raise ConversionFailed("The document converter did not produce a PDF")
        expected_output = candidates[0]
    return expected_output, "application/pdf"


def _convert_media(job_dir: Path, input_path: Path, target_format: str) -> tuple[Path, str]:
    target = target_format.lower().strip().lstrip(".")
    profile = MEDIA_PROFILES.get(target)
    if profile is None:
        raise ValueError(f"Unsupported target format: {target}")
    source_path = input_path
    input_extension = _extension(input_path.name)
    if input_extension in ENCRYPTED_MEDIA_EXTENSIONS:
        source_path = _decode_encrypted_media(job_dir, input_path, input_extension)
    output_path = job_dir / f"{_safe_stem(input_path.name)}.{target}"
    if target == "flac" and _extension(source_path.name) == "flac":
        shutil.copyfile(source_path, output_path)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ConversionFailed("The encrypted media converter did not produce an output file")
        return output_path, str(profile["media_type"])
    command = [
        FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-map_metadata",
        "0",
        *profile["args"],  # type: ignore[index]
        str(output_path),
    ]
    _run_command(command, job_dir)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionFailed("The media converter did not produce an output file")
    return output_path, str(profile["media_type"])


def _decode_encrypted_media(job_dir: Path, input_path: Path, input_extension: str) -> Path:
    decoded_dir = job_dir / "decoded"
    decoded_dir.mkdir()
    if input_extension == "mflac":
        output_path = decoded_dir / f"{_safe_stem(input_path.name)}.flac"
        try:
            _run_command([QMC2_BIN, str(input_path), str(output_path)], job_dir)
        except ConversionFailed as exc:
            if "unknown encryption method" not in str(exc).lower():
                raise
            logger.info("QMC2 does not support this MFLAC variant; trying the Python fallback")
            try:
                decode_mflac(input_path, output_path)
            except (MflacDecodeError, OSError) as fallback_exc:
                raise ConversionFailed(f"MFLAC fallback decoder failed: {fallback_exc}") from fallback_exc
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ConversionFailed("The MFLAC decoder did not produce an output file")
        return output_path

    _run_command([NCDUMP_BIN, str(input_path), "-o", str(decoded_dir)], job_dir)
    candidates = [
        path for path in decoded_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".flac", ".mp3"}
    ]
    if len(candidates) != 1 or candidates[0].stat().st_size == 0:
        raise ConversionFailed("The NCM decoder did not produce a single audio file")
    return candidates[0]


def _file_response(output_path: Path, media_type: str, download_name: str, job_dir: Path) -> FileResponse:
    return FileResponse(
        output_path,
        media_type=media_type,
        filename=download_name,
        background=BackgroundTask(_remove_job_dir, job_dir),
    )


def _handle_failure(job_dir: Path | None, exc: Exception) -> None:
    if job_dir is not None:
        _remove_job_dir(job_dir)
    if isinstance(exc, UploadTooLarge):
        raise HTTPException(status_code=413, detail="Uploaded file is too large") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ConversionFailed):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.exception("Unexpected conversion failure")
    raise HTTPException(status_code=500, detail="Unexpected conversion failure") from exc


def _cors_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOW_ORIGINS", "*")
    if configured.strip() == "*":
        return ["*"]
    return [item.strip() for item in configured.split(",") if item.strip()]


async def _cleanup_downloads_periodically() -> None:
    interval = min(300, max(30, DOWNLOAD_LINK_TTL_SECONDS // 2))
    while True:
        await asyncio.sleep(interval)
        _cleanup_expired_downloads()


async def _maintain_helper_queue() -> None:
    """Expire helper leases and drop staged sources of finished jobs.

    Reclaiming here as well as on claim means a job whose helper died becomes
    claimable even while no helper is polling for work.
    """
    interval = max(30, helper_settings.lease_seconds // 3)
    while True:
        await asyncio.sleep(interval)
        try:
            requeued, timed_out = helper_store_impl.reconcile_expired()
            if requeued or timed_out:
                logger.info("Helper lease reconciliation: requeued=%s timed_out=%s", requeued, timed_out)
            removed = helper_api.cleanup_expired_sources(helper_store_impl, helper_settings)
            if removed:
                logger.info("Removed %s finished helper source directories", removed)
        except Exception:
            logger.exception("Helper queue maintenance failed")


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    global database_ready, helper_queue_ready
    database_ready = _db_initialize()
    if helper_settings.enabled:
        try:
            helper_store_impl.ensure_schema()
            helper_queue_ready = True
            if isinstance(helper_store_impl, helper_store.MemoryHelperStore):
                logger.warning(
                    "Helper queue is using in-process state; jobs will not survive a restart. "
                    "Configure MySQL for durable helper jobs."
                )
        except Exception:
            logger.exception("Could not initialize the helper queue schema")
    else:
        logger.info("Helper protocol is disabled (HELPER_ENROLLMENT_SECRET is not set)")

    cleanup_task = asyncio.create_task(_cleanup_downloads_periodically())
    helper_task = asyncio.create_task(_maintain_helper_queue()) if helper_queue_ready else None
    try:
        yield
    finally:
        for task in (cleanup_task, helper_task):
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


# Job state prefers MySQL so an interrupted helper job survives a restart; the
# in-memory store is only a single-process fallback for local runs.
helper_store_impl: helper_store.HelperStore = (
    helper_store.MySqlHelperStore(_db_connect)
    if (pymysql is not None and MYSQL_PASSWORD)
    else helper_store.MemoryHelperStore()
)

app = FastAPI(
    title="WeChat Converter API",
    version=SERVICE_VERSION,
    description="Document and media conversion service for a WeChat mini program.",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# The private helper protocol and the Mini Program facing job status endpoint.
app.include_router(
    helper_api.build_router(
        store=helper_store_impl,
        settings=helper_settings,
        publish_result=_publish_helper_result,
    )
)
app.include_router(helper_api.build_client_router(store=helper_store_impl))


def _helper_state() -> str:
    if not helper_settings.enabled:
        return "disabled"
    return "ready" if helper_queue_ready else "unavailable"


@app.get("/api/v1/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "database": "mysql" if database_ready else "unavailable",
        "helper_queue": _helper_state(),
    }


@app.get("/api/v1/test")
def test_endpoint() -> dict[str, object]:
    """Standalone capability endpoint for a quick deployment smoke test."""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "message": "converter API is ready",
        "document_endpoint": "POST /api/v1/convert/document-to-pdf",
        "media_endpoint": "POST /api/v1/convert/media",
        "document_extensions": sorted(DOCUMENT_EXTENSIONS),
        "media_formats": sorted(MEDIA_PROFILES),
        "media_input_extensions": sorted(MEDIA_INPUT_EXTENSIONS),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "helper_queue": _helper_state(),
        "helper_endpoints": {
            "enqueue": "POST /api/v1/helper/jobs",
            "claim": "POST /api/v1/helper/jobs/claim",
            "job_status": "GET /api/v1/jobs/{job_id}?client_id=...",
        },
    }


@app.post("/api/v1/convert/document-to-pdf", response_model=None)
def document_to_pdf(
    file: UploadFile = File(...),
    response_mode: str = Form("file", description="Use 'link' for mini-program uploads"),
    client_id: str = Form("anonymous", description="Stable client identifier for history"),
    output_name: str = Form("", description="Desired output filename without or with extension"),
) -> FileResponse | JSONResponse:
    extension = _extension(file.filename)
    if extension not in DOCUMENT_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported document type. Allowed: {', '.join(sorted(DOCUMENT_EXTENSIONS))}",
        )
    if response_mode not in {"file", "link"}:
        raise HTTPException(status_code=400, detail="response_mode must be 'file' or 'link'")
    if not conversion_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Too many conversions in progress")

    record_id = _db_create_conversion(client_id, file.filename or "upload", "pdf")
    job_dir: Path | None = None
    try:
        job_dir = _new_job_dir()
        input_path = job_dir / f"input.{extension}"
        _save_upload(file, input_path)
        output_path, media_type = _convert_document(job_dir, input_path)
        download_name = _output_name(output_name, file.filename, "pdf")
        size_bytes = output_path.stat().st_size
        if response_mode == "link":
            token = _store_download(output_path, job_dir, download_name, client_id, media_type)
            _db_complete_conversion(record_id, token, download_name, media_type, size_bytes)
            return JSONResponse(
                {
                    "download_url": f"/api/v1/download/{token}",
                    "filename": download_name,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "size_text": _size_text(size_bytes),
                    "expires_in": DOWNLOAD_LINK_TTL_SECONDS,
                    "cloud_saved": False,
                }
            )
        _db_complete_conversion(record_id, None, download_name, media_type, size_bytes)
        return _file_response(output_path, media_type, download_name, job_dir)
    except (UploadTooLarge, ConversionFailed, ValueError) as exc:
        _db_fail_conversion(record_id, str(exc))
        _handle_failure(job_dir, exc)
        raise AssertionError("unreachable")
    except Exception as exc:
        _db_fail_conversion(record_id, str(exc))
        _handle_failure(job_dir, exc)
        raise AssertionError("unreachable")
    finally:
        file.file.close()
        conversion_slots.release()


@app.post("/api/v1/convert/media", response_model=None)
def media_convert(
    file: UploadFile = File(...),
    target_format: str = Form(..., description="Target format, for example mp3 or mp4"),
    response_mode: str = Form("file", description="Use 'link' for mini-program uploads"),
    client_id: str = Form("anonymous", description="Stable client identifier for history"),
    output_name: str = Form("", description="Desired output filename without or with extension"),
) -> FileResponse | JSONResponse:
    target = target_format.lower().strip().lstrip(".")
    if target not in MEDIA_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported target format. Allowed: {', '.join(sorted(MEDIA_PROFILES))}",
        )
    if response_mode not in {"file", "link"}:
        raise HTTPException(status_code=400, detail="response_mode must be 'file' or 'link'")
    input_extension = _extension(file.filename)
    if input_extension not in MEDIA_INPUT_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type. Allowed: {', '.join(sorted(MEDIA_INPUT_EXTENSIONS))}",
        )
    if not conversion_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Too many conversions in progress")

    record_id = _db_create_conversion(client_id, file.filename or "upload", target)
    job_dir: Path | None = None
    try:
        job_dir = _new_job_dir()
        input_extension = input_extension or "input"
        input_path = job_dir / f"input.{input_extension}"
        _save_upload(file, input_path)
        output_path, media_type = _convert_media(job_dir, input_path, target)
        download_name = _output_name(output_name, file.filename, target)
        size_bytes = output_path.stat().st_size
        if response_mode == "link":
            token = _store_download(output_path, job_dir, download_name, client_id, media_type)
            _db_complete_conversion(record_id, token, download_name, media_type, size_bytes)
            return JSONResponse(
                {
                    "download_url": f"/api/v1/download/{token}",
                    "filename": download_name,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "size_text": _size_text(size_bytes),
                    "expires_in": DOWNLOAD_LINK_TTL_SECONDS,
                    "cloud_saved": False,
                }
            )
        _db_complete_conversion(record_id, None, download_name, media_type, size_bytes)
        return _file_response(output_path, media_type, download_name, job_dir)
    except (UploadTooLarge, ConversionFailed, ValueError) as exc:
        _db_fail_conversion(record_id, str(exc))
        _handle_failure(job_dir, exc)
        raise AssertionError("unreachable")
    except Exception as exc:
        _db_fail_conversion(record_id, str(exc))
        _handle_failure(job_dir, exc)
        raise AssertionError("unreachable")
    finally:
        file.file.close()
        conversion_slots.release()


@app.get("/api/v1/download/{token}")
def download_converted_file(token: str) -> FileResponse:
    output_path, media_type, download_dir = _resolve_download(token)
    try:
        metadata = json.loads((download_dir / "metadata.json").read_text(encoding="utf-8"))
        if metadata.get("cloud_saved"):
            _db_record_event(str(metadata.get("client_id") or "anonymous"), token, "READ", str(metadata.get("filename") or output_path.name))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return FileResponse(output_path, media_type=media_type, filename=output_path.name)


@app.get("/api/v1/history/{client_id}")
def conversion_history(client_id: str) -> dict[str, object]:
    if database_ready:
        history_items = _db_history(client_id)
        return {"client_id": client_id, "files": history_items, "items": history_items}
    _cleanup_expired_downloads()
    now = time.time()
    records: list[dict[str, object]] = []
    if not TMP_ROOT.exists():
        return {"client_id": client_id, "files": [], "items": []}
    for download_dir in TMP_ROOT.glob("download-*"):
        if not download_dir.is_dir():
            continue
        token = download_dir.name.removeprefix("download-")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            continue
        metadata_path = download_dir / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if metadata.get("client_id", "anonymous") != client_id:
            continue
        created_at_value = metadata.get("created_at")
        created_at_timestamp = _read_created_at(metadata_path)
        if not isinstance(created_at_value, str) or created_at_timestamp is None:
            continue
        output_path = next((path for path in download_dir.iterdir() if path.is_file() and path.name != "metadata.json"), None)
        if output_path is None:
            continue
        size_bytes = int(metadata.get("size_bytes") or output_path.stat().st_size)
        try:
            created_text = datetime.fromisoformat(created_at_value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            created_text = created_at_value
        if size_bytes >= 1024 * 1024:
            size_text = f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:
            size_text = f"{size_bytes / 1024:.1f} KB"
        else:
            size_text = f"{size_bytes} B"
        records.append({
            "id": token,
            "filename": str(metadata.get("filename") or output_path.name),
            "download_url": f"/api/v1/download/{token}",
            "media_type": str(metadata.get("media_type") or "application/octet-stream"),
            "created_at": created_at_value,
            "created_text": created_text,
            "size_bytes": size_bytes,
            "size_text": size_text,
            "expires_in": max(0, int(DOWNLOAD_LINK_TTL_SECONDS - (now - created_at_timestamp))),
        })
    records.sort(key=lambda item: str(item["created_at"]), reverse=True)
    history_items = records[:100]
    return {"client_id": client_id, "files": history_items, "items": history_items}


@app.delete("/api/v1/history/{client_id}/{token}")
def delete_conversion(client_id: str, token: str) -> dict[str, str]:
    return delete_cloud_file(client_id, token)


@app.get("/api/v1/cloud-files/{client_id}")
def cloud_files(client_id: str) -> dict[str, object]:
    if not database_ready:
        return {"client_id": client_id, "files": [], "items": []}
    files = _db_history(client_id, cloud_only=True)
    return {"client_id": client_id, "files": files, "items": files}


@app.post("/api/v1/cloud-files/{client_id}/{token}")
def save_cloud_file(client_id: str, token: str) -> dict[str, object]:
    if not database_ready:
        raise HTTPException(status_code=503, detail="MySQL database is unavailable")
    record = _db_find_record(client_id, token)
    if record is None:
        raise HTTPException(status_code=404, detail="Conversion not found")
    output_path, _, download_dir = _resolve_download(token)
    filename = str(record.get("output_filename") or output_path.name)
    metadata_path = download_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["cloud_saved"] = True
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Could not update cloud file metadata") from exc
    if not _db_mark_cloud_saved(client_id, token, filename):
        raise HTTPException(status_code=500, detail="Could not save cloud file record")
    _db_record_event(client_id, token, "CREATE", filename)
    return {"status": "saved", "filename": filename, "cloud_saved": True}


@app.patch("/api/v1/cloud-files/{client_id}/{token}")
def rename_cloud_file(client_id: str, token: str, filename: str = Query(..., min_length=1, max_length=120)) -> dict[str, object]:
    if not database_ready:
        raise HTTPException(status_code=503, detail="MySQL database is unavailable")
    record = _db_find_record(client_id, token, cloud_only=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Cloud file not found")
    old_path, _, download_dir = _resolve_download(token)
    new_name = _output_name(filename, old_path.name, old_path.suffix.lstrip("."))
    new_path = download_dir / new_name
    try:
        if new_path != old_path:
            old_path.rename(new_path)
        metadata_path = download_dir / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["filename"] = new_name
        metadata["cloud_saved"] = True
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Could not rename cloud file") from exc
    if not _db_rename_cloud(client_id, token, new_name):
        raise HTTPException(status_code=500, detail="Could not update cloud file record")
    _db_record_event(client_id, token, "UPDATE", new_name, {"previous_name": record.get("output_filename")})
    return {"status": "updated", "filename": new_name, "cloud_saved": True}


@app.delete("/api/v1/cloud-files/{client_id}/{token}")
def delete_cloud_file(client_id: str, token: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(status_code=404, detail="Cloud file not found")
    if database_ready:
        record = _db_find_record(client_id, token, cloud_only=True)
        if record is None:
            raise HTTPException(status_code=404, detail="Cloud file not found")
        if not _db_delete_cloud(client_id, token):
            raise HTTPException(status_code=500, detail="Could not delete cloud file record")
        _db_record_event(client_id, token, "DELETE", str(record.get("output_filename") or ""))
    else:
        raise HTTPException(status_code=503, detail="MySQL database is unavailable")
    _remove_job_dir(TMP_ROOT / f"download-{token}")
    return {"status": "deleted", "id": token}


def _legacy_delete_conversion(client_id: str, token: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(status_code=404, detail="Conversion not found")
    download_dir = TMP_ROOT / f"download-{token}"
    metadata_path = download_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Conversion not found") from exc
    if metadata.get("client_id", "anonymous") != client_id:
        raise HTTPException(status_code=404, detail="Conversion not found")
    _remove_job_dir(download_dir)
    return {"status": "deleted", "id": token}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE_NAME, "docs": "/docs", "test": "/api/v1/test"}
