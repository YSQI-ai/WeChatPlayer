# WeChatPlayer Project

## 目标

微信小程序负责选择文件、上传、结果展示以及本地和云端文件管理；FastAPI 负责文档/媒体转换、加密音频解码、临时结果、下载链接、历史记录和云盘审计。

## 模块边界

- `miniprogram/`：原生 JavaScript、WXML、WXSS 和微信 API。
- `converter-api/app/main.py`：FastAPI 路由、任务生命周期、转换和历史/云盘接口。
- `converter-api/app/mflac_fallback.py`：服务端 MFLAC fallback 适配；没有密钥时不能解码 PC MusicEx。
- `converter-api/app/helper_api.py`：`/api/v1/helper` 私有协议与小程序的 `GET /api/v1/jobs/{job_id}`。
- `converter-api/app/helper_store.py`：helper 设备注册表与任务队列状态机；MySQL 与进程内两套实现共用同一组原子原语。
- `mflac-linux-decoder/`：不依赖 pip 的离线 QMC/MFLAC 解码工具，支持显式合法 EKey 或 Android `player_process_db`。
- `windows-helper/`：Windows helper 实现（协议客户端、授权密钥来源、日志脱敏、DPAPI 凭据）；只上传解密后的 FLAC。
- `converter-api/deploy/`：systemd 和 OpenResty/Nginx 模板。

## 数据流

小程序生成持久化 `clientId`，通过 multipart 上传。后端按 1 MiB 分块写入独立任务目录；文档交给 LibreOffice，普通媒体交给 FFmpeg，MFLAC/NCM 先解密。结果可直接返回或以短期下载链接返回。历史、metadata 和云盘审计使用 MySQL；本地微信私有文件与云端文件是两套生命周期。

`PcV2MusicEx` 无法在服务端解码时，源文件进入 helper 队列：Windows helper 在交互式会话内用操作者授权的本地密钥来源解密，只把 FLAC 上传回服务端。EKey 不进入服务端、数据库、URL 或日志。helper 结果复用普通下载目录，因此历史、过期和云盘保存逻辑无需第二套实现。

## 接口概览

- `POST /api/v1/convert/document-to-pdf`
- `POST /api/v1/convert/media`
- `GET /api/v1/download/{token}`
- `GET /api/v1/history/{client_id}`
- `GET|POST|PATCH|DELETE /api/v1/cloud-files/...`
- `GET /api/v1/helper/...`（私有 helper 协议，需 `HELPER_ENROLLMENT_SECRET`）
- `GET /api/v1/jobs/{job_id}?client_id=...`（小程序查询 helper 任务状态）
- `GET /api/v1/health`、`GET /api/v1/test`

媒体目标格式包括 `mp3`、`wav`、`flac`、`ogg`、`opus`、`aac`、`m4a`、`mp4`、`webm`、`mkv`、`mov`、`avi`；输入额外支持 `mflac` 和 `ncm`。

## 部署基线

- 正式 API：`https://api.yssqi.ccwu.cc`。
- 服务目录：`/opt/wechat-converter-api`；systemd 服务：`wechat-converter.service`。
- Uvicorn 监听 `127.0.0.1:18080`；OpenResty 只代理 API、docs 和 openapi。
- 服务器已配置 1 GiB 上传上限、关闭代理请求 buffering、1800 秒读写超时和并发 1。
- QMC2 与 ncmdump 位于服务器 `/opt/wechat-converter-api/bin/`，二进制不写入 Git。
- Adminer `127.0.0.1:9853`、phpMyAdmin `127.0.0.1:8089` 只允许 SSH 隧道访问。
- 微信 request、uploadFile、downloadFile 合法域名均为 `https://api.yssqi.ccwu.cc`。

部署变更后按需检查：

```text
systemctl is-active wechat-converter.service
curl -fsS http://127.0.0.1:18080/api/v1/health
curl -fsS https://api.yssqi.ccwu.cc/api/v1/health
curl -fsS https://api.yssqi.ccwu.cc/api/v1/test
```

## 相关决策

- [001-encrypted-audio.md](docs/decisions/001-encrypted-audio.md)
- [002-large-upload.md](docs/decisions/002-large-upload.md)
- [003-context-workflow.md](docs/decisions/003-context-workflow.md)
- [004-helper-protocol.md](docs/decisions/004-helper-protocol.md)
