# Flexgate

本地 Anthropic API 网关，根据请求中的 model 名称自动路由到不同的 provider。同时提供 Claude Code settings.json 的自动管理功能。

## 用途

Claude Code 只能配置一个 `ANTHROPIC_BASE_URL`，所有 tier（opus/sonnet/haiku）都指向同一个 provider。Flexgate 在本地启动一个 Anthropic 兼容端点，按 model 名称路由到不同 provider。

```
Claude Code → localhost:8765 → opus  → z.ai (glm-5.1)
                           → sonnet → minimax (MiniMax-M3)
                           → haiku  → minimax (MiniMax-M3)
```

> **运行原则：持久化只走 service。** Linux 上的持久化 serve 只由 systemd 用户服务
> `flexgate.service` 管理。`flexgate run` 仅用于前台调试，不再创建第二套
> PID/guardian 后台进程，因此不会再与 service 抢占同一端口。

## 安装

```bash
cd flexible-gateway
uv sync
```

源码开发时可以前台运行：

```bash
uv run flexgate config init
uv run flexgate run
```

日常使用推荐从 PyPI 全局安装，并交给 systemd 用户服务管理：

```bash
pipx install flexgate        # 或: uv tool install flexgate / pip install flexgate
flexgate config init
flexgate service install
```

从源码安装则用：`uv tool install -e .`

## 快速开始

```bash
# 1. 初始化配置文件（~/.flexgate/config.yaml）
flexgate config init
# 编辑 ~/.flexgate/config.yaml 填入你的 API key

# 2. 从已有 Claude Code 配置自动导入（可选）
flexgate settings import         # 读取 ~/.claude/settings.json* 中的凭证

# 3. 安装并启动唯一的持久化服务
# install 会询问是否将 Claude Code settings.json 指向本地网关
flexgate service install

# 4. 如果安装时跳过了 settings 修改，可稍后手动应用
flexgate settings apply
```

## 命令参考

### 服务管理（默认持久化模式）

systemd **用户服务**是 Linux 上唯一推荐的持久化运行方式，负责开机/登录自启、崩溃重启、日志和进程生命周期。

```bash
flexgate service install             # 安装、启用并立即启动
flexgate service install --no-start  # 仅安装并启用，不立即启动
flexgate service start               # 启动；自动修复旧格式或失效的 unit
flexgate service stop                # 停止
flexgate service restart             # 重启
flexgate service reload              # 热重载；host/port 变化时自动安全重启
flexgate service status              # 查看 systemd 状态和当前路由表
flexgate service uninstall           # 停止、禁用并删除 unit
```

说明：
- unit 写入 `~/.config/systemd/user/flexgate.service`，直接运行前台 server，由 systemd 监督（`Type=simple`、`Restart=on-failure`）。
- `install` 会执行 `loginctl enable-linger`，使服务在未登录时仍保持运行并随开机启动。
- `install --no-start` 不会修改 Claude Code settings，避免把客户端指向尚未运行的 endpoint。
- unit 只能引用持久化配置路径；为避免重启后失效，`/tmp` 下的配置会被拒绝。
- `start`/`restart` 会清理旧 PID/guardian 残留、修复旧 unit 或已失效的配置路径，并在端口被其他进程占用时拒绝启动。
- 若升级时检测到旧版后台 gateway 仍在运行，会先准备好 systemd unit，但不会强杀正在服务的进程；按提示手动 `kill <PID>` 停掉旧进程，再执行 `flexgate service start` 完成切换。
- unit 设置了启动速率限制，永久配置错误不会再无限快速重启。
- `service reload` 和 `config set/edit` 会在仅路由变化时发送 SIGUSR1；如果 endpoint 变化，则先检查再 restart。若只改 host、仍复用当前 port，为避免误停服务会要求先执行 `service stop`，再执行 `service start`。
- 查看日志：`journalctl --user -u flexgate -e`。

### 版本与升级

```bash
flexgate --version               # 打印版本号（service status / 裸 flexgate 也会显示）
flexgate doctor                  # 只读体检：Python、PyPI 新版、配置 schema、端口、systemd、Claude settings
flexgate doctor --offline        # 跳过 PyPI 检查
flexgate update                  # 一键升级：pip/pipx/uv 升级包 + 迁移配置 schema + 热重载服务
flexgate update --check          # 只报告将要做什么，不改动
flexgate update --config-only    # 只迁移配置，不升级包
```

升级策略：

- **包升级**：版本号唯一来源是 `flexgate/__init__.py`；发布到 PyPI 后，`flexgate update`
  自动检测安装方式（pipx / uv tool / pip）并升级到最新 release。
- **新版本自动提示**：裸 `flexgate` 和 `flexgate service status` 会自动比对 PyPI 上的
  最新版本，有新版时打印一行升级提示。检查结果缓存在
  `~/.flexgate/update-check.json`，每 24 小时最多访问一次 PyPI，离线时静默跳过。
- **配置迁移**：`config.yaml` 带 `config_version` 标记。每次 schema 变化在
  `flexgate/migrate.py` 的 `MIGRATIONS` 中登记一条 N → N+1 规则，升级时逐级走完整个迁移链。
  迁移前自动备份为 `config.yaml.bak-<时间戳>`；配置比当前 flexgate 更新时会被拒绝并提示先升级。
- **自检**：发版或排障时跑 `flexgate doctor`，有 FAIL 项时退出码为 1，可直接用于 CI 门禁。

### 发布流程（维护者）

仓库托管在 <https://github.com/Agony5757/flexible-gateway>，通过 GitHub Actions
自动发布到 PyPI（trusted publishing，无需 API token）：

```bash
# 1. 修改 flexgate/__init__.py 中的 __version__（唯一版本来源）
# 2. 提交后打 tag，tag 必须与 __version__ 一致（CI 会校验）
git tag v0.2.0
git push origin main --tags
```

推送 `v*` tag 触发 `.github/workflows/release.yml`：校验 tag 与 `__version__` 一致 →
构建 sdist/wheel → 发布到 PyPI。首次发布前需在 PyPI 项目设置中配置
Trusted Publisher（repo: `Agony5757/flexible-gateway`，workflow: `release.yml`）。

### 上游连通性预检

运行 `flexgate check` 会向每个 **被路由引用的 `(provider, model)` 组合**
发送一次 `POST /v1/messages`（`max_tokens=1`，消耗约 1~2 token），用于主动检查：

- DNS / TCP / TLS 不可达（`base_url` 写错、网络不通）
- API key 无效或过期（HTTP 401 / 403）
- 仍是默认占位符（如 `your-zai-api-key`）
- Provider 侧 5xx 故障

可通过 `--verify-timeout N` 调整每个 provider 的超时时间（默认 15 秒）。

### 前台调试与连通性检查

`run` / `check` 是独立的顶层调试命令，不属于持久化服务模式：

```bash
flexgate run                       # 单个前台进程，仅用于开发/调试
flexgate check                     # 上游 provider 连通性检测
```

非 systemd 环境只能使用 `flexgate run` 前台运行。`--port PORT` 也只对
`run` 生效；持久化服务的端口必须写入 `server.port`。

### 配置管理

```bash
flexgate config init             # 创建默认配置（~/.flexgate/config.yaml）
flexgate config show             # 查看当前配置（providers、路由、定时规则）
flexgate config edit             # 交互式选择每个 tier（opus/sonnet/haiku）的 provider/model
flexgate config path             # 打印配置文件路径
flexgate config set <tier> <target> [model]  # 快速设置路由（tier 可为 all/opus/sonnet/haiku）
```

`config set` 支持按 provider 名或 model 名设置路由：

```bash
# 批量切换所有 tier（opus/sonnet/haiku）到同一个 provider
flexgate config set all xiaomi

# 用逗号组合多个 tier
flexgate config set opus,sonnet xiaomi

# 按 provider 名 + model 名
flexgate config set sonnet minimax MiniMax-M3

# 按 provider 名（不改写 model）
flexgate config set opus zai

# 按 model 名自动查找 provider
flexgate config set haiku MiniMax-M3
# → 自动解析为 minimax / MiniMax-M3

# 如果 model 名在多个 provider 中存在，会提示歧义：
# Ambiguous: 'xxx' found in multiple providers:
#   flexgate config set haiku providerA xxx
#   flexgate config set haiku providerB xxx
```

> **注意**：`config set` 不会修改 API key。如需添加新 provider 或修改密钥，请手动编辑配置文件。

#### 交互式编辑（`config edit`）

运行 `flexgate config edit` 进入全屏交互界面，用 **↑/↓ 方向键移动、回车选择**，无需记忆 provider/model 名称：

```text
Flexgate config  —  ~/.flexgate/config.yaml
↑/↓ move · Enter edit tier · s save · q quit

▶ opus      ustc / deepseek-v4-pro
  sonnet    ustc / deepseek-v4-pro
  haiku     ustc / deepseek-v4-pro

○ no unsaved changes
```

- 方向键选中某个 tier（opus/sonnet/haiku），回车进入：先从候选 **provider** 列表选择，再从该 provider 的候选 **model** 列表选择。
- model 列表包含：`available_models` 中的各个模型、「使用 provider 默认（首个可用模型，不写死 model）」、以及「自定义模型…」（手动输入）。
- 按 `s` 保存（并向运行中的网关发送 SIGUSR1 热重载），按 `q` 退出（有未保存改动时会提示保存或放弃）；子菜单中按 `Esc`/`←` 返回上一级。
- 需要交互式终端（TTY）；非交互场景请改用 `flexgate config set`。

### Settings 管理

```bash
flexgate settings import         # 从 ~/.claude/settings.json* 导入凭证到 config.yaml
flexgate settings apply          # 将 config.yaml 配置写入 ~/.claude/settings.json
```

### 全局参数

- `--config PATH` 指定配置文件（默认 `~/.flexgate/config.yaml`）
- `--port PORT` 覆盖配置文件中的端口（仅 `flexgate run`）

## 配置文件

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

运行 `flexgate config init` 创建默认配置，或手动编辑：

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
| `schedule[].name` | 定时规则名称 |
| `schedule[].start/end` | 时间窗口（HH:MM 格式，支持跨夜如 22:00-06:00） |
| `schedule[].routes` | 该时间窗口内生效的路由（格式同 `routes`） |

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
| `FLEXGATE_CONFIG` | `~/.flexgate/config.yaml` | 覆盖配置文件路径 |

## 注意事项

- 配置默认存放在 `~/.flexgate/`，全局安装后可在任意目录管理 systemd 用户服务
- Linux 持久化运行统一使用 `flexgate service`；不要额外启动独立后台进程
- `config.yaml` 已加入 `.gitignore`，不会被提交到 Git
- 请使用 `config.yaml.template` 作为参考模板
- 如果 API 密钥曾经被推送到远程仓库，请立即轮换（rotate）该密钥
