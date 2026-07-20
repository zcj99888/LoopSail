# LoopSail for Claude Code

LoopSail 2.0 是一个 Claude Code Plugin：它把已经审阅的结构化任务清单，逐项交给
真正的会话内子 Agent 执行。Worker 的过程和独立 transcript 可在 Claude Code 子
Agent 面板查看；Coordinator 负责安全边界、独立验证、失败恢复和任务级 Git 提交。

## 安装

```bash
claude plugin marketplace add --scope project zcj99888/LoopSail
claude plugin install --scope project loopsail@loopsail-marketplace
```

只对本机当前项目使用可改成 local scope；所有项目使用可改成 user scope。使用
自定义 Claude profile 时，以同一个 launcher 或 CLAUDE_CONFIG_DIR 执行安装和
启动。例如 profile 位于 .claude-ds：

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-ds" claude plugin marketplace add \
  --scope project zcj99888/LoopSail
CLAUDE_CONFIG_DIR="$HOME/.claude-ds" claude plugin install \
  --scope project loopsail@loopsail-marketplace
```

插件 agents 和 hooks 在会话启动时加载；安装或更新后请启动新会话。不要直接修改
Marketplace clone 或 cache，它们不是权威源码，更新时会被替换。

## 使用

| 命令 | 用途 |
|---|---|
| /loopsail:init | 初始化项目骨架 |
| /loopsail:doctor | 检查 Agent、hooks、配置和 Draft-07 schemas |
| /loopsail:validate | 校验项目根 TASKS.json |
| /loopsail:run-once | 前台执行一个 prepare/Agent/finalize 单元 |
| /loopsail:run-all | 串行执行整份清单 |
| /loopsail:status | 查看任务、lease、分支和最终验证 |
| /loopsail:retry <TASK_ID> | 诊断并恢复阻塞任务 |

推荐从以下流程开始：

```text
/loopsail:init
# 完善 CLAUDE.md 和 TASKS.json
/loopsail:validate
/loopsail:run-once
/loopsail:status
```

run-once 会先写入不可变 worker-request，然后前台创建 loopsail:worker 子 Agent，
最后无条件执行 Coordinator finalize。run-all 只是顺序循环这个协议，不会创建
后台 Python Worker 或嵌套 claude -p 进程。

## 初始化和本地状态

init 会保留已有用户文件，并在缺失时创建：

- CLAUDE.md、AGENTS.md、LOOP.md；
- 经验记录.md、TASKS.template.json 和本地 TASKS.json；
- .gitignore 中的 .loopsail/input、output、runs、logs 与 lock。

TASKS.json 和所有运行协议都是 schema_version 2；LoopSail 2.0 不读取 v1 状态。
已有 1.x 运行应保持原分支和日志，使用新 list 在干净项目中重新开始。

运行数据按职责分开：

```text
.loopsail/input/                  immutable worker requests
.loopsail/output/                 hook-captured worker results
.loopsail/runs/<list>/            state, snapshot, last-step
.loopsail/logs/<list>/            attempt logs and sanitized JSONL events
```

Worker 只允许读取自己绑定的 request，不能读取其他控制文件或写入 .loopsail。
Coordinator 的实际 Git diff 和独立验证是权威信息。

## 配置 v2

配置可位于 ~/.loopsail/config.json、项目 .loopsail/config.json，或维护者显式传入
的路径。配置只接受以下字段：

```json
{
  "schema_version": 2,
  "kind": "loopsail-config",
  "protected_paths": [],
  "verification_output_limit_bytes": 65536,
  "event_log_max_bytes": 5242880
}
```

不再有 claude launcher、Worker 超时、预算或 stdout tail 配置。子 Agent 继承当前
会话模型，运行持续时间由 Claude Code 与用户控制。

## 协议 v2 与安全语义

所有 schema 统一声明 http://json-schema.org/draft-07/schema#，只使用本地引用。
底层 Coordinator action 的 stdout 始终是一个 command-envelope JSON，预期错误也
使用同一格式并让真实退出码与 envelope.exit_code 一致。

插件 hooks 会：

- 在 SubagentStart 绑定 request/agent ID；
- 在 PreToolUse 对 Worker 的路径、Git、密钥、外部写入和破坏性命令 fail closed；
- 在 PostToolUse/PostToolUseFailure 记录不含正文、完整命令和输出的脱敏事件；
- 在 SubagentStop 校验 worker-result，允许一次格式纠正，第二次失败形成结构化阻塞。

主线程和其他 Agent 不受 Worker guard 影响。Worker 永远不能暂存、提交、合并、
推送、发布、部署或修改控制文件。

## 本地开发与更新

直接加载源码：

```bash
claude --plugin-dir ./plugins/loopsail
```

更新正式安装：

```bash
claude plugin marketplace update loopsail-marketplace
claude plugin update --scope project loopsail@loopsail-marketplace
```

验证维护改动：

```bash
python3 -m unittest discover -s tests -v
claude plugin validate --strict plugins/loopsail
claude plugin validate --strict .
git diff --check
```

底层 Python action 仅用于插件编排、测试和诊断；用户入口始终是 /loopsail:*。
