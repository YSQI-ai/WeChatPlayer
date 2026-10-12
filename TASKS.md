# WeChatPlayer Tasks

## P0 待真实验证

- [ ] **在真实 Windows + 授权 EKey 环境下验证 `PcV2MusicEx` 解码**
  - 依赖：Windows 主机、已授权的 QQ 音乐会话、合法 EKey 或导出的 `player_process_db`。
  - 验收：`python run_helper.py --check` 全绿；`--once` 后样例转出有效 FLAC；密钥不进入小程序、URL、数据库或普通日志。
  - 前置：服务端已设置 `HELPER_ENROLLMENT_SECRET`（否则 `/api/v1/helper` 返回 503）。
  - 相关文件：`windows-helper/README.md`、`windows-helper/config.example.ini`。

- [ ] **部署 helper 协议并确认线上可用**
  - 依赖：服务器 `.env` 写入 `HELPER_ENROLLMENT_SECRET` 并重启 `wechat-converter.service`。
  - 验收：`curl -fsS https://api.yssqi.ccwu.cc/api/v1/health` 返回 `helper_queue: ready`；MySQL 中已建 `helper_devices` 和 `helper_jobs` 表。
  - 注意：未配置 MySQL 时队列退回进程内状态，重启即丢失；systemd 已固定 `--workers 1`。

## P1 集成

- [ ] **把 helper 队列接入 `POST /api/v1/convert/media`**
  - 说明：本地解码失败且判定为 PC MusicEx 时返回 job id（202）而不是 422；需要在 `mflac_fallback.py` 增加容器类型判定，并处理已创建的 `conversion_records` 记录收尾。
  - 验收：小程序上传 PC MusicEx 后拿到 job id，轮询 `GET /api/v1/jobs/{job_id}?client_id=` 直到拿到下载链接。
  - 注意：该改动触及已验证可用的转换接口，需单独提交并回归 NCM/普通媒体。

## P1 验证

- [ ] 在微信开发者工具或真机验证 MFLAC、NCM、1 GiB 边界、取消上传和失败提示。
- [ ] 回归文档转 PDF、媒体播放、保存/重复保存/删除、云盘保存/重命名/删除和历史刷新。
- [ ] 检查导航 PNG 在不同屏幕宽度和深色导航栏下的显示比例。

## P0 运维（安全）

- [ ] **关闭安全组对 3306 的公网放行**
  - 实测证据：从公网 `221.12.51.71` 直连 `8.136.99.105:3306`，收到真实 MySQL 8.4.11 握手报文（`caching_sha2_password`）。
  - 原因：容器 `1Panel-mysql-BHnV` 绑定 `0.0.0.0:3306`，且安全组放行了该端口。
  - 应用只经 `127.0.0.1` 连库，公网放行没有业务必要。
  - 验收：外部扫描 3306 不可达；`wechat-converter.service`、1Panel、phpMyAdmin/Adminer 隧道均不受影响。

## P1 运维

- [x] 清理阿里云安全组中 9853/8089 的旧公网放行规则。**已由外部实测复核**：两者从公网均不可达，且仅绑定 `127.0.0.1`。
- [ ] 收紧绑定在 `0.0.0.0` 但当前只靠安全组拦截的服务，避免单层防护：Redis `6379`（容器 `1Panel-redis-4vLB`）、1Panel 面板 `40555`（`1panel-core`）。建议改绑 `127.0.0.1`。主机 `ufw` 当前为 inactive。
- [x] 复核公网 HTTPS 连接重置问题。**结论：非服务端问题**，排查证据见 `CURRENT.md`。
- [ ] 如继续排查 Adminer 公网 502，只检查 1Panel 代理、安全组和容器日志，不修改数据库凭据。
- [ ] 收敛部署目录权限：`/opt/wechat-converter-api` 及其 `app/`、`deploy/` 当前为 `drwxr-xrwx`（other 可写），建议改为 `0755`。
- [ ] 评估内存余量：主机 1.6 GiB、**无 swap**、空闲仅约 42 MiB，而单次转换允许 1 GiB 上传并调用 LibreOffice/FFmpeg。建议加 swap 或限制并发。

## 已验证基线

- [x] API health/test、MySQL 状态、NCM 到 FLAC、历史和云盘基本流程已完成冒烟验证。
- [x] `mflac-linux-decoder` 的 Python 编译、self-test 和样例 inspect 已通过。
- [x] 服务端 helper 协议状态机通过 29 项测试：设备注册与撤销、单任务领取、心跳、成功/失败/超时回报、租约过期重试、旧租约被拒、重复完成不产生第二个结果。
- [x] Windows helper 通过 41 项单元测试与 6 项对真实服务的集成测试。
- [x] 2026-10-12 线上只读巡检：服务 active 2 天、MySQL 已连、3 天内仅 200/422/404 无 5xx、无 traceback、无 OOM；磁盘 23 GiB 可用。
- [x] 最近相关提交：`04b4cfb feat: add MFLAC fallback and helper handoff`。
