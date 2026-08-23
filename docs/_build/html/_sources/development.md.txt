# 开发与发布

## 项目结构

| 文件 | 作用 |
|------|------|
| `flexgate/cli.py` | argparse CLI；service 生命周期命令、前台 `run`/`check`、config/settings/sync 命令 |
| `flexgate/config.py` | dataclass 配置模型（`GatewayConfig`、`ProviderConfig`、`RouteConfig` 等），YAML 加载/保存 |
| `flexgate/router.py` | `resolve(config, model)` — 先定时路由后默认路由，首个正则命中生效 |
| `flexgate/proxy.py` | `handle_request()` — httpx 异步代理，per-key fallback 重试，SSE 流式 + JSON 透传 |
| `flexgate/usage.py` | `flexgate status` / `flexgate usage` 的用量查询 — 各平台适配器 + minimal chat probe 兜底 |
| `flexgate/server.py` | Starlette 应用，`POST /v1/messages` 端点，SIGUSR1 热重载 |
| `flexgate/service.py` | systemd 用户服务的安装/启停/重载/状态（持久化运行的唯一入口） |
| `flexgate/settings.py` | config.yaml ↔ `~/.claude/settings.json` 双向桥接 |
| `flexgate/sync.py` | `flexgate sync` — 通过 confsync 服务器加密同步整份配置 |
| `flexgate/migrate.py` | 配置 schema 版本化：逐级迁移链 + 备份 + 原子重写 |
| `flexgate/doctor.py` | `flexgate doctor` — 只读诊断 |
| `flexgate/update.py` | `flexgate update` — PyPI 版本检查、按安装方式升级、配置迁移、服务重载 |

## 关键设计

- **正则路由**：路由按顺序匹配请求体中的 `model` 字段，首个命中生效，
  末尾用 `".*"` 兜底。
- **代理改写约定**：上游请求携带 provider 的 `x-api-key` 和固定头集；
  只有路由显式设置了 `model` 时才改写 JSON 里的 model 字段。流式响应按
  原始字节转发，不做解析。
- **Key fallback**：可重试错误（401/402/403/429/500/502/503/529、连接
  错误、超时）按 `api_keys` 顺序切换重试；流式请求只能在上游返回 200
  之前切换。
- **配置版本化**：`config_version` 标记 + `MIGRATIONS` 迁移链，升级时
  逐级迁移，先备份再原子重写。

## 发布流程（维护者）

仓库托管在 <https://github.com/Agony5757/flexible-gateway>，通过 GitHub
Actions 自动发布到 PyPI（trusted publishing，无需 API token）：

```bash
# 1. 修改 flexgate/__init__.py 中的 __version__（唯一版本来源）
# 2. 提交后打 tag，tag 必须与 __version__ 一致（CI 会校验）
git tag v0.4.0
git push origin main --tags
```

推送 `v*` tag 触发 `.github/workflows/release.yml`：校验 tag 与
`__version__` 一致 → 构建 sdist/wheel → 发布到 PyPI。首次发布前需在
PyPI 项目设置中配置 Trusted Publisher（repo:
`Agony5757/flexible-gateway`，workflow: `release.yml`）。

## 文档构建

文档使用 [Sphinx](https://www.sphinx-doc.org/) + [Furo](https://pradyunsg.me/furo/)
主题编写，源文件在 `docs/` 目录（MyST Markdown）：

```bash
pip install -r docs/requirements.txt
cd docs && make html
# 打开 docs/_build/html/index.html
```

推送到 `main`（`docs/**` 变更）或打 `v*` tag 时，`.github/workflows/docs.yml`
会自动构建并部署到 GitHub Pages。
