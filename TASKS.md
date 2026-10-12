# WeChatPlayer Tasks

## 已决定关闭

- [x] ~~**取得合法授权密钥来源，完成 `PcV2MusicEx` 真实解码**~~ — **2026-10-12 用户决定放弃**（提交 `d48bccd`）。
  - 结论：PC `PcV2MusicEx`/`MusicEx` 容器不内嵌 EKey；全盘搜索 `C:`、`D:` 不存在 `player_process_db`，也无 `.ekey`；QQ 音乐安装目录内无 `.db` 密钥文件。用户明确不再追这条线。
  - 处理方式：**保留全部代码**（服务端队列、Windows helper、两套测试），只把小程序界面从"等待本机助手"改为"该文件暂不支持"，不再承诺无法兑现的结果。
  - 不再需要用户提供 EKey 或 `player_process_db`。
  - 若日后重新开启：只需要还原 `miniprogram/pages/index/` 的文案，机制本身完好且有测试守着。

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

- [ ] **确认 `_process_running` 的 fail-open 行为是否符合预期**（`windows-helper/helper/agent.py`）。
  - 现状：`tasklist` 不存在或调用失败时返回 `True`（视为"进程在运行"），即**检查无法执行时门禁放行**。
  - 影响：生产环境为 Windows，`tasklist` 始终可用，故实际不受影响；但逻辑上"安全检查无法判定时应倾向拒绝"（fail closed）更稳妥。
  - 这是**行为变更**，需用户决定是否改；当前仅记录，未改动。
- [x] **CI 全绿**（2026-10-12，run #3，提交 `9c4adb8`，conclusion: success）
  - 4 个任务全部通过：`server-tests`（ubuntu）、`helper-tests`（**windows-latest**）、`miniprogram-tests`（ubuntu）、`docker-build`（ubuntu）。
  - `docker-build` 的真实含义：镜像构建成功 → 容器启动 → 轮询 `/api/v1/health` 拿到响应。**至此 Dockerfile 得到实测验证**（本机 Docker 不可用，无法本地验证）。
  - 达成过程中修掉两个真实缺陷：测试依赖 `httpx` 未声明（run #1 失败）、Windows 组件的测试错放在 Linux（run #2 失败）。
- [ ] **本机 Docker 修复后实测 compose 全流程**：`cp .env.example .env` → `docker compose up --build` → `curl 127.0.0.1:18080/api/v1/health`。
- [ ] 在微信开发者工具或真机验证 MFLAC、NCM、1 GiB 边界、取消上传和失败提示。
- [ ] 回归文档转 PDF、媒体播放、保存/重复保存/删除、云盘保存/重命名/删除和历史刷新。
  - 注意：`504f6d4` 改了音视频打开路径（不再走 `openDocument`），`openResult` 与 `openHistoryFile` 都需真机复核"下载并播放"与历史音频重播。
- [ ] 检查导航 PNG 在不同屏幕宽度和深色导航栏下的显示比例。
  - `504f6d4` 已把 `app.json` 导航栏由浅色改为 `#171a2d` + 白色文字，深色导航栏现在成为唯一形态，需一并确认。
  - **2026-10-12 更新**：导航图标已由 300×300 缩到 120×120 并降为 256 色调色板（370.8 KB → 22.6 KB），**需真机确认小尺寸下边缘是否出现色带**（已用 33px/120px 对比预览核对，肉眼无明显差异）。

## P0 运维（安全）

- [ ] **收敛 3306：这是两层防护，要一起做，不是二选一**
  - 实测证据：从公网直连服务器 `3306`，收到真实 MySQL 8.4.11 握手报文（`caching_sha2_password`）。
  - 原因：容器 `1Panel-mysql-BHnV` 绑定 `0.0.0.0:3306`，且安全组放行了该端口。

  **第一层（主防线）：把容器端口绑定改为 `127.0.0.1:3306:3306`**
  - 这是更可靠的一层：改绑后端口在公网接口上根本不监听，无论安全组怎么配都无法从外部连入。
  - **注意不要"完全关闭"该端口的发布**：应用 `wechat-converter.service` 运行在宿主机上、经 `127.0.0.1:3306` 连库，去掉端口发布会让应用连不上数据库。
  - 操作位置：1Panel → 容器 → `1Panel-mysql-BHnV` → 编辑 → 端口映射，把 `0.0.0.0:3306:3306` 改为 `127.0.0.1:3306:3306`。
  - **改绑定会重建容器**，务必先 `mysqldump` 备份，并确认数据目录是具名卷/绑定挂载（重建容器不会丢数据），再在低峰期操作。

  **第二层（清理冗余）：删除安全组 3306 入站规则**
  - 第一层生效后这条规则已无害，但留着就是隐患——将来若有人加了宽松规则（如放行全部端口），3306 会立刻重新暴露。

  - 验收：公网扫描 3306 不可达；`wechat-converter.service` 正常；经 SSH 隧道仍可管理数据库。
  - **数据库访问方式**（改绑后不再能直连公网 3306）：
    - 隧道：`ssh -N -L 13306:127.0.0.1:3306 wechat-prod`，然后本地客户端连 `127.0.0.1:13306`。
    - 隧道让连接看起来来自 `localhost`，因此现有 `wechat_converter` 账号**无需改动授权**即可使用。
    - 现有网页工具同样走隧道：`ssh -N -L 9853:127.0.0.1:9853 -L 8089:127.0.0.1:8089 wechat-prod`，再访问 `http://127.0.0.1:9853`（Adminer）或 `:8089`（phpMyAdmin）。
    - 凭据在服务器 `.env` 的 `MYSQL_USER` / `MYSQL_PASSWORD`，**不要复制到本地明文文件**。

  **2026-10-12 复核结果：绑定那层没生效 → 已修复**
  - ✅ **公网已不可达**：从本机实测 `3306` 为 `TIMEOUT/FILTERED`，同批 `443` 正常连通（对照组），故是端口被拦而非本机网络问题。
  - ❌→✅ **绑定原为 `0.0.0.0`，根因已定位并修复**：`/opt/1panel/apps/mysql/mysql/.env` 中 `HOST_IP=''`（空值），compose 的 `${HOST_IP}:${PANEL_APP_PORT_HTTP}:3306` 因此渲染为 `:3306:3306`，Docker 便绑定全部网卡。同目录的 adminer / phpmyadmin 本就是 `HOST_IP='127.0.0.1'`，唯独 mysql 和 redis 是空值。
  - **修复动作**：`mysqldump` 全量备份（`/root/db-backup-20261012-215827.sql`，3.7 MB / 40 张表）→ `.env` 备份为 `.env.bak-20261012-215838` → 改 `HOST_IP='127.0.0.1'` → `docker compose up -d` 重建容器（MySQL 约 3 秒就绪）。
  - **修复后验证**：`ss` 为 `127.0.0.1:3306`、Docker 为 `127.0.0.1:3306->3306/tcp`；公网 `3306`/`33060` 均 `TIMEOUT`；`/api/v1/health` 返回 `database: mysql`、`helper_queue: ready`；数据完好（`wechat_converter` 4 张表）。
  - **Adminer / phpMyAdmin 未受影响**：三者同在 `1panel-network`，容器内实测可连（`phpMyAdmin -> MySQL: OK`、`Adminer -> MySQL: OK`）。
  - ⚠️ **填服务器地址时的注意点**：Adminer / phpMyAdmin 自身跑在容器里，登录框里**不能填 `127.0.0.1`**（那指向它自己，也不是公网 IP），应填容器名 **`1Panel-mysql-BHnV`**。
  - 数据目录是 bind mount（`/opt/1panel/apps/mysql/mysql/data` → `/var/lib/mysql`），故重建容器不影响数据。

  - ✅ **本质因已消除（绑定层）**，安全组那层依旧保留 = 现在是两层防护。
  - ⏳ **同一缺陷仍在 Redis**：`/opt/1panel/apps/redis/redis/.env` 也是 `HOST_IP=''`，实测 `0.0.0.0:6379` 且仅靠安全组拦截。修法同上（注意重建 Redis 可能让 1Panel 会话短暂失效，建议挑时间）。
  - ⏳ **1Panel 面板自身 `40555` 仍绑 `0.0.0.0` 且公网可达**（返回 1Panel 的 `Access Temporarily Unavailable`，说明安全入口生效）。这项不能用 `HOST_IP` 修（面板不是 1Panel 应用），应在 1Panel「面板设置」里配安全入口 / 授权 IP，或加安全组白名单。
  - 其余端口均已拦截：`6379`、`8080`、`8089`、`9853`、`18080` 全部 `TIMEOUT/FILTERED`；公网开放的只有 `22`、`80`、`443`、`40555`。

## P1 运维

- [x] 清理阿里云安全组中 9853/8089 的旧公网放行规则。**已由外部实测复核**：两者从公网均不可达，且仅绑定 `127.0.0.1`。
- [x] **收紧绑定在 `0.0.0.0` 的服务，消除单层防护**（2026-10-12 完成）
  - **Redis `6379`**：`/opt/1panel/apps/redis/redis/.env` 同样是 `HOST_IP=''`，与 MySQL 同一缺陷。修复前确认**无任何客户端连接、数据目录仅 4 KB**（基本闲置），故重建安全。改 `HOST_IP='127.0.0.1'` → 重建容器 → 2 秒恢复，现监听 `127.0.0.1:6379`。
  - **1Panel 面板 `40555`**：面板不是"1Panel 应用"，没有 `HOST_IP`，且 `1pctl listen-ip` 只能切换 IPv4/IPv6、**不能指定绑定地址**。改用面板自身配置项 `BindAddress`（原值 `0.0.0.0`）：备份 `core.db` → `1pctl stop core` → 改 `BindAddress=127.0.0.1` → 启动 → 验证；脚本内置失败自动回滚。现监听 `127.0.0.1:40555`，本机访问返回 `HTTP 200`。
  - **验收通过**：公网扫描 `3306`/`6379` 为 FILTERED、`40555` 为 REFUSED（安全组仍放行但已无监听），`9853`/`8089`/`18080` 全部 FILTERED；公网开放的只剩 `22`/`80`/`443`。应用健康检查正常（`database: mysql`、`helper_queue: ready`），`wechat-converter.service` active。
  - ⚠️ **访问面板的方式已改变**：不再是 `公网IP:40555`，需经隧道 `ssh -N -L 40555:127.0.0.1:40555 wechat-prod`，然后浏览器访问 `http://127.0.0.1:40555/`（URL 仍需带面板的安全入口路径，可从 `core.db` 的 `SecurityEntrance` 读取）。回滚方式：`1pctl stop core` → 把 `BindAddress` 改回 `0.0.0.0` → `1pctl start core`。
  - 备注：主机 `ufw` 仍为 inactive；现在的防护来自"服务只绑回环 + 安全组"两层。
- [ ] **安全组清理（用户侧）**：`3306` 那条规则的来源已被改为 `127.0.0.1`——对外部流量永不匹配，实际已挡住，但语义上属"绕路"，**建议直接删除该规则**以免后人误解。`40555` 的 `0.0.0.0/0` 规则也建议删除（服务端已不再监听公网，留着只是冗余）。另：`22` 对全公网开放，可考虑收窄到常用 IP。
- [x] 复核公网 HTTPS 连接重置问题。**结论：非服务端问题**，排查证据见 `CURRENT.md`。
- [ ] 如继续排查 Adminer 公网 502，只检查 1Panel 代理、安全组和容器日志，不修改数据库凭据。
- [ ] 收敛部署目录权限：`/opt/wechat-converter-api` 及其 `app/`、`deploy/` 当前为 `drwxr-xrwx`（other 可写），建议改为 `0755`。
- [ ] 评估内存余量：主机 1.6 GiB、**无 swap**、空闲仅约 42 MiB，而单次转换允许 1 GiB 上传并调用 LibreOffice/FFmpeg。建议加 swap 或限制并发。

## 已验证基线

- [x] API health/test、MySQL 状态、NCM 到 FLAC、历史和云盘基本流程已完成冒烟验证。
- [x] `mflac-linux-decoder` 的 Python 编译、self-test 和样例 inspect 已通过。
- [x] 服务端 helper 协议状态机通过 29 项测试：设备注册与撤销、单任务领取、心跳、成功/失败/超时回报、租约过期重试、旧租约被拒、重复完成不产生第二个结果。
- [x] Windows helper 通过 41 项单元测试与 6 项对真实服务的集成测试。
- [x] 小程序整页自检修复（2026-10-12，`504f6d4`、`51eaeee`）：音视频误用 `openDocument`、首页写死的 `+ 24.8%` / `99% 服务可用率` 假数据、helper 任务状态残留、导航栏配色不一致、`README.md` 随包上传、`wx.getSavedFileList` 废弃告警。回归测试 `tools/miniprogram-flow.test.js` 11/11 通过（`node tools/miniprogram-flow.test.js`）。
- [x] 界面不再承诺 helper 能完成（2026-10-12，`d48bccd`）：等待卡片、历史记录行、超时与选择文件提示全部改为按实际能力描述。仅改文案与注释，未动控制流；`node --check` 通过、`<view>` 配对 124/124、流程测试 11/11 通过。
- [x] 2026-10-12 线上只读巡检：服务 active 2 天、MySQL 已连、3 天内仅 200/422/404 无 5xx、无 traceback、无 OOM；磁盘 23 GiB 可用。
- [x] 2026-10-12 helper 首次真实联调：39 MB 样例经 operator 端点入队（job 1），helper 领取、下载、判定无密钥后按设计失败并回报 `no_authorized_key_source`；两侧临时文件均已清理，未泄漏密钥。
- [x] 最近相关提交：`04b4cfb feat: add MFLAC fallback and helper handoff`。
