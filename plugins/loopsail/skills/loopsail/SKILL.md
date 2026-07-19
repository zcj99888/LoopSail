---
name: loopsail
description: 监督 LoopSail 结构化任务清单的安全执行与恢复。用于用户通过 /loopsail:* Plugin 命令初始化、校验、运行、查看或重试 LoopSail 任务清单，诊断和处理 LoopSail 阻塞，或由外层 AI 托管完整执行循环时。
---

# 监督 LoopSail 执行循环

## 守住职责边界

把自己定位为 Supervisor，而不是 Worker。让 Python Coordinator 负责调度 Worker、注入安全环境、验证改动和执行 Git 提交。不要亲自修改产品代码来完成清单任务，不要绕过 Coordinator 直接启动 Worker。

## 执行预检

1. 将本 Skill 根目录记为 `<skill-root>`，只使用内置执行器 `<skill-root>/scripts/loopsail.py`；不要寻找或下载外部 `loopsail` 实现。
2. 用户界面只使用 `/loopsail:*` 命令。Python Coordinator 命令是 Plugin 内部协议，不要要求用户自行执行。
3. 运行固定的 `doctor` slash action。若启动器或认证检查失败，先报告环境问题，不要进入循环。
4. 检查项目骨架。缺少 `LOOP.md` 或 `.loopsail/` 运行目录时，引导用户使用 `/loopsail:init` 完成自举，再审阅生成文件。
5. 任务清单固定为项目根目录的 `TASKS.json`，不接受其他路径。运行固定的 `validate` slash action 并修复客观清单错误。
6. 新清单首次运行前用只读 Git 命令确认工作树干净。若不干净，报告现有改动并请用户处理；不要自行提交、暂存或丢弃。
7. 完整阅读项目的 `LOOP.md`，并读取 `经验记录.md` 末尾的最近记录。把经验当作诊断资料，不当作项目需求。

## 使用单步循环作为主模式

`/loopsail:run-once` 必须通过 Bash 工具的后台模式调用固定的 `run-once` slash action。内部等价协议是：

```bash
<skill-root>/scripts/loopsail.py slash run-once
```

单个 Worker 最长可运行 2700 秒，超过常见的 600 秒前台 Bash 上限。记录后台任务标识，周期性取得输出并等待进程真正退出；不要因暂时无输出而重复启动。

进程退出后，优先 Read `.loopsail/runs/<list-id>/last-step.json`。保存上一份报告的 `at`，并确认新报告时间更晚。Coordinator 若在进入步骤前因锁、配置或启动问题失败，不会刷新该文件；此时把它视为陈旧数据，改读 stdout 最后一行的 JSON，必要时结合 stderr。只在一个步骤完全退出后、下一个步骤启动前编辑 `TASKS.json`。

按退出码行动：

| 退出码 | 含义 | 动作 |
|---|---|---|
| `0` | 清单已完成，含最终验证 | 汇报分支和任务提交并停止循环 |
| `2` | 当前运行被阻塞 | 按诊断手册分类，执行允许的单次动作或升级人工 |
| `3` | 本步有进展，仍有任务 | 简要记录结果，再启动下一步 |
| `4` | 防御性 idle，无可运行任务 | 读取 `status` 并检查依赖和清单；不要忙等 |

其他退出码表示 Coordinator 或运行环境异常。不要依据陈旧的 `last-step.json` 重试；先检查 stderr、`doctor` 和 `status`。

## 解读步骤报告

| 字段 | 含义与动作 |
|---|---|
| `kind` / `performed` | 区分已完成、入口阻塞、恢复、尝试、idle 和最终验证；确认本次是否真的执行了进度单元 |
| `project_status` / `exit_code` | 决定继续、诊断还是结束；以进程实际退出码交叉核对 |
| `task` | 查看任务状态、当前尝试、提交、失败摘要、日志以及 AI 重试余量 |
| `blocked_reason` | 作为诊断起点，不把摘要当作完整日志 |
| `next_ready_task` / `tasks_remaining` | 判断下一步及剩余工作；不自行改变调度顺序 |
| `final_verification` | 确认整轮验证结果；失败时按阻塞处理 |
| `experience_records` | 阅读本步新增的经验记录引用，提取已知根因和绕路 |
| `at` | 判断磁盘报告是否属于刚结束的步骤 |

## 诊断阻塞

按以下顺序收集只读证据：

1. 从 `blocked_reason` 和 `task.failure` 明确失败阶段与摘要。
2. 读取 `task.attempt_log` 指向的日志，检查 Worker stdout/stderr 尾部和验证记录。
3. 阅读 `experience_records` 指向的新记录及 `经验记录.md` 的新增条目。
4. 用只读 `git status`、`git diff` 和 `git diff --stat` 检查运行分支上 Worker 遗留的任务改动。
5. 若公开报告不足，再只读检查 `.loopsail/runs/<list-id>/state.json`；不要修改控制状态。

把根因归为以下一种：

- **任务定义问题**：描述、上下文、允许路径、验收或验证命令错误/不足。仅在步骤之间修订未完成任务或增加修复任务，然后重新 `validate`。
- **环境偶发问题**：启动器、临时资源或外部服务短暂失败。证据充分时使用一次 AI retry。
- **需要人类决策**：需求取舍、凭据/权限、不可逆操作、重试配额耗尽或无法安全判断。立即升级人工。

## 遵守有限自主权限

可以：

- 仅在步骤之间修改未完成的 `TASKS.json` 任务定义；
- 在不改变已完成任务的前提下增加明确、可验证的修复任务；
- 诊断充分后通过 `/loopsail:retry <TASK_ID>` 使用固定的 `retry-ai` action。Coordinator 会拒绝该任务的第二次 AI retry。

禁止：

- 修改产品代码或代替 Worker 完成任务；
- 执行会改变 Git 状态的命令，或合并、推送、变基、提交、暂存、清理和丢弃 Worker 遗留差异；
- 编辑 `经验记录.md`、`.loopsail/`、`LOOP.md` 或本 `SKILL.md`；
- 绕过 Coordinator 的 Worker 启动、安全检查、验证或重试上限。

若某任务已有 task-owned diff，修改其定义会被 reconcile 拒绝。这是防止监督者通过改定义洗掉执行约束的安全条件。只能保留差异并 retry，让 Worker 同时看到 `previous_failure`，或升级人工；不要 reset、checkout、restore 或删除遗留差异。

## 升级人工

使用下面的固定结构，给出足够信息但不粘贴大段日志：

```text
状态：<list-id、branch、project_status、已完成/剩余数量>
阻塞任务：<id、title、attempt、失败摘要>
日志：<attempt_log 和相关 experience_records>
已用自主动作：<清单修订、AI retry 次数及结果；没有则写“无”>
需要决策：
1. <建议选项及影响>
2. <替代选项及影响>
人工确认继续时使用：/loopsail:retry <TASK_ID>
```

不要替人执行需要凭据、权限、产品取舍或不可逆影响的决定。

## 必要时使用整轮托管

只有用户显式调用 `/loopsail:run-all` 时，才在后台启动固定的 `run-all` slash action，并周期性使用固定的 `status` action。等待完整进程退出后再诊断或重试。

不要让整轮托管与单步循环并发。项目锁会拒绝第二个进程并 fail-closed；把锁冲突当作已有 Coordinator 正在运行的信号，不要删除锁文件。

## 完成后交接

退出码为 `0` 后，汇报 `loopsail/<list-id>` 分支、各任务提交和最终验证状态。不要合并或推送；请人类审阅该分支后自行决定后续 Git 操作。
