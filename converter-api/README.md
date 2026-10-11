# WeChat Converter API

This service exposes short-lived conversion responses for a WeChat mini program.
Uploaded files are written to a per-request temporary directory and removed after the response is sent.

## Endpoints

- `GET /api/v1/test`: standalone deployment smoke test and capability list.
- `GET /api/v1/health`: health check.
- `POST /api/v1/convert/document-to-pdf`: multipart field `file`; supports `doc`, `docx`, `odt`, `ott`, `rtf`, and `txt`.
- `POST /api/v1/convert/media`: multipart fields `file` and `target_format`; supports `mp3`, `wav`, `flac`, `ogg`, `opus`, `aac`, `m4a`, `mp4`, `webm`, `mkv`, `mov`, and `avi`. Encrypted `mflac` and `ncm` inputs are decoded before conversion.
- Both conversion endpoints accept `response_mode=link` for clients that upload and download in separate requests. The response contains a one-time download URL that expires after 15 minutes by default. Omit the field or use `response_mode=file` to retain the direct file response.
- `GET /api/v1/download/{token}`: downloads a converted result created with `response_mode=link`; the temporary result is deleted after download or expiry.

## Private helper queue

`PcV2MusicEx` MFLAC files have no embedded EKey, so the Linux service cannot
decode them. The private helper protocol lets an authorized Windows helper decode
such a file locally and upload only the finished FLAC; no EKey reaches this
service. See [docs/decisions/004-helper-protocol.md](../docs/decisions/004-helper-protocol.md)
and [windows-helper/README.md](../windows-helper/README.md).

The whole surface returns `503` unless `HELPER_ENROLLMENT_SECRET` is set. Job and
device state prefers MySQL and falls back to in-process state only for local
single-worker runs.

- `POST /api/v1/helper/devices/register`: enrollment secret; returns a device id and a revocable token.
- `POST /api/v1/helper/devices/heartbeat`, `POST /api/v1/helper/devices/{id}/revoke`, `GET /api/v1/helper/devices`.
- `POST /api/v1/helper/jobs`: enrollment secret; enqueue an encrypted source the service cannot decode itself.
- `POST /api/v1/helper/jobs/claim`, `POST /api/v1/helper/jobs/{id}/heartbeat`, `GET /api/v1/helper/jobs/{id}/source`, `POST /api/v1/helper/jobs/{id}/complete`, `POST /api/v1/helper/jobs/{id}/fail`, `POST /api/v1/helper/jobs/{id}/release`, `GET /api/v1/helper/jobs/{id}`.
- `GET /api/v1/helper/jobs`, `POST /api/v1/helper/jobs/reconcile`: enrollment secret; operator queue view and lease reconciliation.
- `GET /api/v1/jobs/{job_id}?client_id=...`: Mini Program facing status; returns the download link once the helper finishes.

Device and lease tokens travel in the `X-Helper-Token` and `X-Helper-Lease`
headers so they stay out of proxy access logs.

## Tests

```bash
python -m unittest discover -s converter-api/tests -t converter-api/tests -v
```

## Example requests

```bash
curl http://SERVER_IP/api/v1/test
curl -f -F file=@sample.docx http://SERVER_IP/api/v1/convert/document-to-pdf -o sample.pdf
curl -f -F file=@sample.mp4 -F target_format=mp3 http://SERVER_IP/api/v1/convert/media -o sample.mp3
```

The OpenAPI schema is available at `/docs` and `/openapi.json`.
