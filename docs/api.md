# HTTP API

Flexgate 在本地监听（默认 `127.0.0.1:8765`）提供以下端点：

| 端点 | 说明 |
|------|------|
| `POST /v1/messages` | Anthropic Messages API 代理（核心端点） |
| `POST /v1/images/generations` | OpenAI 兼容图像生成（转 MiniMax `image-01`） |
| `POST /v1/images/edits` | 固定返回 501（上游仅支持文生图） |
| 其他路径 | 结构化 JSON 错误（404 / 405 / 501，见下文） |

## POST /v1/messages

标准的 Anthropic Messages 格式代理。请求体中的 `model` 字段用于路由：

- **模型别名归一化**：匹配路由前，裸别名（`sonnet` / `opus` / `haiku` /
  `default`，大小写不敏感，可带 `[plan]` 后缀）映射为对应的
  `claude-<tier>` 前缀（`default` → sonnet）；旧编号名
  （如 `claude-3-7-sonnet-latest`）归约为 tier 前缀；其余名称原样匹配。
- 命中路由后，仅当路由显式配置了 `model` 时才改写上游请求的 model 字段；
  认证头替换为该 provider 当前 active key 的 `x-api-key`。
- 流式（SSE）响应按原始字节透传，不做解析；非流式 JSON 原样转发。
- key 级 fallback：可重试错误按 `api_keys` 顺序切换（详见
  [配置文件](configuration.md#key-fallback)）。
- **多模态降级**：请求携带图片块且目标模型不在多模态允许列表
  （`MULTIMODAL_MODELS`，当前为 `MiniMax-M3`、`glm-4.6v`）中时，图片会被
  剥离，并在请求与响应中注入 `[flexgate]` 文本说明，避免非多模态上游报 4xx。

## POST /v1/images/generations

OpenAI Images API 兼容端点，让 OpenAI 风格的客户端无需任何额外配置即可
通过网关生成图片：

- **上游自动发现**：使用配置中 `base_url` 指向 MiniMax
  （`api.minimaxi.com` / `api.minimax.io`）的 provider 及其 key，无需单独
  配置图像 provider。
- **模型名映射**：请求中的 `model` 含已知图像模型提示（`gpt-image`、
  `dall-e`、`image-01`、`seedream`、`flux`、`sdxl`、`stable-diffusion`、
  `minimax` 等）即接受并映射为 MiniMax 的 `image-01`；无法识别的模型名
  返回 501 `model_not_supported`。
- **尺寸**：`size: "WxH"` 映射为 width/height，各边钳制到 512–2048 并取
  8 的倍数；无法解析的 size 忽略（用上游默认值）。
- **prompt** 超过 1500 字符会被截断。
- **响应**：MiniMax 返回的 `data.image_base64[]`（JPEG）原样转发为 OpenAI
  风格的 `data[].b64_json`，不引入转码依赖。

## POST /v1/images/edits

固定返回 501：MiniMax `image-01` 仅支持文生图，没有通用的图像编辑 /
inpainting 能力。

## 未支持端点的结构化错误

所有未匹配的请求都不会得到裸 404，而是返回 Anthropic / OpenAI 双兼容
的 JSON 错误体，客户端可以据此判断"端口错了"还是"网关不支持该能力"：

```json
{"type": "error", "error": {"type": "...", "message": "...", "code": "..."}}
```

| 状态码 | code | 触发条件 |
|--------|------|----------|
| 404 | `route_not_found` | 完全未知的路径；message 中列出全部有效路由 |
| 405 | `method_not_allowed` | 路径有效但 HTTP 方法不对 |
| 501 | `endpoint_not_supported` | 识别出的 API 家族但网关不服务（`/v1/chat/completions`、`/v1/responses`、`/v1/messages/count_tokens`、`/v1/embeddings` 等），message 给出可操作的建议 |
| 501 | `capability_not_supported` / `model_not_supported` | 有效路由上不支持的操作或模型名（由所属模块返回） |
