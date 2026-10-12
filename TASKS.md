# WeChatPlayer Tasks

## P0 待真实验证

- [ ] **取得合法授权密钥来源，完成 `PcV2MusicEx` 真实解码**
  - 现状：`run_helper.py --check` 已全绿，注册成功（`dev_006f02e375fe2f68`），链路已打通。唯一缺口是密钥。
  - 实测依据：样例 `cadode - 光の暈.mflac` `--inspect` 结果为 `footer=PcV2MusicEx ekey=no`；全盘搜索 `C:`、`D:` 不存在 `player_process_db`，也无 `.ekey`；QQ 音乐装在 `D:\Program Files (x86)\Tencent\QQMusic\` 但安装目录内无任何 `.db` 密钥文件。
  - 需要用户提供其一：合法 EKey 字符串（写入 `key_source_path` 指向的文件），或导出的 `player_process_db`。
  - 验收：样例转出可播放的 FLAC；密钥不进入小程序、URL、数据库或普通日志。
  - 相关文件：`windows-helper/config.example.ini`、`windows-helper/README.md`。

- [x] **部署 helper 协议并确认线上可用**（2026-10-12 完成）
  - 服务端 `.env` 已追加 `HELPER_ENROLLMENT_SECRET`，`app/helper_api.py`、`app/helper_store.py`、`app/main.py` 已上传（上传前备份为 `*.bak-20261012-130409`）。
  - 验收已达成：`GET /api/v1/health` 返回 `helper_queue: ready`；MySQL 已建 `helper_devices`、`helper_jobs`；错误密钥返回 401（而非 503）。
  - 注意：部署形态是**扁平拷贝**，不是 git 检出，因此用 `scp` 覆盖而非 `git pull`。

## P1 集成

- [x] **服务端：把 helper 队列接入 `POST /api/v1/convert/media`**（2026-10-12 完成，提交 `c9668dd`）
  - 已实现：`mflac_fallback.requires_helper()` 读 footer 判定 `PcV2MusicEx`（该容器恒定无 EKey）；只有 QMC2 与 mask fallback **都真的失败后**才抛 `HelperRequiredError`，所以原本能解的文件行为不变。
  - 上传返回 `202` + `job_id` + `status_url`；源文件移入 helper 源目录，转换任务目录不再保留副本。
  - 上传行以 `helper_queued:<job_id>` 标记收尾，客户端可据此区分"已转交"与"真失败"。
  - 线上实测：真实 39 MB 样例经 `/convert/media` 得到 `{"status":"queued","job_id":2,...}`，`GET /api/v1/jobs/2` 返回 `queued`。
- [x] **小程序：处理 202 与轮询**（2026-10-12 完成，提交 `53c9a89`）
  - 已实现：识别 `status: "queued"` 响应，轮询 `status_url`（5 秒一次，上限 60 次）；成功时复用同一套结果卡片，失败时展示服务端 `error_message`；`onUnload` 清理定时器；等待期间显示专用卡片说明"密钥不在文件内，需本机助手解码"。
  - 历史列表中 `error_message` 以 `helper_queued:` 开头的记录渲染为"已转交本机助手解码"，不再显示为失败。
  - 线上实测完整回路：上传得 `queued`（job 2）→ helper 领取、下载 39 MB、判定无密钥 → 回报失败 → 客户端轮询看到 `failed` 与可读的 `error_message`。
  - 静态检查：`node --check` 通过、JSON 有效、WXML `<view>` 配对 124/124。
  - **未做**：微信开发者工具/真机实跑（见下方 P1 验证），因此 202 分支目前只有代码级与接口级验证。
- [ ] **已知瑕疵：一次上传会留下两条历史记录**（一条 `helper_queued:` 标记行 + 一条 helper 成功行）。渲染层已把前者显示为"处理中"，但记录仍重复；后续应改为 helper 发布结果时复用同一条 `conversion_records`。

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
- [x] 2026-10-12 helper 首次真实联调：39 MB 样例经 operator 端点入队（job 1），helper 领取、下载、判定无密钥后按设计失败并回报 `no_authorized_key_source`；两侧临时文件均已清理，未泄漏密钥。
- [x] 最近相关提交：`04b4cfb feat: add MFLAC fallback and helper handoff`。
