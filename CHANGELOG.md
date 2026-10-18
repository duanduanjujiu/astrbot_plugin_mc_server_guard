# 更新日志

本文档记录 AstrBot Minecraft 多服务器监控插件（AI 修改版）的版本更新历史。

---

## [1.1.1] - 2026-10

### 🐛 协议健壮性修复

- **超时不再回退 Legacy Ping**：Python 3.11+ 的 `asyncio.TimeoutError` 是内建
  `TimeoutError` 的别名，而后者继承自 `OSError`，旧的回退条件
  `except (IncompleteReadError, ValueError, OSError)` 会把"服务器不可达"误判为
  "该换协议"。结果是丢包型离线服务器单轮探测耗时翻倍（5s × 2 协议 × 2 重试
  ≈ 20s）。现在超时直接失败，只有连接被关闭、响应异常、非超时网络错误才回退。
- **Legacy Ping 分片读取**：旧实现用单次 `reader.read(65536)`，`StreamReader.read()`
  只要缓冲区有数据就返回，慢链路/代理把响应拆成多个 TCP 段时会解析出错误的
  在线人数与空 MOTD。现改为循环读取直到 EOF 或空闲超时（2s），上限 64 KiB。
- **多服务器并发探测**：监控循环由串行 `for ... await` 改为 `asyncio.gather`，
  单台服务器超时不再拖慢其他服务器的检测节拍；单台异常仍互相隔离。
- **SRV 解析 IPv4 优先修正**：`resolve_host` 之前固定返回 `infos[0]`，当系统把
  AAAA 记录排在前面时会返回 IPv6；现在返回真正命中的 AF_INET 项。
- **Bedrock 默认端口修正**：未显式填写端口时，`server_type: bedrock` 使用 19132
  （旧逻辑固定 25565，基岩服会连不上）；Java 版仍为 25565。
- **SLP 响应体上限**：状态包长度超过 2 MiB 直接判为异常，避免异常/恶意服务端
  触发超大内存分配。

### 📦 规范与工程化

- **移除已废弃的 `@register` 装饰器**：AstrBot 3.5.19+ 会自动识别 `Star` 子类，
  插件元数据以 `metadata.yaml` 为准（见 AstrBot 官方插件开发文档）。
- **新增 `tests/` 单元测试**（30 个用例，`unittest`，无需启动 AstrBot）：
  覆盖 Legacy Ping 解析/分片读取、超时回退行为、Bedrock 默认端口、SRV 主机解析、
  上下线滞回与玩家进出 diff。
- **`pyproject.toml` 增加 ruff 配置**（`target-version`、`line-length`、
  lint 规则与 `main.py` 的 E402 例外），按开发文档要求统一格式化。
- README / `_conf_schema.json` 同步更新协议说明、默认端口、测试命令。

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
