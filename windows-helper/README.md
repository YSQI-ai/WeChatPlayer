# Windows MFLAC helper

This directory is outside `miniprogram/` and is not part of the WeChat Mini
Program upload. Keep Windows-only binaries, local runtime files, and helper
configuration here.

The helper **is implemented** as of `docs/decisions/004-helper-protocol.md`. This
document covers its role, setup, and operation.

## Role

The helper is only needed for QQ Music `PcV2MusicEx` MFLAC files whose EKey is
not embedded in the file. The Linux API cannot recover that key from the file
alone, so the helper runs in a user-authorized QQ Music environment on Windows
and uploads **only the finished FLAC**. The EKey never leaves the helper host:

* no EKey is sent to the server, stored in a database, put in a URL, or written
  to a Mini Program storage location;
* no EKey is written to the helper's disk or to any log message;
* the helper only uses a key source you are authorized to use, and it never
  guesses a key. Without an authorized source, PC MusicEx jobs fail with a
  diagnosable reason.

## Layout

| Path | Purpose |
|---|---|
| `helper/config.py` | INI configuration plus environment overrides |
| `helper/agent.py` | Register, poll, decode, report, clean up |
| `helper/client.py` | HTTP client for the private protocol (stdlib only) |
| `helper/keyprovider.py` | Authorized key sources and the offline decoder |
| `helper/state.py` | DPAPI-protected credentials and the job journal |
| `helper/redact.py` | Removes tokens, EKeys, and private paths from logs |
| `run_helper.py` | Command line entry point |
| `config.example.ini` | Template; copy to `config.ini` |
| `tests/` | Unit suite plus an end-to-end suite against a live server |

The helper needs **no third-party packages**. It reuses the repository decoder at
`mflac-linux-decoder/qmc_mflac.py` by path.

## Setup

1. **Enable the queue on the server.** Set `HELPER_ENROLLMENT_SECRET` in the API
   environment and restart `wechat-converter.service`. Until it is set, every
   helper endpoint returns `503` and `GET /api/v1/health` reports
   `"helper_queue": "disabled"`.

2. **Install Python 3.9+** on the helper host and copy this directory (plus
   `mflac-linux-decoder/qmc_mflac.py`, or set `decoder_path`).

3. **Configure.** Copy `config.example.ini` to `config.ini` and set
   `api_base_url` and `key_source`. Export the enrollment secret so it is not
   stored in the file:

   ```bat
   setx HELPER_ENROLLMENT_SECRET "the-secret-from-the-server-env"
   ```

   An authorized key source is required for PC MusicEx files:

   ```ini
   key_source = ekey_file
   key_source_path = %LOCALAPPDATA%\WeChatPlayerHelper\authorized.ekey
   ```

   `authorized.ekey` must contain only the EKey you are authorized to use. It is
   read at decode time and is never copied, logged, or uploaded. `*.ekey` is
   git-ignored so it cannot be committed by accident.

4. **Validate before running.**

   ```bat
   python run_helper.py --check
   ```

   This prints a secret-free configuration summary, verifies the key source,
   runs the offline decoder's self-test, checks disk space and the optional
   process gate, and confirms the API is reachable.

5. **Register and test one job.**

   ```bat
   python run_helper.py --register
   python run_helper.py --once
   python run_helper.py --status
   ```

6. **Run it unattended.** Use Task Scheduler with an **"At log on"** trigger for
   the interactive user and `python run_helper.py` as the action. A `LocalSystem`
   service is deliberately not supported: reading an authorized QQ Music session
   needs the interactive user session. The helper only makes outbound HTTPS
   connections, so no inbound firewall rule is needed.

## Commands

| Command | Effect |
|---|---|
| `--check` | Validate configuration, key source, decoder, host, and API |
| `--register` | Register (or re-register) this device |
| `--once` | Process at most one job, then exit |
| `--max-jobs N` | Stop after N jobs |
| `--status` | Print device identity, token expiry, queue stats, and journal state |
| `--forget-credentials` | Delete the stored device identity |

## Protocol

All calls are under `/api/v1/helper` and use HTTPS.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /devices/register` | enrollment secret | Get a device id and a revocable token |
| `POST /devices/heartbeat` | device token | Device liveness and queue stats |
| `POST /devices/{id}/revoke` | enrollment secret | Revoke a device |
| `POST /jobs/claim` | device token | Lease one job, or return `null` |
| `POST /jobs/{id}/heartbeat` | device token + lease | Extend the lease |
| `GET /jobs/{id}/source` | device token + lease | Download the encrypted source |
| `POST /jobs/{id}/complete` | device token + lease | Upload the decoded audio |
| `POST /jobs/{id}/fail` | device token + lease | Report failure or timeout |
| `POST /jobs/{id}/release` | device token + lease | Return the job to the queue |
| `GET /jobs/{id}` | device token | Status probe after a restart |

Operator endpoints (`POST /api/v1/helper/jobs` to enqueue, `GET /jobs`,
`POST /jobs/reconcile`) use the enrollment secret. The Mini Program reads results
through `GET /api/v1/jobs/{job_id}?client_id=...`, which is the only place the
download link is returned — **the helper never receives the download token.**

Device tokens and lease tokens travel in the `X-Helper-Token` and
`X-Helper-Lease` headers rather than the URL, so they stay out of proxy access
logs. Only the SHA-256 of a device token is stored server-side.

## Job semantics

A job is `queued -> leased -> succeeded | failed | timeout`, and returns to
`queued` while attempts remain if the lease expires or the helper releases it.

**A job can be completed at most once.** Each lease carries a random token and
reports are conditional on that token, so:

* a repeated success report for an already-finished job is answered
  `status: "duplicate"` and does **not** publish a second result;
* a helper whose lease expired and was reassigned gets `409` instead of
  overwriting the new attempt's result;
* the download token is minted exactly once per job.

The helper relies on this: completion is retried on transient network failures
because a retry is safe. A `claim` whose response is lost is *not* retried, since
that would lease a second job; the abandoned lease is simply reclaimed when it
expires.

## Failure reasons

Reported in the job's `error_message` / `reason` so the Mini Program can show
something useful:

| Reason | Meaning |
|---|---|
| `no_authorized_key_source` | PC MusicEx file and no usable key source — configure `key_source` |
| `decode_failed` | The decoder rejected the file or produced no valid audio |
| `timeout` | The local job deadline was exceeded |
| `network_error` | Transient failure; the lease expires and the server requeues |

## Host preparation

The helper host needs an authorized QQ Music installation and login, a stable
user session, enough temporary disk space, outbound HTTPS access to the API, and
a sleep policy that does not suspend active jobs. The host should stay online
while it owns a job. A single host is a single point of capacity; the server
queue caps attempts per job and hands out one job per claim.

## What the helper does not do

* It does not decrypt on the server, and it does not send a key anywhere.
* It does not use the reference GUI EXE. `KGMusicConverterPro-v1.3.7.exe` is a GUI
  application with no stable unattended command-line interface; it is not a
  drop-in background worker and must not be copied into `miniprogram/`.
* It does not run as a Windows service or accept inbound connections.

## Tests

```bat
python -m unittest discover -s windows-helper/tests -t windows-helper/tests -v
```

The unit suite fakes the network. `test_helper_integration.py` starts a real API
server on loopback and drives it with the real client, covering registration,
claim, download, decode, upload, duplicate handling, revocation, and the mini
program download. Only the offline QMC decode is substituted, because a genuine
PC MusicEx decode requires a legal EKey.
