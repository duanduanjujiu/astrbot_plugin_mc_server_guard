# 更新日志

本文档记录 AstrBot Minecraft 多服务器监控插件（AI 修改版）的版本更新历史。

---

## [1.0.1] - 2026-09

### 🔀 聊天指令加 mc 前缀（避免与 astrbot_plugin_ts3_server_guard 冲突）

- `/查询` → `/mc_query`
- `/推送目标` → `/mc_push_target`
- `/推送测试` → `/mc_push_test`
- `/start_server_monitor` → `/mc_start_server_monitor`
- `/stop_server_monitor` → `/mc_stop_server_monitor`
- `/重置监控` → `/mc_reset_monitor`
- 旧指令名不再生效，请改用带前缀的新指令。

---

## [1.0.0] - 2026-09

### 初始发布（AI 修改版，基于 xlyang 上游 0.1.0）

- 本地 SLP / RAKNET 直连监控 Java/Bedrock 多服务器（沿用上游）
- 上下线滞回确认、单服务器异常隔离、防僵尸监控任务、失联降频
- 推送目标支持 UMO（`/mc_push_target <UMO|QQ群号|本群>`、`/mc_push_test`）
