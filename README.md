# Flexible Gateway

本地 Anthropic API 网关，根据请求中的 model 名称自动路由到不同的 provider。

## 用途

Claude Code 只能配置一个 `ANTHROPIC_BASE_URL`，所有 tier（opus/sonnet/haiku）都指向同一个 provider。本网关在本地启动一个 Anthropic 兼容端点，按 model 名称路由到不同 provider。

```
Claude Code → localhost:8765 → opus  → zai (glm-5.1)
                           → sonnet → minimax (MiniMax-M2.7)
                           → haiku  → minimax (MiniMax-M2.7)
```

## 安装

```bash
cd flexible-gateway
uv sync
```

## 使用

```bash
# 启动后台服务
uv run gateway start

# 查看状态
uv run gateway status

# 停止
uv run gateway stop

# 重启
uv run gateway restart

# 前台运行（调试用）
uv run gateway run
```

可选参数：
- `--config PATH` 指定配置文件（默认 `config.yaml`）
- `--port PORT` 覆盖配置文件中的端口

## 配置

编辑 `config.yaml`：

```yaml
server:
  host: "127.0.0.1"
  port: 8765

providers:
  zai:
    base_url: "https://api.z.ai/api/anthropic"
    api_key: "your-key"
  minimax:
    base_url: "https://api.minimaxi.com/anthropic"
    api_key: "your-key"

routes:  # 从上到下匹配，首个命中生效
  - pattern: "^claude-opus"
    provider: zai
    model: "glm-5.1"        # 可选，发给 provider 的实际模型名
  - pattern: "^claude-sonnet"
    provider: minimax
    model: "MiniMax-M2.7"
  - pattern: "^claude-haiku"
    provider: minimax
    model: "MiniMax-M2.7"
  - pattern: ".*"            # 兜底
    provider: minimax
    model: "MiniMax-M2.7"
```

- `pattern`: 正则匹配请求中的 model 字段
- `provider`: 路由到的 provider 名称
- `model`: 可选，替换发给 provider 的模型名

## Claude Code 集成

启动网关后，修改 `~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
    "ANTHROPIC_AUTH_TOKEN": "gateway",
    "API_TIMEOUT_MS": "3000000",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"
  }
}
```

`ANTHROPIC_AUTH_TOKEN` 值任意但不能为空，网关会替换为对应 provider 的 key。
