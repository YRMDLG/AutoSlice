# AutoSlice Active Plan

> 当前执行入口。运行时状态只在本文件维护；详细 Action 规格见仓库内公开计划。
> 本文件不保存讨论记录、完整测试日志、用户媒体、本机路径或敏感配置。

schema_version: 1
plan_id: backend-hardening-next
plan_ref: docs/plans/backend-hardening-next.md
ledger_ref: docs/action-ledger.md
plan_version: 1
plan_status: active
current_action: H3-LEDGER-GUARD
current_action_status: pending
execution_mode: single-action-stop
last_completed_action: H3-ARCH-DOC

## 执行规则

- 当前活动计划为 `backend-hardening-next`；`frontend-next` 已完成，不重复运行其 Action。
- `H0-SEC-PATH-BOUNDARY` 已完成本地实现、定向验证、独立提交、推送和当前 Windows/Linux CI 双绿，并已登记到总账。
- `H0-CI-GATES` 已完成本地门禁、独立提交、推送和当前 Windows/Linux CI 双绿，并已登记到总账。
- `H3-ARCH-DOC` 已完成本地验证、独立提交、推送、当前 Windows/Linux CI 双绿并登记到总账。
- 当前只允许执行 `H3-LEDGER-GUARD`；完成并登记后立即停止，不自动进入下一项。
- Action 和计划状态只使用 `pending`、`skipped`、`completed`，不得新增 `blocked` 状态。
- 部署证据不足使用计划定义的独立证据字段，不伪装为 Action 完成状态。
- `H0-SEC-PATH-BOUNDARY` 完成前不得把 LAN 标记为安全可用；普通测试不监听真实 LAN。
- H4 真实媒体、模型、FFmpeg、CUDA/GPU 和用户数据验收必须另行逐项授权。
- 状态冲突、计划版本不一致、Action 不存在、依赖冲突或引用路径失效时立即停止。
- 计划规格不保存运行时状态；已完成 Action 登记到公开总账 `docs/action-ledger.md`。
- 一个 Action 一个逻辑提交；commit 和 push 分别服从当前用户授权。
- Windows/Linux CI 证据必须对应当前 Action 的当前 commit，不能使用历史 run 代替。
- 一次只执行一个 Action；当前 Action 完成并登记后立即停止。

## 当前计划范围

- LAN 最终有效路径、登记产物和响应脱敏边界；
- CI 与 wheel 发布门禁；
- 时间轴、媒体和 AutoCover 输入健壮性；
- 架构、公开文档、总账和 LLM transport 治理；
- H4 有限真实验收保持待单独授权。

## 读取顺序

1. 本文件；
2. `AGENTS.md` 和项目 lessons；
3. `docs/plans/backend-hardening-next.md` 的计划规格；
4. `docs/action-ledger.md` 的已完成证据；
5. 仅在专项计划明确需要时读取被忽略的历史 `PLAN.md`。
