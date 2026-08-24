---
plan_id: frontend-next
plan_version: 1
schema_version: 1
---

# AutoSlice 前端下一阶段执行计划

> 本文件只保存新专项计划规格；运行时激活状态和当前 Action 只以根目录 `ACTIVE_PLAN.md` 为准。
>
> 运行时状态只写入根目录 `ACTIVE_PLAN.md`；本文件只保存 Action 目标、范围、依赖和验收标准。

## 1. 计划目标与权威边界

本计划建立在上一轮 `frontend-features` 已完成的事实之上：

- `ACTIVE_PLAN.md` 中 `frontend-features` 为 `completed`；
- `FE-PREP-1`、`FE-PREP-2`、`FE-S0`～`FE-S5`、`ASR-A1`～`ASR-A2`、
  `COVER-C0`～`COVER-C3`、`TASK-T1`～`TASK-T3`、`UI-U1`～`UI-U3` 均已完成；
- 不重复执行旧 Action，不向旧 `frontend-features` 追加新 Action；
- 新计划 ID 为 `frontend-next`，当前不修改 `ACTIVE_PLAN.md`。

本计划的主线是：

```text
当前未提交修复收口
→ 字幕和任务高频摩擦
→ 独立时间轴契约
→ 只读时间轴
→ 任务级安全片段预览
```

### 1.1 G0 逻辑分组与收口边界

G0 的既有差异必须始终按两个逻辑组隔离，不能混合提交：

**字幕组：**

```text
src/autoslice/resources/static/subtitle_workflow.js
src/autoslice/resources/templates/subtitle_workflow.html
tests/integration/test_app.py
```

**AutoCover 文案组：**

```text
src/autoslice_cover/copy_recommendations.py
tests/unit/autoslice_cover/test_copy_recommendations.py
tests/integration/autoslice_cover/test_app.py
```

每组必须独立验证、独立提交、独立推送并取得 Windows/Linux CI 双绿后，才能登记对应 Action 完成。若代码已经提交但尚未取得远程证据，仍视为该 G0 Action 未完成；不得把本地提交直接当作完成依据。

### 1.2 已核实的技术边界

- `quality_overview` 是可截断摘要，不是完整时间轴数据源；
- `MAX_OVERVIEW_BYTES`、`MAX_CLIP_ROWS`、`MAX_EDGE_CANDIDATE_ROWS` 会造成裁剪；
- 完整时间轴需要独立契约，至少包含 `video_duration`、稳定 ID、数值 `start/end` 和完整性字段；
- `quality_overview` 可按需补 `start/end` 供卡片跳转，但不能承担完整时间轴；
- 第一版时间轴不做弹幕热力、不做边界编辑、不触发重新切片；
- 播放器不能接受任意本机 `path=`，只能播放任务已登记的短片产物；
- 字幕页和 AutoCover 的外层窄窗口仍是桌面工作台，手机只做监控/轻量批准；
- `.task-context-facts` 的 `min-width: 940px` 在两个服务中真实存在，需按源码和实际渲染处理；
- AutoCover 已有 `.inspector-tabs`、`.ratio-switch` 的 roving tablist，不能重复实现；
- `aria-live` 不能机械地全局添加 `aria-atomic`。

### 1.3 不属于本计划

- 不自动投稿、发布、跨平台分发、云同步、账户和多人协作；
- 不制作完整手机字幕编辑器或完整视频编辑器；
- 不引入 OpenCV 人脸选帧、自动主体避让或自动配色；
- 不做整场长录播字幕压制、弹幕渲染或不必要的整场重编码；
- 普通测试不调用真实 LLM、FunASR、GPU、CUDA、FFmpeg、Flask 服务或用户媒体；
- wheel、架构重构、PipelineServices、兼容 shim 和发布门禁另立专项计划。

## 2. 执行模式和估时

```text
execution_mode: single-action-stop
```

一次只执行一个 Action：

```text
读取状态 → 明确边界 → 实现 → 定向验证 → 修复 → 主任务审查
→ 独立提交 → Windows/Linux CI 双绿 → 更新状态和总账 → 停止
```

| 阶段 | 目标 | 预计有效时间（不含 CI 等待） |
|---|---|---:|
| G0 | 收口当前字幕和 AutoCover 修复 | 0.5～2 天 |
| F1 | 字幕无障碍、扫描状态、任务上下文 | 2～4 天 |
| T1 | 时间轴契约、只读时间轴、安全预览 | 3～6 天 |
| L1（后置） | 任务中心、投稿准备度和通知 | 2～5 天 |

不承诺一次完成全部阶段。每个阶段通过真实证据后，才能决定是否进入下一阶段。

### 2.1 Action 依赖和可调节档位

| Action | 依赖 | 默认档位 | 风险级别 | 预计有效时间 |
|---|---|---|---|---:|
| G0-SUB | 无 | Standard | 收口现有差异 | 0.25～1 天 |
| G0-COPY | 无 | Standard | 收口现有差异/内容质量 | 0.5～1.5 天 |
| F1-A11Y | G0-SUB | Standard | 纯前端状态与无障碍 | 0.5～1 天 |
| F1-STATE | G0-SUB、G0-COPY | Lite/Standard | 页面状态一致性 | 0.5～1 天 |
| F1-CONTEXT | G0-SUB、G0-COPY | Standard | 双服务布局 | 0.5～1.5 天 |
| F1-PROGRESSIVE | G0-SUB | Standard | 字幕工作流交互 | 0.5～1.5 天 |
| T1-CONTRACT | G0-SUB、G0-COPY | Full | 新后端契约 | 0.5～1 天 |
| T1-API | T1-CONTRACT | Full | 任务级只读接口 | 0.5～1.5 天 |
| T1-UI | T1-API | Standard | 只读前端 | 0.5～1.5 天 |
| T1-MEDIA | T1-API | Full | 文件访问安全/Range | 1～2 天 |
| T1-LINK | T1-UI、T1-MEDIA | Standard | 页面联动 | 0.5～1 天 |

Lite 只允许 B0 级纯前端或现有契约接线；Standard 可以增加可选字段；Full 才允许新增后端契约、媒体 token 或完整发布门禁。一个 Action 失败时只修复当前 Action，不自动跳过依赖或推断下一个 Action。

## 3. Phase G0：当前差异收口

### Action G0-SUB：收口字幕工作台修复

**范围：**只审查当前字幕组，不重新实现上一轮 `FE-S1`/`FE-S5`。

**必须确认：**

- 批量采纳/忽略后，已处理建议从待处理区域消失；
- 手工保护项继续可见；
- 撤销同时恢复正文和建议可见状态；
- gap 处理只改编辑态、不自动写盘，并保留约 0.08 秒间隔；
- 后续人工时间修改不会被旧撤销动作覆盖；
- 不新增第二套字幕建议或质检 owner。

**定向验证：**

```powershell
node --check src/autoslice/resources/static/subtitle_workflow.js
python -B -m unittest tests.integration.test_app
git diff --check
```

**完成条件：**主任务逐行审查差异，补齐行为测试后独立提交；完整门禁和 Windows/Linux CI 双绿后才登记完成。

### Action G0-COPY：收口 AutoCover 文案修复

**范围：**只审查当前 AutoCover 文案组，不把格式兼容误当成内容质量完成。

**必须确认：**

- Terra 格式错误、重试和失败分类不会伪装成成功；
- `evidence_quotes` 来自最终校对 SRT 的真实正文；
- 有字幕证据不等于自由改写后的文案语义可靠；
- “格式合法但语义不通”的候选不能以 AI 成功状态展示；
- 没有可靠候选时明确显示无可靠文案，不为凑三组候选硬生成低质量文字；
- fallback 不机械拼首句/末句；
- 普通测试不调用真实 Luna/Terra。

**定向验证：**

```powershell
python -B -m unittest tests.unit.autoslice_cover.test_copy_recommendations tests.integration.autoslice_cover.test_app
python -B -m compileall -q src/autoslice_cover
git diff --check
```

固定坏模型、无字幕依据、乱码、无可靠候选和旧契约样本必须有测试。真实短样本另行授权。

### G0 退出条件

G0 未完成前不得开始时间轴 Action。若文案仍“格式通过但内容不可用”，必须停在文案质量 Action，不得继续放宽格式门槛或推进后续功能。

## 4. Phase F1：高频前端摩擦

F1 不重复已完成的任务上下文、AutoCover tab 导航和 `quality_overview` 概览。

### Action F1-A11Y：字幕页无障碍与减少动效

**涉及：**字幕模板、字幕脚本和必要样式；不改 AutoCover 已有 roving tablist。

**必须实现：**

- 动态 `.cue-edit` 有唯一 ID 和真实 label，包含序号、时间范围和“校对后文本”；
- 额外词条、筛选按钮、当前投稿和字幕 tabs 具有正确的 ARIA 状态；
- 字幕 `.settings-tabs` 支持 `ArrowLeft/ArrowRight/Home/End` 和 roving tabindex；
- 增加 `prefers-reduced-motion`；
- 简短最终状态才考虑 `aria-atomic`，高频进度节流，长日志考虑 `role=log`，进度条使用 `aria-valuenow`；
- 不暴露本机绝对路径，不改变字幕保存和撤销语义。

**验证：**

```powershell
node --check src/autoslice/resources/static/subtitle_workflow.js
python -B -m unittest tests.integration.test_app
python scripts/architecture_snapshot.py --check
```

另需固定 mock 浏览器行为验证 label、焦点、键盘和减少动效。

### Action F1-STATE：分析扫描失败状态一致性

**必须实现：**

- 扫描开始进入 scanning 并禁用主动作；
- 失败/空结果清除旧选择、旧摘要和旧上下文；
- 只有旧路径仍在新结果中才恢复选择；
- 明确提供重新扫描或恢复上次有效目录；
- 失败后不能继续对旧失效目录“开始分析+切片”。

**验证：**

```powershell
python -B -m unittest tests.integration.test_app tests.integration.test_topic_engine
python scripts/architecture_snapshot.py --check
```

### Action F1-CONTEXT：任务上下文和首屏主动作

**必须实现：**

- `.task-context-facts` 可收缩，长任务名不会推走快捷入口；
- 复测两个服务的 `min-width:940px` 上下文条行为；
- 智能分析在 1440×900 首屏能看到核心主动作或预检摘要；
- 窄窗口明确提示精细编辑应在桌面完成；
- 复测 1920×1080、1440×900、1366×768、390×844、768×1024 及 Windows 125%/150% 缩放；
- 验证媒体查询时按选择器和层叠结果检查，不能把 `.topbar` 规则当成 `.workbench`。

**验证：**

```powershell
python -B -m unittest tests.integration.test_app tests.integration.autoslice_cover.test_app
python scripts/architecture_snapshot.py --check
```

需要固定 mock 数据的长标题、失败扫描和真实浏览器截图。

### Action F1-PROGRESSIVE：字幕页渐进披露

**必须实现：**

- 按“准备字幕 → 校对 → 建议 → 质检 → 保存/导出”分段；
- ASR/背景音设置只在缺字幕或异常时展开；
- 建议和质检默认显示数量与摘要，有内容才展开详情；
- 保存、预览、压制的依赖关系明确，显式保存后才解锁依赖保存结果的操作；
- 不改变当前保存、草稿、撤销和压制 owner。

**验证：**

```powershell
node --check src/autoslice/resources/static/subtitle_workflow.js
python -B -m unittest tests.integration.test_app tests.integration.test_subtitle_workflow
```

## 5. Phase T1：只读切片时间轴与安全预览

T1 只有在 G0 完成、工作区可归因、F1 的关键状态稳定后才能开始。

### Action T1-CONTRACT：时间轴数据契约

新增独立 serializer/owner 和纯逻辑测试，不先画前端画布。首版至少包含：

```json
{
  "schema_version": 1,
  "task_id": "stable-task-id",
  "video_duration": 0.0,
  "clips": [],
  "edge_candidates": [],
  "complete": true,
  "truncated": false,
  "generated_at": "..."
}
```

切片和候选需要稳定 ID、数值 `start/end`、来源和原因。旧任务缺字段时返回明确不完整状态，不猜测时间。
`quality_overview` 仍只负责摘要；首版不放弹幕热力分桶和话题刻度。

**验证：**空数据、异常时间、重复 ID、缺失时间、超长数据和 `complete/truncated` 的 serializer 单元测试。

### Action T1-API：只读时间轴接口

例如：

```text
GET /api/tasks/{task_id}/timeline
```

只能读取已登记任务和产物；不修改任务、检查点、clip marks、字幕或报告；覆盖成功、404、越权任务、旧任务、缺失数据和序列化错误。

### Action T1-UI：只读时间轴渲染

只实现整场刻度、最终切片色块、边缘候选、详情卡片、筛选、结果卡片联动和不完整提示。第一版不做弹幕热力、拖拽边界、合并拆分、写回 clip marks、重新切片或 FFmpeg。

### Action T1-MEDIA：任务级媒体 token 与 Range 预览

独立于 AutoCover 的 AutoSlice owner 管理 token；不接受任意本机路径；验证任务归属、输出边界、文件类型、token 生命周期和错误脱敏；支持 Range；首版只播放已切出短片。

固定 mock 必须覆盖路径穿越、越权任务、过期 token、Range 边界和不存在媒体。

### Action T1-LINK：时间轴和片段预览联动

色块/结果卡片点击后显示对应详情并打开安全预览，关闭后保留选择；跨页只传稳定任务/片段 ID。此 Action 不编辑、不重新切片。

### T1 退出条件

必须证明完整性字段有效、没有静默丢片段、没有任意文件读取、没有整场编码，并且旧任务仍可读取。通过后才另立时间轴编辑计划。

## 6. 后置候选（当前不激活）

### L1：录播项目与任务中心

复用任务历史、SQLite、检查点、切片、字幕和 AutoCover 草稿，显示阶段、错误、推荐下一步、失败续跑和投稿准备度；不得新建第二套状态机。

### E1：时间轴边界编辑

只有 T1 稳定后才考虑边界预览、撤销、写回 clip marks、派生 JSON、重新切片复用和重复续跑证明；必须单独授权。

### L2：数据回读和 Windows 通知

默认手动触发、不常驻、不上传、不自动投稿，排在核心工作流之后。

## 7. 统一验收门槛

每个 Action 都必须通过：

- 单一生产 owner、无循环依赖、无重复顶层定义、private patch 指标不回退；
- 旧 JSON、检查点、整理包、草稿和任务状态可回读；
- 空数据、失败、旧数据和不完整数据有测试；
- 前端有真实 mock 行为/截图验证；
- 不接受任意本机路径，不公开 Token、Cookie、绝对路径、媒体或报告；
- 每个 Action 开始和结束确认 5002/5010 未监听；
- 不停止 0dcloud、Steam 或无关进程；
- 普通测试不启动真实服务、不调用真实 LLM/FunASR/GPU/FFmpeg/用户媒体；
- `python -B scripts/run_hermetic_tests.py -q`；
- `python -B scripts/run_linux_logic_tests.py -q`；
- `python scripts/architecture_snapshot.py --check`；
- `python scripts/compile_public.py`、`python scripts/scan_public_release.py`；
- `python -m compileall -q src`、`git diff --check`；
- 涉及 wheel/静态资源包时增加 `scripts/smoke_packaging.py` 和 `scripts/verify_distribution.py`；
- 一个 Action 一个 Conventional Commit，只显式暂存相关文件；
- Windows/Linux CI 双绿后才登记到 `docs/action-ledger.md`。

## 8. 激活条件

由用户或主任务明确授权开始后，执行：

1. 再读 `ACTIVE_PLAN.md`、项目 `AGENTS.md`、项目 lessons 和 Git 状态；
2. 确认 G0 相关文件仍属于两个逻辑组，且没有未归因的额外差异；
3. 确认将 `frontend-next` 挂接为活动计划；
4. 更新 `ACTIVE_PLAN.md` 的计划 ID、版本、第一个 Action 和状态；
5. 只启动 `G0-SUB` 或 `G0-COPY` 一个 Action，完成后停止；
6. 双绿后更新活动状态和总账，不自动进入下一个 Action。

本计划不包含用户录播路径、人工时间轴路径、媒体样本、截图报告、API 配置或本机维护笔记。
