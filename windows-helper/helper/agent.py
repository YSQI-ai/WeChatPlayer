"""The helper agent: register, poll, decode, report.

Loop shape::

    ensure registered -> reconcile any interrupted job -> repeat:
        device heartbeat (on interval)
        local preconditions (disk space, QQ Music session)
        claim one job
        download -> decode -> upload
        report success / failure / timeout
        always clean the work directory

Guarantees this module is responsible for:

* **One job at a time.** The agent never claims while it owns a job.
* **No duplicate completion.** Completion is retried because the server
  de-duplicates it, and a journal entry lets a restarted helper discover that its
  job already finished instead of decoding it again.
* **No secret on disk.** The EKey lives only in memory for one decode.
* **No leftover media.** The work directory is removed on every path out.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import client as client_module
from .client import AuthenticationError, HelperApiError, HelperClient, Job, TransientApiError
from .config import HelperConfig
from .keyprovider import DecodeFailure, DecodePlan, Decoder, KeyProvider, KeyUnavailable
from .redact import Redactor
from .state import CredentialStore, Credentials, JournalEntry, JobJournal

logger = logging.getLogger(__name__)

# Reasons reported to the server; kept stable so operators can grep them.
REASON_NO_KEY = "no_authorized_key_source"
REASON_DECODE_FAILED = "decode_failed"
REASON_TIMEOUT = "timeout"
REASON_NETWORK = "network_error"
REASON_PRECONDITION = "precondition_failed"


@dataclass
class Precondition:
    """Whether the host is currently allowed to take work."""

    ok: bool
    reason: str = ""


class LeaseKeeper:
    """Extends the server lease while a job is being decoded.

    Runs in a daemon thread so a slow decode does not lose its lease.  If the
    server reports the lease is gone, ``lost`` is set and the worker must not
    report a result for that attempt.
    """

    def __init__(self, client: HelperClient, credentials: Credentials, job: Job, interval: float) -> None:
        self._client = client
        self._credentials = credentials
        self._job = job
        # The caller owns the production floor; a small value here keeps the
        # keeper directly testable.
        self._interval = max(0.05, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False
        self.last_error: str = ""

    def __enter__(self) -> "LeaseKeeper":
        self._thread = threading.Thread(target=self._run, name="lease-keeper", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._client.job_heartbeat(
                    self._credentials.device_id,
                    self._credentials.device_token,
                    self._job.job_id,
                    self._job.lease_token,
                )
            except AuthenticationError as exc:
                self.lost = True
                self.last_error = str(exc)
                logger.warning("Lease lost: device is no longer authorized")
                return
            except HelperApiError as exc:
                # 409 means another attempt owns the job now.
                self.lost = True
                self.last_error = exc.detail
                logger.warning("Lease was lost for job %s: %s", self._job.job_id, exc.detail)
                return
            except TransientApiError as exc:
                # Keep trying: a blip should not abandon a running decode.
                logger.debug("Lease heartbeat failed transiently: %s", exc)


class HelperAgent:
    """Owns the helper's lifecycle."""

    def __init__(
        self,
        config: HelperConfig,
        *,
        client: HelperClient | None = None,
        credential_store: CredentialStore | None = None,
        journal: JobJournal | None = None,
        key_provider: KeyProvider | None = None,
        decoder: Decoder | None = None,
        redactor: Redactor | None = None,
        sleep=time.sleep,
    ) -> None:
        self.config = config
        self.redactor = redactor or Redactor()
        self.client = client or HelperClient(
            config.api_base_url,
            request_timeout=config.request_timeout_seconds,
            download_timeout=config.download_timeout_seconds,
            upload_timeout=config.upload_timeout_seconds,
            retries=config.request_retries,
            backoff_seconds=config.retry_backoff_seconds,
            verify_tls=config.verify_tls,
        )
        self.credential_store = credential_store or CredentialStore(
            config.credentials_path, config.api_base_url
        )
        self.journal = journal or JobJournal(config.journal_path)
        self.key_provider = key_provider
        self.decoder = decoder
        self._sleep = sleep
        self._running = True
        self._current_job: Job | None = None
        self._last_heartbeat = 0.0
        self._credentials: Credentials | None = None

    # -- setup -----------------------------------------------------------

    def prepare(self) -> None:
        """Validate the local environment before any work is claimed."""
        self.config.ensure_directories()
        self.redactor.add_path(self.config.state_dir)
        self.redactor.add_path(self.config.work_dir)
        if self._credentials is not None:
            self.redactor.add_secret(self._credentials.device_token)

        if self.decoder is None:
            from .keyprovider import default_decoder_path

            decoder_path = self.config.decoder_path or default_decoder_path()
            if decoder_path is None:
                raise KeyUnavailable(
                    "the offline decoder (qmc_mflac.py) was not found; set decoder_path"
                )
            self.decoder = Decoder(decoder_path, self.redactor)
        if self.key_provider is None:
            from .keyprovider import build_key_provider

            self.key_provider = build_key_provider(self.config.key_source, self.config.key_source_path)

        usable, reason = self.key_provider.check()
        if not usable:
            # Not fatal: files with an embedded EKey still decode.
            logger.warning("Key source %s is not ready: %s", self.config.key_source, reason)

    def ensure_registered(self) -> Credentials:
        """Return usable credentials, registering the device if needed."""
        if self._credentials is not None:
            return self._credentials
        self.config.ensure_directories()
        existing = self.credential_store.load()
        if existing is not None:
            self.redactor.add_secret(existing.device_token)
            self._credentials = existing
            logger.info("Loaded registered device %s", existing.device_id)
            return existing

        if not self.config.enrollment_secret:
            raise AuthenticationError(
                0, "no stored credentials and HELPER_ENROLLMENT_SECRET is not set"
            )
        import platform as platform_module

        registration = self.client.register(
            enrollment_secret=self.config.enrollment_secret,
            device_name=self.config.device_name,
            platform=f"{platform_module.system()} {platform_module.release()}",
            helper_version=client_module.helper_version(),
            capabilities={
                "key_source": self.config.key_source,
                "targets": ["flac"],
                "max_result_bytes": self.config.min_free_disk_bytes,
            },
        )
        credentials = Credentials(
            device_id=str(registration["device_id"]),
            device_token=str(registration["device_token"]),
            api_base_url=self.config.api_base_url,
            token_expires_at=str(registration.get("token_expires_at") or ""),
            lease_seconds=int(registration.get("lease_seconds") or self.config.lease_seconds),
            heartbeat_seconds=int(registration.get("heartbeat_seconds") or self.config.heartbeat_seconds),
        )
        self.redactor.add_secret(credentials.device_token)
        self.credential_store.save(credentials)
        self._credentials = credentials
        logger.info("Registered helper device %s", credentials.device_id)
        return credentials

    def forget_credentials(self) -> None:
        """Drop stored credentials so the next start registers again."""
        self.credential_store.clear()
        self._credentials = None

    # -- preconditions ---------------------------------------------------

    def check_preconditions(self) -> Precondition:
        """Refuse work when the host cannot finish it."""
        work_dir = self.config.work_dir or self.config.state_dir
        try:
            usage = shutil.disk_usage(work_dir)
            if usage.free < self.config.min_free_disk_bytes:
                return Precondition(False, f"low disk: {usage.free} bytes free")
        except OSError as exc:
            return Precondition(False, f"cannot inspect the work directory: {exc}")

        if self.config.required_process:
            if not _process_running(self.config.required_process):
                return Precondition(False, f"{self.config.required_process} is not running")
        return Precondition(True)

    # -- crash recovery --------------------------------------------------

    def reconcile_journal(self) -> None:
        """Resolve a job left in flight by a previous run.

        The important case is a job that actually succeeded before the helper
        died: the server is asked, so the file is not decoded a second time.
        """
        entry = self.journal.load()
        if entry is None:
            return
        credentials = self.ensure_registered()
        logger.info("Reconciling interrupted job %s (phase=%s)", entry.job_id, entry.phase)
        try:
            status = self.client.job_status(
                credentials.device_id, credentials.device_token, entry.job_id
            )
        except AuthenticationError:
            raise
        except (HelperApiError, TransientApiError) as exc:
            logger.warning("Could not reconcile job %s yet: %s", entry.job_id, exc)
            return

        state = str(status.get("status") or "")
        if state == "succeeded":
            logger.info("Job %s already completed before the restart; nothing to redo", entry.job_id)
            self._discard_work_dir(entry.job_id)
            self.journal.clear()
            return
        if state in {"failed", "timeout", "cancelled"}:
            logger.info("Job %s already finished as %s", entry.job_id, state)
            self._discard_work_dir(entry.job_id)
            self.journal.clear()
            return
        if state == "leased" and str(status.get("lease_owner") or "") == credentials.device_id:
            # We still hold the lease but cannot trust the local result.
            try:
                self.client.release(
                    credentials.device_id, credentials.device_token, entry.job_id, entry.lease_token
                )
                logger.info("Released interrupted job %s back to the queue", entry.job_id)
            except (HelperApiError, TransientApiError) as exc:
                logger.warning("Could not release job %s: %s", entry.job_id, exc)
        self._discard_work_dir(entry.job_id)
        self.journal.clear()

    # -- main loop -------------------------------------------------------

    def stop(self) -> None:
        self._running = False

    def run(self, *, once: bool = False, max_jobs: int | None = None) -> int:
        """Run the agent.  Returns the number of jobs processed."""
        self.prepare()
        credentials = self.ensure_registered()
        self.reconcile_journal()
        logger.info("Helper ready: %s", self.config.describe())

        processed = 0
        try:
            while self._running:
                self._maybe_device_heartbeat(credentials)

                precondition = self.check_preconditions()
                if not precondition.ok:
                    logger.warning("Not claiming work: %s", precondition.reason)
                    if once:
                        return processed
                    self._sleep(self.config.poll_interval_seconds)
                    continue

                job = self._claim(credentials)
                if job is None:
                    if once or (max_jobs is not None and processed >= max_jobs):
                        return processed
                    self._sleep(self.config.poll_interval_seconds)
                    continue

                self._work_job(credentials, job)
                processed += 1
                if once or (max_jobs is not None and processed >= max_jobs):
                    return processed
        except KeyboardInterrupt:
            logger.info("Interrupted; shutting down")
            self._release_current(credentials)
        return processed

    def _maybe_device_heartbeat(self, credentials: Credentials) -> None:
        now = time.monotonic()
        interval = max(15, credentials.heartbeat_seconds or self.config.heartbeat_seconds)
        if now - self._last_heartbeat < interval:
            return
        try:
            self.client.device_heartbeat(credentials.device_id, credentials.device_token)
            self._last_heartbeat = now
        except AuthenticationError:
            raise
        except (HelperApiError, TransientApiError) as exc:
            logger.warning("Device heartbeat failed: %s", exc)

    def _claim(self, credentials: Credentials) -> Job | None:
        try:
            return self.client.claim(credentials.device_id, credentials.device_token)
        except AuthenticationError:
            raise
        except TransientApiError as exc:
            logger.warning("Claim failed transiently: %s", exc)
            return None
        except HelperApiError as exc:
            logger.warning("Claim refused: %s", exc)
            return None

    def _release_current(self, credentials: Credentials) -> None:
        """Return an in-flight job to the queue during a clean shutdown."""
        job = self._current_job
        if job is None:
            return
        self._safe_release(credentials, job)
        self._discard_work_dir(job.job_id)
        self._current_job = None

    def _safe_release(self, credentials: Credentials, job: Job) -> None:
        """Best-effort release so another helper can retry immediately."""
        try:
            self.client.release(
                credentials.device_id, credentials.device_token, job.job_id, job.lease_token
            )
            logger.info("Released job %s back to the queue", job.job_id)
        except (HelperApiError, TransientApiError, AuthenticationError) as exc:
            logger.warning("Could not release job %s: %s", job.job_id, exc)
        finally:
            self.journal.clear()

    # -- one job ---------------------------------------------------------

    def _work_dir(self, job_id: int) -> Path:
        base = self.config.work_dir or (self.config.state_dir / "work")
        return base / f"job-{job_id}"

    def _discard_work_dir(self, job_id: int) -> None:
        shutil.rmtree(self._work_dir(job_id), ignore_errors=True)

    def _work_job(self, credentials: Credentials, job: Job) -> None:
        """Download, decode, and report exactly one job."""
        self._current_job = job
        work_dir = self._work_dir(job.job_id)
        self._discard_work_dir(job.job_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        if self.config.lease_heartbeat_seconds:
            lease_interval = float(self.config.lease_heartbeat_seconds)
        else:
            lease_interval = max(10.0, (credentials.lease_seconds or self.config.lease_seconds) / 3)
        self.journal.record(
            JournalEntry(job_id=job.job_id, lease_token=job.lease_token, phase="claimed", attempt=job.attempt)
        )
        logger.info(
            "Working job %s (attempt %s/%s, %s)",
            job.job_id,
            job.attempt,
            job.max_attempts,
            job.source_filename,
        )
        timeout_seconds = min(self.config.job_timeout_seconds, max(30, credentials.lease_seconds * 3))

        try:
            with LeaseKeeper(self.client, credentials, job, lease_interval) as keeper:
                source_path = work_dir / "source.mflac"
                self.client.download_source(
                    credentials.device_id,
                    credentials.device_token,
                    job.job_id,
                    job.lease_token,
                    source_path,
                    max_bytes=self.config.min_free_disk_bytes * 4,
                )
                self.journal.record(
                    JournalEntry(
                        job_id=job.job_id, lease_token=job.lease_token, phase="downloaded", attempt=job.attempt
                    )
                )

                started = time.monotonic()
                try:
                    result_path = self._decode(job, source_path, work_dir)
                except KeyUnavailable as exc:
                    logger.warning("Job %s has no authorized key source: %s", job.job_id, exc)
                    self._report_failure(credentials, job, REASON_NO_KEY, str(exc))
                    return
                except DecodeFailure as exc:
                    logger.warning("Job %s failed to decode: %s", job.job_id, exc)
                    self._report_failure(credentials, job, REASON_DECODE_FAILED, str(exc))
                    return

                elapsed = time.monotonic() - started
                if elapsed > timeout_seconds:
                    logger.warning("Job %s exceeded the local deadline", job.job_id)
                    self._report_failure(credentials, job, REASON_TIMEOUT, "local job deadline exceeded")
                    return

                self.journal.record(
                    JournalEntry(
                        job_id=job.job_id,
                        lease_token=job.lease_token,
                        phase="decoded",
                        attempt=job.attempt,
                        result_size_bytes=result_path.stat().st_size,
                    )
                )

                if keeper.lost:
                    # Another attempt owns the job now; reporting would be stale.
                    logger.warning("Abandoning job %s: %s", job.job_id, keeper.last_error)
                    return

                response = self.client.complete(
                    credentials.device_id,
                    credentials.device_token,
                    job.job_id,
                    job.lease_token,
                    result_path,
                    output_name=Path(job.source_filename).with_suffix(f".{job.target_format}").name,
                )
                outcome = str(response.get("status") or "unknown")
                if outcome == "duplicate":
                    # A retry of an already-recorded result: exactly what we want.
                    logger.info("Job %s was already recorded by this attempt", job.job_id)
                else:
                    logger.info("Job %s completed", job.job_id)
                self.journal.clear()
        except AuthenticationError as exc:
            logger.error("Device is no longer authorized: %s", exc)
            self.journal.clear()
            self.stop()
        except KeyboardInterrupt:
            # Release before the finally clears _current_job, so a Ctrl-C during
            # a decode hands the job straight back instead of leaving it leased.
            logger.info("Interrupted while working job %s", job.job_id)
            self._safe_release(credentials, job)
            raise
        except TransientApiError as exc:
            # Keep the journal: the lease will expire and the server requeues.
            logger.warning("Job %s hit a network error; leaving it to expire: %s", job.job_id, exc)
        except HelperApiError as exc:
            logger.warning("Job %s was rejected by the server: %s", job.job_id, exc)
            self.journal.clear()
        finally:
            # Never leave decrypted audio or the source on disk.
            self._discard_work_dir(job.job_id)
            self._current_job = None

    def _decode(self, job: Job, source_path: Path, work_dir: Path) -> Path:
        """Produce the decoded audio for one job."""
        assert self.decoder is not None and self.key_provider is not None
        plan: DecodePlan = self.key_provider.plan(source_path)
        if plan.ekey is None and plan.key_db is None and self.config.key_source == "none":
            # Only files with an embedded EKey can succeed; say so clearly when
            # the container needs an external key.
            kind = self.decoder.inspect(source_path)
            if kind == "PcV2MusicEx":
                raise KeyUnavailable(
                    "this PC MusicEx file has no embedded EKey and no authorized key source is "
                    "configured; set key_source to ekey_file or player_process_db"
                )
        result_path = work_dir / f"result.{job.target_format}"
        kind = self.decoder.decode(source_path, result_path, plan)
        if not result_path.is_file() or result_path.stat().st_size == 0:
            raise DecodeFailure(f"the decoder produced no output for a {kind} container")
        with result_path.open("rb") as handle:
            header = handle.read(4)
        if job.target_format == "flac" and header != b"fLaC":
            raise DecodeFailure("the decoded output is not a valid FLAC file")
        return result_path

    def _report_failure(self, credentials: Credentials, job: Job, reason: str, message: str) -> None:
        try:
            self.client.fail(
                credentials.device_id, credentials.device_token, job.job_id, job.lease_token, reason, message
            )
            self.journal.clear()
        except AuthenticationError:
            raise
        except (HelperApiError, TransientApiError) as exc:
            logger.warning("Could not report failure for job %s: %s", job.job_id, exc)


def _process_running(name: str) -> bool:
    """Best-effort check for a running process by image name."""
    try:
        import subprocess

        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # If the check cannot run, do not block work on it.
        return True
    return name.lower() in (result.stdout or "").lower()
