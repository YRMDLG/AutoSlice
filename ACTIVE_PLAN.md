# AutoSlice Active Plan

> 当前执行入口。运行时状态只在本文件维护；详细 Action 规格见仓库内公开计划。
> 本文件不保存讨论记录、完整测试日志、用户媒体、本机路径或敏感配置。

schema_version: 1
plan_id: frontend-features
plan_ref: docs/plans/frontend-features.md
ledger_ref: docs/action-ledger.md
plan_version: 1
plan_status: active
current_action: FE-S3
current_action_status: pending
execution_mode: single-action-stop
last_completed_action: FE-S2

## 执行规则

- 当前处于 active，`FE-S2` 已完成实现、验证、提交和 Windows/Linux CI 双绿；下一 Action 为 `FE-S3`。
- 状态冲突、计划版本不一致、Action 不存在或引用路径失效时立即停止。
- 计划规格不保存运行时状态；已完成 Action 登记到公开总账 `docs/action-ledger.md`。
- 本机完整架构历史仍保留在被忽略的 `PLAN.md`，不作为公开状态来源。
- 一次只执行一个 Action；本 Action 完成、提交、CI 双绿并登记后停止。
- `FE-PREP-1`、`FE-PREP-2`、`FE-S0`、`FE-S1`、`FE-S2` 均已登记完成，不得重复运行；后续从 `FE-S3` 继续。

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
