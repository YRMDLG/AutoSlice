# AutoSlice Active Plan

> 当前执行入口。运行时状态只在本文件维护；详细 Action 规格见仓库内公开计划。
> 本文件不保存讨论记录、完整测试日志、用户媒体、本机路径或敏感配置。

schema_version: 1
plan_id: frontend-features
plan_ref: docs/plans/frontend-features.md
ledger_ref: docs/action-ledger.md
plan_version: 1
plan_status: active
current_action: TASK-T3
current_action_status: pending
execution_mode: single-action-stop
last_completed_action: TASK-T2

## 执行规则

- 当前处于 active，`TASK-T2` 已完成实现、验证、提交、Windows full hermetic 与 Linux logic-only CI 双绿并登记；当前 Action 为 `TASK-T3`，状态为 pending。
- 状态冲突、计划版本不一致、Action 不存在或引用路径失效时立即停止。
- 计划规格不保存运行时状态；已完成 Action 登记到公开总账 `docs/action-ledger.md`。
- 本机完整架构历史仍保留在被忽略的 `PLAN.md`，不作为公开状态来源。
- 一次只执行一个 Action；本轮仅登记 `TASK-T2`，登记后立即停止，不得开始 `TASK-T3`。
- `FE-PREP-1`、`FE-PREP-2`、`FE-S0`、`FE-S1`、`FE-S2`、`FE-S3`、`FE-S4`、`FE-S5`、`ASR-A1`、`ASR-A2`、`COVER-C0`、`COVER-C1`、`COVER-C2`、`COVER-C3`、`TASK-T1`、`TASK-T2` 均已登记完成，不得重复运行；`TASK-T3` 当前为 pending，须在后续单独授权后执行。

## 当前计划范围

- 字幕工作台高频体验；
- 字幕识别与断句质量；
- AutoCover 成果衔接与制作效率；
- 任务体验、分析质量和桌面端收口。

## 读取顺序

1. 本文件；
2. `AGENTS.md` 和项目 lessons；
3. `docs/plans/frontend-features.md` 的当前 Action；
4. 仅在当前 Action 必要时读取 `PLAN.md` 历史记录。
