# 安装与快速开始

## 安装

日常使用推荐从 PyPI 全局安装，并交给 systemd 用户服务管理：

```bash
pipx install flexgate        # 或: uv tool install flexgate / pip install flexgate
flexgate config init
flexgate service install
```

从源码安装（开发模式）：

```bash
cd flexible-gateway
uv sync
uv tool install -e .
```

源码开发时也可以直接前台运行：

```bash
uv run flexgate config init
uv run flexgate run
```

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

安装完成后，Claude Code 的请求会经由本地网关按路由规则转发到对应
provider。日常管理常用：

```bash
flexgate status                  # providers + fallback 链 + 每个 key 的用量 + 当前路由
flexgate config edit             # 交互式调整每个 tier 的 provider/model
flexgate log                     # 查看服务日志（-r 只看路由决策行）
```
