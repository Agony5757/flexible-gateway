# 配置文件

主要运行时资源：

| 文件 | 说明 |
|------|------|
| `~/.flexgate/config.yaml` | 主配置文件 |
| `~/.flexgate/service-state.json` | 最近一次成功启动所应用的 config 路径与 endpoint |
| `~/.flexgate/update-check.json` | PyPI 新版本检查的缓存（24h 有效期） |
| `~/.config/systemd/user/flexgate.service` | 唯一的持久化服务 unit |
| systemd journal | 服务日志（`journalctl --user -u flexgate`） |

旧版本的 `~/.flexgate/flexgate.pid`、`flexgate.guardian.pid` 和
`flexgate.log` 不再属于当前运行架构；service 启动时会安全清理 PID 残留，
历史日志文件可按需手动删除。

## 完整示例

运行 `flexgate config init` 创建默认配置，或手动编辑：

```yaml
server:
  host: "127.0.0.1"
  port: 8765

providers:
  zai:
    base_url: "https://api.z.ai/api/anthropic"
    api_keys:
      - "your-zai-api-key"
  minimax:
    base_url: "https://api.minimaxi.com/anthropic"
    api_keys:                   # 同一上游可配多个 key，互为 fallback
      - key: "your-minimax-api-key"
        note: "主账号"          # 可选备注，存在配置里，status/日志中显示
      - key: "your-minimax-api-key-2"
        note: "备用账号"
      - "your-minimax-api-key-3"   # 纯字符串写法（无备注）

claude_settings:
  default_opus_model: "claude-opus-4-7"
  default_sonnet_model: "claude-sonnet-4-6"
  default_haiku_model: "claude-haiku-4-5"
  api_timeout_ms: 3000000

# 定时路由（可选）：按时间自动切换，首个时间窗口命中生效
# schedule:
#   - name: "night-shift"
#     start: "22:00"
#     end: "06:00"
#     routes:
#       - pattern: "^claude-sonnet"
#         provider: zai
#         model: "glm-5.1"

routes:                          # 从上到下匹配，首个命中生效
  - pattern: "^claude-opus"
    provider: zai
    model: "glm-5.1"            # 可选，发给 provider 的实际模型名
  - pattern: "^claude-sonnet"
    provider: minimax
    model: "MiniMax-M3"
  - pattern: "^claude-haiku"
    provider: minimax
    model: "MiniMax-M3"
  - pattern: ".*"               # 兜底
    provider: minimax
    model: "MiniMax-M3"
```

## 配置字段说明

| 字段 | 说明 |
|------|------|
| `server.host/port` | 网关监听地址 |
| `providers.<name>.base_url` | Provider 的 API 地址 |
| `providers.<name>.api_keys` | API key 列表（一个或多个，互为 fallback）；每项为 key 字符串或 `{key, note}`，`note` 是存在配置里的备注 |
| `providers.<name>.available_models` | 该 provider 的可用模型列表，首个条目作为路由省略 `model` 时的回退模型 |
| `claude_settings.*` | 写入 settings.json 的模型和超时配置 |
| `routes[].pattern` | 正则匹配请求中的 model 字段 |
| `routes[].provider` | 路由到的 provider 名称 |
| `routes[].model` | 可选，替换发给 provider 的模型名 |
| `schedule[].name` | 定时规则名称 |
| `schedule[].start/end` | 时间窗口（HH:MM 格式，支持跨夜如 22:00-06:00） |
| `schedule[].routes` | 该时间窗口内生效的路由（格式同 `routes`） |

## Key fallback

同一上游有多个账号/key 时（例如多个 MiniMax 订阅），在 `api_keys` 里配
多个 key，而不是拆成多个 provider。每项可以是纯 key 字符串，也可以写成
`{key, note}` 加一个存在配置里的备注（`flexgate status` 和 fallback 日志
都会显示 note，方便分辨是哪个账号的 key）：

- 请求先走第一个 key；失败时按列表顺序自动重试后续 key，直到某个 key
  成功或返回不可重试的错误。
- 触发切换的条件：HTTP **401 / 402 / 403 / 429 / 500 / 502 / 503 / 529**
  （key 失效、余额/额度耗尽、限流、平台过载）以及连接错误、超时。
  其他 4xx（如 400 请求格式错误）不会触发切换。
- 流式请求只有在上游返回非 200 状态码之前才能切换 key；一旦开始吐
  token，响应已提交，无法再 fallback。
- 每次切换都会在服务日志中留下记录（key 只显示前后各 4 位）。
- 用 `flexgate status` 可以查看每个 provider 的 fallback 链和每个 key 的
  实时用量。
- 旧版 `api_key` + `fallback_keys` 写法仍然兼容，`flexgate update` 会
  自动迁移为 `api_keys` 列表（config_version 3 → 4）。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FLEXGATE_CONFIG` | `~/.flexgate/config.yaml` | 覆盖配置文件路径 |

:::{warning}
`config.yaml` 已加入 `.gitignore`，不会被提交到 Git。如果 API 密钥曾经
被推送到远程仓库，请立即轮换（rotate）该密钥。
:::
