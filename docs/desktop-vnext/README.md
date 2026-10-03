# Desktop vNext 文档总入口

> 本目录是 AutoSlice 桌面端 vNext 的唯一文档入口。当前有效文档以本文件链接的根目录文档为准；`archive/` 只保存历史方案、阶段计划和审计快照，不构成当前规范。
>
> 整理日期：2026-10-03。本文档整理不改变业务代码、测试语义或发布流程。

## 推荐阅读顺序

第一次了解项目时，按下面的顺序阅读：

1. [PRODUCT.md](PRODUCT.md)：产品定位、真实工作流、功能边界和主导航。
2. [DESKTOP_STATUS.md](DESKTOP_STATUS.md)：当前已经实现的桌面端能力、未完成项和验收状态。
3. [AUTOCOVER_SPEC.md](AUTOCOVER_SPEC.md)：AutoCover 的当前产品与交互契约。
4. [ARCHITECTURE.md](ARCHITECTURE.md)：模块边界、状态共享、异步任务和持久化约束。
5. [DECISIONS.md](DECISIONS.md)：已经冻结的关键决策，按 ADR-like 条目集中维护。
6. [ROADMAP.md](ROADMAP.md)：近期、中期和暂缓事项，以及进入下一阶段的验收门槛。

需要追溯背景时，再阅读根目录的长期参考或进入 [archive/README.md](archive/README.md)。

## 当前有效文档

| 文档 | 作用 | 状态 |
|---|---|---|
| [PRODUCT.md](PRODUCT.md) | 产品定位、工作流、边界和导航 | 当前有效 |
| [DESKTOP_STATUS.md](DESKTOP_STATUS.md) | Desktop vNext 和 AutoCover Beta 的真实状态 | 当前有效，随实机验收更新 |
| [AUTOCOVER_SPEC.md](AUTOCOVER_SPEC.md) | AutoCover 产品、数据模型、交互和验收契约 | 当前有效 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构边界和不可违反的实现约束 | 当前有效 |
| [DECISIONS.md](DECISIONS.md) | 冻结决策清单 | 当前有效；新决策追加或更新状态 |
| [ROADMAP.md](ROADMAP.md) | 后续路线和暂缓范围 | 当前有效；不代替执行计划 |

这些文件描述“应该保持什么”，不会代替代码、测试或用户实际反馈成为实现证据。

## 长期参考资料

下面的资料仍有长期价值，保留在本目录根部，供实现和验收引用：

- [AUTOCOVER_COPY_GOLDEN_SET.md](AUTOCOVER_COPY_GOLDEN_SET.md)：基础文案黄金案例。
- [AUTOCOVER_LAYOUT_GOLDEN_SET.md](AUTOCOVER_LAYOUT_GOLDEN_SET.md)：布局黄金案例。
- [AUTOCOVER_EDITOR_MODEL_PLAN.md](AUTOCOVER_EDITOR_MODEL_PLAN.md)：编辑器领域模型的原始规划和边界。
- [AUTOCOVER_UX_AUDIT.md](AUTOCOVER_UX_AUDIT.md)：AutoCover UX 审计中的问题证据和判断。

它们不是新的总纲，但其中的案例、审计发现和模型原则仍然有效。若与当前规范发生冲突，以本目录当前有效文档和用户最新实机反馈为准。

## 历史归档

[archive/](archive/) 保存已经被当前总纲覆盖的阶段性文档，包括旧的 CURRENT 状态快照、Desktop 阶段计划、AutoCover 阶段方案、视觉基线和架构审计。它们不删除、不改写，只用于追溯当时的决策和实现背景。归档规则与清单见 [archive/README.md](archive/README.md)。

## 项目级关联文档（保持原位）

以下文件涉及整个 AutoSlice 仓库的计划、架构或发布，不是 Desktop vNext 的唯一规范，因此本次不移动：

- 根目录的 [PLAN.md](../../PLAN.md) 和 [ACTIVE_PLAN.md](../../ACTIVE_PLAN.md)：项目级执行计划状态。
- [docs/架构重构.md](../架构重构.md)：全仓库模块 owner、兼容 façade 和安全边界。
- [docs/开发与发布.md](../开发与发布.md)：代码 owner、发布 staging 和公开发布门禁。
- [autocover_tool/README.md](../../autocover_tool/README.md)：旧 AutoCover 入口和运行数据兼容说明。

它们可以作为背景资料阅读，但不能绕过本目录的产品、状态和 AutoCover 规范重新定义桌面端行为。

## 文档更新规则

- 新的产品或交互结论先更新当前有效文档，再在 `DECISIONS.md` 写出可执行的冻结决策。
- 代码已经实现不等于视觉验收通过；状态文档要分别记录“实现证据”和“用户实机反馈”。
- 旧计划和旧审计不在原地继续叠加新状态；被覆盖后移入 `archive/`，避免出现两个“当前版本”。
- 文档引用代码时指向真实 owner；不要把兼容入口、临时报告或历史路径写成新的实现边界。
- 仅做文档整理时，不运行服务、不调用真实 LLM/FunASR、不处理用户媒体，不顺手修改 `src/`、`tests/` 或安装构建目录。

## 目录概览

```text
docs/desktop-vnext/
├── README.md                         # 本入口
├── PRODUCT.md                        # 产品与工作流
├── DESKTOP_STATUS.md                 # 当前实现状态
├── AUTOCOVER_SPEC.md                 # AutoCover 当前规范
├── ARCHITECTURE.md                   # 架构与迁移约束
├── DECISIONS.md                      # 冻结决策
├── ROADMAP.md                        # 后续路线
├── AUTOCOVER_COPY_GOLDEN_SET.md      # 长期参考：文案黄金集
├── AUTOCOVER_LAYOUT_GOLDEN_SET.md    # 长期参考：布局黄金集
├── AUTOCOVER_EDITOR_MODEL_PLAN.md    # 长期参考：编辑器模型规划
├── AUTOCOVER_UX_AUDIT.md             # 长期参考：UX 审计
└── archive/                          # 历史资料，不是当前规范
```

## 长期项目上下文与协作记忆

除了“当前规范”，本目录还保存一组长期上下文文档，用于让新的 ChatGPT / Work 线程继承项目状态，而不是重新翻聊天记录：

- [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)：完整项目背景、真实工作流、当前阶段、运行环境和代码语境。
- [USER_REQUIREMENTS.md](USER_REQUIREMENTS.md)：用户明确需求、偏好、禁忌、性能/AI/UI/部署要求。
- [INTERACTION_CONTRACT.md](INTERACTION_CONTRACT.md)：字幕与 AutoCover 已冻结的可操作行为和验收契约。
- [COLLABORATION_NOTES.md](COLLABORATION_NOTES.md)：ChatGPT / Work / Remote Desktop Commander 的协作与执行规则。
- [HISTORY_AND_LESSONS.md](HISTORY_AND_LESSONS.md)：Desktop vNext 和 AutoCover 的演进、失败尝试与经验教训。

建议新的执行线程先读：
`README → PROJECT_CONTEXT → USER_REQUIREMENTS → DESKTOP_STATUS → AUTOCOVER_SPEC → INTERACTION_CONTRACT`。

这些文档记录的是长期上下文，不代替当前代码和用户最新反馈；如果历史记录与新的真实实机体验冲突，以用户最新实机反馈为准。
