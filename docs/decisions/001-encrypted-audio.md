# Encrypted Audio Decision

## 结论

Linux 服务端继续使用 QMC2/ncmdump 处理已支持的格式，并保留 Python fallback。`PcV2MusicEx`/`MusicEx` MFLAC 不假设文件内含 EKey；没有合法密钥时必须返回可诊断失败。

## 原因

用户样例已确认没有内嵌 EKey，QMC2 报 `unknown encryption method`。公开工具通常需要客户端进程、Frida 或外部 EKey，不能直接作为无交互 FastAPI 后端替换。

## 安全边界

EKey 不得写入 Mini Program 存储、URL、数据库或普通日志。解密源文件、临时密钥和明文结果应在任务结束或失败后清理。项目不提供绕过 DRM 的密钥提取功能。
