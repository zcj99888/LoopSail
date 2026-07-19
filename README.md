# LoopSail for Claude Code

LoopSail 是一个 Claude Code Plugin，用受控的任务清单监督 Worker 执行。它负责任务调度、运行环境隔离、验证、失败恢复和任务级 Git 提交；Worker 不会获得修改控制文件或 Git 元数据的权限。

## 安装

在目标项目中注册 Marketplace，并按项目范围安装：

```bash
claude plugin marketplace add \
  --scope project \
  zcj99888/LoopSail

claude plugin install \
  --scope project \
  loopsail@loopsail-marketplace
```

只在当前机器的当前项目使用时，将两个命令中的 `--scope project` 改为 `--scope local`。希望所有项目都可使用时，可以使用 `--scope user`。

插件源码不会复制到目标项目。Claude Code 会从 GitHub Marketplace 下载并缓存插件，目标项目只保存 Marketplace 和启用范围配置。

## 多 Claude Profile

LoopSail 不绑定 `~/.claude`、本机 alias 或特定账号。它始终跟随启动外层 Claude Code 时的 `CLAUDE_CONFIG_DIR`：Plugin 运行文件由 Claude Code 提供的 `${CLAUDE_PLUGIN_ROOT}` 定位，Worker 使用普通 `claude` 命令并继承同一个 `CLAUDE_CONFIG_DIR`、认证和模型环境。

因此，使用自定义 launcher 时，Marketplace 和 Plugin 操作也应通过同一个 launcher 执行。例如：

```bash
ccds plugin marketplace add \
  --scope project \
  zcj99888/LoopSail

ccds plugin install \
  --scope project \
  loopsail@loopsail-marketplace
```

若 `ccds` 设置了 `CLAUDE_CONFIG_DIR="$HOME/.claude-ds"`，本地 Marketplace clone、安装索引和版本缓存会分别保存在：

```text
~/.claude-ds/plugins/marketplaces/loopsail-marketplace/
~/.claude-ds/plugins/installed_plugins.json
~/.claude-ds/plugins/cache/loopsail-marketplace/loopsail/<version>/
```

`user`、`project` 和 `local` scope 只决定插件在哪个配置层启用，不改变版本缓存所属的 Claude profile。若显式设置 `CLAUDE_CODE_PLUGIN_CACHE_DIR`，则由该变量覆盖 Plugin 根目录。

`~/.loopsail/config.json` 是所有 Claude profile 共享的可选 Runner 配置，只应用于超时、预算或确有必要的自定义 launcher。普通多 profile 使用不需要配置 `claude.command_prefix`；错误地在这里固定某个本机 alias，会让其他 profile 的 Worker 也转向该 alias。项目专属覆盖可放在项目的 `.loopsail/config.json`。

## 本地开发

从任意目标项目启动 Claude Code，并直接加载本地插件目录：

```bash
claude --plugin-dir /path/to/LoopSail/plugins/loopsail
```

也可以在本仓库根目录使用：

```bash
claude --plugin-dir ./plugins/loopsail
```

自定义 profile 同样只需替换 launcher，例如 `ccds --plugin-dir ./plugins/loopsail`。`--plugin-dir` 直接加载源码，仅对当前会话生效，不创建 Marketplace 安装记录或版本缓存。

## 命令

| 命令 | 用途 |
|---|---|
| `/loopsail:init` | 检查环境并初始化项目骨架 |
| `/loopsail:doctor` | 检查 Plugin 运行时、活动 Claude profile、launcher 和认证 |
| `/loopsail:validate` | 校验项目根目录的 `TASKS.json` |
| `/loopsail:run-once` | 执行或恢复一个进度单元 |
| `/loopsail:run-all` | 显式启动整轮无人值守执行 |
| `/loopsail:status` | 查看任务、分支、阻塞和最终验证状态 |
| `/loopsail:retry <TASK_ID>` | 诊断并安全恢复当前阻塞任务 |

推荐流程：

```text
/loopsail:init
/loopsail:validate
/loopsail:run-once
/loopsail:status
```

Claude Code 会自动给 Plugin 命令添加 `loopsail:` 命名空间。请使用上表中的完整命令；未加命名空间的 `/init`、`/run-once` 等不属于该 Plugin。

## 初始化内容

`/loopsail:init` 会在目标项目根目录生成或保留：

- `CLAUDE.md`、`AGENTS.md`、`LOOP.md`
- `经验记录.md`、`TASKS.template.json` 和本地 `TASKS.json`
- `.gitignore` 中的 LoopSail 本地输入与运行状态忽略项

初始化不会把 Plugin 或 Skill 副本写入目标仓库。运行状态保存在目标项目的 `.loopsail/`，可选的跨 profile 用户级配置保存在 `~/.loopsail/config.json`。

## 更新和卸载

更新 Marketplace 和已安装插件：

```bash
claude plugin marketplace update loopsail-marketplace
claude plugin update --scope project loopsail@loopsail-marketplace
```

卸载当前项目中的插件：

```bash
claude plugin uninstall --scope project loopsail@loopsail-marketplace
```

## 维护

运行本地测试和 Plugin 校验：

```bash
python3 -m unittest discover -s tests -v
claude plugin validate --strict plugins/loopsail
claude plugin validate --strict .
```

底层 Python Coordinator 仅用于自动化测试和故障诊断；面向用户的主入口是 Claude Code 中的 `/loopsail:*` 命令。

## 设计边界

- 不读取或拆解 PRD，不替人补充产品决策。
- 所有 slash 操作固定使用项目根目录的 `TASKS.json`。
- Worker 不得修改控制文件、Git 元数据、经验记录或 Plugin 安全文件。
- Coordinator 独占验证、Git 提交和有限重试权限。
- Plugin 不会推送、合并、变基、发布、部署或丢弃改动。
