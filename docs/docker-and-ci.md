# Docker 与 CI 入门：以本项目为例

这份文档面向**没系统学过 Docker/CI** 的读者，用本仓库的真实文件讲清楚三件事：镜像怎么构建、本地怎么跑起来、CI 自动做了什么。

---

## 一、Docker 到底解决什么问题

不用 Docker 时，你的部署是这样的（也就是你现在的生产环境）：

```
服务器上手动装好 python3、libreoffice、ffmpeg、nginx、mysql
→ 建 venv → pip install → 配 systemd → 启动
```

问题在于：**这些步骤只存在于你的记忆和某个文档里**。换一台服务器，你得重来一遍，而且很容易漏（比如忘了装中文字体，中文 PDF 就全变方块）。

Docker 的思路是：把"装依赖 + 放代码 + 配启动命令"写成一个**文件**，然后由它生成一个**可分发的包**。这个包在哪台机器上跑，环境都完全一样。

三个词先分清：

| 词 | 是什么 | 类比 |
|---|---|---|
| **镜像（image）** | 只读的模板，含操作系统层 + 依赖 + 代码 | 一张装系统的光盘 |
| **容器（container）** | 镜像跑起来的一个实例 | 用光盘装出来的一台机器 |
| **仓库（registry）** | 存放镜像的地方 | 光盘的网盘 |

**关键点：镜像由文件定义，可重复构建。** 这就是它比"手动装一遍"可靠的地方。

---

## 二、逐行讲你的 `converter-api/Dockerfile`

```dockerfile
FROM python:3.12-slim AS builder
```
`FROM` 是起点。`python:3.12-slim` 是官方 Python 镜像的精简版（不含编译器、文档等）。
`AS builder` 给它起个名字，后面第二个阶段要引用它——**这叫多阶段构建**。

```dockerfile
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
```
- `COPY requirements.txt .`：把依赖清单复制进镜像（`.` 表示当前 `WORKDIR`）
- `RUN`：构建时执行的命令
- `--prefix=/install`：**把包装到一个独立目录里**，而不是散在系统路径中。这样下一阶段可以直接把这一个目录整体搬走
- `--no-cache-dir`：不保留 pip 下载缓存，减小体积

**为什么要单独一个 builder 阶段？** 因为构建 Python 包时常需要编译器、头文件等，这些在运行时根本用不到。分两阶段，最后镜像里只有"装好的包"，没有编译器。

```dockerfile
FROM python:3.12-slim
```
第二个阶段，**重新从干净的镜像开始**。上面 builder 里的东西不会自动带过来。

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        ffmpeg \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
```
装系统级依赖。这里有三处刻意的取舍：

- **`libreoffice-writer` 而不是 `libreoffice`**：完整元包还会带上 Calc、Impress、Draw、Base，体积大很多，而你的 API 只处理文档转 PDF，writer 足够
- **`fonts-noto-cjk`**：**这个不是可选项**。不装中文字体，中文文档转出的 PDF 里**每个字都是空白方块**
- **`rm -rf /var/lib/apt/lists/*`**：apt 的软件包索引装完就没用了，删掉能省下几十 MB。**这是 Dockerfile 里最常见的瘦身动作之一**

```dockerfile
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin converter
```
建一个**非 root 用户**。为什么重要：你的服务会用子进程调用 LibreOffice 去解析**用户上传的任意文件**——这是整个系统里最容易被攻击的一环。以 root 跑，一旦解析器被攻破就等于交出整台机器。

```dockerfile
COPY --from=builder /install /usr/local
```
**从 builder 阶段把装好的包搬过来。** 这一行是两阶段构建的落点。

```dockerfile
WORKDIR /app
COPY app ./app
```
只复制 `app/` 这一个目录。配合 `.dockerignore`，测试、README、`.env` 都不会进镜像——**`.env` 里有数据库密码，绝不能打进镜像**。

```dockerfile
RUN mkdir -p /var/lib/wechat-converter/tmp \
    && chown -R converter:converter /var/lib/wechat-converter
USER converter
```
建好转换用的临时目录，**把属主改成运行时用户**，然后 `USER` 切过去。顺序很重要：必须先 `chown` 再 `USER`，否则非 root 用户建不了目录。

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18080/api/v1/health', timeout=4).status == 200 else 1)"]
```
**健康检查**：Docker 每 30 秒自己探一次，连续失败就标记容器为 unhealthy。
用 `python -c` 而不是 `curl`，是为了不必再往镜像里装一个只为探测存在的工具。
`--start-period=20s` 给应用启动留时间，这段时间内失败不算数。

⚠️ **注意一个细节**：你的 `/api/v1/health` **即使连不上数据库也返回 200**，只是在 body 里写 `"database": "unavailable"`。所以这个探针只能证明"进程活着"，**不能证明它能连上数据库**。真正的就绪检查应该读 body。我在 Dockerfile 注释里写明了这一点——**区分"存活（liveness）"和"就绪（readiness）"是运维很基础但很容易搞混的概念**。

---

## 三、`docker-compose.yml`：一次启动一组服务

Dockerfile 只能描述**一个**容器。但你的服务还需要 MySQL。Compose 就是用来描述"一组容器怎么一起跑"的文件。

```bash
cp .env.example .env     # 填入密码
docker compose up --build
```

几个值得注意的设计：

**1. MySQL 故意不发布端口**

```yaml
  mysql:
    # 没有 ports: 这一段，这是刻意的
```

api 容器通过**服务名 `mysql`** 在 Compose 内部网络里连数据库：

```yaml
      MYSQL_HOST: mysql      # 不是 127.0.0.1！
```

**这正是你 3306 事故的教训**：数据库根本不需要对外可达。如果确实要用图形客户端连，也只能绑回环：

```yaml
    # ports:
    #   - "127.0.0.1:3306:3306"    # 绝不能写 0.0.0.0
```

**2. `127.0.0.1:18080:18080` 而不是 `18080:18080`**

三个部分的含义是 `主机IP:主机端口:容器端口`。**省略主机 IP 就等于 `0.0.0.0`**——这跟你 1Panel 里 `HOST_IP=''` 导致绑定全网卡是**完全同一个坑**。

**3. 等健康检查通过再启动**

```yaml
    depends_on:
      mysql:
        condition: service_healthy
```

MySQL 容器"启动完成"和"能接受连接"之间差好几秒。只写 `depends_on: [mysql]` 会出现 api 先起来、连不上库然后崩溃重启的循环。`condition: service_healthy` 才会真的等。

**4. `$$` 的含义**

```yaml
      test: ["CMD-SHELL", "mysqladmin ping ... -p\"$$MYSQL_ROOT_PASSWORD\""]
```
Compose 会把 `$VAR` 在**宿主机上**替换掉。写 `$$` 是转义，让这个变量**传到容器里再由 shell 展开**——否则它会在宿主机上被替换成空值。

---

## 四、CI：让测试自动跑

**CI（持续集成）**的意思是：每次你推送代码，服务器自动跑一遍检查，结果直接显示在 GitHub 上。

你的 `.github/workflows/ci.yml` 里有三个任务：

| 任务 | 做什么 |
|---|---|
| `python-tests` | 装依赖，跑 36 项服务端协议测试 + 42 项客户端单元测试 |
| `miniprogram-tests` | 用 Node 跑 13 项前端流程测试 |
| `docker-build` | **真正构建镜像、启动容器、等健康检查通过** |

第三个任务特别值得说：**构建成功不等于镜像能用**。所以我让 CI 把镜像跑起来，反复请求 `/api/v1/health`，拿到响应才算通过；失败时自动打印容器日志。

这就是 CI 的价值：**它把你手动做的事变成每次提交都自动发生**。对运维岗来说，"我把测试接进了流水线"比"我写过测试"更有说服力。

触发条件写的是：

```yaml
on:
  push:
    branches: [main]
  pull_request:
```

推送和 PR 都会跑。`concurrency` 那段是"新推送取消同一分支上还在跑的旧任务"，避免浪费。

### CI 第一次运行就抓到了一个真实问题

这不是假设，是这个仓库真实发生的事，很能说明 CI 的价值：

**现象**：本地 89 项测试全过，推上去之后 CI 里"服务端协议测试（36 项）"**1 秒就失败了**——1 秒这个速度说明不是断言失败，而是**导入就崩了**。

**根因**：测试文件里有这一行：

```python
from fastapi.testclient import TestClient
```

`fastapi.testclient` 其实是 `starlette.testclient` 的转发，而它**建立在 `httpx` 之上**。但 `httpx` **不在 `requirements.txt` 里**——因为它是测试用的，不是运行用的。

**为什么本地能过**：我本机碰巧装了 `httpx`（别的项目带进来的）。所以测试通过是"侥幸"，不是"正确"。

**这意味着什么**：**任何人克隆这个仓库、只装 `requirements.txt`，都跑不了那 36 项测试。** 而 README 当时还写着"克隆后无需安装依赖即可运行"——这句话是错的。

**修复**：拆出 `converter-api/requirements-dev.txt` 显式声明测试依赖，CI 改装这个文件；并且用**全新的虚拟环境**（只装声明过的依赖）复现并验证，确认 36 + 42 项都能通过。

**这件事值得记住的三点**：

1. **"在我机器上能跑"往往意味着"依赖了我本机碰巧有的东西"**——干净环境才能暴露出来
2. **CI 第一次运行就找出一个真 bug，这就是它存在的理由**，而不是走个形式
3. 发现问题后要**连文档一起改**。README 那句"无需安装依赖"如果不改，下一个人还会踩

### 第二个问题：我把 Windows 程序的测试放到了 Linux 上跑

修好 `httpx` 之后，36 项服务端测试过了，**42 项助手测试又挂了**。这次的原因更值得讲。

助手有个"进程门禁"功能：配置里可以要求某个进程必须在运行（比如 QQ 音乐），否则不领任务。实现是这样的：

```python
def _process_running(name):
    try:
        result = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", ...])
    except (OSError, subprocess.SubprocessError):
        return True        # 检查跑不起来时，当作"在运行"
    return name.lower() in result.stdout.lower()
```

问题出在那个 `return True`：

| 平台 | `tasklist` | 结果 | 测试 |
|---|---|---|---|
| Windows | 存在，但查不到该进程 | 返回 `False` → **拦住** | ✅ 通过 |
| Linux | **不存在** → 抛 `OSError` | 返回 `True` → **放行** | ❌ 失败 |

有一个测试叫 `test_required_process_gate_can_block_work`，它断言"门禁应该拦住任务"。在 Linux 上因为上面那个 `return True`，任务被放行了，所以断言失败。

**根本问题在我的设计，不在代码**：助手是一个 Windows 程序（它用 `tasklist` 查进程、用 DPAPI 加密凭据文件），**它的测试本来就该在 Windows 上跑**。我图省事把它和 Linux 的服务端测试塞进了同一个任务。

**修复**：把助手测试拆成独立任务，跑在 `windows-latest` 上：

```yaml
  helper-tests:
    runs-on: windows-latest      # 不是一个平台，是这个组件真实的运行环境
```

**顺带值得注意的一点**：那个 `return True` 是"**检查不出来就放行**"（fail open）。这意味着**在任何没有 `tasklist` 的环境里，这个门禁等于不存在**。因为生产环境就是 Windows，所以不影响实际使用，但它是"检查失败时的默认行为该倾向哪边"的一个真实例子——安全相关的检查通常应该 fail closed。这个行为**我没有擅自改**，只把它记在了 `TASKS.md` 里。

**两轮 CI 一共找出两个真实缺陷。** 这就是为什么值得花时间接流水线。

---

## 五、怎么验证（重要：你本机 Docker 目前用不了）

我在这台机器上检查过，**你的本地 Docker Desktop 现在跑不起来**：

```
WSL 报错：当前计算机配置不支持 WSL2。
请启用"虚拟机平台"功能，并确保在 BIOS 中启用虚拟化。
```

`com.docker.service` 也是 Stopped。所以：

- **我无法在本地实测镜像构建**。Dockerfile 和 compose 我做了能做的最强校验：
  - `docker compose config` 完整解析通过（这不需要引擎），确认**没有任何服务绑定 `0.0.0.0`**、健康检查字符串正确、卷映射正确
  - YAML 语法校验通过
  - Dockerfile 里 `COPY` 引用的路径逐一确认存在
- **但"镜像真的能构建成功"这件事，我在这里没有证据**。请不要把它当成已验证。

**你有两条验证路径：**

**路径 A（推荐）：推送后让 CI 验证**

CI 的 `docker-build` 任务会在 GitHub 的机器上真实构建并启动容器。推送后打开仓库的 **Actions** 标签页看结果——**这条路不需要你本机 Docker 可用**。

**路径 B：修好本地 Docker**

1. 打开「启用或关闭 Windows 功能」
2. 勾选 **虚拟机平台**（Virtual Machine Platform）和 **适用于 Linux 的 Windows 子系统**
3. **重启电脑**
4. 确认 BIOS 里虚拟化（VT-x / AMD-V）是开启的
5. 启动 Docker Desktop，等待鲸鱼图标变绿

然后：

```bash
cp .env.example .env      # 改掉里面的密码
docker compose up --build
curl http://127.0.0.1:18080/api/v1/health
```

---

## 六、为什么这些对你的运维简历有用

你现在简历上能写的运维事实，很多**你已经做过但没意识到**：

| 你做过的事 | 对应运维能力 |
|---|---|
| systemd 服务加固（`NoNewPrivileges`、`ProtectSystem`…） | 服务最小权限 |
| 发现 3306 对公网开放并定位到 `HOST_IP` 空值 | 暴露面治理 + 根因定位 |
| 改端口前先 `mysqldump`、确认 bind mount | **变更管理**（运维核心） |
| 用 443 做对照证明"3306 被拦"不是网络问题 | 对照实验的排障方法 |
| 判断 HTTPS 重置是链路问题而非服务端 | 区分故障域 |

**加上 Docker + CI 之后，你还多了一条："我把部署写成了可重复执行的代码，并让测试自动跑。"** 这句话在运维岗面试里分量很重，因为它对应的是**可重复性**——运维最核心的价值观。

---

## 七、下一步可以做的

按投入产出比排序：

1. **推送后去 Actions 看 CI 是否绿**（这一步免费，且能验证 Dockerfile）
2. 给镜像加**体积优化**：目前含 LibreOffice + FFmpeg，镜像约 700 MB–1 GB。可以试试 `docker images` 看实际大小，再想办法压
3. 学 **Prometheus + Grafana**：给 `/api/v1/health` 做真实监控和告警
4. **别急着上 Kubernetes**：宁波的实习岗基本不要求，投入产出比低

---

## 附：常用命令速查

```bash
docker build -t wechat-converter-api:local ./converter-api   # 构建镜像
docker images                                                # 看镜像和体积
docker run -d -p 127.0.0.1:18080:18080 wechat-converter-api:local   # 跑一个容器
docker ps                                                    # 看运行中的容器
docker logs -f <容器名>                                       # 跟日志
docker exec -it <容器名> sh                                   # 进容器
docker compose up --build -d                                 # 起整套
docker compose down                                          # 停并删容器（卷保留）
docker compose config                                        # 校验并打印解析后的配置
```
