# Flexgate

本地 Anthropic API 网关，根据请求中的 model 名称自动路由到不同的 provider。同时提供 Claude Code settings.json 的自动管理功能。

## 用途

Claude Code 只能配置一个 `ANTHROPIC_BASE_URL`，所有 tier（opus/sonnet/haiku）都指向同一个 provider。Flexgate 在本地启动一个 Anthropic 兼容端点，按 model 名称路由到不同 provider。

```
Claude Code → localhost:8765 → opus  → z.ai (glm-5.1)
                           → sonnet → minimax (MiniMax-M2.7)
                           → haiku  → minimax (MiniMax-M2.7)
```

## 安装

```bash
cd flexible-gateway
uv sync
```

安装后通过 `uv run flexgate` 运行：

```bash
uv run flexgate gateway start
uv run flexgate settings import
uv run flexgate settings apply
```

如果希望全局使用 `flexgate` 命令：

```bash
uv tool install -e .
flexgate gateway start
```

## 快速开始

```bash
# 1. 从模板创建配置文件
cp config.yaml.template config.yaml
# 编辑 config.yaml 填入你的 API key

# 2. 从已有 Claude Code 配置自动导入（可选）
flexgate settings import         # 读取 ~/.claude/settings.json* 中的凭证

# 3. 启动网关
flexgate gateway start

# 4. 将 Claude Code 指向网关
flexgate settings apply          # 自动修改 ~/.claude/settings.json
```

## 命令参考

### 网关管理

```bash
flexgate gateway start           # 启动后台服务
flexgate gateway stop            # 停止服务
flexgate gateway restart         # 重启服务
flexgate gateway status          # 查看运行状态
flexgate gateway run             # 前台运行（调试用）
```

全局参数：
- `--config PATH` 指定配置文件（默认 `config.yaml`）
- `--port PORT` 覆盖配置文件中的端口（仅 gateway 子命令）

### Settings 管理

```bash
flexgate settings import         # 从 ~/.claude/settings.json* 导入凭证到 config.yaml
flexgate settings apply          # 将 config.yaml 配置写入 ~/.claude/settings.json
```

## 配置文件

编辑 `config.yaml`（首次从 `config.yaml.template` 复制）：

```yaml
server:
  host: "127.0.0.1"
  port: 8765

providers:
  zai:
    base_url: "https://api.z.ai/api/anthropic"
    api_key: "your-zai-api-key"
  minimax:
    base_url: "https://api.minimaxi.com/anthropic"
    api_key: "your-minimax-api-key"

claude_settings:
  default_opus_model: "claude-opus-4-7"
  default_sonnet_model: "claude-sonnet-4-6"
  default_haiku_model: "claude-haiku-4-5"
  api_timeout_ms: 3000000

routes:                          # 从上到下匹配，首个命中生效
  - pattern: "^claude-opus"
    provider: zai
    model: "glm-5.1"            # 可选，发给 provider 的实际模型名
  - pattern: "^claude-sonnet"
    provider: minimax
    model: "MiniMax-M2.7"
  - pattern: "^claude-haiku"
    provider: minimax
    model: "MiniMax-M2.7"
  - pattern: ".*"               # 兜底
    provider: minimax
    model: "MiniMax-M2.7"
```

### 配置字段说明

| 字段 | 说明 |
|------|------|
| `server.host/port` | 网关监听地址 |
| `providers.<name>.base_url` | Provider 的 API 地址 |
| `providers.<name>.api_key` | Provider 的 API 密钥 |
| `claude_settings.*` | 写入 settings.json 的模型和超时配置 |
| `routes[].pattern` | 正则匹配请求中的 model 字段 |
| `routes[].provider` | 路由到的 provider 名称 |
| `routes[].model` | 可选，替换发给 provider 的模型名 |

## Settings Import

`flexgate settings import` 会扫描 `~/.claude/settings.json*`，从每个文件中提取 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`，自动写入 config.yaml 的 providers 部分。

文件名与 provider 名称的映射规则：
- `settings.json` → 根据域名自动推断（如含 `z.ai` → `zai`）
- `settings.json.zai` → provider 名 `zai`
- `settings.json.minimax` → provider 名 `minimax`
- `settings.json.bak.*` → 跳过（备份文件）

适合场景：你有多套 Claude Code 配置文件，想要快速将凭证合并到网关中统一管理。

## Settings Apply

`flexgate settings apply` 会：
1. 读取 config.yaml 中的 `server` 和 `claude_settings`
2. 备份当前 `~/.claude/settings.json` 为 `settings.json.bak.{timestamp}`
3. 生成新的 settings.json，将 `ANTHROPIC_BASE_URL` 指向本地网关
4. 保留原有的 `permissions` 等非 env 字段

生成的 settings.json 示例：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
    "ANTHROPIC_AUTH_TOKEN": "gateway",
    "API_TIMEOUT_MS": "3000000",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"
  },
  "permissions": {
    "defaultMode": "bypassPermissions"
  }
}
```

`ANTHROPIC_AUTH_TOKEN` 值任意但不能为空，网关会替换为对应 provider 的 key。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FLEXGATE_CONFIG` | `config.yaml` | 配置文件路径 |

## 注意事项

- `config.yaml` 已加入 `.gitignore`，不会被提交到 Git
- 请使用 `config.yaml.template` 作为模板，填入真实密钥后另存为 `config.yaml`
- 如果 API 密钥曾经被推送到远程仓库，请立即轮换（rotate）该密钥
