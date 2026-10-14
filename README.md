# WeChatPlayer

微信小程序文件转换服务：手机上传文档或音视频，服务端转换后返回可下载链接。

项目分为两部分——**FastAPI 服务端**与**微信小程序客户端**：小程序上传文件，服务端用 LibreOffice 或 FFmpeg 转换，返回下载链接并记录历史。

**技术栈**：Python · FastAPI · MySQL · 微信小程序（原生 WXML/WXSS/JS）· Nginx · systemd · Docker · FFmpeg · LibreOffice

---

## 目录

- [功能](#功能)
- [架构与数据流](#架构与数据流)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [测试](#测试)
- [配置](#配置)
- [API](#api)
- [部署](#部署)
- [安全设计](#安全设计)
- [已知限制](#已知限制)
- [设计文档](#设计文档)

---

## 功能

- **文档转 PDF**：`doc` `docx` `odt` `ott` `rtf` `txt`
- **音视频转码**：`mp3` `wav` `flac` `ogg` `opus` `aac` `m4a` `mp4` `webm` `mkv` `mov` `avi`
- **加密容器解密**：`ncm`、`mflac`（限于密钥内嵌于文件的格式，见[已知限制](#已知限制)）
- **大文件上传**：单文件上限默认 1 GiB，可配置
- **转换历史与云盘**：按客户端 ID 保存记录，支持重命名、删除、有效期管理
- **健康检查与部署自检**：`/api/v1/health`、`/api/v1/test`
- **OpenAPI 文档**：`/docs`、`/openapi.json`

---

## 架构与数据流

```
┌─────────────────┐
│  微信小程序      │  选择文件 / 上传 / 下载 / 播放
└────────┬────────┘
         │ HTTPS
┌────────▼────────┐
│  Nginx          │  TLS 终止、反向代理
└────────┬────────┘
         │ 127.0.0.1:18080
┌────────▼────────────────────────────┐
│  FastAPI 服务端                      │
│  ├── 转换：LibreOffice / FFmpeg      │
│  └── 存储：MySQL（记录、历史、云盘）   │
└─────────────────────────────────────┘
```

上传后按文件类型分派给对应转换器，结果写入独立的任务目录，并以带有效期的令牌对外提供下载。转换是**同步完成**的：服务端能处理的文件直接返回结果，处理不了的文件返回错误，不会留下一个永远等不到结果的任务。

---

## 项目结构

```text
.
├── converter-api/            FastAPI 服务端
│   ├── app/
│   │   ├── main.py           应用主体、转换流程、业务 API
│   │   └── mflac_fallback.py MFLAC 容器判定与掩码回退
│   ├── deploy/               systemd 与 Nginx 模板
│   ├── requirements.txt
│   └── README.md
│
├── miniprogram/              微信小程序
│   ├── pages/index/          工作台、历史、播放
│   ├── config.js             读取本地覆盖的 API 地址
│   ├── config.example.js     本地覆盖模板
│   └── README.md
│
├── mflac-linux-decoder/      离线 QMC/MFLAC 解码器（可独立运行）
├── tools/                    开发工具与前端流程测试
└── docs/decisions/           技术决策记录
```

---

## 快速开始

### 依赖

- Python 3.10+
- LibreOffice（文档转 PDF）、FFmpeg（音视频转码）
- MySQL 8（可选；未配置时使用内存存储）
- Node.js（仅用于前端流程测试）

### 启动服务端

```bash
cd converter-api
python -m venv .venv

# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt

# 复制并按需修改配置（至少改数据库密码）
cp .env.example .env

uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload
```

服务启动后：

| 地址 | 说明 |
| --- | --- |
| `http://127.0.0.1:18080/` | 服务根路径 |
| `http://127.0.0.1:18080/docs` | Swagger 文档 |
| `http://127.0.0.1:18080/api/v1/health` | 健康检查 |
| `http://127.0.0.1:18080/api/v1/test` | 部署自检 |

### 配置小程序

小程序不把 API 地址写进仓库。复制模板并填入自己的地址：

```bash
cp miniprogram/config.example.js miniprogram/config.local.js
# 编辑 config.local.js，写入你的 API 地址
```

`config.local.js` 已被 git 忽略；未创建时回落到占位地址，项目仍可运行。

---

## 测试

前端流程测试使用 **Node.js 原生断言**，不依赖 npm 包：

```bash
# 13 项：桩化 wx 运行时，覆盖上传、结果渲染、历史与错误提示
node tools/miniprogram-flow.test.js
```

它把 `pages/index/index.js` 放进 `vm` 沙箱执行，用一个桩化的 `wx` 对象替换真实运行时，因此在没有小程序环境的情况下也能验证页面逻辑。守住的行为包括：

- 音频与视频结果**不会**被交给 `wx.openDocument`（该 API 只支持文档和 PDF），而是走播放器
- 下载失败时给出提示，而不是静默失败
- 用户拒绝隐私授权时明确提示原因；主动取消则保持安静
- 历史记录里的失败行如实呈现为失败

这些检查由 GitHub Actions 在每次 push 与 pull request 时自动执行（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)），另有镜像构建任务。

### 镜像构建任务

CI 不只构建镜像——它把镜像**跑起来并轮询** `/api/v1/health`，直到拿到响应才算通过，失败时自动打印容器日志。因为**能构建不代表能运行**。

---

## 配置

### 服务端（环境变量，见 `converter-api/.env.example`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAX_UPLOAD_BYTES` | `1073741824` | 单文件上限（1 GiB） |
| `CONVERSION_TIMEOUT_SECONDS` | `600` | 单次转换超时 |
| `MAX_CONCURRENT_JOBS` | `1` | 并发转换数 |
| `DOWNLOAD_LINK_TTL_SECONDS` | `900` | 下载链接有效期 |
| `CONVERTER_TMP_ROOT` | `/var/lib/wechat-converter/tmp` | 临时目录 |
| `CORS_ALLOW_ORIGINS` | `*` | 允许的来源，逗号分隔 |
| `MYSQL_HOST` / `MYSQL_PORT` | `127.0.0.1` / `3306` | 数据库地址 |
| `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD` | — | 数据库凭据 |

**未配置 MySQL 时**服务端使用内存存储，重启即丢失记录——适合本地开发，不适合生产。

---

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 健康检查，含数据库状态 |
| `GET` | `/api/v1/test` | 部署自检，返回能力列表 |
| `POST` | `/api/v1/convert/document-to-pdf` | 文档转 PDF |
| `POST` | `/api/v1/convert/media` | 音视频转码 |
| `GET` | `/api/v1/history/{client_id}` | 转换历史 |
| `DELETE` | `/api/v1/history/{client_id}/{token}` | 删除历史记录 |
| `GET` | `/api/v1/cloud-files/{client_id}` | 云盘列表 |
| `POST` | `/api/v1/cloud-files/{client_id}/{token}` | 保存到云盘 |
| `PATCH` | `/api/v1/cloud-files/{client_id}/{token}` | 重命名云盘文件 |
| `DELETE` | `/api/v1/cloud-files/{client_id}/{token}` | 从云盘删除 |
| `GET` | `/api/v1/download/{token}` | 下载结果 |

### 示例

```bash
# 文档转 PDF
curl -f -F "file=@sample.docx" \
  http://127.0.0.1:18080/api/v1/convert/document-to-pdf \
  -o sample.pdf

# 音视频转码
curl -f -F "file=@sample.mp4" -F "target_format=mp3" \
  http://127.0.0.1:18080/api/v1/convert/media \
  -o sample.mp3
```

### 状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 成功 |
| `400` | 目标格式或 `response_mode` 参数无效 |
| `413` | 超过上传大小限制 |
| `415` | 不支持的文件类型 |
| `422` | 转换失败、超时或未生成输出 |
| `429` | 已达并发上限 |
| `500` | 未预期的服务端错误 |

---

## 部署

`converter-api/deploy/` 提供 systemd 与 Nginx 模板，约定：

1. 应用部署到 `/opt/wechat-converter-api`，在该目录创建 `.venv`
2. 以专用低权限用户运行 Uvicorn，监听 `127.0.0.1:18080`
3. Nginx 对外提供 HTTPS，并将 `/api/v1/`、`/docs`、`/openapi.json` 反向代理到应用
4. `/var/lib/wechat-converter` 保存临时文件，需授予运行用户读写权限

```bash
sudo apt-get install -y python3 python3-venv libreoffice ffmpeg nginx
sudo useradd --system --home /var/lib/wechat-converter --shell /usr/sbin/nologin converter
sudo mkdir -p /opt/wechat-converter-api /var/lib/wechat-converter
sudo chown -R converter:converter /opt/wechat-converter-api /var/lib/wechat-converter

cd /opt/wechat-converter-api/converter-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp deploy/wechat-converter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-converter
```

数据库只监听本机回环地址；需要从外部管理时使用 SSH 隧道，不要把数据库端口暴露到公网：

```bash
ssh -N -L 13306:127.0.0.1:3306 user@your-server
# 然后本地连接 127.0.0.1:13306
```

### Docker / Docker Compose

`converter-api/Dockerfile` 采用两阶段构建：依赖装在 builder 的独立前缀里，只有该前缀被复制进运行镜像，构建工具不进最终产物。运行镜像以非 root 用户启动，并预装 CJK 字体（缺字体会导致中文文档转出的 PDF 全是空白方块）。

服务端与 MySQL 可用 Compose 一并启动：

```bash
cp .env.example .env      # 填入数据库密码
docker compose up --build
```

Compose 中有两处刻意的设计，都是安全考虑：

- **MySQL 不发布任何主机端口**——api 容器经 Compose 网络以服务名 `mysql` 连接，数据库无需对外可达。确实需要图形客户端时，只绑回环：`127.0.0.1:3306:3306`，**不要写 `0.0.0.0`**。
- **api 发布到 `127.0.0.1:18080`** 而非 `18080`。省略主机 IP 等同于 `0.0.0.0`，会把服务暴露到所在网络。

> 逐行讲解与 CI 说明见 [`docs/docker-and-ci.md`](docs/docker-and-ci.md)。

---

## 安全设计

- **日志脱敏**：密钥与本地路径在写日志前替换为占位符
- **临时文件**：每个请求独立目录，响应结束后清理；下载目录在 POSIX 上应用 `0700`
- **专用低权限用户**：转换会调用外部程序，不以 root 运行
- **默认拒绝**：Nginx 只放行 `/api/v1/` 等已知前缀，其余路径返回 404

**不要提交**凭据、私钥、证书或真实用户媒体。相关文件已加入 `.gitignore`。

---

## 已知限制

**PC 端加密的 MFLAC 文件无法解密。** 这是当前最主要的限制，说明如下。

QQ 音乐的 MFLAC 容器分两类：密钥内嵌于文件本身的（如 QMC0/QMC2），以及依赖外部密钥的（PC `PcV2MusicEx`/`MusicEx`）。后者**密钥不在文件内**，由服务端在账号授权后下发，任何纯离线的方法都无法恢复它。

因此：

- 密钥内嵌的容器（`ncm`、QMC 系列）可以正常解密
- PC MusicEx 文件会以**可读的错误**结束，而不是无限等待
- 客户端明确提示该文件暂不支持，并说明可用格式

项目**不包含也不计划包含**绕过 DRM 的密钥提取功能。需要处理这类文件时，请在具备合法授权的环境下自行导出。

其他限制：

- 未配置 MySQL 时记录仅存内存，重启丢失
- 单机部署，未做分布式任务分片
- 大文件转换占用 CPU 与内存，并发数需按机器规格调整

---

## 设计文档

关键决策都记录在 `docs/decisions/`，包含背景、备选方案与取舍原因：

| 文档 | 内容 |
| --- | --- |
| [001-encrypted-audio.md](docs/decisions/001-encrypted-audio.md) | 加密音频格式的处理范围与边界 |
| [002-large-upload.md](docs/decisions/002-large-upload.md) | 大文件上传与临时目录策略 |

---

## 许可

未附加开源许可证。仓库公开用于展示与交流，如需使用请先联系作者。
