# LoopSail 插件开发指南

本文面向 LoopSail 2.x 维护者。开始修改前先读根目录 AGENTS.md，并检索
开发经验.md。用户安装与使用见 README.md。

## 环境与原则

需要 Git、Python 3.10+ 和 Claude Code。运行时只依赖 Python 标准库。真实 Agent
冒烟必须在独立临时 Git 项目和新 Claude 会话中执行，不能把测试状态写入源码仓库。

## 架构

```text
/loopsail:run-once 或 run-all Supervisor
          |
          v
loopsail.py slash prepare-step
  - lock / task selection / attempt lease
  - immutable .loopsail/input worker-request
          |
          v
Agent(loopsail:worker, run_in_background=false)
  - agents/worker.md
  - hooks: bind -> guard -> sanitized events -> result capture
          |
          v
loopsail.py slash finalize-step
  - binding/input/experience/diff scope checks
  - independent verification
  - experience record + task commit
```

Coordinator 不启动 claude CLI。子 Agent 继承当前会话模型，在 Claude Code 面板可见
并拥有独立 transcript。Agent 调用失败也必须 finalize。

## 仓库结构

```text
plugins/loopsail/
  .claude-plugin/plugin.json
  agents/worker.md
  hooks/hooks.json
  commands/*.md
  skills/loopsail/
    SKILL.md
    references/*.schema.json
    scripts/
      protocol.py
      loopsail.py
      hook.py
      guard.py
    templates/
tests/
```

职责边界：

- commands 和 SKILL 定义 Supervisor 的固定编排；
- protocol.py 是版本、kind、严格验证器和 schema builders 的单一来源；
- loopsail.py 独占 prepare/finalize、状态、验证、经验和 Git 写操作；
- hook.py 独占 Agent lease 绑定、事件追加和最终结果捕获；
- guard.py 只对已绑定 loopsail:worker 做 fail-closed 工具判定；
- Worker 自报 diff 和验证只供参考，Coordinator 的真实 diff 与重跑结果权威。

## Protocol v2

所有持久输入、状态、日志、报告和命令输出都带 schema_version 2 与明确 kind。
所有 checked-in schema 必须使用精确 URI：

```text
http://json-schema.org/draft-07/schema#
```

只用本地引用和 definitions，不使用远程 meta-schema 或 Draft 2020-12 的 $defs。
测试必须断言 protocol.schema_documents() 与 references 中的文件逐字义一致。

LoopSail 2.x 不读取 v1 TASKS 或 state。遇到旧版本返回
unsupported_schema_version，不能静默重建或猜测迁移。

底层 action stdout 只允许一个 command-envelope JSON。预期错误也写 stdout；
真实退出码必须等于 envelope.exit_code。退出码：

| 代码 | 含义 |
|---|---|
| 0 | 成功或最终完成 |
| 2 | 预期错误/阻塞 |
| 3 | prepare/finalize 有进展 |
| 4 | 防御性 idle |

## prepare/finalize 状态机

prepare-step 在项目锁内：

1. 校验 config/TASKS/state 并切换或确认 loopsail/<list> 分支；
2. 若有未完成 lease：已有合法结果则要求 finalize；无结果则按中断阻塞并保留 diff；
3. 选择一个 dependency-ready 任务，增加 attempt_sequence；
4. 写 immutable worker-request 与 active_request lease；
5. 返回 step-report.action=spawn_worker；
6. 无任务时执行最终验证。

finalize-step 在项目锁内：

1. 读取 hook 捕获的 output 并核对 request/list/task/attempt/agent；
2. 核对 TASKS 与经验文件哈希；
3. 获取真实 Git diff 并检查 protected_paths/allowed_paths；
4. 独立运行 verify_commands；
5. 记录结构化 attempt log 和目标项目经验；
6. 成功时创建单任务提交；失败时按重复失败指纹、三次上限和立即阻塞规则迁移状态；
7. 幂等拒绝重复提交。

人类 retry 重置任务重试计数和 AI 配额；AI 自主 retry 每个阻塞周期最多一次。重试
保留 diff，不得 reset/restore/clean。

## Hooks 与 guard

hooks.json 必须覆盖 SubagentStart、PreToolUse、PostToolUse、
PostToolUseFailure、SubagentStop。

- 只对 agent_type=loopsail:worker 生效；主线程和其他 Agent 原样放行。
- 无唯一 active request、agent ID 不匹配或状态损坏时 fail closed。
- Worker 只可读取当前 request，禁止读取其他 .loopsail 文件或写入任意控制路径。
- Edit/Write 必须满足 allowed_paths，且不能命中内置或配置 protected_paths。
- 禁止 Git 写操作、密钥路径、插件安装/发布、外部写入和破坏性删除。
- 事件只保存时间、序号、绑定、工具名、相对目标路径、命令类别与 SHA-256、
  outcome；不能保存文件正文、完整命令、tool input/output。
- 事件达到 event_log_max_bytes 后只追加一次 log_truncated 并停止。
- SubagentStop 第一次无效结果以 decision=block 返回精确修正；第二次生成合法的
  protocol-failure worker-result 并允许退出。

Agent frontmatter 只开放 Read/Edit/Write/Glob/Grep/Bash，不开放 Agent、Skill、
Web、MCP 或 AskUserQuestion。run_in_background=false 由 Supervisor 每次显式提供。

## 配置

config v2 只有元数据和三个业务字段：

- protected_paths；
- verification_output_limit_bytes，默认 65536；
- event_log_max_bytes，默认 5 MiB。

不允许 claude.*、worker_timeout_seconds、max_budget_usd 或旧 stdout tail。配置按
内置、用户 ~/.loopsail/config.json、项目 .loopsail/config.json、显式文件合并；
每个配置文件本身必须声明 v2 kind。

## 开发与测试

本地源码会话：

```bash
claude --plugin-dir ./plugins/loopsail
```

Agent 和 hooks 在会话启动时加载，修改后必须重启会话。不要手改 marketplace clone
或 cache 进行验证。

提交前运行：

```bash
python3 -m unittest discover -s tests -v
claude plugin validate --strict plugins/loopsail
claude plugin validate --strict .
git diff --check
```

测试至少覆盖协议缺失/额外字段、绑定错误、schema 生成一致性、hook agent 过滤、
request 权限、事件脱敏与截断、一次纠正、prepare/finalize 成功和所有失败语义、
幂等 finalize、初始化安全及每个 action 的唯一 envelope。

## 发布与正式冒烟

1. 更新 plugin.json 的语义化版本和文档；
2. 审阅 git diff，只暂存本次文件；
3. 提交并推送经授权的发布分支；
4. 用目标 CLAUDE_CONFIG_DIR 执行 marketplace update 和 plugin update；
5. 确认 installed_plugins 与实际 cache 版本；
6. 启动新会话，在 mktemp 创建的空 Git 项目做
   init -> validate -> run-once；
7. 验收 Agent 可见/独立 transcript、v2 request/result/event/step、任务提交和
   不存在 --json-schema 错误；
8. 卸载临时 project-scope 安装后再删除已确认的临时目录。

是否创建和推送版本标签是独立发布授权，不因版本号变更自动推断。

## 提交前清单

- [ ] protocol builders、runtime validators 和 checked-in schemas 同步；
- [ ] commands、SKILL、Agent、hooks、Coordinator 和模板同步；
- [ ] 没有嵌套 claude -p 或 --json-schema；
- [ ] Worker 安全边界、事件脱敏和 Coordinator Git 权威未削弱；
- [ ] stdout 是唯一 envelope，退出码一致；
- [ ] unittest、两项 strict validate 和 git diff --check 通过；
- [ ] 开发经验.md 已按真实问题更新；
- [ ] 未包含凭据、私有绝对路径、cache、运行状态或临时项目。
