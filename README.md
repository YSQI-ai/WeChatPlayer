# WeChatPlayer

微信小程序文件转换服务：手机上传文档或音视频，服务端转换后返回可下载链接。

项目分为三个部分——**FastAPI 服务端**、**微信小程序客户端**、**Windows 解密助手**——它们组合成一条完整的链路：小程序上传文件，服务端直接转换；当遇到服务端无法解密的容器时，任务进入私有队列，由用户自己电脑上的助手领取、解密、回传。

**技术栈**：Python · FastAPI · MySQL · 微信小程序（原生 WXML/WXSS/JS）· Nginx · systemd · Docker · FFmpeg · LibreOffice

---

## 目录

- [功能](#功能)
- [架构与数据流](#架构与数据流)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [测试](#测试)
- [私有任务队列](#私有任务队列)
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
- **私有任务队列**：服务端无法处理的文件可转交本机助手，见下
- **健康检查与部署自检**：`/api/v1/health`、`/api/v1/test`
- **OpenAPI 文档**：`/docs`、`/openapi.json`

---

## 架构与数据流

```
┌─────────────────┐
│  微信小程序      │  选择文件 / 上传 / 轮询 / 播放
└────────┬────────┘
         │ HTTPS
┌────────▼────────┐
│  Nginx          │  TLS 终止、反向代理
└────────┬────────┘
         │ 127.0.0.1:18080
┌────────▼────────────────────────────┐
│  FastAPI 服务端                      │
│  ├── 转换：LibreOffice / FFmpeg      │
│  ├── 存储：MySQL（记录、历史、云盘）   │
│  └── 队列：helper_jobs / helper_devices │
└────────┬────────────────────────────┘
         │ 服务端无法解密的容器 → 入队，返回 202 + job_id
         │
┌────────▼──────────────┐
│  Windows 助手          │  用户自己的电脑
│  领取 → 下载 → 解密 → 回传 │
└───────────────────────┘
```

**两条路径的差别**：

| 场景 | 响应 |
| --- | --- |
| 服务端能自行处理 | `200` + 文件流，直接返回结果 |
| 需要本机解密 | `202` + `job_id` + `status_url`，客户端轮询直到拿到下载链接 |

队列路径**只在服务端确实解不开之后**才触发（先试内嵌密钥解密，再试掩码回退，都失败才判定），所以原本能转换的文件行为不变。

---

## 项目结构

```text
.
├── converter-api/            FastAPI 服务端
│   ├── app/
│   │   ├── main.py           应用主体、转换流程、业务 API
│   │   ├── helper_api.py     私有队列：路由、设备与任务状态机
│   │   ├── helper_store.py   队列持久化（MySQL / 内存双实现）
│   │   └── mflac_fallback.py MFLAC 容器判定与掩码回退
│   ├── tests/                服务端测试
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
├── windows-helper/           Windows 解密助手
│   ├── helper/
│   │   ├── agent.py          主循环：注册、心跳、领取、回传
│   │   ├── client.py         HTTP 客户端
│   │   ├── keyprovider.py    密钥来源解析
│   │   ├── state.py          本地任务状态
│   │   ├── config.py         配置加载
│   │   └── redact.py         日志脱敏
│   ├── run_helper.py         入口（支持 --check）
│   ├── tests/                助手测试
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

# 复制并按需修改配置（至少改数据库密码与队列密钥）
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

### 运行助手（仅在使用队列时需要）

```bash
cd windows-helper
cp config.example.ini config.ini
# 编辑 config.ini：填写 api_base_url 与注册密钥
python run_helper.py --check     # 先做配置自检
python run_helper.py             # 启动
```

---

## 测试

项目使用 **Python 标准库 `unittest`**，不依赖 pytest；前端测试用 Node.js 原生断言，不依赖 npm 包。Python 测试需先安装依赖：

```bash
pip install -r converter-api/requirements-dev.txt
```

前端流程测试只依赖 Node 内置模块，无需安装任何包。

### 离线测试（91 项）

不需要数据库、不需要运行中的服务。以下命令均从**仓库根目录**执行：

```bash
# 服务端队列协议：36 项
cd converter-api && python -m unittest discover -s tests -p "test_helper_protocol.py"

# 助手单元测试：42 项
cd windows-helper && python -m unittest discover -s tests -p "test_helper_units.py"

# 小程序流程测试：13 项（桩化 wx 运行时，只依赖 Node 内置模块）
node tools/miniprogram-flow.test.js
```

> `cd` 作用于当前终端；若在同一次会话中连续执行，请自行 `cd` 回仓库根目录。

### 集成测试（6 项）

需要一个可达的真实服务端：

```bash
cd windows-helper
python -m unittest discover -s tests -p "test_helper_integration.py"
```

覆盖真实客户端到真实 uvicorn 的完整流程：设备注册、领取任务、下载源文件、解密、上传结果、去重、设备撤销，以及小程序下载链接。

### 持续集成

上述检查由 GitHub Actions 在每次 push 与 pull request 时自动执行（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)），共三个任务：

| 任务 | 运行环境 | 内容 |
| --- | --- | --- |
| 服务端协议测试 | ubuntu-latest | 36 项队列协议测试 |
| 助手单元测试 | **windows-latest** | 42 项助手单元测试——**该组件是 Windows 程序**，用 `tasklist` 查进程、用 DPAPI 保护凭据文件，在 Linux 上测不到真实行为 |
| 小程序流程测试 | ubuntu-latest | 13 项前端流程测试 |
| 镜像构建 | ubuntu-latest | 构建 Docker 镜像、启动容器、**等待健康检查通过**后才算成功 |

镜像构建任务不只看"构建成功"——它会把镜像跑起来并轮询 `/api/v1/health`，失败时自动打印容器日志，因为**能构建不代表能运行**。

### 测试覆盖的关键行为

服务端队列协议测试重点验证状态机的边界情况：

- 并发领取唯一性——多个助手同时领取，**只有一个**能拿到同一个任务
- 租约过期后任务自动重投，不会永久卡死
- **旧租约不能覆盖已被重新分配的任务**（防止重复处理）
- 重复完成只产生一个结果（幂等）
- 设备撤销后无法再领取任务

---

## 私有任务队列

有些加密容器的密钥不在文件里，服务端无法解密。与其直接失败，不如把任务交给用户自己的电脑处理——这是队列存在的理由。

### 设计要点

| 机制 | 取值 | 作用 |
| --- | --- | --- |
| 注册密钥 | `HELPER_ENROLLMENT_SECRET` | 为空时整个 `/api/v1/helper` 返回 503，默认部署不会暴露无认证的工作队列 |
| 任务租约 | 900 秒 | 助手崩溃后任务可被其他助手接管 |
| 心跳间隔 | 60 秒 | 维持租约 |
| 最大尝试次数 | 3 | 超过则标记失败，避免无限重试 |
| 设备令牌 | 90 天 | 长期免重复注册 |

### 为什么需要租约而不是简单加锁

助手可能在任何时刻崩溃、断网或关机。如果只用加锁，任务会永远卡在"已领取"状态。因此领取时发放一个**有期限的租约**：助手需要定期心跳续约，超时未续约则任务自动回到队列。

关键细节是**租约必须带序号**：当任务被重新分配后，原助手如果恢复并回报结果，它的旧租约已经失效，服务端会拒绝这次回报——否则同一个任务可能被处理两次并产生冲突的结果。

### 安全边界

- **服务端永不接收密钥**：助手只在本地解密，只上传解密后的音频文件
- **不把 EKey 写入小程序存储、URL、数据库或普通日志**
- 日志中的密钥与用户路径统一脱敏为 `<redacted>` / `<local-path>`

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
| `HELPER_ENROLLMENT_SECRET` | 空（队列关闭） | 队列注册密钥 |
| `HELPER_LEASE_SECONDS` | `900` | 租约时长 |
| `HELPER_MAX_ATTEMPTS` | `3` | 最大尝试次数 |
| `HELPER_HEARTBEAT_SECONDS` | `60` | 心跳间隔 |

**未配置 MySQL 时**服务端使用内存存储，重启即丢失记录——适合本地开发，不适合生产。

### 助手（`windows-helper/config.ini`）

见 `windows-helper/config.example.ini`。支持 `%LOCALAPPDATA%` 这类环境变量写法（`configparser` 的插值已关闭，路径展开由程序自身负责）。

---

## API

### 业务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 健康检查，含数据库与队列状态 |
| `GET` | `/api/v1/test` | 部署自检，返回能力列表 |
| `POST` | `/api/v1/convert/document-to-pdf` | 文档转 PDF |
| `POST` | `/api/v1/convert/media` | 音视频转码；需解密的文件可能返回 `202` |
| `GET` | `/api/v1/history/{client_id}` | 转换历史 |
| `DELETE` | `/api/v1/history/{client_id}/{token}` | 删除历史记录 |
| `GET` | `/api/v1/cloud-files/{client_id}` | 云盘列表 |
| `POST` | `/api/v1/cloud-files/{client_id}/{token}` | 保存到云盘 / 重命名 |
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

# 需要本机解密时会返回 202
# {"status":"queued","job_id":1,"status_url":"/api/v1/jobs/1?client_id=..."}
```

### 状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 成功 |
| `202` | 已转交本机助手，需轮询 `status_url` |
| `400` | 目标格式参数无效 |
| `413` | 超过上传大小限制 |
| `415` | 不支持的文件类型 |
| `422` | 转换失败、超时或未生成输出 |
| `429` | 已达并发上限 |
| `500` | 未预期的服务端错误 |

### 队列接口

`/api/v1/helper/*` 为私有接口，需要设备令牌；`HELPER_ENROLLMENT_SECRET` 为空时整体返回 `503`。

设备：`POST /devices/register`、`POST /devices/heartbeat`、`POST /devices/{id}/revoke`

任务：`POST /jobs`（入队）、`POST /jobs/claim`（领取）、`POST /jobs/{id}/heartbeat`、`POST /jobs/{id}/complete`、`POST /jobs/{id}/fail`、`POST /jobs/{id}/release`、`GET /jobs/{id}/source`、`POST /jobs/{id}/reconcile`

客户端查询：`GET /api/v1/jobs/{job_id}`

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

- **密钥不落地**：服务端不接收、不存储解密密钥；助手只上传解密后的音频
- **队列默认关闭**：`HELPER_ENROLLMENT_SECRET` 为空时，整个私有接口返回 `503`
- **租约序号校验**：拒绝过期租约的结果回报，避免重复处理
- **日志脱敏**：密钥与本地路径在写日志前替换为占位符
- **临时文件**：每个请求独立目录，响应结束后清理；下载目录在 POSIX 上应用 `0700`
- **专用低权限用户**：转换会调用外部程序，不以 root 运行

**不要提交**凭据、私钥、证书、真实用户媒体或授权密钥文件。相关文件已加入 `.gitignore`。

---

## 已知限制

**PC 端加密的 MFLAC 文件无法解密。** 这是当前最主要的限制，说明如下。

QQ 音乐的 MFLAC 容器分两类：密钥内嵌于文件本身的（如 QMC0/QMC2），以及依赖外部密钥的（PC `PcV2MusicEx`/`MusicEx`）。后者**密钥不在文件内**，必须由外部来源提供，例如本地密钥库导出文件，或用户自己合法持有的密钥。

因此：

- 队列基础设施、Windows 助手、任务状态机**均已实现并通过测试**
- 但对 PC MusicEx 文件，助手会判定无可用密钥来源，任务以可读错误结束
- 客户端不再承诺"助手正在处理"——界面明确提示该文件暂不支持，并说明可用格式

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
| [003-context-workflow.md](docs/decisions/003-context-workflow.md) | 项目上下文与协作流程 |
| [004-helper-protocol.md](docs/decisions/004-helper-protocol.md) | 私有队列协议：租约、心跳、幂等与安全边界 |

---

## 许可

未附加开源许可证。仓库公开用于展示与交流，如需使用请先联系作者。
