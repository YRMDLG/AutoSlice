# Desktop vNext 历史资料归档

> 本目录只保存历史资料，仅供追溯，不是当前规范。当前有效结论请从上一级 [README.md](../README.md) 开始阅读。

## 归档规则

- 归档文件不删除，保留原文、原日期和当时的上下文。
- 新的产品、交互、架构和状态结论不得继续写入这些旧稿；应更新上一级目录的当前有效文档。
- 旧稿中的路径、行号、测试数量、实现状态和链接可能只对应当时的工作区，不能直接当作今天的证据。
- 如果历史资料与当前规范冲突，以当前有效文档和用户最新实机反馈为准。
- 黄金测试集、编辑器模型规划和 UX 审计仍留在上一级目录，因为它们包含可复用案例和长期约束；本目录不复制它们。

## 文件清单

| 文件 | 历史类型 | 由哪个当前入口承接 |
|---|---|---|
| `ADR-001-desktop-gui-and-player.md` | Qt 壳、播放器和打包阶段 ADR | [ARCHITECTURE.md](../ARCHITECTURE.md)、[DECISIONS.md](../DECISIONS.md) |
| `AI_SUGGESTIONS.md` | 字幕 AI 建议阶段契约 | [DESKTOP_STATUS.md](../DESKTOP_STATUS.md)、[ARCHITECTURE.md](../ARCHITECTURE.md) |
| `ARCHITECTURE_REVIEW.md` | Desktop/AutoCover 架构审计报告 | [ARCHITECTURE.md](../ARCHITECTURE.md)；审计原文保留 |
| `AUTOCOVER_AUDIT.md` | AutoCover 最小工作流审计 | [AUTOCOVER_SPEC.md](../AUTOCOVER_SPEC.md)、[DESKTOP_STATUS.md](../DESKTOP_STATUS.md) |
| `AUTOCOVER_EDITOR_PLAN.md` | 编辑器阶段实施计划 | [AUTOCOVER_SPEC.md](../AUTOCOVER_SPEC.md)、[ROADMAP.md](../ROADMAP.md) |
| `AUTOCOVER_VNEXT_PLAN.md` | AutoCover vNext 阶段总计划 | [AUTOCOVER_SPEC.md](../AUTOCOVER_SPEC.md)、[ROADMAP.md](../ROADMAP.md) |
| `AUTOSLICE_VNEXT_PRODUCT_ROADMAP.md` | 早期产品路线草稿 | [PRODUCT.md](../PRODUCT.md)、[ROADMAP.md](../ROADMAP.md) |
| `BACKLOG.md` | 阶段 backlog 和视觉收口事项 | [ROADMAP.md](../ROADMAP.md) |
| `CURRENT.md` | 旧的当前状态快照 | [DESKTOP_STATUS.md](../DESKTOP_STATUS.md) |
| `DESKTOP_FOUNDATION.md` | Desktop 基础壳与迁移阶段说明 | [ARCHITECTURE.md](../ARCHITECTURE.md)、[DECISIONS.md](../DECISIONS.md) |
| `DESKTOP-08.8-PLAN.md` | Desktop-08.8 阶段计划 | [ROADMAP.md](../ROADMAP.md)、[DECISIONS.md](../DECISIONS.md) |
| `MIGRATION_MAP.md` | 旧版迁移映射和边界快照 | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| `UX.md` | 综合 UX 设计稿和阶段交互说明 | [PRODUCT.md](../PRODUCT.md)、[AUTOCOVER_SPEC.md](../AUTOCOVER_SPEC.md) |
| `VISUAL_SYSTEM.md` | 旧视觉基线和尺寸令牌 | [PRODUCT.md](../PRODUCT.md)、[ROADMAP.md](../ROADMAP.md)；当前 UI Cleanup 需要重新验收 |

## 为什么保留架构审计和阶段计划

这些文件记录过真实的代码证据、风险判断和用户反馈，删除会让后续维护者失去追溯路径。它们保留的是历史证据，不是继续扩展的待办清单；实现时先读当前总纲，再按需要回看原文。