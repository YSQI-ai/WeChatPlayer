# Current State

更新时间：2026-10-11（Asia/Shanghai）

## 当前阶段

P0 阻塞已解除。Windows helper 与服务端私有任务协议已实现并通过测试，最近一次功能提交为 `04b4cfb feat: add MFLAC fallback and helper handoff`，本次改动尚未提交。

## 已验证

- FastAPI、MFLAC fallback、Linux 解码器和小程序历史样式通过静态检查。
- `mflac-linux-decoder/qmc_mflac.py --self-test` 通过。
- 服务器 API health、NCM 转 FLAC、历史和云盘基本流程已完成冒烟验证。
- 用户样例属于不内嵌 EKey 的 PC `PcV2MusicEx`/`MusicEx`；上传链路正常，但现有 QMC2 返回 `unknown encryption method`。
- **服务端 helper 协议**：`converter-api/tests/test_helper_protocol.py` 29 项通过，含并发领取唯一性、租约过期重试、旧租约不能覆盖已重分配任务、重复完成只产生一个结果。
- **Windows helper**：`windows-helper/tests/` 41 项单元测试通过；6 项集成测试用真实 helper 客户端打到真实 uvicorn 服务，覆盖注册、领取、下载、解密、上传、去重、撤销和小程序下载链接。
- 两套测试均以 stdlib `unittest` 运行，不需要 pytest。
- 安全组 9853/8089 旧公网放行规则已由用户清理完成（未在服务器侧复核）。

## 当前阻塞

实际 `PcV2MusicEx` 解码仍未在真实环境验证：需要一台 Windows 主机、已授权的 QQ Music 会话和合法 EKey。helper 的解码步骤在测试中以替身完成，真实解码路径只验证到「读取操作者授权密钥文件并交给 `qmc_mflac.py`」。

部署前必须在服务端设置 `HELPER_ENROLLMENT_SECRET`，否则整个 `/api/v1/helper` 返回 503。

## 工作区

当前分支为 `main`。P0 相关改动（服务端协议、helper、测试、文档）已写入工作区但尚未提交。工作区仍有本地样例音频、GUI EXE、证书、设计预览和 `.agents/` 等被 `.gitignore` 忽略的文件，不应纳入普通功能提交。

本地 Windows 沙箱中发现三个环境级现象，已规避，不影响生产（Linux）：

- `tempfile.mkdtemp` 在受限令牌下会因 `PermissionError` 重试循环而卡住；`helper_api.py` 因此改用自有的 `_unique_dir`，不再依赖 `tempfile`。
- `os.mkdir(mode=0o700)` 在受限令牌下会创建自己都写不进去的目录；`main.py` 的下载目录只在 POSIX 上应用 `0o700`。
- `python -m py_compile` 写入 `converter-api/app/__pycache__` 会被拒绝（该目录既有权限不含当前用户），需用 `PYTHONPYCACHEPREFIX` 指向可写目录。

## 下一步

1. 在真实 Windows + 授权 EKey 环境下执行 `python run_helper.py --check` 与 `--once`，确认 `PcV2MusicEx` 样例可转出有效 FLAC。
2. 在服务端设置 `HELPER_ENROLLMENT_SECRET` 并部署，用 `GET /api/v1/health` 确认 `helper_queue: ready`。
3. 把 helper 队列接入 `POST /api/v1/convert/media`：本地解码失败且判定为 PC MusicEx 时返回 job id 而不是 422，并让小程序轮询 `GET /api/v1/jobs/{job_id}`。
4. 完成 `TASKS.md` 中的 P1 验证（开发者工具/真机）和剩余运维检查。
5. 复核公网 HTTPS 连接重置问题。

## 约束

不提交凭据、私钥、证书、真实用户媒体、GUI 二进制或运行时文件；不把 EKey 写入小程序存储、URL、数据库或普通日志。helper 只上传解密后的 FLAC，服务端永不接收 EKey。`*.ekey`、`windows-helper/config.ini` 和 `windows-helper/state/` 已加入 `.gitignore`。
