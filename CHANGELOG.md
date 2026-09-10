# 更新日志

本文档记录 AstrBot Minecraft 多服务器监控插件（AI 修改版）的版本更新历史。

---

## [1.1.0] - 2026-09

### 📦 维护性重构（按 AstrBot 官方开发文档合规化）

- **命令名去空格**：`mc查询` 等含空格的命令被 AstrBot 解析成 `mc` + `查询` 两个 token，注册不上。已全部改为下划线：
  - `/mc查询` → `/mc_query`
  - `/mc重置监控` → `/mc_reset_monitor`（并加 ADMIN 权限）
  - `/mc推送目标` → `/mc_push_target`
  - `/mc推送测试` → `/mc_push_test`
  - 用户提示文本 / README / CHANGELOG / `_conf_schema.json` hint 同步
- **修复 `mc_reset_monitor` 竞态**：原实现直接替换 `server_states` / `server_locks`，运行中的 `_monitor_tick` 会孤立修改旧 `ServerState` 对象。改为 stop-then-reset：先 cancel + await monitor task（3s 超时），再清空状态；同时加 ADMIN 权限（之前任何用户可调用）。
- **`enable_auto_monitor` 默认值修复**：schema 默认 `true`，但 `load_settings` 兜底 `false`——静默不一致。现统一为 `True`。
- **默认值集中化**：把 `check_interval = 45` / `min 5` / `enable_auto_monitor` 默认值提取到 `models.py` 的常量；schema / config / resolve_check_interval 都引用同一处。
- **删除未文档化的 `servers` 字段兜底**：config.py 不再回退读取 `servers`，只支持 `server_entries`。
- **新增 `pyproject.toml`**：声明 `aiohttp` + `dnspython` 依赖和 `requires-python >= 3.10`，与 TS3 插件一致。
- **`metadata.yaml` 补 `short_desc` + `support_platforms`**：声明支持 `aiocqhttp` / `qq_official` / `satori`。
- **小修**：`safe_int` 异常从 `Exception` 收紧到 `(TypeError, ValueError)`。
- **作者归属**：author / repo URL 从 `pengjinrui` 改为 `duanduanjujiu`。

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
