"""End-to-end test: the real helper client against the real server.

Unlike the unit suite, nothing is faked on the wire.  A real FastAPI app is
served by uvicorn on a loopback port; a real :class:`HelperClient` registers,
claims, downloads, uploads, and the result is fetched back through the Mini
Program facing endpoint.  Only the offline QMC decoder is replaced, because a
genuine PC MusicEx decode needs a legal EKey that is not available here.

Run with::

    python -m unittest discover -s windows-helper/tests -t windows-helper/tests -p "test_*integration*.py" -v
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HELPER_DIR = Path(__file__).resolve().parent.parent
CONVERTER_API_DIR = HELPER_DIR.parent / "converter-api"
for path in (str(HELPER_DIR), str(CONVERTER_API_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Configure the server before importing it: the module reads these at import.
_TEST_ROOT = Path(tempfile.gettempdir()) / f"wechat-helper-integration-{os.getpid()}"
_TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["CONVERTER_TMP_ROOT"] = str(_TEST_ROOT / "downloads")
os.environ["HELPER_ENROLLMENT_SECRET"] = "integration-enrollment-secret"
os.environ["HELPER_LEASE_SECONDS"] = "900"
os.environ["HELPER_MAX_ATTEMPTS"] = "3"

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402
from helper.agent import HelperAgent  # noqa: E402
from helper.client import encode_multipart  # noqa: E402
from helper.config import HelperConfig  # noqa: E402
from helper.keyprovider import DecodePlan  # noqa: E402

ENROLL_SECRET = "integration-enrollment-secret"
FAKE_EKEY = "AUTHORIZED-EKEY-FOR-INTEGRATION-TEST-0123456789"
FLAC_PAYLOAD = b"fLaC" + b"\x00" * 512


def qmc_source() -> bytes:
    """A minimal file that passes the server's QQ Music container check."""
    return b"\x22" * 1024 + b"musicex\x00"


class RecordingDecoder:
    """Writes a valid FLAC instead of running the real QMC decoder."""

    def __init__(self) -> None:
        self.plans: list[DecodePlan] = []
        self.sources: list[int] = []

    def inspect(self, source_path: Path) -> str:
        return "PcV2MusicEx"

    def decode(self, source_path: Path, output_path: Path, plan: DecodePlan) -> str:
        self.plans.append(plan)
        self.sources.append(source_path.stat().st_size)
        output_path.write_bytes(FLAC_PAYLOAD)
        return "PcV2MusicEx"

    def self_test(self) -> None:
        return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def multipart_request(url: str, fields: dict[str, str], filename: str, payload: bytes, headers: dict[str, str]):
    body_file = _TEST_ROOT / f"upload-{time.monotonic_ns()}.mflac"
    body_file.write_bytes(payload)
    body, content_type = encode_multipart(fields, "file", body_file, filename)
    request = urllib.request.Request(
        url,
        data=bytes().join(body),  # small payload: buffering here is fine
        headers={**headers, "Content-Type": content_type, "Content-Length": str(body.length)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        body_file.unlink(missing_ok=True)


class HelperIntegrationTests(unittest.TestCase):
    """The full helper loop against a live server."""

    server: uvicorn.Server
    thread: threading.Thread
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=cls.port, log_level="warning", lifespan="on", access_log=False
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, name="test-server", daemon=True)
        cls.thread.start()

        deadline = time.time() + 30
        while time.time() < deadline:
            if cls.server.started and cls._healthy():
                break
            time.sleep(0.1)
        else:
            cls.server.should_exit = True
            raise unittest.SkipTest("could not start a loopback uvicorn server in this environment")

        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=10)

    @classmethod
    def _healthy(cls) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/v1/health", timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def make_config(self, **overrides) -> HelperConfig:
        base = _TEST_ROOT / f"case-{time.monotonic_ns()}"
        ekey_file = base / "authorized.ekey"
        ekey_file.parent.mkdir(parents=True, exist_ok=True)
        ekey_file.write_text(FAKE_EKEY + "\n", encoding="utf-8")
        params = {
            "api_base_url": self.base_url,
            "state_dir": base / "state",
            "work_dir": base / "work",
            "enrollment_secret": ENROLL_SECRET,
            "device_name": "integration-helper",
            "key_source": "ekey_file",
            "key_source_path": ekey_file,
            "lease_heartbeat_seconds": 2,
        }
        params.update(overrides)
        config = HelperConfig(**params)
        config.ensure_directories()
        return config

    def enqueue(self, client_id: str) -> int:
        status, payload = multipart_request(
            f"{self.base_url}/api/v1/helper/jobs",
            {"client_id": client_id, "target_format": "flac", "reason": "missing_ekey"},
            "song.mflac",
            qmc_source(),
            {"X-Helper-Enroll-Secret": ENROLL_SECRET},
        )
        self.assertIn(status, (200, 201), payload)
        return int(payload["job_id"])

    def job_status(self, job_id: int, client_id: str) -> dict:
        with urllib.request.urlopen(
            f"{self.base_url}/api/v1/jobs/{job_id}?client_id={client_id}", timeout=15
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_full_round_trip(self) -> None:
        """Register, claim, decode, upload, and download the result."""
        config = self.make_config()
        decoder = RecordingDecoder()
        agent = HelperAgent(config, decoder=decoder, sleep=lambda _s: None)

        agent.prepare()
        credentials = agent.ensure_registered()
        self.assertTrue(credentials.device_id.startswith("dev_"))
        self.assertTrue(config.credentials_path.is_file())

        job_id = self.enqueue("integration-client")
        processed = agent.run(once=True)

        self.assertEqual(processed, 1)
        self.assertEqual(len(decoder.plans), 1, "the file must be decoded exactly once")
        # The real ExplicitEkeyFileProvider read the operator's authorized file.
        self.assertEqual(decoder.plans[0].ekey, FAKE_EKEY)
        self.assertEqual(decoder.sources, [len(qmc_source())])

        status = self.job_status(job_id, "integration-client")
        self.assertEqual(status["status"], "succeeded")
        self.assertTrue(status["download_url"].startswith("/api/v1/download/"))

        with urllib.request.urlopen(f"{self.base_url}{status['download_url']}", timeout=15) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), FLAC_PAYLOAD)

        self.assertFalse(agent._work_dir(job_id).exists(), "no decrypted media may remain locally")
        self.assertFalse(
            (_TEST_ROOT / "downloads" / f"helper-source-{job_id}").exists()
            or any((_TEST_ROOT / "downloads").glob(f"helper-source-*")),
            "the server must drop the staged source after completion",
        )

    def test_second_run_reuses_the_registered_device(self) -> None:
        config = self.make_config()
        first = HelperAgent(config, decoder=RecordingDecoder(), sleep=lambda _s: None)
        first.prepare()
        device_id = first.ensure_registered().device_id

        second = HelperAgent(config, decoder=RecordingDecoder(), sleep=lambda _s: None)
        second.prepare()
        self.assertEqual(second.ensure_registered().device_id, device_id)

    def test_revoked_device_is_refused(self) -> None:
        """A revoked helper must stop claiming work."""
        config = self.make_config()
        agent = HelperAgent(config, decoder=RecordingDecoder(), sleep=lambda _s: None)
        agent.prepare()
        credentials = agent.ensure_registered()

        request = urllib.request.Request(
            f"{self.base_url}/api/v1/helper/devices/{credentials.device_id}/revoke",
            data=b"",
            headers={"X-Helper-Enroll-Secret": ENROLL_SECRET},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertEqual(response.status, 200)

        from helper.client import AuthenticationError

        with self.assertRaises(AuthenticationError):
            agent.run(once=True)

    def test_duplicate_upload_is_reported_as_duplicate(self) -> None:
        """Re-uploading the same result must not publish a second file."""
        from helper.client import HelperClient

        config = self.make_config()
        agent = HelperAgent(config, decoder=RecordingDecoder(), sleep=lambda _s: None)
        agent.prepare()
        credentials = agent.ensure_registered()
        job_id = self.enqueue("duplicate-client")

        client = HelperClient(self.base_url, retries=1)
        job = client.claim(credentials.device_id, credentials.device_token)
        self.assertIsNotNone(job)
        assert job is not None

        work = config.work_dir / "manual"
        work.mkdir(parents=True, exist_ok=True)
        result = work / "result.flac"
        result.write_bytes(FLAC_PAYLOAD)

        first = client.complete(credentials.device_id, credentials.device_token, job.job_id, job.lease_token, result)
        self.assertEqual(first["status"], "completed")

        second = client.complete(
            credentials.device_id, credentials.device_token, job.job_id, job.lease_token, result
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertNotIn("download_url", second)

        status = self.job_status(job.job_id, "duplicate-client")
        self.assertEqual(status["status"], "succeeded")

    def test_claim_returns_nothing_when_the_queue_is_drained(self) -> None:
        from helper.client import HelperClient

        config = self.make_config()
        agent = HelperAgent(config, decoder=RecordingDecoder(), sleep=lambda _s: None)
        agent.prepare()
        credentials = agent.ensure_registered()
        client = HelperClient(self.base_url, retries=1)

        for _ in range(50):
            if client.claim(credentials.device_id, credentials.device_token) is None:
                return
        self.fail("the queue never drained")

    def test_no_authorized_key_source_reports_a_diagnosable_failure(self) -> None:
        """With no key source the agent must fail the job, not guess."""
        config = self.make_config(key_source="none", key_source_path=None)
        agent = HelperAgent(config, decoder=RecordingDecoder(), sleep=lambda _s: None)
        agent.prepare()
        agent.ensure_registered()
        job_id = self.enqueue("no-key-client")

        processed = agent.run(once=True)

        self.assertEqual(processed, 1)
        status = self.job_status(job_id, "no-key-client")
        self.assertEqual(status["status"], "failed")
        self.assertIn("authorized key source", status["error_message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
