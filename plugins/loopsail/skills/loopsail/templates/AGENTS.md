# Agent 入口

修改本仓库前，完整阅读 `CLAUDE.md`，并以其中记录的项目事实、命令和工程规则为准。

准备或维护 LoopSail 任务列表时，同时阅读 `LOOP.md`。不要把 `.loopsail/input/` 中的工作任务、`.loopsail/runs/` 中的运行状态或 `.loopsail/logs/` 中的日志当作项目规范。

`经验记录.md` 是 LoopSail Coordinator 自动维护的复盘记录，不是项目规范或任务输入。Worker 不得修改该文件；LoopSail 运行期间也不要人工编辑。

已安装的 `loopsail` Plugin 是外层 AI 的监督控制与执行来源，不属于 Worker 的任务范围；Worker 不得修改或绕过它。
