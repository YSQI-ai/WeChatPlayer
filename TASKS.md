# WeChatPlayer Tasks

## P0 阻塞

- [ ] **实现 Windows helper 与服务端私有任务协议**
  - 依赖：交互式 QQ 音乐会话、合法 EKey/明文来源和认证方案。
  - 验收：helper 可注册设备、单任务领取、心跳、成功/失败/超时回报；服务端可重试且不会重复完成任务。
  - 相关文件：`windows-helper/README.md`、`converter-api/app/main.py`。
- [ ] **支持当前 `PcV2MusicEx` MFLAC 样例的授权解码路径**
  - 依赖：合法 EKey 或用户授权的客户端/数据库来源。
  - 验收：样例可转为有效 FLAC；密钥不进入小程序、URL、数据库或普通日志。

## P1 验证

- [ ] 在微信开发者工具或真机验证 MFLAC、NCM、1 GiB 边界、取消上传和失败提示。
- [ ] 回归文档转 PDF、媒体播放、保存/重复保存/删除、云盘保存/重命名/删除和历史刷新。
- [ ] 检查导航 PNG 在不同屏幕宽度和深色导航栏下的显示比例。

## P1 运维

- [ ] 清理阿里云安全组中 9853/8089 的旧公网放行规则。
- [ ] 如继续排查 Adminer 公网 502，只检查 1Panel 代理、安全组和容器日志，不修改数据库凭据。
- [ ] 复核公网 HTTPS 连接重置问题；SSH 侧 systemd 和内网 health 已正常时，单独定位网关链路。

## 已验证基线

- [x] API health/test、MySQL 状态、NCM 到 FLAC、历史和云盘基本流程已完成冒烟验证。
- [x] `mflac-linux-decoder` 的 Python 编译、self-test 和样例 inspect 已通过。
- [x] 最近相关提交：`04b4cfb feat: add MFLAC fallback and helper handoff`。
