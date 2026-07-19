# LoopSail 工作流

## 职责边界

LoopSail 执行已经由人审阅的结构化任务列表。它负责校验、调度、验证和提交单个任务，不负责读取 PRD 后自行拆解需求，也不替人补充缺失的产品决策。

## 准备任务列表

1. 完善 `CLAUDE.md` 中的项目事实、工程规则和验证命令。
2. 在项目根目录的 `TASKS.json` 中替换所有 TODO，并补齐必填数组；不需要的可选字段应删除或保留为空数组（`allowed_paths` 若保留则不能为空）。
3. 由人审阅任务边界、依赖关系、验收条件、允许修改的路径和验证命令。
4. 在 Claude Code 中运行 `/loopsail:validate`，修复全部校验错误后再执行任务。

## 任务编写要求

- 每个任务只表达一个可以独立验证和提交的完整结果。
- `description` 说明期望行为和必要约束，不把实现决策留给 Worker 猜测。
- `context_files` 只列必须先读且确实存在的文件或 glob。
- `allowed_paths` 用于限制改动范围；无法合理穷举时可以删除该可选字段。
- `acceptance` 必须可以客观检查，避免“优化一下”“按需处理”等模糊表述。
- `verify_commands` 和 `final_verify_commands` 使用 argv 数组，不通过 shell 解释字符串。
- 使用 `depends_on` 表达真实依赖，不依赖任务在数组中的偶然顺序。
- 在 `non_goals` 和 `stop_conditions` 中明确排除项、外部依赖和必须停止请示的情况。

## 执行与恢复

在 Claude Code 中使用 `/loopsail:validate`、`/loopsail:run-once` 和 `/loopsail:status`。日常监督优先单步执行；只有明确需要无人值守时才使用 `/loopsail:run-all`。

由外层 AI 监督时，`/loopsail:run-once` 每次只在后台启动一个固定的 Coordinator 步骤并等待进程结束；单个 Worker 默认最长可运行 2700 秒，不要受前台工具的较短超时限制。

每步只会恢复一次中断、执行一次任务尝试或执行最终验证。以新写入的 `.loopsail/runs/<list-id>/last-step.json` 为准，并检查其中 `at` 晚于上一步；文件未刷新时，回退检查进程 stdout 最后一行和 stderr。退出码 `0` 表示完成，`2` 表示阻塞，`3` 表示本步有进展且应继续，`4` 表示当前无可运行任务、需要检查状态和依赖。只能在两个步骤之间修改 `TASKS.json`，不得在步骤运行中修改。

运行被阻塞后，先检查状态、日志和工作区差异。只有在阻塞原因已经解决且确认应继续时，才使用 `/loopsail:retry <TASK_ID>`；恢复后使用 `/loopsail:run-once` 继续。

外层 AI 可以在诊断后由 `/loopsail:retry` 选择一次 AI 自主重试；第二次会被 Coordinator 拒绝。人工确认的重试会重置该任务的 AI 重试配额。重试会保留 Worker 遗留的任务改动和上次失败上下文，使下一个 Worker 能看到 `previous_failure`。若这些改动仍存在，修改同一任务定义会被 Coordinator 拒绝；此时只能重试原任务或升级人工处理，不能丢弃差异来绕过限制。

整轮托管必须由用户显式调用 `/loopsail:run-all`，并由 Supervisor 周期性读取状态。不要让完整运行与单步运行并发；项目锁会以拒绝第二个进程的方式 fail-closed。

不要手工修改 `.loopsail/runs/`、`.loopsail/logs/` 或锁文件。完成的任务列表保持不可变；后续工作使用新的 `list_id`。

## 经验记录

- Worker 每次尝试都会判断是否存在真实困难、绕路、非显而易见的根因或可复用经验；顺利执行时不编造记录。
- Worker 只通过结构化结果上报经验，由 Coordinator 以中文追加到根目录的 `经验记录.md`。该文件不受任务 `allowed_paths` 限制，Worker 不得直接修改。
- 失败、阻塞、验证失败和执行中断即使没有 Worker 复盘，也会生成一条脱敏的自动失败记录，并指向对应 `.loopsail/logs/`。
- 成功任务的经验随该任务提交；失败经验先保留在工作区，在后续任务成功时一并提交。最终验证失败同理，需要修复任务成功后才会进入提交。
- `经验记录.md` 不是项目规范或任务输入。LoopSail 运行期间不要手工编辑，运行结束后可以人工整理。
