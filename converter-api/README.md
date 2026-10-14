# WeChat Converter API

This service exposes short-lived conversion responses for a WeChat mini program.
Uploaded files are written to a per-request temporary directory and removed after the response is sent.

## Endpoints

- `GET /api/v1/test`: standalone deployment smoke test and capability list.
- `GET /api/v1/health`: health check. It reports the database state in its body.
- `POST /api/v1/convert/document-to-pdf`: multipart field `file`; supports `doc`, `docx`, `odt`, `ott`, `rtf`, and `txt`.
- `POST /api/v1/convert/media`: multipart fields `file` and `target_format`; supports `mp3`, `wav`, `flac`, `ogg`, `opus`, `aac`, `m4a`, `mp4`, `webm`, `mkv`, `mov`, and `avi`. Encrypted `mflac` and `ncm` inputs are decoded before conversion.
- Both conversion endpoints accept `response_mode=link` for clients that upload and download in separate requests. The response contains a download URL guarded by a token that expires after 15 minutes by default. Omit the field or use `response_mode=file` to retain the direct file response.
- `GET /api/v1/download/{token}`: downloads a converted result created with `response_mode=link`; the temporary result is deleted after download or expiry.

## Encrypted containers

`ncm` and the QMC containers whose key is embedded in the file are decoded by the
service itself. A container whose per-file key is issued by the vendor to an
authorized client — such as PC `PcV2MusicEx` — carries no key in the file, so no
offline method can open it. Those uploads fail with `422` and a readable detail
rather than being accepted and left pending.

See [docs/decisions/001-encrypted-audio.md](../docs/decisions/001-encrypted-audio.md)
for the decision and its boundaries.

## Tests

The service has no Python test suite of its own; the mini program flow tests
(`node tools/miniprogram-flow.test.js`) and the container build in CI are what
cover this code. CI builds the image, starts it, and polls `/api/v1/health`.

## Example requests

```bash
curl http://SERVER_IP/api/v1/test
curl -f -F file=@sample.docx http://SERVER_IP/api/v1/convert/document-to-pdf -o sample.pdf
curl -f -F file=@sample.mp4 -F target_format=mp3 http://SERVER_IP/api/v1/convert/media -o sample.mp3
```

The OpenAPI schema is available at `/docs` and `/openapi.json`.
