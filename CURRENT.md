# Current State

更新时间：2026-10-11（Asia/Shanghai）

## 当前阶段

项目处于加密音频能力收尾和真实环境验证阶段。最近一次功能提交为 `04b4cfb feat: add MFLAC fallback and helper handoff`。

## 已验证

- FastAPI、MFLAC fallback、Linux 解码器和小程序历史样式通过静态检查。
- `mflac-linux-decoder/qmc_mflac.py --self-test` 通过。
- 服务器 API health、NCM 转 FLAC、历史和云盘基本流程已完成冒烟验证。
- 用户样例属于不内嵌 EKey 的 PC `PcV2MusicEx`/`MusicEx`；上传链路正常，但现有 QMC2 返回 `unknown encryption method`。

## 当前阻塞

样例 MFLAC 不能在没有合法 EKey 的情况下离线解码。生产 Windows helper、设备认证、任务队列和结果回调尚未实现。

## 工作区

当前分支为 `main`。业务改动已提交；工作区仍有本地样例音频、GUI EXE、证书、设计预览和 `.agents/` 等被 `.gitignore` 忽略的文件，不应纳入普通功能提交。

## 下一步

1. 先定义并实现 helper 私有协议和服务端任务状态机。
2. 在合法授权来源可用时验证 `PcV2MusicEx` 解码，再联调 helper。
3. 使用微信开发者工具/真机完成 `TASKS.md` 中的 P1 验证。
4. 完成安全组和公网 HTTPS 检查后，再更新本文件并创建下一个边界清晰的提交。

## 约束

不提交凭据、私钥、证书、真实用户媒体、GUI 二进制或运行时文件；不把 EKey 写入小程序存储、URL、数据库或普通日志。
