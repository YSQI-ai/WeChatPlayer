# WeChatPlayer Agent Rules

## 读取顺序

每次开始工作先读取本文件，然后检查：

1. `CURRENT.md`：当前阶段、最近提交、已验证事实、阻塞点和下一步。
2. `TASKS.md`：未完成任务、依赖和验收条件。
3. `PROJECT.md`：只有当前任务涉及架构、接口、数据流或部署时才读取。
4. `docs/decisions/`：只有当前任务触及对应技术决策时才读取。

根据任务继续按需读取源码、配置和测试文件。不要默认扫描整个仓库，也不要重复读取已经在 `CURRENT.md` 中标记为已验证且与当前任务无关的模块。若文档和实际代码、测试或服务器状态冲突，以实际状态为准，并在完成任务时修正文档。

## 工作边界

- 一次只处理一个可验收的任务边界，先确认涉及模块和验收条件，再修改文件。
- 保留用户已有的无关改动；禁止使用 `git reset --hard`、`git clean` 或删除未授权文件。
- 不读取、输出或提交密码、私钥、令牌、EKey、`.env` 内容或真实用户媒体。
- 不把本地 GUI 二进制、证书、样例音频、设计预览和运行时文件加入提交，除非任务明确要求。
- MFLAC 只能在有合法密钥或授权来源时处理；不得把 EKey 写入小程序存储、URL、数据库或普通日志。
- 修改微信小程序时遵循 `wechat-miniprogram` 技能要求，并将静态检查与开发者工具/真机验证分开报告。

## 验证与提交

按修改范围运行定向检查；常用检查如下：

```text
node --check miniprogram/pages/index/index.js
python -m py_compile converter-api/app/main.py
python -m py_compile converter-api/app/mflac_fallback.py
python -m py_compile mflac-linux-decoder/qmc_mflac.py
python mflac-linux-decoder/qmc_mflac.py --self-test
git diff --check
git status --short --branch
```

服务器部署或配置变更后，再按 `PROJECT.md` 中的部署检查确认 systemd、内网 health 和公网 API；不要用本地静态检查代替线上验证。

通过相关检查后创建边界清晰的 Git commit。提交前检查 staged diff，确认没有凭据、私钥、证书、媒体或无关资源。

## 状态文件维护

- `CURRENT.md` 使用覆盖更新，只记录当前状态、最近改动、验证结果、风险和下一步；不要追加聊天历史。
- `TASKS.md` 只维护未完成、进行中和待验证事项；完成后删除或移入已验证简表，不复制长篇过程。
- `PROJECT.md` 只放相对稳定的架构和接口事实；代码能直接说明的细节不要重复抄写。
- `docs/decisions/` 记录需要长期保留的技术结论和原因。
- 完成一个任务后更新 `CURRENT.md` 和必要的 `TASKS.md`，同时记录提交号。除非状态发生变化，不重复改写这些文件。

## 续接提示

若用户只要求“继续开发”或指名读取 `AGENTS.md`，按上述顺序自行定位所需文档，然后从 `CURRENT.md` 的下一步开始。只有遇到明确依赖、测试失败或文档不足时，才扩大检索范围；最终汇报修改文件、测试结果、剩余问题和提交号。
