# AutoSlice Active Plan

> 当前执行入口。运行时状态只在本文件维护；详细 Action 规格见仓库内公开计划。
> 本文件不保存讨论记录、完整测试日志、用户媒体、本机路径或敏感配置。

schema_version: 1
plan_id: frontend-next
plan_ref: docs/plans/frontend-next.md
ledger_ref: docs/action-ledger.md
plan_version: 1
plan_status: active
current_action: F1-CONTEXT
current_action_status: pending
execution_mode: single-action-stop
last_completed_action: F1-STATE

## 执行规则

- 当前活动计划为 `frontend-next`；上一轮 `frontend-features` 已完成，新计划从 G0 收口阶段开始。
- `G0-SUB` 已完成实现、定向验证、独立提交、推送和 Windows/Linux CI 双绿，并已登记到总账。
- `G0-COPY` 已完成实现、定向验证、架构护栏修正、独立提交、推送和 Windows/Linux CI 双绿，并已登记到总账。
- `F1-A11Y` 已完成实现、主任务复核、完整门禁、独立提交、推送和 Windows/Linux CI 双绿，并已登记到总账。
- `F1-STATE` 已完成实现、主任务复核、完整门禁、独立提交、推送和 Windows/Linux CI 双绿，并已登记到总账。
- 下一个可执行 Action 为 `F1-CONTEXT`，当前仅登记为 pending；不得在本次状态收口中自动开始。
- 状态冲突、计划版本不一致、Action 不存在或引用路径失效时立即停止。
- 计划规格不保存运行时状态；已完成 Action 登记到公开总账 `docs/action-ledger.md`。
- 本机完整架构历史仍保留在被忽略的 `PLAN.md`，不作为公开状态来源。
- 一次只执行一个 Action；当前 Action 完成并登记后立即停止，不得自动进入下一个 Action。
- 旧计划的 `FE-PREP-1`、`FE-PREP-2`、`FE-S0`、`FE-S1`、`FE-S2`、`FE-S3`、`FE-S4`、`FE-S5`、`ASR-A1`、`ASR-A2`、`COVER-C0`、`COVER-C1`、`COVER-C2`、`COVER-C3`、`TASK-T1`、`TASK-T2`、`TASK-T3`、`UI-U1`、`UI-U2`、`UI-U3` 已完成，不得重复运行。

## 当前计划范围

- 字幕工作台高频体验；
- 字幕识别与断句质量；
- AutoCover 成果衔接与制作效率；
- 任务体验、分析质量和桌面端收口。

## 读取顺序

1. 本文件；
2. `AGENTS.md` 和项目 lessons；
3. `docs/plans/frontend-features.md` 的计划规格；
4. 仅在后续专项计划明确需要时读取 `PLAN.md` 历史记录。
