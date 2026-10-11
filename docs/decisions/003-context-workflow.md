# Context Workflow Decision

## 结论

项目使用分层状态文档：`AGENTS.md` 管规则，`PROJECT.md` 管稳定架构，`TASKS.md` 管未完成工作，`CURRENT.md` 管当前交接状态，`docs/decisions/` 管长期技术结论。`context.md` 只作为旧入口索引。

## 原因

把架构、部署、验证历史和待办放进一个文件会让每次续接重复读取无关内容。按任务按需读取可保留工程连续性，同时减少上下文和重复总结。

## 更新规则

任务完成后创建清晰 Git 提交，覆盖更新 `CURRENT.md`，只更新受影响的 `TASKS.md` 条目。不要追加聊天记录，也不要在多个文件重复维护同一事实。
