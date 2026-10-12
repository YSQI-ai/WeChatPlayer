# Current State

更新时间：2026-10-12（Asia/Shanghai）

## 当前阶段

helper 协议**已上线并完成首次真实联调**（2026-10-12）。链路从入队到回报全程打通，唯一缺口是**没有合法授权密钥**，因此 PC MusicEx 文件仍解不出 FLAC。下一个动服务器的场合，优先处理「线上巡检」里 3306 公网暴露的问题。

## 已验证

- FastAPI、MFLAC fallback、Linux 解码器和小程序历史样式通过静态检查。
- `mflac-linux-decoder/qmc_mflac.py --self-test` 通过。
- 服务器 API health、NCM 转 FLAC、历史和云盘基本流程已完成冒烟验证。
- 用户样例属于不内嵌 EKey 的 PC `PcV2MusicEx`/`MusicEx`；上传链路正常，但现有 QMC2 返回 `unknown encryption method`。
- **服务端 helper 协议**：`converter-api/tests/test_helper_protocol.py` 29 项通过，含并发领取唯一性、租约过期重试、旧租约不能覆盖已重分配任务、重复完成只产生一个结果。
- **Windows helper**：`windows-helper/tests/` 42 项单元测试通过；6 项集成测试用真实 helper 客户端打到真实 uvicorn 服务，覆盖注册、领取、下载、解密、上传、去重、撤销和小程序下载链接。
- 两套测试均以 stdlib `unittest` 运行，不需要 pytest。

## helper 上线与首次真实联调（2026-10-12）

**服务端部署**（写操作，均已备份）：`app/helper_api.py`、`app/helper_store.py`、`app/main.py` 经 `scp` 覆盖上传，上传前核对服务器 `main.py` 与仓库改动前版本**逐字节一致**（1146 行、归一化后 sha256 相同），确认没有需要保留的线上热修。`.env` 追加 `HELPER_ENROLLMENT_SECRET`（只追加，未读取原有内容；权限保持 `640 root:converter`）。重启后：

- `GET /api/v1/health` → `"helper_queue":"ready"`
- 错误密钥注册 → `401`（而非 `503`），证明协议确实生效
- MySQL 自动建表 `helper_devices`、`helper_jobs`

**本机 helper**：`--check` 全绿，注册成功为 `dev_006f02e375fe2f68`。因为本机到公网的 HTTPS 被重置（见下），测试改用 `ssh -L 18081:127.0.0.1:18080` 隧道，`config.ini` 里的 `api_base_url` 仍保持生产用的公网地址，隧道只在测试时用 `HELPER_API_BASE_URL` 覆盖。

**端到端**：把 39 MB 的 `cadode - 光の暈.mflac` 经 operator 端点入队为 job 1，helper 领取、下载、判定该文件 `footer=PcV2MusicEx ekey=no` 且无可用密钥来源后，**按设计失败**并回报 `no_authorized_key_source`。小程序侧查询得到 `status: failed` 与可诊断的 `error_message`。两侧临时文件均已清理（本机只剩 `credentials.bin` 与空的 `work/`，服务器无残留 `helper-source-*`），日志中密钥与用户路径被脱敏为 `<redacted>` / `<local-path>`。

**未解决**：全盘搜索 `C:`、`D:` **不存在 `player_process_db`，也没有 `.ekey`**；QQ 音乐装在 `D:\Program Files (x86)\Tencent\QQMusic\`，但安装目录内没有任何 `.db` 密钥文件。因此真实解码仍需用户提供合法 EKey 或导出的 `player_process_db`。

**顺带修掉的 bug**：`configparser` 默认启用 `BasicInterpolation`，遇到 `%LOCALAPPDATA%` 这类路径直接抛 `InterpolationSyntaxError`，而 `config.example.ini` 推荐的正是这种写法 —— 即 helper 用官方示例配置根本起不来。已改为 `interpolation=None`（路径展开本来就由 `_expand()` 负责），并让 `device_name` 也走 `_expand()`。原测试用的是字面量路径，因此漏掉了这个 `%`，已补回归用例。

## 线上巡检（2026-10-12，只读）

通过 `ssh wechat-prod`（`203.0.113.10`，root，`~/.ssh/config` 已有免密条目）执行，全程只读，未改动服务器。

**运行状态正常**

- `wechat-converter.service` active 且 enabled，运行 2 天；`curl 127.0.0.1:18080/api/v1/health` 返回 `status: ok`、`database: mysql`。
- 3 天内响应码只有 200/422/404，**无 5xx**、无 traceback、无 OOM kill；磁盘 23 GiB 可用（40%）。
- 部署形态是**扁平拷贝而非 git 检出**：巡检当时 `/opt/wechat-converter-api/app/` 只有 `main.py`、`mflac_fallback.py`，没有 `helper_api.py`/`helper_store.py`，`.env` 中 `HELPER_ENROLLMENT_SECRET` 行数为 0 —— 与「helper 未部署」一致。因此本次部署用 `scp` 覆盖而非 `git pull`（当天稍后已完成，见下节）。
- 运行栈为 1Panel + Docker：`1Panel-openresty-iPSG`（80/443）、`1Panel-mysql-BHnV`、`1Panel-redis-4vLB`、Adminer（`127.0.0.1:9853`）、phpMyAdmin（`127.0.0.1:8089`）。

**需要处理**

- **3306 对公网开放**：从公网 `198.51.100.20` 直连 `203.0.113.10:3306` 收到真实 MySQL 8.4.11 握手（`caching_sha2_password`）。容器绑定 `0.0.0.0:3306` 且安全组放行；应用只用 `127.0.0.1`，无公网必要。
- Redis `6379`、1Panel 面板 `40555` 同样绑定 `0.0.0.0`，目前仅靠安全组拦截（主机 `ufw` 为 inactive），属单层防护。
- 部署目录 `/opt/wechat-converter-api` 及 `app/`、`deploy/` 权限为 `drwxr-xrwx`（other 可写）。
- 主机 1.6 GiB 内存、**无 swap**、空闲约 42 MiB，而单次转换允许 1 GiB 上传并调用 LibreOffice/FFmpeg。

**9853/8089 安全组清理已复核**：两者从公网均不可达，且只监听 `127.0.0.1`。

**公网 HTTPS 连接重置：已定位，非服务端问题**

从本机（`198.51.100.20`）访问 `https://api.example.com/` 时 TLS 握手被重置（`curl` exit 35，3/3 稳定复现），但证据指向链路而非服务器：

1. openresty 日志中该 IP 只有两条 **80 端口**请求（301 跳转），**没有任何 443 记录** —— 说明重置发生在到达服务器之前。
2. 在服务器上访问自身公网 IP 与 `127.0.0.1` 均返回 200；不经该中间链路的路径完全正常。
3. 不带 SNI 的握手能到达 openresty 并收到正常的 TLS alert，说明服务端 TLS 本身健康。
4. 独立云端抓取（Firecrawl）访问同一 URL 返回 **200** 与正确 JSON，证明公网其他位置可正常访问。
5. 附带发现本机 DNS 被污染：`api.example.com` 额外解析出 `198.41.0.4`（即 `a.root-servers.net`），而 `119.29.29.29`、`8.8.8.8` 只返回正确 IP。

结论：服务器与网关配置无需修改；症状出现在特定客户端网络的 TLS/SNI 链路上。

## 当前阻塞

**只差合法授权密钥，其余每一环都已打通并上线。**

「手机上传 `.mflac` → 拿到 `.flac`」这条链路现在完整实现并实测过：上传得 202 + job_id，小程序轮询状态，本机 helper 领取、下载、解码、回传，小程序拿到下载链接。唯一的缺口是 helper 没有密钥，因此对 PC MusicEx 文件会走到 `failed` 并回报可读的 `no_authorized_key_source`。

**用户已决定不再等待密钥**：界面不再承诺 helper 会完成（见下节 `d48bccd`），该文件现在被明确告知为不支持。队列代码保留，但这条链路**在产品意义上已经关闭**。若日后拿到合法密钥来源，改回承诺只需还原文案。

能解开只有两条路：一份导出的 `player_process_db`（`key_source = player_process_db`），或一个合法 EKey 字符串（`key_source = ekey_file`）。已全盘确认本机 `C:`、`D:` 不存在任何 `player_process_db` 或 `.ekey`。

服务端 `HELPER_ENROLLMENT_SECRET` 已配置，`/api/v1/helper` 与 `/api/v1/jobs` 均正常。

## 手机上传链路（2026-10-12 完成并上线）

需求「手机小程序上传 `.mflac` → 服务器返回 `.flac`」除密钥外全部实现，服务端与小程序两侧均已部署。

**服务端**（`c9668dd`）：`POST /api/v1/convert/media` 遇到无内嵌 EKey 的 `PcV2MusicEx` 容器时不再 422，而是把源文件移入 helper 源目录、返回 `202 {status:"queued", job_id, status_url}`。判定用 `mflac_fallback.requires_helper()` 读 footer；只有在 QMC2 与 mask fallback **都真的失败之后**才走这条路，所以原本能解的文件行为不变（有测试守着）。上传行以 `helper_queued:<job_id>` 收尾，客户端据此区分"已转交"与"真失败"。

**小程序**（`53c9a89`）：识别 202，轮询 `status_url`（5 秒一次、上限 60 次），成功复用同一套结果卡片，失败展示服务端 `error_message`；`onUnload` 清理定时器；等待期间显示专用卡片说明"密钥不在文件内、需本机助手解码"；历史列表把 `helper_queued:` 行渲染为"已转交本机助手解码"。

**线上实测完整回路**：真实 39 MB 样例上传 → `202 job 2 queued` → helper 领取并下载 → 判定无密钥 → 回报 `no_authorized_key_source` → 客户端轮询看到 `failed` 与可读错误信息。两侧临时文件均已清理。

**顺带试过但没有出路的两条**：`mflac_fallback` 的掩码法（不需要密钥）在真实文件上实测失败 `MFLAC mask could not be detected`；`KGMusicConverterPro-v1.3.7.exe` 是未签名、时间戳清零、内嵌 CPython 3.11 且载荷 gzip 压缩的打包程序，其密钥获取方式未做逆向（等于复刻绕过 DRM 的密钥提取器，不做）。

**已知瑕疵**：一次上传会留下两条历史记录（`helper_queued:` 标记行 + helper 成功行），渲染层已处理显示，记录本身仍重复，记在 `TASKS.md`。

## 小程序全页自检修复（2026-10-12，提交 `504f6d4`、`51eaeee`）

对 `pages/index` 做了一次整页排查，每个问题都先用桩化的 `wx` 运行时复现再改，新增回归测试 `tools/miniprogram-flow.test.js`（11 项全通过，`node tools/miniprogram-flow.test.js`）。

**播放链路（`504f6d4`）**：`wx.openDocument` 只能渲染文档与 PDF，之前被误用于音视频，导致两个可见故障 —— 内嵌播放器已出现后仍调 `openDocument`，于是"下载并播放"必定叠加弹一个 `无法打开此文件`；历史记录里的音频点击后直接报 `无法直接打开`，完全放不出来。现在音视频交给工作台播放器，PDF/文档保持原行为。

**假数据（`504f6d4`）**：首页仪表盘把 `+ 24.8%`、`99% 服务可用率` 写死成字面量，配着"你的效率正在上升"和装饰性折线，看上去像在汇报并不存在的分析数据。现已改为真实状态：文件是否就绪、API 是否连通、本机文件真实数量、链接有效期（这个确实是 30 天）。

**状态残留（`504f6d4`）**：上一次上传或切换模式遗留的 helper 任务会继续显示在屏幕上，因为 `startConversion`、`clearFile`、`chooseMode` 只重置了 `result` 没重置 `job`。

**其他（`504f6d4`）**：`app.json` 是浅色导航栏配近黑页面，已统一；`miniprogramRoot` 为 `"./"` 且无忽略规则，`miniprogram/README.md` 被打进了上传包，现经 `packOptions.ignore` 排除（文件保留在仓库，只是不随包上传），流程测试也放在 `tools/` 而非随包发布。

**废弃 API（`51eaeee`）**：`loadSavedFiles` 在 `onLoad` 和切 tab 时各调一次 `wx.getSavedFileList`，开发者工具报两次"即将废弃"。改用 `wx.getFileSystemManager().getSavedFileList`，旧调用仅作为旧基础库的兜底保留。

**未验证**：上述修复经桩化运行时与静态检查验证，**仍未在开发者工具或真机端到端实跑**；上一次会话曾在开发者工具中做自动化驱动的尝试，但在截图脚本刚写好时被中断，未产出结论。

## 用户界面不再承诺 helper 能完成（2026-10-12，提交 `d48bccd`）

用户决定**保留代码、移除承诺**：helper 队列与轮询继续留在仓库里（它们是可用的队列实现，也是简历项目的实际内容），但小程序界面不再暗示"助手正在处理、稍后会有结果"——那个承诺无法兑现，因为 PC MusicEx 文件里没有密钥，当前也没有合法的密钥来源。

改写的是文案与注释，**没有动任何控制流**：

- 等待卡片由"等待本机助手"改为"该文件暂不支持"，并明确说明密钥不在文件内、可用的替代格式是 QMC0/QMC2。
- 历史记录行由"已转交本机助手解码"改为"暂不支持该文件格式"；展开详情不再声称结果会出现在记录中。
- 超时文案原先让用户"确认本机助手正在运行后重试"——启动助手也不会有用，已删除该指引。
- 选择文件的副标题与格式提示原先称 MFLAC 可自动解密，现已改为按实际能力描述。

保留机制的理由：即使永远解不开，它仍能把文件从"一个笼统的失败"变成**可读、准确的说明**，这是产品价值，与密钥能否取得无关。

**注意**：`windows-helper/` 目录、服务端 `helper_api.py` / `helper_store.py` 及其测试**全部保留未动**；若日后要彻底移除，需一并处理 `docs/decisions/004-helper-protocol.md` 与本文件的相关章节。

## 公开仓库整理（2026-10-12，提交 `d0f0db7`）

用户要求把积压的工作推送到 GitHub。推送前发现远程停在 `434373a`（2026-09-28），**本地领先 32 个提交**，即两周工作全部只存在于本地。

**脱敏**：9 个文件含真实基础设施标识（服务器 IP、家庭宽带 IP、API 域名、证书路径）。由于 `CURRENT.md` 同时记录着"3306 对公网开放"，直接推送等于把"这个 IP 的数据库端口开着"一并公开。已全部替换为文档保留地址 `203.0.113.10` / `198.51.100.20` 与 `api.example.com`。

**两个不能简单替换的文件**：`miniprogram/config.js` 与 `windows-helper/helper/config.py` 里的域名是**运行时默认值**，不是文档——直接改会让小程序和助手连不上服务器。改为读取被 git 忽略的本地覆盖（`config.local.js` / `helper/local_config.py`），读不到才回落到占位符。两种路径都已实测：本地有覆盖时解析出真实地址，模拟全新克隆时回落到占位符且测试照常通过。

**结果**：`434373a..d0f0db7` 推送成功，本地与远程一致（ahead 0）。仓库名已由 `WeChatPlyaer` 改为 `WeChatPlayer`（用户自行在网页操作）。

## README 重写（2026-10-12）

原 README 严重过期：目录结构只画了 `converter-api/`，且在结尾写着"**尚未提供自动化测试套件**"——而实际有 89 项离线测试。这句话恰好把项目最强的一项说没了。已重写为覆盖三端架构、测试、队列协议与安全边界的完整说明。

关键修正：测试章节按**可复现**的方式写，命令均从仓库根目录逐字验证过（36 + 42 + 11 = 89）；集成测试 6 项单独标注需要真实服务。另新增「已知限制」章节，如实说明 PC MusicEx 无法解密及不包含 DRM 绕过功能。

## 小程序代码质量检测修复（2026-10-12）

开发者工具「代码质量」面板两项未通过，均已修复：

- **组件按需注入**：`app.json` 缺 `lazyCodeLoading`，补 `"requiredComponents"`。本项目只有 `pages/index` 一个页面、无自定义组件，开启后无兼容风险。
- **图片和音频资源超 200 K**：4 个导航图标为 300×300 RGBA、合计 **370.8 KB**，而实际显示尺寸仅 `58rpx`（约 29-33 px）——存在约 3.6 倍冗余。已缩到 **120×120** 并降至 **256 色调色板**，合计 **22.6 KB（-94%）**，远低于阈值。
  - 保留 alpha 通道（`tRNS`，alpha 0-255）：底栏背景是 `rgba(35,40,63,.96)` 半透明，**不能**把图标合成到纯色背景上。
  - 已按真实显示尺寸（33 px）与放大尺寸（120 px）生成对比图核对，肉眼无明显差异。
  - 原始图标可用 `git checkout HEAD~1 -- miniprogram/assets/nav/` 取回。

## 3306 复核：公网已拦住，但绑定那层没生效（2026-10-12）

用户表示已把 3306 改为指向 `127.0.0.1`，实测复核结论是**只完成了一半**。

**已验证（公网侧，从本机）**：`3306` 返回 `TIMEOUT/FILTERED`；同批测试的 `443` 正常连通，作为对照排除了本机网络问题。公网开放端口只剩 `22`、`80`、`443`、`40555`；`6379`、`8080`、`8089`、`9853`、`18080` 全部被拦。

**但绑定并未改动（服务器侧实测）**：

- `ss -ltn` 仍显示 `0.0.0.0:3306`
- Docker 仍是 `0.0.0.0:3306->3306/tcp`
- 容器 `1Panel-mysql-BHnV` 创建时间为 `2026-09-28`，**从未重建**
- 主机 `ufw` inactive，iptables 无 3306 拦截规则

因此当前的拦截**只来自安全组**，属于单层防护。用户实际完成的是安全组那一层。要真正生效，必须在 1Panel 里改端口映射并**重建容器**（这就是没生效的原因：Docker 的端口映射改动只有重建容器才会应用）。

**应用未受影响**：`/api/v1/health` 返回 `database: mysql`、`helper_queue: ready`，`wechat-converter.service` active。

**新发现**：`40555`（`1panel-core`）从公网可达，返回 `HTTP 200 / Access Temporarily Unavailable`——1Panel 的安全入口在起作用，但端口本身开放，建议改绑或加白名单。

## 3306 绑定已修复（2026-10-12）

**根因**：`/opt/1panel/apps/mysql/mysql/.env` 里 `HOST_IP=''`（空值）。compose 写作 `${HOST_IP}:${PANEL_APP_PORT_HTTP}:3306`，空值渲染成 `:3306:3306`，Docker 就绑定所有网卡。同目录的 adminer、phpmyadmin 本就是 `HOST_IP='127.0.0.1'`，**只有 mysql 和 redis 是空值**——这也解释了为什么用户看到"数据库指向公网地址"。

**动作**：`mysqldump` 全量备份（`/root/db-backup-20261012-215827.sql`，3.7 MB / 40 张表）→ `.env` 备份 `.env.bak-20261012-215838` → 改 `HOST_IP='127.0.0.1'` → `docker compose up -d` 重建容器，MySQL 约 3 秒就绪。

**验证**（全部实测）：

| 检查 | 结果 |
|---|---|
| 服务器监听 | `127.0.0.1:3306` ✅（原 `0.0.0.0:3306`） |
| Docker 映射 | `127.0.0.1:3306->3306/tcp` ✅ |
| 公网可达性 | `3306`、`33060` 均 `TIMEOUT/FILTERED` ✅ |
| 应用健康 | `database: mysql`、`helper_queue: ready`、service active ✅ |
| 数据完整性 | `wechat_converter` 4 张表 ✅ |
| Adminer / phpMyAdmin | 容器内实测均能连上 ✅ |

数据目录是 bind mount（`data/` → `/var/lib/mysql`），重建容器不影响数据。

**容器内外的地址差别（易踩坑）**：Adminer / phpMyAdmin 自身在容器里，登录框**不能填 `127.0.0.1`**（那指向它自己），也不能填公网 IP（已失效），应填容器名 **`1Panel-mysql-BHnV`**。

**同类问题仍未修**：Redis（`HOST_IP=''`，实测 `0.0.0.0:6379`）与 1Panel 面板自身（`40555`，公网可达）。面板不是 1Panel 应用，不能用 `HOST_IP` 修，需在面板设置里配安全入口/授权 IP 或加安全组白名单。

## 运维化改造：Docker + CI（2026-10-12）

按「方向选择与项目建议」的结论，为项目补上运维外壳，使同一个项目可以按运维方向讲述。

**新增文件**：`converter-api/Dockerfile`、`converter-api/.dockerignore`、`docker-compose.yml`、根 `.env.example`（供 Compose 用）、`.github/workflows/ci.yml`、`docs/docker-and-ci.md`。

**Dockerfile 的取舍**：两阶段构建（依赖装进 builder 的独立前缀，只复制该前缀）；文档转换只装 `libreoffice-writer` 而非完整 `libreoffice` 元包；**预装 `fonts-noto-cjk`**（缺字体时中文 PDF 全是空白方块，这是本项目特有的必要项）；非 root 用户（uid 10001）运行，因为服务要用子进程解析不可信的上传文件。

**Compose 的两处安全设计**（复刻 3306 事故的教训）：MySQL **完全不发布主机端口**，api 经 Compose 网络按服务名 `mysql` 连接；api 只绑 `127.0.0.1:18080`，不写裸 `18080`（省略主机 IP 就等于 `0.0.0.0`）。

**CI**：三个任务——Python 测试（36+42）、小程序流程测试（11）、**镜像构建 + 启动容器 + 等待健康检查通过**。第三个任务刻意不只做构建，因为"能构建"不等于"能运行"。

**⚠️ 验证状态（重要）**：

- ✅ **已验证**：`docker compose config` 完整解析通过（该命令不需要引擎），确认**没有任何服务绑定 `0.0.0.0`**、健康检查字符串、卷映射、`depends_on` 条件均正确；`docker-compose.yml` 与 `ci.yml` 的 YAML 语法校验通过；Dockerfile 中 `COPY` 引用的路径逐一确认存在；确认运行时唯一需要的可写路径（`CONVERTER_TMP_ROOT`，`download_dir` 也派生自它）已在镜像中创建并授权。
- ❌ **未验证**：**镜像能否真正构建成功、容器能否正常提供服务，本机没有任何证据**。本机 Docker 引擎无法启动，原因见下节。**不要当作已验证**。推送后由 CI 的 `docker-build` 任务在 GitHub runner 上完成真实构建与启动验证（该路径不依赖本机 Docker）。

## 本机 Docker 不可用（2026-10-12 发现）

本机 Docker Desktop 无法工作，`docker info` 全程返回 500：

```
WSL 报错：当前计算机配置不支持 WSL2。
请启用"虚拟机平台"功能，并确保在 BIOS 中启用虚拟化。
```

`docker-desktop` WSL 发行版为 Stopped，`wsl -d docker-desktop` 报 `HCS_E_SERVICE_NOT_AVAILABLE`，`com.docker.service` 为 Stopped。故本机**无法构建或运行任何镜像**。修复需要启用 Windows「虚拟机平台」功能并在 BIOS 开启虚拟化后重启——属于用户的机器配置，未代为改动。

另记一次自身失误：启动 Docker Desktop 后我用 `docker info ... 2>$null` 加 `$LASTEXITCODE` 判断"就绪"，**把错误信息重定向掉了，因此误判为已就绪**，随后的构建才暴露问题。正确的就绪判据是拿到非空的 `ServerVersion`。

## CI 首次运行即发现真实缺陷：测试依赖未声明（2026-10-12）

CI 第一次运行的结果：小程序流程测试（11 项）**通过**；服务端协议测试（36 项）**失败**；Docker 构建当时仍在进行。

**根因**：`converter-api/tests/test_helper_protocol.py` 使用 `from fastapi.testclient import TestClient`。`fastapi.testclient` 是 `starlette.testclient` 的转发，而后者**依赖 `httpx`**，但 `httpx` **不在 `requirements.txt`**（它是测试依赖，不是运行依赖）。本机恰好装了 `httpx 0.28.1`，所以本地一直是"侥幸通过"。

**影响**：任何克隆仓库、只安装 `requirements.txt` 的人**都无法运行那 36 项测试**。而 README 当时写的"克隆后无需安装依赖即可运行"是**错误陈述**，已在本次一并修正（README 与两份简历中的同款表述）。

**定位方法**：用 sys.meta_path 钩子屏蔽 `httpx` 后导入 `fastapi.testclient`，确认必然抛 `ImportError`，从而排除其他可能。

**修复**：新增 `converter-api/requirements-dev.txt`（`-r requirements.txt` + `httpx==0.28.1`），CI 改装该文件并同时纳入 pip 缓存键。Dockerfile 仍只用 `requirements.txt`，保持运行镜像不含测试依赖。

**验证**：建立全新虚拟环境、**只安装声明过的依赖**，按 CI 的调用方式运行，36 + 42 项全部通过。

**教训（已写入 `docs/docker-and-ci.md`）**："在我机器上能跑"往往意味着依赖了本机碰巧存在的东西；干净环境才能暴露；发现问题要连文档一起改。

## CI 第二轮又发现一个缺陷：Windows 组件的测试被放在 Linux 上跑（2026-10-12）

`httpx` 修好后，36 项服务端测试通过、**Docker 构建任务也通过了**（镜像真实构建 + 启动 + 健康检查成功，Dockerfile 至此得到实测验证），但 **42 项助手单元测试失败**。

**根因**：`windows-helper/helper/agent.py` 的进程门禁实现为"检查跑不起来就放行"：

```python
def _process_running(name):
    try:
        result = subprocess.run(["tasklist", ...])
    except (OSError, subprocess.SubprocessError):
        return True          # fail open
    return name.lower() in (result.stdout or "").lower()
```

- **Windows**：`tasklist` 存在、查不到该进程 → `False` → 拦住 → 测试通过。
- **Linux**：无 `tasklist` → `OSError` → **`True`** → 放行 → 任务被领取 → `test_required_process_gate_can_block_work` 断言失败。

**这是设计错误而非代码错误**：助手是 Windows 程序（另有 DPAPI 凭据保护同样只在 Windows 生效），其测试本就应在 Windows 上运行。原先把它与 Linux 的服务端测试塞在同一任务里，是我图省事。

**修复**：CI 拆成 `server-tests`（ubuntu）与 `helper-tests`（**windows-latest**），后者在该组件真实的运行环境上验证。

**另记一个设计问题（未擅自改动）**：`_process_running` 是 **fail open**——在任何没有 `tasklist` 的环境下该门禁形同虚设。生产即 Windows，故不影响实际使用，但"安全检查在无法判定时应倾向拒绝"是通常做法。已记入 `TASKS.md` 待用户决定。

**至此两轮 CI 共发现两个真实缺陷**（`httpx` 依赖未声明、测试平台选错），Dockerfile 与 compose 得到实测验证。

## 服务器侧暴露面收敛完成（2026-10-12）

用户已在安全组把 3306 来源改为 `127.0.0.1`（效果上已挡住外部），并要求我把**服务器侧**的部分做完。

**Redis `6379`（同一缺陷）**：`/opt/1panel/apps/redis/redis/.env` 同样是 `HOST_IP=''`。修复前先确认**无任何客户端连接**（`ss -tn state established` 为空）、数据目录仅 4 KB，故重建无风险。改 `HOST_IP='127.0.0.1'` → 重建容器 → **2 秒恢复**，现监听 `127.0.0.1:6379`。

**1Panel 面板 `40555`（无 HOST_IP 可用）**：面板不是"1Panel 应用"，且 `1pctl listen-ip` 只支持 `ipv4`/`ipv6`，**不能指定绑定地址**。改用面板自身的配置项 `BindAddress`（原值 `0.0.0.0`）：

- 备份 `core.db` → `1pctl stop core` → 用 Python `sqlite3`（stdlib，服务器上没装 sqlite3 命令行）改 `BindAddress='127.0.0.1'` → `1pctl start core` → 验证。
- 先停后改，避免运行中的进程把改动覆盖回去；脚本内置**失败自动回滚**（还原 core.db 并重启）。
- 结果：监听 `127.0.0.1:40555`，本机 `curl` 返回 `HTTP 200`，`1pctl status core` 为运行中。

**最终监听状态（服务器侧实测）**：

| 端口 | 绑定 | 说明 |
|---|---|---|
| 22 / 80 / 443 | `0.0.0.0` | 对外服务，符合预期 |
| 18080 | `127.0.0.1` | 应用（Nginx 反代） |
| 3306 | `127.0.0.1` | MySQL |
| 6379 | `127.0.0.1` | Redis |
| 40555 | `127.0.0.1` | 1Panel 面板 |
| 8089 / 9853 | `127.0.0.1` | phpMyAdmin / Adminer |

**公网复扫**：`3306`/`6379` 为 FILTERED，`40555` 为 REFUSED（安全组仍放行但公网接口已无监听），`9853`/`8089`/`18080` 全部 FILTERED。**公网开放的只剩 22/80/443。** 应用健康检查正常（`database: mysql`、`helper_queue: ready`），`wechat-converter.service` active。

⚠️ **面板访问方式已变**：不再能通过 `公网IP:40555` 打开，需走隧道 `ssh -N -L 40555:127.0.0.1:40555 wechat-prod`，浏览器访问 `http://127.0.0.1:40555/`（仍需带安全入口路径）。回滚：`1pctl stop core` → `BindAddress` 改回 `0.0.0.0` → `1pctl start core`。

**待用户处理（安全组侧）**：建议**删除** 3306 那条来源为 `127.0.0.1` 的规则（对外永不匹配，属绕路写法，留着易误解），并删除 40555 的 `0.0.0.0/0` 规则。

## CI 全绿（2026-10-12，run #3，提交 `9c4adb8`）

四个任务全部通过：`server-tests`（ubuntu）、`helper-tests`（**windows-latest**）、`miniprogram-tests`（ubuntu）、`docker-build`（ubuntu）。

**`docker-build` 通过的意义**：它不是"构建成功"而已——镜像构建 → 容器启动 → 轮询 `/api/v1/health` 拿到响应才算通过。**至此 `Dockerfile` 与镜像的运行时行为得到实测验证**，补上了本机 Docker 不可用留下的验证缺口。

**为达成全绿修掉的两个真实缺陷**（都是 CI 发现的，不是猜的）：

1. **run #1 失败**：测试依赖 `httpx` 未声明 → `fastapi.testclient` 无法导入 → 36 项测试在干净环境跑不起来。见上文专节。
2. **run #2 失败**：Windows 助手的测试被放在 Linux 上跑 → `_process_running` 的 fail-open 分支导致门禁放行 → 断言失败。见上文专节。

**CI 的实际价值已经体现**：两轮运行各暴露一个真实缺陷，其中一个（依赖未声明）会让任何克隆仓库的人无法运行测试。这也说明**"本地通过"不能替代在干净环境验证**。

## 小程序提审前的错误处理修复（2026-10-12，提交 `dfd7323`）

为提审（版本 0.0.7）做了一轮代码体检，发现 `pages/index` 里 **17 个异步调用只有 16 个 `fail` 处理器**——缺的正是 `wx.chooseMessageFile`。

**为什么这个缺陷在提审场景下要紧**：`chooseMessageFile` 是隐私接口，用户在隐私弹窗上点「拒绝」时该调用会失败；而没有 `fail` 处理器意味着**按钮点了毫无反应**。审核员恰恰很可能用「拒绝授权」来测试小程序，看到的就是一个像坏掉的界面。

**修复**：区分两种情况——取消不算失败、保持安静；授权被拒则明确提示"需要授权后才能选择文件"；其余情况回退为通用重试提示。

**新增 2 项回归测试，并且验证过它们真的有效**：对着修复前的 `index.js` 运行，隐私那条以 `a refused prompt must produce feedback` 失败（12/13）；修复后 13/13 通过。取消那条在修复前后都通过——**它防的不是原 bug，而是防止我修过头**（若不加 `/cancel/i` 判断，取消也会弹提示，它就会失败）。

**测试计数同步更新**：前端流程 11 → **13**，离线合计 89 → **91**（36 服务端协议 + 42 助手单元 + 13 前端流程）。README、`docs/docker-and-ci.md` 与两份简历中的旧数字已一并修正。

**一次自身失误（已记录）**：为对比修复前后，我用 `git show ... | Set-Content -NoNewline` 取旧版本，PowerShell 把多行**拼接成了一行**，文件被截断。已从 HEAD 恢复，并改用 `git checkout` + 编辑工具重做对比。

## CI 偶发失败：定时敏感的测试（2026-10-12）

**发现**：run #7（提交 `6455b92`，纯文档改动）失败，但**同批测试在 run #3 是绿的**，且这期间**没有任何提交改动 `windows-helper/`**。逐任务核对后确认失败的是 `helper-tests`（windows-latest），而 `server-tests`、`miniprogram-tests`、`docker-build` 均通过。

**排除法**：本地连续运行 6 次助手测试全过、耗时稳定 4.05 秒 → 不是确定性缺陷。原始日志需要登录才能读（注解接口只给到 `exit code 1`）。

**根因（代码层定位）**：3 个 lease keeper 测试依赖**固定短睡眠**等待后台线程，余量极小：

| 测试 | 依赖 | 余量 |
|---|---|---|
| `test_lease_keeper_extends_the_lease_while_working` | 睡 0.3s，心跳间隔 0.05s | 尚可 |
| `test_lease_keeper_marks_the_lease_lost_on_conflict` | 睡 **0.2s** 等 `keeper.lost` | **很薄** |
| `test_lease_keeper_marks_the_lease_lost_when_revoked` | 睡 **0.2s** 等 `keeper.lost` | **很薄** |

共享 runner 上线程调度延迟几百毫秒是常事，睡 0.2 秒就可能来不及 → 偶发失败。

**修复**：新增模块级 `wait_until(predicate, ceiling=5.0)`，把"睡固定时长后断言"改为"**等到条件成立**，仅在真失败时用上限兜底"。这消除了对调度时序的依赖，同时因为不再空等**反而更快**（本地 4.05s → 3.5s）。本地连续 8 次全过。

**顺带修的**：CI 里 `miniprogram-tests` 的任务名还写着"(11)"，已改为 (13)。

**顺带加的**：助手测试步骤改用 `shell: pwsh`，失败时把具体失败测试**写成 GitHub 注解**。因为原始日志需要登录才能读，而注解可以通过 API 读到——这正是本次排查卡住的地方。已用 PowerShell 语法解析器校验该脚本可解析。

## 工作区

当前分支为 `main`。工作区仅剩用户原有的未跟踪 `.obsidian/`，以及未跟踪的 `resume/`（个人简历与求职分析，**不建议公开**，如需保留请加入 `.gitignore`）。`windows-helper/config.ini`、`miniprogram/config.local.js`、`windows-helper/helper/local_config.py` 按预期被忽略。

本地 Windows 沙箱中发现三个环境级现象，已规避，不影响生产（Linux）：

- `tempfile.mkdtemp` 在受限令牌下会因 `PermissionError` 重试循环而卡住；`helper_api.py` 因此改用自有的 `_unique_dir`，不再依赖 `tempfile`。
- `os.mkdir(mode=0o700)` 在受限令牌下会创建自己都写不进去的目录；`main.py` 的下载目录只在 POSIX 上应用 `0o700`。
- `python -m py_compile` 写入 `converter-api/app/__pycache__` 会被拒绝（该目录既有权限不含当前用户），需用 `PYTHONPYCACHEPREFIX` 指向可写目录。

另：`.git` 目录归属另一用户 SID，受限令牌下 `git add` 无法创建 `index.lock`，需以完全权限运行。

## 下一步

1. **关闭 3306 公网放行**：安全组删除入站规则，并把容器端口绑定改为 `127.0.0.1:3306`（外层+内层两层防护）。详见 `TASKS.md`。
2. 完成 `TASKS.md` 中的 P1 验证：开发者工具/真机实跑，以及小程序那批修复的端到端确认。
3. 复核 Redis `6379` 与 1Panel `40555` 的绑定方式，以及部署目录权限 `drwxr-xrwx`。
4. 注意 helper 目前只在测试时经 SSH 隧道连通；真实部署到其他 Windows 主机时应使用自己的公网地址。

**已关闭，不再推进**：PC MusicEx 密钥（用户已决定放弃，见上文 `d48bccd` 一节）。

## 约束

不提交凭据、私钥、证书、真实用户媒体、GUI 二进制或运行时文件；不把 EKey 写入小程序存储、URL、数据库或普通日志。helper 只上传解密后的 FLAC，服务端永不接收 EKey。`*.ekey`、`windows-helper/config.ini` 和 `windows-helper/state/` 已加入 `.gitignore`。
