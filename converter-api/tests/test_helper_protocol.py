"""Acceptance tests for the private Windows helper protocol.

Run with the standard library only::

    python -m unittest discover -s converter-api/tests -v

Covers the P0 acceptance criteria: device registration, single-job claim,
heartbeat, success/failure/timeout reporting, server-side retry, and the
guarantee that a job cannot be completed twice.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

CONVERTER_API_DIR = Path(__file__).resolve().parent.parent
if str(CONVERTER_API_DIR) not in sys.path:
    sys.path.insert(0, str(CONVERTER_API_DIR))

# ``tempfile.mkdtemp`` creates a 0o700 directory.  In a confined Windows
# environment that mode can make the directory unwritable for the creating
# token, and ``tempfile`` retries on the denial instead of raising -- so the
# test temp root is created explicitly with the default mode.
_TEST_TMP = Path(tempfile.gettempdir()) / f"wechat-helper-tests-{os.getpid()}"
_TEST_TMP.mkdir(parents=True, exist_ok=True)
os.environ["CONVERTER_TMP_ROOT"] = str(_TEST_TMP)
os.environ["HELPER_ENROLLMENT_SECRET"] = "test-enrollment-secret"
os.environ["HELPER_LEASE_SECONDS"] = "900"
os.environ["HELPER_MAX_ATTEMPTS"] = "3"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import helper_api, helper_store, mflac_fallback  # noqa: E402
from app import main as app_main  # noqa: E402
from app.main import TMP_ROOT, app  # noqa: E402

ENROLL_SECRET = "test-enrollment-secret"
FLAC_PAYLOAD = b"fLaC" + b"\x00" * 256


def qmc_source() -> bytes:
    """A minimal file that satisfies the enqueue-time QQ Music container check."""
    return b"\x11" * 512 + b"musicex\x00"


def enroll_headers() -> dict[str, str]:
    return {"X-Helper-Enroll-Secret": ENROLL_SECRET}


def register(client: TestClient, name: str = "test-helper") -> tuple[str, str]:
    response = client.post(
        "/api/v1/helper/devices/register",
        json={"device_name": name, "platform": "windows", "helper_version": "0.1.0"},
        headers=enroll_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["device_id"], body["device_token"]


def device_headers(device_id: str, token: str, lease: str | None = None) -> dict[str, str]:
    headers = {"X-Helper-Device": device_id, "X-Helper-Token": token}
    if lease is not None:
        headers["X-Helper-Lease"] = lease
    return headers


def enqueue(client: TestClient, client_id: str = "client-1", payload: bytes | None = None) -> dict:
    response = client.post(
        "/api/v1/helper/jobs",
        files={"file": ("song.mflac", payload or qmc_source(), "application/octet-stream")},
        data={"client_id": client_id, "target_format": "flac", "reason": "missing_ekey"},
        headers=enroll_headers(),
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def claim(client: TestClient, device_id: str, token: str) -> dict:
    response = client.post("/api/v1/helper/jobs/claim", headers=device_headers(device_id, token))
    assert response.status_code == 200, response.text
    return response.json()


def download_dirs() -> list[Path]:
    return sorted(TMP_ROOT.glob("download-*"))


class StoreStateMachineTests(unittest.TestCase):
    """Pure state-machine semantics, independent of HTTP."""

    def setUp(self) -> None:
        self.store = helper_store.MemoryHelperStore()
        self.now = helper_store.utcnow()

    def _job(self, **overrides) -> dict:
        params = {
            "client_id": "client-1",
            "source_filename": "song.mflac",
            "source_path": str(Path(_TEST_TMP) / "source.mflac"),
            "max_attempts": overrides.pop("max_attempts", 3),
        }
        params.update(overrides)
        job, _ = self.store.create_job(**params, now=self.now)
        return job

    def test_claim_is_exclusive(self) -> None:
        job = self._job()
        first = self.store.claim_job(device_id="dev_a", lease_token="a" * 32, now=self.now)
        second = self.store.claim_job(device_id="dev_b", lease_token="b" * 32, now=self.now)
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], job["id"])
        self.assertEqual(first["attempts"], 1)
        self.assertIsNone(second, "a second claim must not receive the same or a new job")

    def test_only_one_job_is_claimed_at_a_time(self) -> None:
        for _ in range(3):
            self._job()
        claimed = [
            self.store.claim_job(device_id="dev_a", lease_token=helper_store.new_lease_token(), now=self.now)
            for _ in range(3)
        ]
        ids = [job["id"] for job in claimed if job]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3, "each claim must lease a distinct job")

    def test_claim_returns_none_when_queue_is_empty(self) -> None:
        self.assertIsNone(
            self.store.claim_job(device_id="dev_a", lease_token="a" * 32, now=self.now)
        )

    def test_completion_is_idempotent_and_mints_one_token(self) -> None:
        job = self._job()
        lease = "a" * 32
        self.store.claim_job(device_id="dev_a", lease_token=lease, now=self.now)

        first = self.store.complete_job(
            job["id"], lease,
            result_token="result-token-one",
            result_filename="song.flac",
            result_media_type="audio/flac",
            result_size_bytes=10,
            now=self.now,
        )
        second = self.store.complete_job(
            job["id"], lease,
            result_token="result-token-two",
            result_filename="song.flac",
            result_media_type="audio/flac",
            result_size_bytes=10,
            now=self.now,
        )
        self.assertEqual(first, helper_store.COMPLETED)
        self.assertEqual(second, helper_store.DUPLICATE)

        stored = self.store.get_job(job["id"])
        self.assertEqual(stored["status"], helper_store.SUCCEEDED)
        self.assertEqual(stored["result_token"], "result-token-one")

    def test_stale_lease_cannot_overwrite_a_reassigned_job(self) -> None:
        job = self._job(max_attempts=3)
        stale_lease = "a" * 32
        self.store.claim_job(device_id="dev_a", lease_token=stale_lease, now=self.now)

        # The first helper goes silent and its lease expires.
        later = self.now + timedelta(seconds=901)
        requeued, timed_out = self.store.reconcile_expired(now=later)
        self.assertEqual((requeued, timed_out), (1, 0))

        fresh_lease = "b" * 32
        reclaimed = self.store.claim_job(device_id="dev_b", lease_token=fresh_lease, now=later)
        self.assertEqual(reclaimed["attempts"], 2)

        # The first helper wakes up and reports success with its dead lease.
        outcome = self.store.complete_job(
            job["id"], stale_lease,
            result_token="stale-token",
            result_filename="song.flac",
            result_media_type="audio/flac",
            result_size_bytes=1,
            now=later,
        )
        self.assertEqual(outcome, helper_store.STALE)
        self.assertEqual(self.store.get_job(job["id"])["status"], helper_store.LEASED)

        # The current owner still wins.
        outcome = self.store.complete_job(
            job["id"], fresh_lease,
            result_token="fresh-token",
            result_filename="song.flac",
            result_media_type="audio/flac",
            result_size_bytes=1,
            now=later,
        )
        self.assertEqual(outcome, helper_store.COMPLETED)
        self.assertEqual(self.store.get_job(job["id"])["result_token"], "fresh-token")

    def test_retry_until_attempts_are_exhausted_then_timeout(self) -> None:
        job = self._job(max_attempts=2)
        for attempt in range(2):
            lease = chr(ord("a") + attempt) * 32
            claimed = self.store.claim_job(device_id="dev_a", lease_token=lease, now=self.now)
            self.assertEqual(claimed["attempts"], attempt + 1)
            requeued, timed_out = self.store.reconcile_expired(now=self.now + timedelta(seconds=901))
            if attempt == 0:
                self.assertEqual((requeued, timed_out), (1, 0), "first expiry requeues")
            else:
                self.assertEqual((requeued, timed_out), (0, 1), "last expiry times out")

        stored = self.store.get_job(job["id"])
        self.assertEqual(stored["status"], helper_store.TIMEOUT)
        self.assertTrue(stored["error_message"])
        self.assertIsNone(
            self.store.claim_job(device_id="dev_a", lease_token="z" * 32, now=self.now),
            "a timed-out job must not be claimable again",
        )

    def test_fail_and_timeout_reports_are_terminal(self) -> None:
        for reported, expected in ((helper_store.FAILED, helper_store.FAILED), (helper_store.TIMEOUT, helper_store.TIMEOUT)):
            with self.subTest(reported=reported):
                job = self._job()
                lease = helper_store.new_lease_token()
                self.store.claim_job(device_id="dev_a", lease_token=lease, now=self.now)
                outcome = self.store.fail_job(job["id"], lease, status=reported, error_message="boom", now=self.now)
                self.assertEqual(outcome, helper_store.COMPLETED)
                stored = self.store.get_job(job["id"])
                self.assertEqual(stored["status"], expected)
                self.assertEqual(stored["error_message"], "boom")
                # A late duplicate report is recognised, not treated as new work.
                again = self.store.fail_job(job["id"], lease, status=reported, error_message="boom", now=self.now)
                self.assertEqual(again, helper_store.DUPLICATE)

    def test_release_returns_job_to_the_queue(self) -> None:
        job = self._job(max_attempts=3)
        lease = helper_store.new_lease_token()
        self.store.claim_job(device_id="dev_a", lease_token=lease, now=self.now)
        self.assertEqual(self.store.release_job(job["id"], lease, now=self.now), helper_store.COMPLETED)
        stored = self.store.get_job(job["id"])
        self.assertEqual(stored["status"], helper_store.QUEUED)
        self.assertIsNone(stored["lease_token"])

    def test_release_after_last_attempt_times_out(self) -> None:
        job = self._job(max_attempts=1)
        lease = helper_store.new_lease_token()
        self.store.claim_job(device_id="dev_a", lease_token=lease, now=self.now)
        self.store.release_job(job["id"], lease, now=self.now)
        self.assertEqual(self.store.get_job(job["id"])["status"], helper_store.TIMEOUT)

    def test_job_key_makes_enqueue_idempotent(self) -> None:
        params = {
            "client_id": "client-1",
            "source_filename": "song.mflac",
            "source_path": "/tmp/a.mflac",
            "job_key": "same-key",
        }
        first, created_first = self.store.create_job(**params, now=self.now)
        second, created_second = self.store.create_job(**params, now=self.now)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["id"], second["id"])

    def test_concurrent_claims_never_double_lease(self) -> None:
        job = self._job()
        results: list[dict | None] = []
        lock = threading.Lock()

        def worker() -> None:
            claimed = self.store.claim_job(
                device_id=f"dev_{threading.get_ident()}",
                lease_token=helper_store.new_lease_token(),
                now=self.now,
            )
            with lock:
                results.append(claimed)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [job for job in results if job is not None]
        self.assertEqual(len(winners), 1, "exactly one thread may lease the job")
        self.assertEqual(self.store.get_job(job["id"])["attempts"], 1)

    def test_heartbeat_extends_only_the_current_lease(self) -> None:
        job = self._job()
        lease = helper_store.new_lease_token()
        self.store.claim_job(device_id="dev_a", lease_token=lease, now=self.now)
        original = self.store.get_job(job["id"])["lease_expires_at"]

        later = self.now + timedelta(seconds=60)
        self.assertTrue(self.store.extend_lease(job["id"], lease, lease_seconds=900, now=later))
        extended = self.store.get_job(job["id"])["lease_expires_at"]
        self.assertGreater(extended, original)

        self.assertFalse(
            self.store.extend_lease(job["id"], "w" * 32, lease_seconds=900, now=later),
            "a foreign lease token must not extend the lease",
        )


class DisabledProtocolTests(unittest.TestCase):
    """The surface must stay closed when no enrollment secret is configured."""

    def test_every_endpoint_is_unavailable_without_a_secret(self) -> None:
        store = helper_store.MemoryHelperStore()
        settings = helper_api.HelperSettings(enrollment_secret="", tmp_root=TMP_ROOT)
        probe = FastAPI()
        probe.include_router(
            helper_api.build_router(store=store, settings=settings, publish_result=lambda **_: 0)
        )
        probe.include_router(helper_api.build_client_router(store=store))
        with TestClient(probe) as client:
            self.assertEqual(client.post("/api/v1/helper/devices/register", json={}).status_code, 503)
            self.assertEqual(client.post("/api/v1/helper/jobs/claim").status_code, 503)
            self.assertEqual(client.post("/api/v1/helper/jobs").status_code, 503)


class HelperProtocolHttpTests(unittest.TestCase):
    """End-to-end protocol behaviour over HTTP against the real application."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._client = TestClient(app)
        cls._client.__enter__()
        # A janitor device leases leftover jobs so each test observes only the
        # job it enqueued; claim deliberately hands out the oldest queued job.
        cls._janitor_id, cls._janitor_token = register(cls._client, "janitor-helper")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.__exit__(None, None, None)

    def setUp(self) -> None:
        for _ in range(64):
            if claim(self._client, self._janitor_id, self._janitor_token)["job"] is None:
                break

    @property
    def client(self) -> TestClient:
        return self._client

    def test_registration_requires_the_enrollment_secret(self) -> None:
        self.assertEqual(
            self.client.post("/api/v1/helper/devices/register", json={}).status_code, 401
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/helper/devices/register",
                json={},
                headers={"X-Helper-Enroll-Secret": "wrong"},
            ).status_code,
            401,
        )

    def test_device_authentication_rejects_bad_credentials(self) -> None:
        device_id, token = register(self.client, "auth-helper")
        self.assertEqual(self.client.post("/api/v1/helper/jobs/claim").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/v1/helper/jobs/claim",
                headers=device_headers(device_id, "x" * 43),
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/helper/jobs/claim",
                headers=device_headers("dev_0000000000000000", token),
            ).status_code,
            401,
        )

    def test_revoked_device_cannot_claim(self) -> None:
        device_id, token = register(self.client, "revoked-helper")
        revoke = self.client.post(
            f"/api/v1/helper/devices/{device_id}/revoke", headers=enroll_headers()
        )
        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/v1/helper/jobs/claim", headers=device_headers(device_id, token)
            ).status_code,
            403,
        )

    def test_enqueue_claim_complete_and_client_download(self) -> None:
        device_id, token = register(self.client, "flow-helper")
        enqueued = enqueue(self.client, client_id="flow-client")
        job_id = enqueued["job_id"]
        self.assertTrue(enqueued["created"])

        claimed = claim(self.client, device_id, token)
        job = claimed["job"]
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["attempts"], 1)
        lease = job["lease_token"]
        self.assertNotIn("source_path", job, "server paths must not leak to the helper")

        source = self.client.get(
            f"/api/v1/helper/jobs/{job_id}/source", headers=device_headers(device_id, token, lease)
        )
        self.assertEqual(source.status_code, 200)
        self.assertEqual(source.content, qmc_source())

        beat = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/heartbeat", headers=device_headers(device_id, token, lease)
        )
        self.assertEqual(beat.status_code, 200)

        complete = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(complete.json()["status"], helper_store.COMPLETED)

        status = self.client.get(f"/api/v1/jobs/{job_id}", params={"client_id": "flow-client"})
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body["status"], helper_store.SUCCEEDED)
        self.assertTrue(body["download_url"].startswith("/api/v1/download/"))

        download = self.client.get(body["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, FLAC_PAYLOAD)

    def test_helper_response_never_carries_the_download_token(self) -> None:
        device_id, token = register(self.client, "secrecy-helper")
        job_id = enqueue(self.client, client_id="secrecy-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]

        complete = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(complete.status_code, 200)
        self.assertNotIn("download_url", complete.json())
        self.assertNotIn("result_token", complete.json())

        duplicate = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(duplicate.json()["status"], helper_store.DUPLICATE)
        self.assertNotIn("download_url", duplicate.json())

    def test_duplicate_completion_leaves_exactly_one_result(self) -> None:
        device_id, token = register(self.client, "dup-helper")
        job_id = enqueue(self.client, client_id="dup-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]

        first = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(first.json()["status"], helper_store.COMPLETED)
        after_first = len(download_dirs())

        second = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(second.json()["status"], helper_store.DUPLICATE)
        self.assertEqual(len(download_dirs()), after_first, "a repeat report must not publish again")

        status = self.client.get(f"/api/v1/jobs/{job_id}", params={"client_id": "dup-client"})
        download = self.client.get(status.json()["download_url"])
        self.assertEqual(download.content, FLAC_PAYLOAD)

    def test_same_source_is_not_queued_twice(self) -> None:
        first = enqueue(self.client, client_id="idem-client")
        second = enqueue(self.client, client_id="idem-client")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_another_device_cannot_touch_a_leased_job(self) -> None:
        owner_id, owner_token = register(self.client, "owner-helper")
        other_id, other_token = register(self.client, "other-helper")
        job_id = enqueue(self.client, client_id="owner-client")["job_id"]
        lease = claim(self.client, owner_id, owner_token)["job"]["lease_token"]

        self.assertEqual(
            self.client.get(
                f"/api/v1/helper/jobs/{job_id}/source",
                headers=device_headers(other_id, other_token, lease),
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/helper/jobs/{job_id}/complete",
                files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
                headers=device_headers(other_id, other_token, lease),
            ).status_code,
            409,
        )

    def test_source_download_rejects_a_stale_lease(self) -> None:
        device_id, token = register(self.client, "stale-helper")
        job_id = enqueue(self.client, client_id="stale-client")["job_id"]
        claim(self.client, device_id, token)
        self.assertEqual(
            self.client.get(
                f"/api/v1/helper/jobs/{job_id}/source",
                headers=device_headers(device_id, token, "e" * 32),
            ).status_code,
            409,
        )

    def test_failure_report_marks_the_job_failed(self) -> None:
        device_id, token = register(self.client, "fail-helper")
        job_id = enqueue(self.client, client_id="fail-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]

        failed = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/fail",
            data={"reason": "failed", "error_message": "no authorized key source configured"},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["job_status"], helper_store.FAILED)

        status = self.client.get(f"/api/v1/jobs/{job_id}", params={"client_id": "fail-client"})
        self.assertEqual(status.json()["status"], helper_store.FAILED)
        self.assertIn("no authorized key source", status.json()["error_message"])

    def test_timeout_report_is_recorded_as_timeout(self) -> None:
        device_id, token = register(self.client, "timeout-helper")
        job_id = enqueue(self.client, client_id="timeout-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]

        reported = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/fail",
            data={"reason": "timeout", "error_message": "QQ Music session not available"},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(reported.json()["job_status"], helper_store.TIMEOUT)

    def test_release_requeues_for_another_helper(self) -> None:
        device_id, token = register(self.client, "release-helper")
        job_id = enqueue(self.client, client_id="release-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]

        released = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/release",
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(released.json()["job_status"], helper_store.QUEUED)

        other_id, other_token = register(self.client, "second-helper")
        reclaimed = claim(self.client, other_id, other_token)
        self.assertIsNotNone(reclaimed["job"], "a released job must become claimable again")

    def test_claim_reports_an_empty_queue(self) -> None:
        device_id, token = register(self.client, "idle-helper")
        # Drain anything another test left behind, then confirm the empty answer.
        for _ in range(20):
            if claim(self.client, device_id, token)["job"] is None:
                break
        self.assertIsNone(claim(self.client, device_id, token)["job"])

    def test_rejects_a_non_audio_result(self) -> None:
        device_id, token = register(self.client, "garbage-helper")
        job_id = enqueue(self.client, client_id="garbage-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]

        rejected = self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", b"not-audio-at-all", "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )
        self.assertEqual(rejected.status_code, 422)

    def test_succeeded_job_exposes_everything_the_client_renders(self) -> None:
        """The Mini Program builds its result card from these fields alone."""
        device_id, token = register(self.client)
        job_id = enqueue(self.client, client_id="render-client")["job_id"]
        lease = claim(self.client, device_id, token)["job"]["lease_token"]
        self.client.post(
            f"/api/v1/helper/jobs/{job_id}/complete",
            files={"file": ("song.flac", FLAC_PAYLOAD, "audio/flac")},
            headers=device_headers(device_id, token, lease),
        )

        body = self.client.get(
            f"/api/v1/jobs/{job_id}", params={"client_id": "render-client"}
        ).json()
        self.assertEqual(body["status"], helper_store.SUCCEEDED)
        for field in ("job_id", "filename", "media_type", "size_bytes", "download_url"):
            self.assertIn(field, body, body)
        self.assertEqual(body["size_bytes"], len(FLAC_PAYLOAD))
        self.assertTrue(body["download_url"].startswith("/api/v1/download/"))

    def test_client_status_hides_other_clients_jobs(self) -> None:
        enqueue(self.client, client_id="private-client")
        jobs = self.client.get("/api/v1/helper/jobs", headers=enroll_headers()).json()["jobs"]
        target = next(job for job in jobs if job["client_id"] == "private-client")
        response = self.client.get(
            f"/api/v1/jobs/{target['job_id']}", params={"client_id": "someone-else"}
        )
        self.assertEqual(response.status_code, 404)

    def test_client_status_is_not_public(self) -> None:
        response = self.client.get("/api/v1/jobs/1")
        self.assertEqual(response.status_code, 422, "client_id is required")


class ContainerDetectionTests(unittest.TestCase):
    """The server must recognise a container that can only be opened with a key."""

    def write(self, name: str, payload: bytes) -> Path:
        path = _TEST_TMP / name
        path.write_bytes(payload)
        return path

    def test_pc_musicex_needs_a_helper(self) -> None:
        path = self.write("detect-musicex.mflac", qmc_source())
        self.assertEqual(mflac_fallback.detect_container(path), "PcV2MusicEx")
        self.assertTrue(mflac_fallback.requires_helper(path))

    def test_embedded_key_containers_do_not_need_a_helper(self) -> None:
        # A QTag/STag container carries its own key, so it must not be queued.
        for name, tail in (("detect-qtag.mflac", b"QTag"), ("detect-stag.mflac", b"STag")):
            with self.subTest(marker=tail):
                path = self.write(name, b"\x11" * 512 + tail)
                self.assertIn(mflac_fallback.detect_container(path), {"QTag", "STag"})
                self.assertFalse(mflac_fallback.requires_helper(path))

    def test_unknown_file_is_not_treated_as_needing_a_helper(self) -> None:
        path = self.write("detect-plain.bin", b"not a qmc file at all")
        self.assertEqual(mflac_fallback.detect_container(path), "unknown")
        self.assertFalse(mflac_fallback.requires_helper(path))


class MediaHelperHandoffTests(unittest.TestCase):
    """An upload the server cannot decode must be queued, not rejected."""

    def setUp(self) -> None:
        self.client = TestClient(app)
        self.client.__enter__()  # run the lifespan so the queue reports ready
        self.addCleanup(self.client.__exit__, None, None, None)

    def post_media(self, payload: bytes, name: str = "cadode.mflac"):
        return self.client.post(
            "/api/v1/convert/media",
            files={"file": (name, payload, "application/octet-stream")},
            data={"target_format": "flac", "client_id": "media-test", "response_mode": "link"},
        )

    def test_pc_musicex_upload_returns_202_with_a_job(self) -> None:
        # The container has no EKey, so every offline decoder refuses it.  The
        # endpoint must hand it to the helper queue instead of returning 422.
        with mock.patch.object(
            app_main,
            "_run_command",
            side_effect=app_main.ConversionFailed("unknown encryption method"),
        ):
            response = self.post_media(qmc_source())
        self.assertEqual(response.status_code, 202, response.text)

        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertGreater(int(body["job_id"]), 0)
        self.assertIn("/api/v1/jobs/", body["status_url"])
        self.assertTrue(body["filename"].endswith(".flac"))

        # The queued job must be visible to the client that uploaded it.
        status = self.client.get(
            f"/api/v1/jobs/{body['job_id']}", params={"client_id": "media-test"}
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "queued")

    def test_queued_upload_is_not_reported_as_a_hard_failure(self) -> None:
        with mock.patch.object(
            app_main,
            "_run_command",
            side_effect=app_main.ConversionFailed("unknown encryption method"),
        ):
            response = self.post_media(qmc_source())
        self.assertEqual(response.status_code, 202)
        # The upload row is closed with a marker, so a client can tell a queued
        # upload apart from a genuine failure.
        history = self.client.get("/api/v1/history/media-test")
        if history.status_code == 200:
            rows = history.json().get("records") or history.json().get("history") or []
            for row in rows:
                if row.get("status") == "failed":
                    self.assertTrue(
                        str(row.get("error_message", "")).startswith("helper_queued:"),
                        row,
                    )

    def test_non_qmc_upload_still_fails_normally(self) -> None:
        # A file that is not a QQ Music container must keep the old behaviour.
        with mock.patch.object(
            app_main,
            "_run_command",
            side_effect=app_main.ConversionFailed("unknown encryption method"),
        ):
            response = self.post_media(b"this is not an mflac file", name="plain.mflac")
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
