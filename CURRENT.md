# Current State

更新时间：2026-10-12（Asia/Shanghai）

## 当前阶段

P0 开发已完成并提交（`aa3c483`、`838ac50`）。生产环境尚未部署本次改动，helper 未上线。下一次动服务器时，优先处理下面「线上巡检」里 3306 公网暴露的问题。

## 已验证

- FastAPI、MFLAC fallback、Linux 解码器和小程序历史样式通过静态检查。
- `mflac-linux-decoder/qmc_mflac.py --self-test` 通过。
- 服务器 API health、NCM 转 FLAC、历史和云盘基本流程已完成冒烟验证。
- 用户样例属于不内嵌 EKey 的 PC `PcV2MusicEx`/`MusicEx`；上传链路正常，但现有 QMC2 返回 `unknown encryption method`。
- **服务端 helper 协议**：`converter-api/tests/test_helper_protocol.py` 29 项通过，含并发领取唯一性、租约过期重试、旧租约不能覆盖已重分配任务、重复完成只产生一个结果。
- **Windows helper**：`windows-helper/tests/` 41 项单元测试通过；6 项集成测试用真实 helper 客户端打到真实 uvicorn 服务，覆盖注册、领取、下载、解密、上传、去重、撤销和小程序下载链接。
- 两套测试均以 stdlib `unittest` 运行，不需要 pytest。

## 线上巡检（2026-10-12，只读）

通过 `ssh wechat-prod`（`8.136.99.105`，root，`~/.ssh/config` 已有免密条目）执行，全程只读，未改动服务器。

**运行状态正常**

- `wechat-converter.service` active 且 enabled，运行 2 天；`curl 127.0.0.1:18080/api/v1/health` 返回 `status: ok`、`database: mysql`。
- 3 天内响应码只有 200/422/404，**无 5xx**、无 traceback、无 OOM kill；磁盘 23 GiB 可用（40%）。
- 部署形态是**扁平拷贝而非 git 检出**：`/opt/wechat-converter-api/app/` 只有 `main.py`、`mflac_fallback.py`，**没有 `helper_api.py`/`helper_store.py`**，`.env` 中 `HELPER_ENROLLMENT_SECRET` 行数为 0 —— 与「helper 未部署」一致。因此部署方式应为 `scp` 覆盖 + 重启，而非 `git pull`。
- 运行栈为 1Panel + Docker：`1Panel-openresty-iPSG`（80/443）、`1Panel-mysql-BHnV`、`1Panel-redis-4vLB`、Adminer（`127.0.0.1:9853`）、phpMyAdmin（`127.0.0.1:8089`）。

**需要处理**

- **3306 对公网开放**：从公网 `221.12.51.71` 直连 `8.136.99.105:3306` 收到真实 MySQL 8.4.11 握手（`caching_sha2_password`）。容器绑定 `0.0.0.0:3306` 且安全组放行；应用只用 `127.0.0.1`，无公网必要。
- Redis `6379`、1Panel 面板 `40555` 同样绑定 `0.0.0.0`，目前仅靠安全组拦截（主机 `ufw` 为 inactive），属单层防护。
- 部署目录 `/opt/wechat-converter-api` 及 `app/`、`deploy/` 权限为 `drwxr-xrwx`（other 可写）。
- 主机 1.6 GiB 内存、**无 swap**、空闲约 42 MiB，而单次转换允许 1 GiB 上传并调用 LibreOffice/FFmpeg。

**9853/8089 安全组清理已复核**：两者从公网均不可达，且只监听 `127.0.0.1`。

**公网 HTTPS 连接重置：已定位，非服务端问题**

从本机（`221.12.51.71`）访问 `https://api.yssqi.ccwu.cc/` 时 TLS 握手被重置（`curl` exit 35，3/3 稳定复现），但证据指向链路而非服务器：

1. openresty 日志中该 IP 只有两条 **80 端口**请求（301 跳转），**没有任何 443 记录** —— 说明重置发生在到达服务器之前。
2. 在服务器上访问自身公网 IP 与 `127.0.0.1` 均返回 200；不经该中间链路的路径完全正常。
3. 不带 SNI 的握手能到达 openresty 并收到正常的 TLS alert，说明服务端 TLS 本身健康。
4. 独立云端抓取（Firecrawl）访问同一 URL 返回 **200** 与正确 JSON，证明公网其他位置可正常访问。
5. 附带发现本机 DNS 被污染：`api.yssqi.ccwu.cc` 额外解析出 `198.41.0.4`（即 `a.root-servers.net`），而 `119.29.29.29`、`8.8.8.8` 只返回正确 IP。

结论：服务器与网关配置无需修改；症状出现在特定客户端网络的 TLS/SNI 链路上。

## 当前阻塞

实际 `PcV2MusicEx` 解码仍未在真实环境验证：需要一台 Windows 主机、已授权的 QQ Music 会话和合法 EKey。helper 的解码步骤在测试中以替身完成，真实解码路径只验证到「读取操作者授权密钥文件并交给 `qmc_mflac.py`」。

部署前必须在服务端设置 `HELPER_ENROLLMENT_SECRET`，否则整个 `/api/v1/helper` 返回 503。

## 工作区

当前分支为 `main`，工作区干净（仅剩用户原有的未跟踪 `.obsidian/`）。P0 改动已分两次提交：`aa3c483` 服务端协议、`838ac50` helper。

本地 Windows 沙箱中发现三个环境级现象，已规避，不影响生产（Linux）：

- `tempfile.mkdtemp` 在受限令牌下会因 `PermissionError` 重试循环而卡住；`helper_api.py` 因此改用自有的 `_unique_dir`，不再依赖 `tempfile`。
- `os.mkdir(mode=0o700)` 在受限令牌下会创建自己都写不进去的目录；`main.py` 的下载目录只在 POSIX 上应用 `0o700`。
- `python -m py_compile` 写入 `converter-api/app/__pycache__` 会被拒绝（该目录既有权限不含当前用户），需用 `PYTHONPYCACHEPREFIX` 指向可写目录。

另：`.git` 目录归属另一用户 SID，受限令牌下 `git add` 无法创建 `index.lock`，需以完全权限运行。

## 下一步

1. 关闭安全组对 3306 的公网放行，并复核 6379/40555 的绑定方式。
2. 在真实 Windows + 授权 EKey 环境下执行 `python run_helper.py --check` 与 `--once`，确认 `PcV2MusicEx` 样例可转出有效 FLAC。
3. 部署 helper：`scp` 覆盖 `app/helper_api.py`、`app/helper_store.py`、`app/main.py`，在 `.env` 追加 `HELPER_ENROLLMENT_SECRET`，重启并确认 `helper_queue: ready`。
4. 把 helper 队列接入 `POST /api/v1/convert/media`：本地解码失败且判定为 PC MusicEx 时返回 job id 而不是 422，并让小程序轮询 `GET /api/v1/jobs/{job_id}`。
5. 完成 `TASKS.md` 中的 P1 验证（开发者工具/真机）和剩余运维项。

## 约束

不提交凭据、私钥、证书、真实用户媒体、GUI 二进制或运行时文件；不把 EKey 写入小程序存储、URL、数据库或普通日志。helper 只上传解密后的 FLAC，服务端永不接收 EKey。`*.ekey`、`windows-helper/config.ini` 和 `windows-helper/state/` 已加入 `.gitignore`。
