---
name: loopsail
description: 监督 LoopSail 2.0 结构化任务清单通过可见的会话内子 Agent 安全执行、验收、提交与恢复。用于 /loopsail:* 命令、协议诊断或整轮托管。
---

# 监督 LoopSail 2.0

## 职责边界

把自己定位为 Supervisor。Python Coordinator 只负责 prepare/finalize、状态、验证、
经验记录和 Git 提交；真正的实现由插件级 loopsail:worker 子 Agent 完成。不要亲自
修改产品代码，不要绕过 Coordinator 创建未绑定 Agent。

## 预检

1. 只使用本 Skill 自带的 scripts/loopsail.py 和固定 slash actions。
2. 运行 doctor 和 validate；每个 action 的 stdout 必须解析为唯一
   command-envelope v2 JSON，实际退出码须等于 envelope.exit_code。
3. 任务输入固定为项目根 TASKS.json，且必须是 schema_version 2、kind task-list。
   v1 状态和任务不会迁移。
4. 新 list 首次 prepare 前确认工作树干净。
5. 完整阅读目标项目 LOOP.md，并把 经验记录.md 只作为诊断材料。

## 单步主流程

/loopsail:run-once 严格执行一个前台串行单元：

1. 调用固定 prepare-step。
2. data.action 为 spawn_worker 时，调用 Agent 工具，指定
   subagent_type loopsail:worker、run_in_background false。Hook 会把 Agent ID
   绑定到 active request，并注入唯一权威 request 路径。
3. Agent 无论正常、blocked、工具失败或意外结束，都必须调用 finalize-step。
4. data.action 为 finalize_pending 时，不再创建 Agent，直接 finalize。
5. 报告完成后停止，不自动推进下一任务。

Agent 过程显示在 Claude Code 子 Agent 面板，拥有独立 transcript。不要用后台
Bash 轮询，也不要因为暂时安静而重复创建 Agent。

## 整轮托管

只有用户显式调用 /loopsail:run-all 才循环单步协议。始终保持一个前台 Agent；
finalize 完成后才能再次 prepare。遇到 blocked 或 idle 立即停止，complete 或
already_complete 成功结束。项目锁、attempt lease 和 agent_id 会拒绝并发。

## 协议与退出码

所有底层 action 只输出 command-envelope v2：

| 退出码 | 含义 |
|---|---|
| 0 | action 成功，或整轮最终验证完成 |
| 2 | 预期错误或阻塞；读取 envelope.error 与 data |
| 3 | prepare/finalize 已推进，仍有后续步骤 |
| 4 | 防御性 idle；检查依赖和状态，不忙等 |

step-report.action 的关键值是 spawn_worker、finalized、blocked、idle、complete
和 already_complete。Coordinator 的实际 Git diff、独立验证和提交始终权威；
Worker 自报 changed_files/verification_results 仅供参考。

## Worker 安全边界

插件 hooks 只对 agent_type loopsail:worker 生效：

- SubagentStart 绑定 request 与 agent_id；
- PreToolUse 在无有效 lease、错误 ID、越界路径、密钥路径、Git 写操作、控制文件
  或外部写入时 fail closed；
- PostToolUse/PostToolUseFailure 只记相对路径、命令类别和哈希等脱敏事件；
- SubagentStop 严格验证唯一 worker-result v2 JSON。第一次不合法会阻止结束并
  返回具体修正；第二次仍不合法会捕获协议失败结果并允许结束。

Worker 只能读取当前 request，不能读取其他 .loopsail 控制文件，也不能修改
TASKS.json、经验记录.md、Git 元数据或插件安装目录。

## 阻塞诊断

按顺序读取：

1. step-report.blocked_reason 与 task.failure；
2. .loopsail/logs/<list>/<task>-attempt-<n>.json；
3. 同次脱敏 events.jsonl；
4. 经验记录.md 新增条目；
5. 只读 git status/diff；
6. 公开证据不足时才只读 state.json。

任务定义客观错误可在没有 task-owned diff 时修订未完成任务并 validate。确证的
瞬时故障最多使用一次 AI retry。产品判断、凭据、权限、不可逆动作、不确定诊断
或配额耗尽必须获得人工确认。保留 Worker diff，禁止 reset、checkout、restore、
clean、暂存、提交、合并、推送或发布。

## 完成交接

完成后汇报 loopsail/<list-id> 分支、任务级提交和最终验证。LoopSail 不负责合并
或推送目标项目分支。
