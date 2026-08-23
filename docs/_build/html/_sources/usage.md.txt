# 用量查询

`flexgate status` 和 `flexgate usage` 会逐一查询**每个 key** 的用量/额度。
各平台的查询方式差异很大（有的平台根本没有可用的接口），flexgate 按
`base_url` 自动选择适配器：

| 平台 | 查询方式 | 说明 |
|------|----------|------|
| MiniMax（api.minimaxi.com / api.minimax.io） | `GET /v1/api/openplatform/coding_plan/remains`（Bearer 认证，复用 provider key） | 官方 token plan 接口，返回 5 小时窗口和周窗口的**剩余**次数（注意响应里 `*_usage_count` 字段实际是剩余量） |
| Kimi Code（api.kimi.com） | `GET {base}/v1/usages`（Bearer 认证，复用 provider key） | 未公开文档、与 Kimi Code CLI `/usage` 相同的接口，返回周配额、5 小时滚动窗口剩余量和并发上限 |
| z.ai / 智谱（api.z.ai / open.bigmodel.cn） | `GET /api/monitor/usage/quota/limit`（Authorization 直接带 key） | 未公开文档、但 z.ai 官方 coding 插件在用的接口，返回各窗口已用百分比与重置时间 |
| USTC（api.llm.ustc.edu.cn，LiteLLM） | `GET /key/info`（Bearer 认证） | LiteLLM proxy 自带的 key 信息接口，返回 spend / max_budget / 限速等 |
| 小米 MiMo（token-plan-cn.xiaomimimo.com） | **无 key 可用的官方接口**（控制台内部接口需要浏览器 cookie，不采用） | 退化为 minimal probe |
| 其他/未知平台 | minimal chat probe | 发一条输入 `"hi"`、`max_tokens=128` 的最小 `/v1/messages` 请求，验证 key 是否还能正常服务（会消耗极少量额度） |

如果某平台的专用接口调用失败，flexgate 会自动退化为 minimal probe 再试
一次。

:::{caution}
用量查询接口多为平台内部接口，可能随时变动；查询结果仅供参考。
:::

## 输出示例

```text
Usage (timeout 15s per key):
  minimax:
    ✓ key #1 (sk-c***abcd) [main]  [MiniMax coding_plan API]
        MiniMax-M3: 5h window remaining 450/450 (reset 08-24 00:00); weekly remaining 900/900 (reset 08-24 00:00)
  kimi:
    ✓ key #1 (sk-k***66N6) [ywj]  [Kimi Code usages API (unofficial)]
        plan: advanced
        weekly remaining 63/100 (reset 08-28 22:17)
        5h window remaining 74/100 (reset 08-23 14:17)
        parallel limit: 30
```

## 新增平台适配器

适配器登记在 `flexgate/usage.py` 的 `_ADAPTERS` 列表中：每条是
`(url 子串, 适配器协程, 方法标签)`，首个命中生效。已知没有 key 可用接口的
平台放入 `_PROBE_ONLY_MARKERS`，直接走 probe，避免浪费一次失败的 HTTP
请求。
