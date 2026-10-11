# Helper Protocol Decision

## 结论

`PcV2MusicEx` 的授权解码走「Windows helper 本地解密、只上传 FLAC 结果」的私有任务协议。服务端不接收、不存储、不转发 EKey。

协议实现为 `/api/v1/helper` 下的设备注册、单任务租约领取、心跳、成功/失败/超时回报，以及面向小程序的 `GET /api/v1/jobs/{job_id}`。

## 原因

Linux 服务端无法从文件本身恢复 PC MusicEx 的 EKey（见 001）。把 EKey 送到服务端会立即产生「密钥必须落库、落日志或落 URL」的存储与审计问题，反而扩大暴露面。让 helper 在已授权的 Windows 会话内完成解密，服务端只处理标准 FLAC，可以完全避开这条路径。

helper 用 Python 实现而不是复用现有 GUI EXE：`KGMusicConverterPro-v1.3.7.exe` 是没有稳定无人值守命令行的图形程序，无法作为后台 worker。

## 任务状态机

`queued -> leased -> succeeded | failed | timeout`，租约过期或 helper 主动释放时按剩余次数回到 `queued`。

**同一任务最多完成一次。** 每次领取生成随机 `lease_token`，所有回报都以 `status='leased' AND lease_token=?` 为条件更新：

- 已完成任务的重复成功回报返回 `duplicate`，不生成第二个结果；
- 租约过期后被重新分配的旧 helper 回报得到 `409`，不会覆盖新一次尝试的结果；
- 下载令牌每个任务只生成一次。

helper 依赖这条契约：网络抖动时重试 `complete` 是安全的；而 `claim` 在响应丢失后不重试，因为那会多领一个任务，被放弃的租约到期后自动回收。

## 安全边界

- 整个 helper 接口在未配置 `HELPER_ENROLLMENT_SECRET` 时返回 503，默认部署不暴露队列。
- 注册用共享密钥；其余调用用每设备令牌，服务端只保存 SHA-256，比较使用常量时间。
- 设备令牌与租约令牌走请求头（`X-Helper-Token`、`X-Helper-Lease`），不进 URL，避免代理访问日志留存。
- helper 永远拿不到结果下载令牌；该链接只通过小程序接口返回。
- 密钥来源仅限操作者授权的本地来源（`ekey_file` 或 `player_process_db`）；没有授权来源时就地失败并给出可诊断原因，绝不猜测密钥。
- helper 凭据与任务日志用 Windows DPAPI 保护；EKey 只在单次解密期间存在于内存，并立即注册进日志脱敏器。
- 任务结束后源文件、工作目录和解密中间产物一并清理。

## 部署约束

helper 需要在交互式用户会话中运行（读取已授权的 QQ Music 会话），因此用任务计划程序的「登录时」触发器，不使用 `LocalSystem` 服务，也不需要入站端口。

服务端任务状态优先使用 MySQL；未配置 MySQL 时退回进程内状态，仅适用于本地单进程运行，重启会丢失队列。
