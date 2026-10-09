# WeChatPlayer 项目上下文

更新时间：2026-10-09（Asia/Shanghai）

本文是当前项目的交接基线。若旧聊天记录与本文或实际代码、服务器状态冲突，以实际状态和本文前部为准。本文不记录 SSH 密码、数据库密码、私钥或其他可直接登录的秘密。

## 1. 技术栈

- 客户端：微信小程序原生开发；JavaScript、WXML、WXSS、微信小程序 API。
- 文件传输：wx.chooseMessageFile、wx.uploadFile、wx.downloadFile、wx.saveFile、wx.openDocument。
- 后端：Python 3、FastAPI、Uvicorn、Pydantic、python-multipart。
- 文档转换：LibreOffice headless，将 doc、docx、odt、ott、rtf、txt 转为 PDF。
- 媒体转换：FFmpeg，支持常见音频、视频格式和 FLAC 输出。
- 加密音频解码：C++/CMake 构建的 pushbox/qmc2（MFLAC/MGG 解密）和 taurusxin/ncmdump（NCM 解密）。
- 数据库：MySQL，通过 PyMySQL 和 utf8mb4 连接；Redis 容器作为基础设施保留，转换记录主要写入 MySQL。
- 部署：阿里云 ECS、Ubuntu 26.04、1Panel、Docker、systemd。
- 入口与安全：OpenResty/Nginx、TLS 1.2/1.3、HTTPS、HTTP 到 HTTPS 跳转、HSTS、nosniff、Referrer-Policy。
- 检查工具：Git、Node.js node --check、Python py_compile、git diff --check、CMake、G++、System.Drawing。

## 2. 当前状态

- 工作区：D:\gemini\WeChatPlayer。
- 当前分支：main。
- 正式 API：https://api.yssqi.ccwu.cc，DNS 指向 8.136.99.105。
- 小程序配置：miniprogram/config.js 使用正式 HTTPS API 地址。
- 后端服务：wechat-converter.service，监听 127.0.0.1:18080，Uvicorn 单 worker。
- API 健康检查已验证返回 200，数据库状态为 MySQL。
- Adminer：127.0.0.1:9853；phpMyAdmin：127.0.0.1:8089；两者只通过 SSH 隧道访问。
- MFLAC/NCM 解码器已部署，服务已重启并处于 active 状态。
- 服务器环境已验证 MAX_UPLOAD_BYTES=1073741824、MAX_CONCURRENT_JOBS=1。

## 3. 处理流程与数据

1. 小程序生成并持久化设备级 clientId，上传时通过 multipart client_id 传给后端。
2. 后端将上传内容按 1 MiB 分块写入独立任务目录，由 MAX_UPLOAD_BYTES 限制大小。
3. 文档由 LibreOffice 转 PDF；普通媒体由 FFmpeg 转换；mflac 先由 QMC2 解密，ncm 先由 ncmdump 解密，再按目标格式处理。
4. 结果以文件流或下载链接返回。链接模式的结果存放在：
   /var/lib/wechat-converter/tmp/download-<token>/
   目录包含实际输出文件和 metadata.json。
5. metadata 记录 client_id、文件名、媒体类型、创建时间、大小和云盘状态。
6. conversion_records 记录转换；cloud_file_events 记录云盘 CREATE、READ、UPDATE、DELETE。
7. 小程序本地文件使用微信私有持久路径管理，与云端文件是两套位置。

## 4. 后端能力与接口

代码：converter-api/app/main.py

- 文档：POST /api/v1/convert/document-to-pdf。
- 媒体：POST /api/v1/convert/media。
- 目标格式：mp3、wav、flac、ogg、opus、aac、m4a、mp4、webm、mkv、mov、avi。
- 媒体输入额外支持：mflac、ncm。
- 下载：GET /api/v1/download/{token}。
- 历史：GET /api/v1/history/{client_id}，最多 100 条，兼容 files 和 items。
- 云盘：GET、POST、PATCH、DELETE /api/v1/cloud-files/...。
- 服务检查：GET /api/v1/health、GET /api/v1/test。
- 输出文件名可编辑；后端清洗路径分隔符和非法字符，并强制追加正确扩展名。
- link 响应包含 download_url、filename、media_type、size_bytes、size_text、expires_in、cloud_saved。
- CORS 已允许 GET、POST、PATCH、DELETE、OPTIONS。

### 解码器

- QMC2：/opt/wechat-converter-api/bin/QMC2-decoder。
  - 仓库：https://github.com/pushbox/qmc2。
  - 构建提交：7d3a4cf6b2cca394884fc90afc825e50f3c44bd7。
  - SHA-256：cc1a2b25f1910959966b9fb33ab42a58a27ab9eac8454f83641a6a080005c9fe。
- ncmdump：/opt/wechat-converter-api/bin/ncmdump。
  - 版本：1.5.1，Linux x86_64 官方发布包。
  - SHA-256：1fb62a06487c1c2a4bcc4bdf61bd9191e6dd83dfd61cd3425b56a4379f916eea。
- 后端默认路径可由 QMC2_BIN、NCDUMP_BIN 环境变量覆盖。
- 二进制权限为 755，不写入 Git。

## 5. 小程序现状

主要文件：

- miniprogram/pages/index/index.js
- miniprogram/pages/index/index.wxml
- miniprogram/pages/index/index.wxss
- miniprogram/config.js

已完成：

- 文档/媒体模式切换，媒体目标格式包含 FLAC。
- 文件选择支持普通媒体、MFLAC、NCM；选择 MFLAC/NCM 后默认输出 FLAC。
- 前端单文件检查和界面提示已改为 1 GB。
- 输出文件名编辑、非法字符校验、结果文件名持久化。
- PDF 打开、媒体预览、本地保存、重复保存识别、本地删除。
- 云端历史刷新、云盘保存、列表、重命名、删除。
- 设备 clientId 持久化到 converter_client_id。
- 底部导航固定在安全区，保留工作台、记录、帮助、我的文字标签。
- 用户提供的导航图标已裁剪为透明 PNG，位于 miniprogram/assets/nav/：
  - workspace.png
  - history.png
  - help.png
  - profile.png

## 6. 服务器与反向代理

- SSH 主机：root@8.136.99.105。凭据只从本机安全凭据或 SSH 配置获取，不写入本文。
- 服务目录：/opt/wechat-converter-api。
- systemd 服务：wechat-converter.service。
- 临时目录：/var/lib/wechat-converter/tmp。
- 代理配置：服务器 /opt/1panel/www/conf.d/wechat-converter.conf；仓库模板 converter-api/deploy/wechat-converter.conf。
- 443 仅代理 /api/v1/、/docs、/openapi.json，其他路径返回 404。
- 当前代理上传限制为 1g，关闭请求 buffering，读写超时为 1800 秒。
- 证书位于服务器 OpenResty SSL 目录；私钥权限 600，证书和私钥内容不得写入 Git 或聊天。

### 微信合法域名

以下三个字段都填写同一个 origin，不带路径、IP 或端口：

~~~
https://api.yssqi.ccwu.cc
~~~

- request 合法域名
- uploadFile 合法域名
- downloadFile 合法域名

socket、UDP、TCP、DNS 预解析和 preconnect 当前留空。

### 管理工具隧道

~~~
ssh -N -L 9853:127.0.0.1:9853 -L 8089:127.0.0.1:8089 root@8.136.99.105
~~~

Adminer 使用 MySQL/MariaDB、Server 1Panel-mysql-BHnV、Database/Username wechat_converter；密码只从服务器 .env 读取。

### 安全组

- 保留 80、443。
- 22 尽量限制为固定管理 IP。
- 不开放 18080、3306、6379。
- 9853、8089 已绑定 localhost，旧的公网放行规则仍应删除或限制为固定管理 IP。

## 7. 大文件负载与策略

1 GiB 文件会增加客户端到网关的持续带宽占用，同时占用临时磁盘；解密和 FFmpeg 转码会增加 CPU。

当前已采取：

- 后端分块写入，避免把整个上传文件读入内存。
- Nginx 关闭请求 buffering，减少网关额外等待。
- 服务器并发限制为 1，适配约 1.6 GiB 内存和约 23 GiB 可用磁盘。
- 任务结束或失败清理任务目录；云盘文件不参与临时清理。
- MFLAC/NCM 解密后如果目标就是 FLAC，直接复制，避免二次编码。

微信 wx.uploadFile 和平台版本对单文件大小可能存在独立限制。目前只验证了后端和网关允许 1 GiB，尚未用开发者工具或真机确认平台上限。若真机限制低于 1 GiB，应采用分片上传、断点续传、服务端合并和最终哈希校验。

## 8. 已完成验证

- node --check miniprogram/pages/index/index.js 通过。
- python -m py_compile converter-api/app/main.py 通过。
- git diff --check 通过。
- WXML view 开闭数量 117/117，block 开闭数量 12/12。
- nginx -t 通过，wechat-converter.service 为 active。
- 外部 health 返回 200，报告 1 GiB 上限和 MySQL 状态。
- 外部 test 已报告 mflac、ncm 输入和 FLAC 输出能力。
- ncmdump 使用仓库 test/test.ncm 成功生成 FLAC。
- API 上传 NCM 并下载结果成功，输出为有效 FLAC，大小 478070 字节。
- 四个导航 PNG 均为 300x300，透明角点 alpha 为 0。
- 既有转换、云盘保存/列表/重命名/删除、历史和 MySQL 审计流程已完成冒烟验证。
- 2026-10-09 使用用户提供的 `cadode - 光の暈.mflac` 做服务器端到端回归：上传链路正常，QMC2 返回 `unknown encryption method`，HTTP 422；问题定位为当前 QMC2 不支持该 MFLAC 加密变体，不是上传或 FFmpeg 错误。
- 后端 `_run_command` 现在把转换器退出码和 stderr 尾部（最多 2000 字符）写入 `error_message`，历史接口同时返回失败记录的原始文件名；修复已部署到 `/opt/wechat-converter-api`，`wechat-converter.service` active，内网 health 返回 200。
- 小程序历史页现支持记录展开：失败记录显示转换日志；成功记录放大显示文件名，并提供打开文件、保存到云端按钮；转换接口返回 4xx/5xx 后会自动刷新历史列表。

## 9. 当前待办与限制

- 接入支持当前 QQ 音乐 MFLAC 加密变体的无交互服务端解码器，并确认所需密钥/授权来源；现有 QMC2 只能明确报告 `unknown encryption method`，不能成功转换该样例。
- 在微信开发者工具/真机验证 MFLAC、NCM、1 GB 边界、大文件取消和失败提示。
- 验证导航 PNG 在不同手机宽度和深色导航栏上的显示比例。
- 回归上传、转换、PDF 预览、媒体播放、本地保存/重复保存/删除、云盘保存/重命名/删除、历史刷新。
- 清理阿里云安全组中 9853/8089 的旧公网放行规则。
- 若继续排查 Adminer 公网 502，只检查 1Panel 代理、安全组和容器日志，不修改数据库凭据。

### 9.1 2026-10-09 MFLAC 失败日志排查

- 用户样例：`cadode - 光の暈.mflac`，约 37.5 MiB。
- 服务端日志：`Converter exited with code 1: QMC2 decoder (cli) v0.2 ... ERROR: unknown encryption method`。
- 已验证：上传、任务记录、MySQL 历史查询和 422 返回均正常；原实现的问题是 stderr 只进 systemd 日志，数据库错误字段被固定成 `The file could not be converted`。
- 公开候选工具需要桌面 QQ 音乐进程/Frida 或额外 ekey，不能直接作为当前无交互 FastAPI 后端的替换方案；未替换线上 QMC2。
- 公网 HTTPS 探测曾出现 TLS 连接被重置；SSH 侧 systemd 和 `http://127.0.0.1:18080/api/v1/health` 正常，公网网关问题需单独排查。

### 9.2 2026-10-09 Linux MFLAC 独立解码器

- 已在工作区新建 `mflac-linux-decoder/`，当前包含 `qmc_mflac.py` 和 `README.md`。
- `qmc_mflac.py` 为 Python 3 标准库实现，可在 Linux 上运行，不依赖 pip、FFmpeg 或网络服务；支持 QTag、旧版 PC EKey、PC MusicEx 尾部解析、Tencent TEA EKey 解包、QMC Map 和改造版 RC4 流解密，并按 4 MiB 分块写出结果。
- 支持命令：
  - `python3 qmc_mflac.py --self-test`
  - `python3 qmc_mflac.py <input.mflac> <output.flac> --ekey '<EKEY>'`
  - `python3 qmc_mflac.py <input.mflac> <output.flac> --ekey-db player_process_db`
  - `python3 qmc_mflac.py <input.mflac> --inspect`
- 用户样例 `cadode - 光の暈.mflac` 已确认是 PC `PcV2MusicEx`/`MusicEx` 格式：文件大小约 37.5 MiB，尾部长度 192 字节，`media_mid=003hu7tI4cI8wf`，`media_filename=F0M000430fru0eU0v0.mflac`，文件内没有 EKey。
- 因此样例不能仅凭文件离线解码；必须提供合法的 `--ekey`，或从 QQ 音乐 Android 客户端导出的 `player_process_db` 中匹配密钥。也可以在兼容的 Windows QQ 音乐客户端环境中取得 EKey/明文后转移到 Linux。
- 已验证：`python -m py_compile mflac-linux-decoder/qmc_mflac.py`、`python mflac-linux-decoder/qmc_mflac.py --self-test`、样例 `--inspect` 和 `git diff --check` 均通过。
- 公开参考：`pushbox/qmc2`、ix64 的 MGG/MFLAC 格式研究、`mzj3920/qqmusic-decrypt`。这些资料均说明 PC MusicEx 新版文件不内嵌可离线使用的 EKey；不能把当前工具宣称为无密钥破解工具。

## 10. Git 与文件约定

最近相关提交：

- 5249442 chore: configure production API gateway
- c81afe6 ui: use supplied icons in bottom navigation
- 105230e feat: support encrypted audio conversion
- d32f2f9 fix: include conversion size metadata
- a0a6ec5 fix: preserve temporary file expiry on startup
- a3b047b feat: add WeChat Mini Program client

当前刻意保留的未提交文件：

- Converter.png
- design-previews/
- mflac-linux-decoder/
- yssqi.ccwu.cc-fullchain.pem

不要使用 git reset --hard、git clean 或删除无关文件。提交时只纳入本次相关源代码和资源。

## 11. 常用检查命令

~~~
node --check miniprogram/pages/index/index.js
python -m py_compile converter-api/app/main.py
python -m py_compile mflac-linux-decoder/qmc_mflac.py
python mflac-linux-decoder/qmc_mflac.py --self-test
git diff --check
git status --short --branch
~~~

服务器部署后至少验证：

~~~
systemctl is-active wechat-converter.service
curl -fsS http://127.0.0.1:18080/api/v1/health
curl -fsS https://api.yssqi.ccwu.cc/api/v1/health
curl -fsS https://api.yssqi.ccwu.cc/api/v1/test
~~~

## 12. 产品目标

这是一个微信小程序文件/媒体转换工具：小程序负责选取文件、上传、结果展示和本地/云端文件管理；FastAPI 负责转换、解密、临时结果、下载链接、历史记录和云盘审计。产品重点是保留真实文件名、提供可靠的打开/保存/删除流程、区分云端与本地文件，并在平台能力允许时支持大文件和加密音频转换。
