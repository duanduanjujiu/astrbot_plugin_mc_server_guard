# AstrBot Minecraft 多服务器监控插件（AI 修改版）

这是一个面向 AstrBot 的 Minecraft 多服务器监控插件，支持同时监控多台 Java 版或 Bedrock 版服务器，并在状态变化时向指定会话发送通知。由 AI（DeepSeek 编码助手）基于上游插件修改生成，详见下方「来源与 AI 生成声明」。

插件重点能力：

- 支持在同一个插件实例中配置多台服务器
- **采用本地 Minecraft SLP 协议直连**（Java 版走 TCP 握手/状态查询，Bedrock 版走 RAKNET Unconnected Ping），不再依赖任何远程 HTTP API
- Java 版同时支持现代 SLP 与 1.6 之前的 Legacy Ping，兼容老服、Mohist、反向代理等
- **支持 Minecraft SRV 记录**（`_minecraft._tcp.<hostname>`），自动发现真实地址与端口
- 支持 Java / Bedrock 两种服务器类型
- 支持按服务器独立设置检测间隔
- 支持手动查询全部服务器状态
- 支持通过 GUI 开关控制消息展示项
- 仅在状态变化时推送通知，减少刷屏
- **推送目标支持 UMO**（AI 修改版）：`/推送目标 <UMO|QQ群号|本群>` 聊天内动态设置推送群，无需改 WebUI
- **上下线滞回判定**（AI 修改版）：连续探测失败确认后才报「已离线」，避免瞬时网络抖动刷屏

## 来源与 AI 生成声明

本仓库 **`astrbot_plugin_mc_server_guard`** 是 **由 AI（DeepSeek 编码助手）辅助修改生成** 的分叉/改版插件，基于以下项目修改而来：

- **上游插件**：[astrbot_plugin_minecraft_multi_monitor](https://github.com/XiaolongYang-HZAU/astrbot_plugin_minecraft_multy_monitor)（作者：xlyang / XiaolongYang-HZAU，AGPL-3.0）
- **设计思路参考**：[cryfly666/astrbot_plugin_apimcmc](https://github.com/cryfly666/astrbot_plugin_apimcmc)

主要修改内容（AI 辅助完成）：

- 上下线滞回确认（连续探测失败/成功才报离线/上线，避免瞬时抖动刷屏）；
- 单台服务器轮询异常隔离（旧版异常会让整个监控循环永久退出）；
- 修复插件重载可能残留僵尸监控任务的问题；失联自动降频；
- 推送目标支持 UMO（`/推送目标 <UMO|QQ群号|本群>`），可动态切换推送群。

> 保留上游 AGPL-3.0 许可证与版权声明（见 `LICENSE`）。本仓库插件名
> （metadata `name`）为 `astrbot_plugin_mc_server_guard`，与原插件的
> `astrbot_plugin_minecraft_multi_monitor` 不同，可并存安装；上游更新本仓库不会自动跟随。

## 适用场景

如果你有多台 Minecraft 服务器，希望统一在 AstrBot 中监控它们的在线状态、在线人数和基础运行信息，这个插件就是为这个场景设计的。

典型用法包括：

- 一个群内统一接收多个服的上下线通知
- 同时监控生存服、测试服、活动服
- 对 Java 服和 Bedrock 服进行混合监控

## 功能说明

### 1. 多服务器监控

插件会按配置列表逐台轮询服务器。每台服务器都可以有自己的：

- 唯一标识 `key`
- 显示名称 `server_name`
- 地址和端口
- 类型 `java` / `bedrock`
- 独立检测间隔
- 启用状态

### 2. 状态变化通知

监控任务会记录每台服务器最近一次状态，在以下情况发生时推送消息：

- 服务器上线或离线（**滞回确认**：连续探测失败/成功后才判定，避免瞬时抖动刷屏）
- 在线玩家人数变化
- 玩家加入或离开

首次加载 / 启动监控时只静默建立基线，不推送“监控已启动”类消息。

### 3. GUI 友好的配置方式

多服务器配置已经调整为 GUI 可逐栏填写的结构。

### 4. 可配置的消息展示项

可以在插件配置页中通过开关控制以下内容是否显示：

- 服务器名称
- 服务器地址
- 服务器类型
- 游戏版本
- 在线玩家数量
- MOTD
- 当前在线玩家列表
- 更新时间
- 一言

默认关闭的项目：

- 服务器地址
- 游戏版本
- 当前在线玩家列表
- 一言

## 指令

- `/start_server_monitor` 启动监控任务
- `/stop_server_monitor` 停止监控任务
- `/查询` 立即查询当前所有已启用服务器的状态
- `/重置监控` 重置监控缓存
- `/推送目标` 查看当前推送目标与用法（管理员）
- `/推送目标 <UMO>` 按 UMO 设置推送目标，如 `/推送目标 atri:GroupMessage:1092815819`，并发送测试消息（管理员）
- `/推送目标 <QQ群号>` 兼容旧版：按纯数字群号设置（管理员）
- `/推送目标 本群` 把当前会话设为推送目标（管理员）
- `/推送目标 清除` 清除聊天下令设置，恢复 WebUI 面板的 `target_umo` / `target_group`（管理员）
- `/推送测试` 向当前生效目标发送测试消息（管理员）

推送目标生效优先级：**聊天下令设置（持久化） > WebUI `target_umo` > WebUI `target_group`**。
聊天下令设置的目标持久化于 `data/plugin_data/astrbot_plugin_mc_server_guard/relay_state.json`。

> UMO 是 AstrBot 的会话唯一标识，形如 `<平台实例名>:<消息类型>:<会话ID>`，
> 例如 QQ 群 `atri:GroupMessage:1092815819`；发到 UMO 比“纯群号 + send_group_msg”更通用。

## 配置说明

插件通过 AstrBot 的 WebUI 配置，核心配置项如下。

### 全局配置

- `target_group`: 接收通知的 QQ 群号
- `target_umo`: 推送目标 UMO（如 `atri:GroupMessage:1092815819`），优先于 `target_group`
- `check_interval`: 全局默认检测间隔
- `enable_auto_monitor`: 插件加载后是否自动启动监控
- `display_options`: 消息展示项开关

### 服务器列表配置

`server_entries` 中的每一项代表一台服务器，支持以下字段：

- `key`: 唯一标识
- `enabled`: 是否启用
- `server_name`: 服务器名称
- `server_address`: **服务器地址（统一输入框，支持多种格式）**
- `server_type`: `java` 或 `bedrock`
- `check_interval`: 当前服务器的检测间隔

#### `server_address` 支持的格式

一个输入框就能填所有形式，插件会自动识别：

| 输入示例 | 含义 |
|---|---|
| `192.168.1.10` | IPv4，默认端口 25565 |
| `192.168.1.10:25566` | IPv4 + 端口 |
| `play.example.com` | 域名，默认端口 25565；如域名无 A 记录则自动尝试 SRV |
| `play.example.com:25566` | 域名 + 端口 |
| `_minecraft._tcp.play.example.com` | Minecraft SRV 记录（端口由 DNS 决定） |

#### SRV 记录自动尝试

插件按以下顺序尝试解析：

1. 如果输入是 IP 字面量 → 直接使用
2. 尝试解析 A/AAAA 记录 → 成功就用
3. 如果第 2 步失败 → **自动**查询 `_minecraft._tcp.<domain>` SRV 记录
4. 如果 SRV 也不存在 → 报错

这意味着你**只需填普通域名**（如 `play.example.com`），插件会按 Minecraft 客户端的标准行为（先 A 记录，再 SRV fallback）去发现服务器，无需关心 SRV 细节。

> **升级提示**：旧版的 `server_ip` / `server_port` / `srv_record` 三个字段仍然兼容——如果你只填了 `server_ip`，插件会照常使用；如果只填了 `srv_record`，插件也会照常解析。建议迁移到 `server_address` 统一字段。

## 工程结构

当前代码已按职责拆分为包结构：

- `main.py`: AstrBot 插件入口
- `minecraft_multi_monitor/config.py`: 配置解析与默认值处理
- `minecraft_multi_monitor/clients.py`: 本地 SLP 协议状态查询客户端（Java TCP + Bedrock UDP，含 Legacy Ping fallback）
- `minecraft_multi_monitor/srv_resolver.py`: Minecraft SRV 记录异步解析（依赖 dnspython）
- `minecraft_multi_monitor/monitoring.py`: 消息格式化与状态变化检测
- `minecraft_multi_monitor/services.py`: 群消息发送与一言服务
- `minecraft_multi_monitor/models.py`: 数据模型

## 安装

将本插件目录放入 AstrBot 插件目录中，并确保 AstrBot 能读取以下文件：

- `main.py`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`（运行依赖）
- `README.md`

### 运行依赖与 AstrBot 升级

插件依赖已写入插件根目录的 `requirements.txt`：

- `aiohttp>=3.9,<4`：一言（hitokoto）HTTP 接口
- `dnspython>=2.0,<3`：Minecraft SRV 记录异步解析

升级 / 重建 AstrBot（如更换 docker 镜像）后若报缺库，在 AstrBot 容器内执行一次：

```bash
pip install -r /AstrBot/data/plugins/astrbot_plugin_mc_server_guard/requirements.txt
```

然后重载插件；或在 WebUI 里对该插件执行一次重装 / 更新，让 AstrBot 按
`requirements.txt` 自动安装依赖。

## 实现说明

本插件**不再依赖任何远程 HTTP API**，所有 Minecraft 服务器状态都通过本地 SLP 协议直连采集：

- **Java 版**：通过 TCP 连接 `server_ip:server_port`，优先使用现代 Minecraft Server List Ping 协议（1.7+，握手包 + 状态请求包 → JSON 响应，含 motd / version / players.sample 等）。如果服务器拒绝现代 SLP（常见于某些 1.4-1.6 服务端、Patched Mohist、反向代理等），自动回退到 Minecraft 1.6 之前的 Legacy Ping 协议（0xFE，UTF-16BE 编码的 protocol / version / motd / online / max 字段）。
- **Bedrock 版**：通过 UDP 连接 `server_ip:server_port`，构造 RAKNET Unconnected Ping 包（包 ID `0x01` + 时间戳 + 客户端 GUID + magic），解析服务器返回的 Unconnected Pong 包（包 ID `0x1c`），从中提取版本号 / 在线玩家数 / MOTD 等字段。

协议实现细节：

- 单次查询默认超时 5 秒；连续失败 2 次（间隔 0.3 秒）后视为离线。
- 查询失败（超时、连接拒绝、协议解析错误）会被转化为 `status="offline"` 的事件，用于触发"已离线"通知。
- 每次探测均创建独立连接并在使用后立即关闭，避免资源泄漏。
- Java 版 Legacy Ping 同时兼容 Beta 1.8-1.3（`§` 分隔）、1.4-1.5（`§1\x00` 前缀）、1.6（纯 `\x00` 分隔）三种历史格式。

一言内容来自：

- `https://v1.hitokoto.cn/`

如果关闭"一言"展示开关，插件将不会额外请求该接口。

## 注意事项

- 请确保机器人已加入目标 QQ 群（或目标 UMO 会话）且具备发言权限
- 请不要把检测间隔设置得过低，建议不低于 30 秒
- 若某台服务器检测间隔填写为 `0` 或负数，将回退到全局默认值
- 消息推送默认走官方 `context.send_message(umo, ...)`；纯数字 `target_group` 时使用 QQ OneBot/AIOCQHTTP 适配器的 `send_group_msg`，发送失败自动重试
- **本地直连要求 AstrBot 主机能直接访问目标服务器的 IP 与端口**（同一台机器、内网或开放公网均可）；如有 NAT / 防火墙，请放行相应端口
