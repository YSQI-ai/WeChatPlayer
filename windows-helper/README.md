# Windows MFLAC helper

This directory is outside `miniprogram/` and is not part of the WeChat Mini
Program upload. Keep Windows-only binaries, local runtime files, and helper
configuration here.

## Role

The helper is only needed for QQ Music `PcV2MusicEx` MFLAC files whose EKey is
not embedded in the file. The Linux API cannot recover that key from the file
alone. A Windows helper must run in a user-authorized QQ Music environment and
either:

1. produce a standard FLAC and upload that FLAC to the Linux API, or
2. send a short-lived, authorized EKey to a private conversion endpoint.

The second option requires an authenticated private channel and must never put
the EKey in Mini Program storage, URLs, database records, or ordinary logs.

## Current binary

`KGMusicConverterPro-v1.3.7.exe` remains in the workspace root because it was
provided separately. Do not copy it into `miniprogram/` or commit it as part of
the Mini Program source. The current executable is a GUI application; this
directory does not claim that it has a stable unattended command-line API.

## Linux side

The server already contains the Linux conversion service and the standard
library decoder in `mflac-linux-decoder/qmc_mflac.py`. Files with an embedded
EKey can be decoded on Linux. `PcV2MusicEx` files still require a key or a
client-side decrypted FLAC.

Keep any local helper settings, task state, and credentials in this directory
or in the OS credential store. Do not place them under `miniprogram/`.

## Required helper-agent capabilities

The production helper is a separate agent and is not the reference GUI EXE.
It must provide all of the following before it is used with the Mini Program:

- Start automatically after the Windows user logs in.
- Run in the interactive user session when it needs to inspect an authorized
  QQ Music process. A `LocalSystem` Windows service is not suitable for that
  mode.
- Keep an outbound HTTPS connection to the Linux API. The helper should not
  require an inbound firewall port or public listener.
- Register a device and authenticate with a revocable helper token.
- Poll for pending jobs, claim one job at a time, and report heartbeats,
  success, failure, and timeout states.
- Download the source MFLAC, obtain the authorized key or produce a standard
  FLAC, and upload only the result required by the server workflow.
- Retry transient network failures without duplicating a completed job.
- Remove source files, temporary keys, and decrypted audio after completion or
  failure.
- Redact keys and user media paths from logs, and never put an EKey in Mini
  Program storage, URLs, database records, or ordinary log messages.
- Stop claiming work when QQ Music is not running, the user is logged out, the
  helper token is revoked, or the local disk is low.

## Host preparation

The helper host needs an authorized QQ Music installation and login, a stable
user session, enough temporary disk space, outbound HTTPS access to the API,
and a sleep policy that will not suspend active jobs. The host must remain
online while it owns a server job. A single helper host is also a single point
of capacity; configure a queue and explicit concurrency limits on the Linux
side before adding more hosts.

## Integration status

The Linux API currently has the normal upload and conversion endpoints, but it
does not yet expose the private helper job queue, device registration, or
result callback described above. The reference GUI EXE is therefore not a
drop-in background worker. Implement and test that protocol before starting a
production helper.
