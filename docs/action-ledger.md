# AutoSlice Action Ledger

> AutoSlice 的公开 Action 完成总账。
>
> 当前执行状态的唯一权威来源是 [`ACTIVE_PLAN.md`](../ACTIVE_PLAN.md)。
> 每个专项计划的目标、范围和验收标准见 [`docs/plans/`](plans/) 下对应的公开规格。
> 本文件只登记已经完成验证、提交并满足项目要求的 Action；未完成 Action 不在这里登记。

## 当前活动计划入口

- 计划规格：[`docs/plans/frontend-features.md`](plans/frontend-features.md)
- 当前运行状态：[`ACTIVE_PLAN.md`](../ACTIVE_PLAN.md)

> `ACTIVE_PLAN.md` 才是当前计划、当前 Action、状态和执行模式的唯一权威；
> 本总账不复制这些运行时字段，避免出现两份可相互漂移的状态。

## 已完成 Action

| 全局 ID | 来源计划 | 完成时间 | Commit | CI | 摘要 |
|---|---|---|---|---|---|
| FE-PREP-1 | frontend-features | 2026-08-23 | [a224c7d](../commit/a224c7db6a48cf31dda6fe2f285b464adbed45c3) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32601658949) | 清理字幕工作台重复顶层函数并增加唯一 owner 护栏 |
| FE-PREP-2 | frontend-features | 2026-08-23 | [941c819](../commit/941c8199913d9d9fa3f79ad2ae994fa975fc1955) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32602956293) | 外置字幕工作台 JavaScript 并覆盖真实静态资源加载路径 |
| FE-S0 | frontend-features | 2026-08-23 | [952a7d1](../commit/952a7d13d559e358a9e77b74cfde1c7f0aeaba02) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32605419982) | 完善无字幕成片识别入口、格式契约和刷新/断线任务恢复 |
| FE-S1 | frontend-features | 2026-08-23 | [2c34c13](../commit/2c34c138ec5554fad81e2588769fa52001e41804) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32618372222) | 增加字幕 AI 建议批量采纳、忽略、分组展示和可撤销操作 |
| FE-S2 | frontend-features | 2026-08-23 | [21999b0](../commit/21999b003a1bf54f9548f1bef896ec75a8bd5fbb) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32626570926) | 增加本视频额外词条、主播错词映射、本机安全覆盖和缓存失效契约 |
| FE-S3 | frontend-features | 2026-08-23 | [d244802](../commit/d244802eab76385a1f5e6d59dad1dc5020e7bcb7) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32633581563) | 预览使用临时编辑字幕，显式保存后才允许压制和生成标题 |
| FE-S4 | frontend-features | 2026-08-23 | [a7a08ea](../commit/a7a08ea336476e35e03ddff0596db9a6ed5f8950) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32636722609) | 合并、删除、时间调整统一交互 |
| FE-S5 | frontend-features | 2026-08-23 | [e9dc202](../commit/e9dc202aab7b6672863d656fbe9a8137da5417bc) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32656930831) | 字幕质检列表 |
| ASR-A1 | frontend-features | 2026-08-23 | [a5b72c9](https://github.com/YRMDLG/AutoSlice/commit/a5b72c9e676ed38057c289ef5f3441458fd82d5d) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32663710559) | 增加背景音 off/soft/strict 三模式、安全回退和可见统计信息 |
| ASR-A2 | frontend-features | 2026-08-23 | [909450e](https://github.com/YRMDLG/AutoSlice/commit/909450e88b200ed6e8bc92d8b24cb21b9c260107) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32667949178) | 增加可追踪断句决策，修复尾字漂移并保护自然开句连接词 |
| COVER-C0 | frontend-features | 2026-08-23 | [d38fc1a](https://github.com/YRMDLG/AutoSlice/commit/d38fc1ac0ac72f1bc08a5047b4bbd40fb69da3b9) / [3cd441e](https://github.com/YRMDLG/AutoSlice/commit/3cd441ea3bd8e6569d7e7e18a6361c2a2ba39c14) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32673279361) | 稳定关联 AutoSlice 标题、爆点锚点与最终短片，匹配不可靠时安全回退 |
| COVER-C1 | frontend-features | 2026-08-24 | [8a6f0d4](https://github.com/YRMDLG/AutoSlice/commit/8a6f0d4f74ee6345d1599f903627057a8f00f6c3) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32676897922) | 围绕可靠爆点锚点加密选帧，优先无字幕原片并保留人工时间轴 |
| COVER-C2 | frontend-features | 2026-08-24 | [020ce66](https://github.com/YRMDLG/AutoSlice/commit/020ce667cd27d0a519372d8150b22d00eb5abd5a) | [Windows/Linux 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32681082161) | 基于校对字幕和切片理由生成 Luna/Terra 两阶段封面文案，并支持角色配色与候选切换 |
| COVER-C3 | frontend-features | 2026-08-24 | [4f928ea](https://github.com/YRMDLG/AutoSlice/commit/4f928ea7df66dcf551ffa292f8ddde2d114b2d73) | [Windows full hermetic / Linux logic-only 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32685676235) | 将主播专属封面替换和系列规则迁入 profile，并为未知主播保留通用默认 |
| TASK-T1 | frontend-features | 2026-08-24 | [d42bc08](https://github.com/YRMDLG/AutoSlice/commit/d42bc08fc1e92fdcf3c4a98b5c6e95f2d3fd5764) | [Windows full hermetic / Linux logic-only 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32688553162) | topic_v2 消费 SSE init 恢复 queued、running、done、error、cancelled、interrupted 六种任务状态，queued/running 可取消，interrupted 显示中断原因与检查点续跑提示，并保持 SSE、无高频轮询 |
| TASK-T2 | frontend-features | 2026-08-24 | [97e56cd](https://github.com/YRMDLG/AutoSlice/commit/97e56cd78e82fc185c8baec483f0c68d29bb3ff9) | [Windows full hermetic / Linux logic-only 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32693645395) | 以核心半开区间限定输出，提供前后只读上下文，确定性协调并合并相邻候选，同时保持去重和后续边界复核 |
| TASK-T3 | frontend-features | 2026-08-24 | [9d66010](https://github.com/YRMDLG/AutoSlice/commit/9d66010eb509de1c289efaa528154a4e731414f0) | [Windows full hermetic / Linux logic-only 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32700905549) | 新增结果质量概览，展示最终切片数量、时长分布、投稿价值分、含弹幕峰值、锚点来源和边缘候选；仅用于排序和选择，不恢复每小时配额 |
| UI-U1 | frontend-features | 2026-08-24 | [9665c9e](https://github.com/YRMDLG/AutoSlice/commit/9665c9ef68260b9fb8c801c53bd21496c614909c) | [Windows full hermetic / Linux logic-only 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32710486579) | 为三个页面增加紧凑的当前任务上下文条，展示视频、主播、阶段、产物、下一步、结果目录及字幕校对与 AutoCover 入口 |
| UI-U2 | frontend-features | 2026-08-24 | [63ae73b](https://github.com/YRMDLG/AutoSlice/commit/63ae73b9902372bc068bc9be4e52f937922ed787) | [Windows full hermetic / Linux logic-only 双绿](https://github.com/YRMDLG/AutoSlice/actions/runs/32716545852) | 在跳转 AutoCover 前通过共享本机探测 owner 检查实际动态端口；未启动时显示 503 明确提示，端口占用或服务不兼容时显示 409 安全实际原因，同时保持 loopback、Host/Origin 和无代理边界。 |

## 记录规则

- 只有验证、提交和要求的 CI 检查全部完成后，才能追加记录。
- 记录使用全局 Action ID；同一 Action 不重复登记，也不覆盖既有证据。
- 历史架构与发布记录保留在本机 `PLAN.md`，不在此复制本机路径、媒体信息或敏感配置。
