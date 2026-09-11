# Flexgate 文档

本地 Anthropic API 网关：根据请求中的 model 名称，把流量自动路由到不同的
provider，并附带 Claude Code `settings.json` 的自动管理、多 key fallback、
用量查询和配置同步能力。

## 解决什么问题

Claude Code 只能配置一个 `ANTHROPIC_BASE_URL`，所有 tier（opus/sonnet/haiku）
都指向同一个 provider。Flexgate 在本地启动一个 Anthropic 兼容端点，按 model
名称把请求路由到不同 provider：

```text
Claude Code → localhost:8765 → opus  → z.ai (glm-5.1)
                           → sonnet → minimax (MiniMax-M3)
                           → haiku  → minimax (MiniMax-M3)
```

:::{note}
**运行原则：持久化只走 service。** Linux 上的持久化 serve 只由 systemd 用户
服务 `flexgate.service` 管理。`flexgate run` 仅用于前台调试。
:::

## 特性一览

- **按 model 路由**：正则匹配请求中的 model 字段，首个命中生效，支持兜底路由；
  裸别名（`sonnet`/`opus`/`haiku`）与旧编号名自动归一化
- **定时路由**：按时间窗口自动切换路由（支持跨夜，如 22:00-06:00）
- **多 key fallback**：同一上游配置多个 key，遇到 401/402/403/429/5xx/529
  或连接错误时自动切换到下一个 key
- **多模态降级**：非多模态模型的请求自动剥离图片块并注入说明，避免上游 4xx
- **OpenAI 兼容图像生成**：`POST /v1/images/generations` 自动转 MiniMax
  `image-01`，详见 [HTTP API](api.md)
- **用量查询**：`flexgate status` / `flexgate usage` 按平台适配器查询每个
  key 的实时额度（MiniMax、Kimi Code、z.ai、LiteLLM 等）；查询失败的 key
  自动缓存跳过，`usage --force` 强制重查
- **Claude Code settings 管理**：import / apply 双向同步
- **配置同步**：`flexgate sync` 通过 confsync 服务器加密同步配置
- **自愈升级**：`flexgate doctor` 体检、`flexgate update` 一键升级并迁移配置

## 目录

```{toctree}
:maxdepth: 2

quickstart
configuration
api
cli
usage
development
```

## 链接

- GitHub 仓库：[Agony5757/flexible-gateway](https://github.com/Agony5757/flexible-gateway)
- PyPI：[flexgate](https://pypi.org/project/flexgate/)
- 问题反馈：[Issues](https://github.com/Agony5757/flexible-gateway/issues)
