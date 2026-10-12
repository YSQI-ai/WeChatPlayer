"""Unit tests for the Windows helper agent.

Run with::

    python -m unittest discover -s windows-helper/tests -t windows-helper/tests -v
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HELPER_DIR = Path(__file__).resolve().parent.parent
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))

from helper.agent import REASON_NO_KEY, REASON_TIMEOUT, HelperAgent  # noqa: E402
from helper.client import (  # noqa: E402
    AuthenticationError,
    HelperApiError,
    Job,
    encode_multipart,
    encode_multipart_document,
)
from helper.config import ConfigError, HelperConfig  # noqa: E402
from helper.keyprovider import DecodeFailure, DecodePlan, KeyProvider, KeyUnavailable  # noqa: E402
from helper.redact import REDACTED, Redactor, RedactingFilter  # noqa: E402
from helper.state import (  # noqa: E402
    CredentialStore,
    Credentials,
    JournalEntry,
    JobJournal,
    PlaintextProtector,
    ProtectedJsonFile,
    StateError,
)

FLAC_BYTES = b"fLaC" + b"\x00" * 128
_TEST_ROOT = Path(tempfile.gettempdir()) / f"wechat-helper-unit-{os.getpid()}"


def make_config(**overrides) -> HelperConfig:
    base = _TEST_ROOT / f"case-{time.monotonic_ns()}"
    params = {
        "api_base_url": "https://helper.test",
        "state_dir": base / "state",
        "work_dir": base / "work",
        "enrollment_secret": "enroll-secret",
        "device_name": "unit-test-helper",
    }
    params.update(overrides)
    config = HelperConfig(**params)
    config.ensure_directories()
    return config


# -- test doubles --------------------------------------------------------


class FakeDecoder:
    """Stands in for the offline QMC decoder."""

    def __init__(self, kind: str = "PcV2MusicEx", delay: float = 0.0, fail: Exception | None = None) -> None:
        self.kind = kind
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.seen_plans: list[DecodePlan] = []

    def inspect(self, source_path: Path) -> str:
        return self.kind

    def decode(self, source_path: Path, output_path: Path, plan: DecodePlan) -> str:
        self.calls += 1
        self.seen_plans.append(plan)
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        output_path.write_bytes(FLAC_BYTES)
        return self.kind

    def self_test(self) -> None:
        return None


class StubKeyProvider(KeyProvider):
    def __init__(self, plan: DecodePlan | None = None, error: Exception | None = None) -> None:
        self._plan = plan or DecodePlan(ekey="AUTHORIZED-EKEY-VALUE-0123456789", source_kind="ekey_file")
        self._error = error

    def plan(self, source_path: Path) -> DecodePlan:
        if self._error is not None:
            raise self._error
        return self._plan

    def check(self) -> tuple[bool, str]:
        return (False, "stub") if self._error else (True, "ok")


class FakeClient:
    """In-memory stand-in for :class:`helper.client.HelperClient`."""

    def __init__(self, jobs: list[Job] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.registered = 0
        self.completed: list[tuple[int, str, int]] = []
        self.failures: list[tuple[int, str, str]] = []
        self.releases: list[int] = []
        self.heartbeats: list[int] = []
        self.device_heartbeats = 0
        self.statuses: dict[int, str] = {}
        self.sources: dict[int, bytes] = {}
        self.complete_status = "completed"
        self.claim_error: Exception | None = None
        self.complete_error: Exception | None = None
        self.download_error: Exception | None = None

    def register(self, **kwargs):
        self.registered += 1
        return {
            "device_id": "dev_0123456789abcdef",
            "device_token": "T" * 43,
            "token_expires_at": "2099-01-01T00:00:00",
            "lease_seconds": 900,
            "heartbeat_seconds": 60,
        }

    def device_heartbeat(self, device_id: str, device_token: str):
        self.device_heartbeats += 1
        return {"status": "ok", "queue": {}}

    def claim(self, device_id: str, device_token: str) -> Job | None:
        if self.claim_error is not None:
            raise self.claim_error
        return self.jobs.pop(0) if self.jobs else None

    def job_heartbeat(self, device_id: str, device_token: str, job_id: int, lease: str):
        self.heartbeats.append(job_id)
        return {"status": "ok"}

    def download_source(self, device_id, device_token, job_id, lease, destination: Path, max_bytes: int) -> int:
        if self.download_error is not None:
            raise self.download_error
        payload = self.sources.get(job_id, b"\x11" * 256 + b"musicex\x00")
        destination.write_bytes(payload)
        return len(payload)

    def complete(self, device_id, device_token, job_id, lease, result_path: Path, output_name: str = ""):
        if self.complete_error is not None:
            raise self.complete_error
        self.completed.append((job_id, lease, result_path.stat().st_size))
        self.statuses[job_id] = "succeeded"
        return {"status": self.complete_status, "job_id": job_id}

    def fail(self, device_id, device_token, job_id, lease, reason, message):
        self.failures.append((job_id, reason, message))
        self.statuses[job_id] = "timeout" if reason == REASON_TIMEOUT else "failed"
        return {"status": "completed", "job_status": self.statuses[job_id]}

    def release(self, device_id, device_token, job_id, lease):
        self.releases.append(job_id)
        self.statuses[job_id] = "queued"
        return {"status": "released", "job_status": "queued"}

    def job_status(self, device_id, device_token, job_id):
        return {"job_id": job_id, "status": self.statuses.get(job_id, "queued"), "lease_owner": None}


def make_job(job_id: int = 1, **overrides) -> Job:
    params = {
        "job_id": job_id,
        "lease_token": "L" * 32,
        "source_filename": "song.mflac",
        "target_format": "flac",
        "attempt": 1,
        "max_attempts": 3,
    }
    params.update(overrides)
    return Job(**params)


def wait_until(predicate, ceiling: float = 5.0) -> bool:
    """Poll until ``predicate`` holds, up to ``ceiling`` seconds.

    The lease-keeper tests exercise a background thread.  A fixed short sleep
    (a fifth of a second, say) flakes whenever a shared runner is slow to
    schedule that thread, so they wait for the condition instead and only use
    the ceiling to bound how long a genuine failure takes to surface.
    """
    deadline = time.monotonic() + ceiling
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- redaction -----------------------------------------------------------


class RedactorTests(unittest.TestCase):
    def test_registered_secrets_are_removed(self) -> None:
        redactor = Redactor()
        redactor.add_secret("SUPER-SECRET-TOKEN-VALUE-123456")
        self.assertNotIn("SUPER-SECRET-TOKEN-VALUE-123456", redactor.scrub("token=SUPER-SECRET-TOKEN-VALUE-123456"))
        self.assertIn(REDACTED, redactor.scrub("token=SUPER-SECRET-TOKEN-VALUE-123456"))

    def test_ekey_assignment_is_removed(self) -> None:
        redactor = Redactor()
        self.assertNotIn("abcdef123456", redactor.scrub("ekey: abcdef123456"))
        self.assertNotIn("abcdef123456", redactor.scrub("eKey=abcdef123456"))

    def test_long_tokens_are_removed(self) -> None:
        redactor = Redactor()
        token = "A" * 43
        self.assertNotIn(token, redactor.scrub(f"downloading {token} now"))

    def test_user_paths_are_masked(self) -> None:
        redactor = Redactor()
        scrubbed = redactor.scrub(r"reading C:\Users\alice\Music\song.mflac")
        self.assertNotIn("alice", scrubbed)
        self.assertIn("<user>", scrubbed)

    def test_private_paths_are_masked(self) -> None:
        redactor = Redactor()
        redactor.add_path(Path(r"C:\Temp\helper-state"))
        scrubbed = redactor.scrub(r"work dir C:\Temp\helper-state\work\job-1")
        self.assertNotIn("helper-state", scrubbed)

    def test_short_values_are_left_alone(self) -> None:
        redactor = Redactor()
        redactor.add_secret("abc")
        self.assertEqual(redactor.scrub("abc def"), "abc def")

    def test_log_filter_scrubs_message_and_args(self) -> None:
        redactor = Redactor()
        redactor.add_secret("MY-DEVICE-TOKEN-VALUE-123456")
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "token=%s", ("MY-DEVICE-TOKEN-VALUE-123456",), None
        )
        RedactingFilter(redactor).filter(record)
        self.assertNotIn("MY-DEVICE-TOKEN-VALUE-123456", record.getMessage())

    def test_filter_scrubs_dict_args(self) -> None:
        redactor = Redactor()
        redactor.add_secret("MY-DEVICE-TOKEN-VALUE-123456")
        # logging unwraps a single-element tuple holding a mapping.
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "%(token)s", ({"token": "MY-DEVICE-TOKEN-VALUE-123456"},), None
        )
        RedactingFilter(redactor).filter(record)
        self.assertNotIn("MY-DEVICE-TOKEN-VALUE-123456", record.getMessage())


# -- protected state -----------------------------------------------------


class StateTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        path = _TEST_ROOT / "state" / "round-trip.bin"
        store = ProtectedJsonFile(path, PlaintextProtector())
        store.write({"a": 1, "b": "two"})
        self.assertEqual(store.read(), {"a": 1, "b": "two"})

    def test_missing_file_reads_as_none(self) -> None:
        store = ProtectedJsonFile(_TEST_ROOT / "state" / "absent.bin", PlaintextProtector())
        self.assertIsNone(store.read())

    def test_foreign_file_is_rejected(self) -> None:
        path = _TEST_ROOT / "state" / "foreign.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a helper state file")
        with self.assertRaises(StateError):
            ProtectedJsonFile(path, PlaintextProtector()).read()

    def test_clear_removes_the_file(self) -> None:
        path = _TEST_ROOT / "state" / "clear.bin"
        store = ProtectedJsonFile(path, PlaintextProtector())
        store.write({"a": 1})
        store.clear()
        self.assertFalse(path.exists())

    def test_credentials_round_trip_and_api_binding(self) -> None:
        path = _TEST_ROOT / "state" / "cred.bin"
        store = CredentialStore(path, "https://one.test", PlaintextProtector())
        store.save(Credentials(device_id="dev_0123456789abcdef", device_token="T" * 43))
        loaded = store.load()
        self.assertEqual(loaded.device_id, "dev_0123456789abcdef")

        # Credentials registered against another server must not be reused.
        other = CredentialStore(path, "https://two.test", PlaintextProtector())
        self.assertIsNone(other.load())

    def test_journal_round_trip(self) -> None:
        path = _TEST_ROOT / "state" / "journal.bin"
        journal = JobJournal(path, PlaintextProtector())
        journal.record(JournalEntry(job_id=7, lease_token="L" * 32, phase="decoded"))
        entry = journal.load()
        self.assertEqual((entry.job_id, entry.phase), (7, "decoded"))
        journal.clear()
        self.assertIsNone(journal.load())


# -- configuration -------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_rejects_unknown_key_source(self) -> None:
        with self.assertRaises(ConfigError):
            HelperConfig(api_base_url="https://x.test", key_source="magic")

    def test_key_source_requires_a_path(self) -> None:
        with self.assertRaises(ConfigError):
            HelperConfig(api_base_url="https://x.test", key_source="ekey_file")

    def test_describe_never_contains_the_secret(self) -> None:
        config = make_config()
        described = config.describe()
        self.assertNotIn("enroll-secret", str(described))
        self.assertTrue(described["enrollment_secret_configured"])

    def test_ini_file_with_percent_variables_is_not_interpolated(self) -> None:
        """A %LOCALAPPDATA%-style path must survive INI parsing.

        configparser's default BasicInterpolation raises on a bare %, and the
        shipped example config uses exactly that syntax, so this guards the
        interpolation=None setting.
        """
        path = _TEST_ROOT / "config" / "percent.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[helper]\n"
            "api_base_url = https://from-file.test\n"
            "state_dir = %LOCALAPPDATA%\\WeChatPlayerHelper\n"
            "key_source = none\n",
            encoding="utf-8",
        )
        os.environ["LOCALAPPDATA"] = r"C:\Users\tester\AppData\Local"
        try:
            config = HelperConfig.load(path)
        finally:
            os.environ.pop("LOCALAPPDATA", None)
        self.assertEqual(
            config.state_dir, Path(r"C:\Users\tester\AppData\Local\WeChatPlayerHelper")
        )

    def test_ini_file_is_read_and_env_overrides_secret(self) -> None:
        path = _TEST_ROOT / "config" / "helper.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[helper]\n"
            "api_base_url = https://from-file.test\n"
            "state_dir = C:\\Temp\\helper-state\n"
            "key_source = none\n"
            "poll_interval_seconds = 7\n",
            encoding="utf-8",
        )
        os.environ["HELPER_ENROLLMENT_SECRET"] = "from-env"
        try:
            config = HelperConfig.load(path)
        finally:
            os.environ.pop("HELPER_ENROLLMENT_SECRET", None)
        self.assertEqual(config.api_base_url, "https://from-file.test")
        self.assertEqual(config.enrollment_secret, "from-env")
        self.assertEqual(config.poll_interval_seconds, 7)
        # A literal Windows path survives INI parsing unchanged.
        self.assertIn("helper-state", str(config.state_dir))


# -- multipart -----------------------------------------------------------


class MultipartTests(unittest.TestCase):
    def test_file_body_contains_parts_and_declares_its_length(self) -> None:
        path = _TEST_ROOT / "multipart" / "result.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FLAC_BYTES)
        body, content_type = encode_multipart({"output_name": "song.flac"}, "file", path, "result.flac")
        raw = b"".join(body)
        self.assertIn(b'name="output_name"', raw)
        self.assertIn(b'name="file"; filename="result.flac"', raw)
        self.assertIn(FLAC_BYTES, raw)
        self.assertTrue(raw.endswith(b"--\r\n"))
        self.assertEqual(body.length, len(raw), "declared Content-Length must match the body")
        self.assertIn("multipart/form-data; boundary=", content_type)

    def test_document_body_has_no_file_part(self) -> None:
        raw, content_type = encode_multipart_document({"reason": "timeout"})
        self.assertIn(b'name="reason"', raw)
        self.assertIn(b"timeout", raw)
        self.assertIn("multipart/form-data; boundary=", content_type)


# -- agent ---------------------------------------------------------------


class AgentTests(unittest.TestCase):
    def make_agent(self, config: HelperConfig, client: FakeClient, **kwargs) -> HelperAgent:
        return HelperAgent(
            config,
            client=client,
            key_provider=kwargs.pop("key_provider", StubKeyProvider()),
            decoder=kwargs.pop("decoder", FakeDecoder()),
            sleep=lambda _seconds: None,
            **kwargs,
        )

    def test_registers_when_no_credentials_exist(self) -> None:
        config = make_config()
        client = FakeClient()
        agent = self.make_agent(config, client)
        agent.prepare()
        credentials = agent.ensure_registered()
        self.assertEqual(client.registered, 1)
        self.assertEqual(credentials.device_id, "dev_0123456789abcdef")
        self.assertTrue(config.credentials_path.is_file())

        # A second agent on the same state dir must reuse the stored identity.
        second_client = FakeClient()
        second = self.make_agent(config, second_client)
        second.prepare()
        second.ensure_registered()
        self.assertEqual(second_client.registered, 0)

    def test_requires_enrollment_secret_to_register(self) -> None:
        config = make_config(enrollment_secret="")
        agent = self.make_agent(config, FakeClient())
        agent.prepare()
        with self.assertRaises(AuthenticationError):
            agent.ensure_registered()

    def test_processes_one_job_and_removes_the_work_directory(self) -> None:
        config = make_config()
        client = FakeClient([make_job(11)])
        decoder = FakeDecoder()
        agent = self.make_agent(config, client, decoder=decoder)

        processed = agent.run(once=True)

        self.assertEqual(processed, 1)
        self.assertEqual(len(client.completed), 1)
        self.assertEqual(decoder.calls, 1)
        job_id, _lease, size = client.completed[0]
        self.assertEqual(job_id, 11)
        self.assertEqual(size, len(FLAC_BYTES))
        self.assertFalse(agent._work_dir(11).exists(), "work directory must be cleaned up")
        self.assertIsNone(agent.journal.load())

    def test_passes_the_authorized_key_to_the_decoder(self) -> None:
        config = make_config()
        client = FakeClient([make_job(12)])
        decoder = FakeDecoder()
        provider = StubKeyProvider(DecodePlan(ekey="AUTHORIZED-KEY-0123456789", source_kind="ekey_file"))
        agent = self.make_agent(config, client, decoder=decoder, key_provider=provider)

        agent.run(once=True)

        self.assertEqual(decoder.seen_plans[0].ekey, "AUTHORIZED-KEY-0123456789")

    def test_reports_missing_key_source_without_guessing(self) -> None:
        config = make_config(key_source="none")
        client = FakeClient([make_job(13)])
        provider = StubKeyProvider(DecodePlan(source_kind="none"))
        # The agent asks the decoder for the container kind; PC MusicEx has no key.
        agent = self.make_agent(config, client, key_provider=provider, decoder=FakeDecoder("PcV2MusicEx"))

        agent.run(once=True)

        self.assertEqual(len(client.failures), 1)
        job_id, reason, message = client.failures[0]
        self.assertEqual(job_id, 13)
        self.assertEqual(reason, REASON_NO_KEY)
        self.assertIn("authorized key source", message)
        self.assertEqual(client.completed, [], "nothing may be uploaded without a key")

    def test_embedded_key_file_decodes_even_with_key_source_none(self) -> None:
        config = make_config(key_source="none")
        client = FakeClient([make_job(14)])
        # An embedded-EKey container is not PcV2MusicEx, so no external key is needed.
        agent = self.make_agent(
            config, client, key_provider=StubKeyProvider(DecodePlan()), decoder=FakeDecoder("QTag")
        )

        agent.run(once=True)

        self.assertEqual(len(client.completed), 1)
        self.assertEqual(client.failures, [])

    def test_decode_failure_is_reported(self) -> None:
        config = make_config()
        client = FakeClient([make_job(15)])
        agent = self.make_agent(config, client, decoder=FakeDecoder(fail=DecodeFailure("bad container")))

        agent.run(once=True)

        self.assertEqual(client.failures[0][1], "decode_failed")
        self.assertIn("bad container", client.failures[0][2])

    def test_duplicate_completion_counts_as_success(self) -> None:
        config = make_config()
        client = FakeClient([make_job(16)])
        client.complete_status = "duplicate"
        agent = self.make_agent(config, client)

        agent.run(once=True)

        self.assertEqual(len(client.completed), 1)
        self.assertEqual(client.failures, [])
        self.assertIsNone(agent.journal.load(), "a duplicate report must still close the journal")

    def test_local_deadline_reports_timeout(self) -> None:
        config = make_config(job_timeout_seconds=1)
        client = FakeClient([make_job(17)])
        agent = self.make_agent(config, client, decoder=FakeDecoder(delay=1.2))

        agent.run(once=True)

        self.assertEqual(len(client.failures), 1)
        self.assertEqual(client.failures[0][1], REASON_TIMEOUT)
        self.assertEqual(client.completed, [])

    def test_network_error_leaves_the_journal_for_reconciliation(self) -> None:
        from helper.client import TransientApiError

        config = make_config()
        client = FakeClient([make_job(18)])
        client.download_error = TransientApiError("connection reset")
        agent = self.make_agent(config, client)

        agent.run(once=True)

        entry = agent.journal.load()
        self.assertIsNotNone(entry, "the journal must survive so a restart can reconcile")
        self.assertEqual(entry.job_id, 18)
        self.assertEqual(client.completed, [])
        self.assertFalse(agent._work_dir(18).exists(), "no media may be left on disk")

    def test_authentication_error_stops_the_agent(self) -> None:
        config = make_config()
        client = FakeClient()
        client.claim_error = AuthenticationError(403, "Helper device is revoked")
        agent = self.make_agent(config, client)

        with self.assertRaises(AuthenticationError):
            agent.run(once=True)

    def test_reconcile_skips_a_job_that_already_succeeded(self) -> None:
        config = make_config()
        client = FakeClient()
        client.statuses[21] = "succeeded"
        decoder = FakeDecoder()
        agent = self.make_agent(config, client, decoder=decoder)
        agent.prepare()
        agent.ensure_registered()
        agent.journal.record(JournalEntry(job_id=21, lease_token="L" * 32, phase="decoded"))

        agent.reconcile_journal()

        self.assertEqual(decoder.calls, 0, "a completed job must never be decoded again")
        self.assertIsNone(agent.journal.load())
        self.assertEqual(client.releases, [])

    def test_reconcile_releases_a_job_this_device_still_owns(self) -> None:
        config = make_config()
        client = FakeClient()
        client.statuses[22] = "leased"
        agent = self.make_agent(config, client)
        agent.prepare()
        credentials = agent.ensure_registered()
        client.job_status = lambda d, t, j: {"job_id": j, "status": "leased", "lease_owner": credentials.device_id}
        agent.journal.record(JournalEntry(job_id=22, lease_token="L" * 32, phase="downloaded"))

        agent.reconcile_journal()

        self.assertEqual(client.releases, [22])
        self.assertIsNone(agent.journal.load())

    def test_reconcile_clears_a_finished_job(self) -> None:
        config = make_config()
        client = FakeClient()
        client.statuses[23] = "timeout"
        agent = self.make_agent(config, client)
        agent.prepare()
        agent.ensure_registered()
        agent.journal.record(JournalEntry(job_id=23, lease_token="L" * 32, phase="claimed"))

        agent.reconcile_journal()

        self.assertIsNone(agent.journal.load())
        self.assertEqual(client.releases, [])

    def test_keyboard_interrupt_releases_the_job(self) -> None:
        config = make_config()
        client = FakeClient([make_job(24)])
        agent = self.make_agent(config, client, decoder=FakeDecoder(fail=KeyboardInterrupt()))

        processed = agent.run(once=True)

        self.assertEqual(processed, 0)
        self.assertEqual(client.releases, [24], "an interrupted job must go back to the queue")
        self.assertEqual(client.completed, [])
        self.assertFalse(agent._work_dir(24).exists())

    def test_lease_keeper_extends_the_lease_while_working(self) -> None:
        from helper.agent import LeaseKeeper

        client = FakeClient()
        credentials = Credentials(device_id="dev_0123456789abcdef", device_token="T" * 43)
        job = make_job(25)
        with LeaseKeeper(client, credentials, job, 0.05):
            wait_until(lambda: len(client.heartbeats) >= 2)
        self.assertGreaterEqual(len(client.heartbeats), 2, "the lease must be renewed periodically")
        self.assertTrue(all(job_id == 25 for job_id in client.heartbeats))

    def test_lease_keeper_marks_the_lease_lost_on_conflict(self) -> None:
        from helper.agent import LeaseKeeper

        client = FakeClient()

        def conflict(*_args, **_kwargs):
            raise HelperApiError(409, "Job is leased to another helper")

        client.job_heartbeat = conflict  # type: ignore[method-assign]
        credentials = Credentials(device_id="dev_0123456789abcdef", device_token="T" * 43)
        keeper = LeaseKeeper(client, credentials, make_job(26), 0.05)
        with keeper:
            wait_until(lambda: keeper.lost)
        self.assertTrue(keeper.lost, "a reassigned job must stop the current attempt")
        self.assertIn("another helper", keeper.last_error)

    def test_lease_keeper_marks_the_lease_lost_when_revoked(self) -> None:
        from helper.agent import LeaseKeeper

        client = FakeClient()

        def revoked(*_args, **_kwargs):
            raise AuthenticationError(403, "Helper device is revoked")

        client.job_heartbeat = revoked  # type: ignore[method-assign]
        credentials = Credentials(device_id="dev_0123456789abcdef", device_token="T" * 43)
        keeper = LeaseKeeper(client, credentials, make_job(27), 0.05)
        with keeper:
            wait_until(lambda: keeper.lost)
        self.assertTrue(keeper.lost)

    def test_agent_skips_reporting_when_the_lease_was_lost(self) -> None:
        config = make_config(lease_heartbeat_seconds=1)
        client = FakeClient([make_job(28)])

        def conflict(*_args, **_kwargs):
            raise HelperApiError(409, "Job is leased to another helper")

        # A decoded result must not be reported by an attempt that lost its lease.
        client.job_heartbeat = conflict  # type: ignore[method-assign]
        agent = self.make_agent(config, client, decoder=FakeDecoder(delay=1.2))
        agent._credentials = Credentials(
            device_id="dev_0123456789abcdef", device_token="T" * 43, lease_seconds=30
        )

        agent.run(once=True)

        self.assertEqual(client.completed, [], "a stale attempt must not upload its result")

    def test_low_disk_stops_work_being_claimed(self) -> None:
        config = make_config(min_free_disk_bytes=10**18)
        client = FakeClient([make_job(26)])
        agent = self.make_agent(config, client)

        processed = agent.run(once=True)

        self.assertEqual(processed, 0)
        self.assertEqual(client.completed, [], "no work may be claimed while preconditions fail")

    def test_required_process_gate_can_block_work(self) -> None:
        config = make_config(required_process="definitely-not-a-real-process.exe")
        client = FakeClient([make_job(27)])
        agent = self.make_agent(config, client)

        processed = agent.run(once=True)

        self.assertEqual(processed, 0)
        self.assertEqual(client.completed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
