# 命令参考

```text
usage: flexgate [-h] [--config CONFIG] [--version]
                {service,run,check,status,usage,log,settings,sync,help,doctor,update,config} ...
```

全局参数：

- `--config PATH` 指定配置文件（默认 `~/.flexgate/config.yaml`）
- `--port PORT` 覆盖配置文件中的端口（仅 `flexgate run`）
- `--version` 打印版本号（`service status` / 裸 `flexgate` 也会显示）

## 服务管理（默认持久化模式）

systemd **用户服务**是 Linux 上唯一推荐的持久化运行方式，负责开机/登录
自启、崩溃重启、日志和进程生命周期。

```bash
flexgate service install             # 安装、启用并立即启动
flexgate service install --no-start  # 仅安装并启用，不立即启动
flexgate service install --no-claude-settings  # 跳过修改 ~/.claude/settings.json 的交互询问
flexgate service start               # 启动；自动修复旧格式或失效的 unit
flexgate service stop                # 停止
flexgate service restart             # 重启
flexgate service reload              # 热重载；host/port 变化时自动安全重启
flexgate service status              # 查看 systemd 状态和当前路由表
flexgate service uninstall           # 停止、禁用并删除 unit
```

说明：

- unit 写入 `~/.config/systemd/user/flexgate.service`，直接运行前台
  server，由 systemd 监督（`Type=simple`、`Restart=on-failure`）。
- `install` 会执行 `loginctl enable-linger`，使服务在未登录时仍保持运行
  并随开机启动。
- `install --no-start` 不会修改 Claude Code settings，避免把客户端指向
  尚未运行的 endpoint。
- unit 只能引用持久化配置路径；为避免重启后失效，`/tmp` 下的配置会被拒绝。
- `start`/`restart` 会清理旧 PID/guardian 残留、修复旧 unit 或已失效的
  配置路径，并在端口被其他进程占用时拒绝启动。
- 若升级时检测到旧版后台 gateway 仍在运行，会先准备好 systemd unit，但
  不会强杀正在服务的进程；按提示手动 `kill <PID>` 停掉旧进程，再执行
  `flexgate service start` 完成切换。
- unit 设置了启动速率限制，永久配置错误不会再无限快速重启。
- `service reload` 和 `config set/edit` 会在仅路由变化时发送 SIGUSR1；
  如果 endpoint 变化，则先检查再 restart。若只改 host、仍复用当前 port，
  为避免误停服务会要求先执行 `service stop`，再执行 `service start`。
- 查看日志：`flexgate log`（`-f` 跟随、`--since`/`--grep` 过滤、`-r` 只看路由决策行）。

## 状态总览与用量查询

```bash
flexgate status                  # providers + fallback 链 + 每个 key 的用量 + 当前路由
flexgate status --no-usage       # 跳过用量查询，只看配置
flexgate status --usage-timeout 30
flexgate usage                   # 只看每个 key 的用量/额度（不打印配置和路由）
flexgate usage --usage-timeout 30
flexgate usage --force           # 重查上次失败的 key（默认跳过，读缓存）
```

查询失败的 key 会被缓存并在此后的 `status` / `usage` 中默认跳过
（显示为 `cached failure` 并附带提示），`--force` 强制重新查询；
详见[用量查询](usage.md)。各平台的用量查询适配器同样见
[用量查询](usage.md)。

## 查看日志（log）

```bash
flexgate log                     # 最近 50 行服务日志（systemd user journal）
flexgate log -n 200              # 最近 200 行
flexgate log -f                  # 实时跟随（Ctrl-C 退出）
flexgate log --since "10 min ago"
flexgate log --grep 429          # 只显示包含 429 的行（大小写不敏感）
flexgate log -r                  # 只看路由决策行
flexgate log -f -r               # 实时观察每个请求被路由到哪个 provider/模型
```

`flexgate log` 读取 `flexgate.service` 的 systemd 用户日志（等价于
`journalctl --user -u flexgate`，输出为单时间戳的 cat 格式）。`-r` 只显示
每个请求完成时的路由决策行：

```text
[default] claude-sonnet-4-6 -> zai (glm-5.3) | 200 | 6676ms
```

想知道"刚才那条回复到底是哪个模型服务的"，看这一行即可。前台调试用的
`flexgate run` 日志直接打印在终端，不进 journal。

## 上游连通性预检

`flexgate doctor` 会向每个**被路由引用的 `(provider, model)` 组合**发送一次
`POST /v1/messages`（`max_tokens=1`，消耗约 1~2 token），用于主动检查：

- DNS / TCP / TLS 不可达（`base_url` 写错、网络不通）
- API key 无效或过期（HTTP 401 / 403）
- 仍是默认占位符（如 `your-zai-api-key`）
- Provider 侧 5xx 故障

可通过 `--verify-timeout N` 调整每个 provider 的探测超时（默认 15 秒）；
`--offline` 跳过全部网络检查（PyPI 新版检查 + 上游探测）。旧的
`flexgate check` 命令已弃用：仍可运行，但会打印提示并委托给 `doctor`
（注意 doctor 还检查本地安装，退出码语义更宽）。

## 前台调试

`run` 是独立的顶层调试命令，不属于持久化服务模式：

```bash
flexgate run                       # 单个前台进程，仅用于开发/调试
```

非 systemd 环境只能使用 `flexgate run` 前台运行。`--port PORT` 也只对
`run` 生效；持久化服务的端口必须写入 `server.port`。

## 配置管理

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
```

如果 model 名在多个 provider 中存在，会提示歧义并给出带 provider 名的
写法。

:::{note}
`config set` 不会修改 API key。如需添加新 provider 或修改密钥，请手动
编辑配置文件。
:::

### 交互式编辑（`config edit`）

运行 `flexgate config edit` 进入全屏交互界面，用 **↑/↓ 方向键移动、回车
选择**，无需记忆 provider/model 名称：

```text
Flexgate config  —  ~/.flexgate/config.yaml
↑/↓ move · Enter edit · s save · q quit

▶ opus      ustc / deepseek-v4-pro
  sonnet    ustc / deepseek-v4-pro
  haiku     ustc / deepseek-v4-pro
  fallback  ustc / deepseek-v4-pro
  api keys  select the active key per route

○ no unsaved changes
```

- 方向键选中某个 tier（opus/sonnet/haiku）或 `fallback`（兜底路由
  `.*`，未命中任何 tier 的请求走它），回车进入：先从候选
  **provider** 列表选择，再从该 provider 的候选 **model** 列表选择。
- 选中 `api keys` 回车进入：先选一条**路由**，再选它的 **active key**
  （该路由的请求从这个 key 开始；fallback 会自动前移指针，所有 key
  轮换一圈都失败则返回错误）。key 列表会实时查询每个 key 的用量/有效性
  （与 `flexgate status` 相同），并标注当前 active 的 key。
- model 列表包含：`available_models` 中的各个模型、「使用 provider 默认
  （首个可用模型，不写死 model）」、以及「自定义模型…」（手动输入）。
- 按 `s` 保存（并向运行中的网关发送 SIGUSR1 热重载，**无需重启即生效**），
  按 `q` 退出（有未保存改动时会询问 "Config changed — activate now?"：
  选 Yes 立即保存并热重载生效，选 No 放弃改动）；子菜单中按 `Esc`/`←`
  返回上一级。
- 需要交互式终端（TTY）；非交互场景请改用 `flexgate config set`。

## Settings 管理

```bash
flexgate settings import           # 从 ~/.claude/settings.json* 导入凭证到 config.yaml（多 key 追加）
flexgate settings apply            # 将网关 env 写入 ~/.claude/settings.json（非破坏性）
flexgate settings apply --dry-run  # 预览将要修改的 env 键，不写文件
```

**import** 会扫描 `~/.claude/settings.json*`，从每个文件中提取
`ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`，合并进 config.yaml 的
providers：provider 已存在时**追加**到它的 `api_keys` 列表（已存在同一把
key 则跳过），不存在则新建。文件名与 provider 名称的映射规则：

- `settings.json` → 根据域名自动推断（如含 `z.ai` → `zai`）
- `settings.json.zai` → provider 名 `zai`
- `settings.json.bak.*` → 跳过（备份文件）

**apply** 是非破坏性的：备份当前 `~/.claude/settings.json` 为
`settings.json.bak.{timestamp}` 后，**只重写 flexgate 托管的 6 个 env 键**
（`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`API_TIMEOUT_MS`、三个
`ANTHROPIC_DEFAULT_*_MODEL`），settings.json 中的其余字段（hooks、
permissions、自定义 env 等）全部原样保留。token 策略与 `service install`
共用一条路径：显式参数 → 文件里已有的 token → 兜底 `"gateway"`：

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

## 配置同步（sync）

`flexgate sync` 把整个 `config.yaml` 作为一份**加密文档**，通过 confsync
服务器在多机之间同步（使用共享的 confsync 凭证，配置文件中不再保存
confsync 配置段）：

```bash
flexgate sync              # 默认 pull：用远端文档整体替换本地配置（先备份）
flexgate sync pull         # 同上
flexgate sync push         # 上传本地 config.yaml
flexgate sync --dry-run    # 只预览，不写配置
```

更多细节见 `flexgate help`。

## 版本与升级

```bash
flexgate --version               # 打印版本号
flexgate doctor                  # 只读体检：Python、PyPI 新版、配置 schema、端口、systemd、Claude settings、上游连通性
flexgate doctor --offline        # 跳过全部网络检查（PyPI 新版检查 + 上游探测）
flexgate update                  # 一键升级：pip/pipx/uv 升级包 + 迁移配置 schema + 热重载服务
flexgate update --check          # 只报告将要做什么，不改动
flexgate update --config-only    # 只迁移配置，不升级包
```

升级策略：

- **包升级**：版本号唯一来源是 `flexgate/__init__.py`；发布到 PyPI 后，
  `flexgate update` 自动检测安装方式（pipx / uv tool / pip）并升级到最新
  release。
- **新版本自动提示**：裸 `flexgate` 和 `flexgate service status` 会自动
  比对 PyPI 上的最新版本，有新版时打印一行升级提示。检查结果缓存在
  `~/.flexgate/update-check.json`，每 24 小时最多访问一次 PyPI，离线时
  静默跳过。
- **配置迁移**：`config.yaml` 带 `config_version` 标记。每次 schema 变化
  在 `flexgate/migrate.py` 的 `MIGRATIONS` 中登记一条 N → N+1 规则，升级
  时逐级走完整个迁移链。迁移前自动备份为 `config.yaml.bak-<时间戳>`；
  配置比当前 flexgate 更新时会被拒绝并提示先升级。
- **自检**：发版或排障时跑 `flexgate doctor`，有 FAIL 项时退出码为 1，
  可直接用于 CI 门禁。
