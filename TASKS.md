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

## P1 运维

- [x] 清理阿里云安全组中 9853/8089 的旧公网放行规则（用户已执行；未在服务器侧复核）。
- [ ] 如继续排查 Adminer 公网 502，只检查 1Panel 代理、安全组和容器日志，不修改数据库凭据。
- [ ] 复核公网 HTTPS 连接重置问题；SSH 侧 systemd 和内网 health 已正常时，单独定位网关链路。

## 已验证基线

- [x] API health/test、MySQL 状态、NCM 到 FLAC、历史和云盘基本流程已完成冒烟验证。
- [x] `mflac-linux-decoder` 的 Python 编译、self-test 和样例 inspect 已通过。
- [x] 服务端 helper 协议状态机通过 29 项测试：设备注册与撤销、单任务领取、心跳、成功/失败/超时回报、租约过期重试、旧租约被拒、重复完成不产生第二个结果。
- [x] Windows helper 通过 41 项单元测试与 6 项对真实服务的集成测试。
- [x] 最近相关提交：`04b4cfb feat: add MFLAC fallback and helper handoff`。
