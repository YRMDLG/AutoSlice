# Desktop vNext 架构审查报告

> 审查日期：2026-10-01  
> 审查范围：`src/autoslice/desktop/`、`src/autoslice_cover/`、Qt Desktop 入口及相关架构契约  
> 对照依据：[`AUTOSLICE_VNEXT_PRODUCT_ROADMAP.md`](AUTOSLICE_VNEXT_PRODUCT_ROADMAP.md)、[`ARCHITECTURE.md`](ARCHITECTURE.md)、[`MIGRATION_MAP.md`](MIGRATION_MAP.md)、[`AI_SUGGESTIONS.md`](AI_SUGGESTIONS.md)  
> 方法：静态代码审查与 AST 结构统计；未启动桌面端、Flask、AutoCover、FFmpeg 或外部 LLM。

## 结论

Desktop vNext 已经形成了比较清晰的基础分层：项目扫描在 `projects.py`，字幕文档在 `subtitles.py`，AI 适配在 `ai_review.py`，字幕预览/压制在独立 service，封面渲染也通过 `cover_service.py` 调用 `autoslice_cover`。当前没有发现需要立即推翻的循环依赖或 AutoCover 侵入旧 Web 状态的问题。

最大的未来风险集中在 **Qt 主窗口和跨模块上下文**，而不是底层媒体或封面渲染：

1. [`qt_app/window.py`](../../src/autoslice/desktop/qt_app/window.py) 已达到 2,664 行，`DesktopWindow` 有 120 个方法、约 122 个实例字段；它同时管理窗口构建、字幕文档、选择状态、播放器、时间轴、AI 队列、草稿、压制任务、会话恢复和关闭流程。
2. AutoCover 的控件与服务已经隔离，但 `DesktopWindow` 仍负责创建它、注入项目/视频上下文、转发播放头，并承载页面切换生命周期。只要封面功能继续增加，主窗口很容易重新成为跨模块协调中心。
3. AI 业务大体已在 `ai_review.py`，但 `DesktopWindow` 仍持有 AI 会话的全部交互状态和撤销/跳过历史；未来增加封面 AI、标题候选或更多审查阶段时，当前结构会把不同 AI 用例重新揉进窗口。
4. `SubmissionProjectService.snapshot` 是可用的扫描快照，但它还不是完整的应用级 `Project Context`。当前项目、视频、字幕文档、封面草稿、播放头、AI 会话和任务状态分别保存在窗口与子控件中，切页、刷新、异步回调和恢复时存在状态串线的增长风险。

建议暂不做大范围重写，先以小步抽取和契约测试控制风险。优先顺序是：先冻结窗口边界和上下文契约，再抽取 AI/任务协调器，最后按需要拆分页面控制器。AutoCover 当前可以继续演进，但应保持它只接收显式上下文和服务结果。

## 1. `window.py` 职责审查

### 现状证据

`DesktopWindow` 的构造函数同时创建并持有以下对象：`SubmissionProjectService`、`DesktopStorage`、`AIReviewService`、`SubtitleRenderService`、`SubtitlePreviewService`、`WaveformCache`、`MpvAdapter`、`CommandDispatcher`、`CueSelection` 以及封面编辑器。窗口还直接初始化 10 余组异步/播放器/编辑状态，例如 `_ai_*`、`_render_jobs`、`_player_*`、`_pending_*`、`_draft_timer` 和 `_preview_timer`。

方法按职责大致分为：

| 职责 | 代表方法 | 当前风险 |
| --- | --- | --- |
| 页面与控件构建 | `_cover_page`、`_subtitle_work_area`、`_ai_panel`、`_timeline` | 视觉改动会触碰业务窗口，控件生命周期难以独立测试 |
| 项目/视频切换 | `refresh`、`_scanned`、`_select_real_project`、`_load_video`、`_loaded` | 刷新、切页、异步加载和未保存决策共享多个布尔状态 |
| 字幕编辑 | `_cue_clicked`、`_open_inline_editor`、`_commit_editor`、`_edited` | 编辑焦点、选择、时间轴和草稿互相调用，回归成本高 |
| 播放器/时间轴 | `_seek_to`、`_player_status`、`_play_window`、`_timeline_changed` | 播放器事件直接改窗口字段，后续更换 adapter 会扩大改动面 |
| AI 审查 | `_start_ai_check`、`_ai_finished`、`_accept_ai`、`_skip_ai`、`_render_ai` | 领域状态、线程取消、展示和撤销逻辑集中在窗口 |
| 草稿/正式保存 | `_write_draft`、`_resolve_unsaved`、`save`、`_saved` | 字幕保存、封面保存、关闭和切换的策略容易产生分叉 |
| 压制任务 | `_start_render`、`_render_finished`、`closeEvent` | 任务生命周期与窗口关闭绑定，未来多任务类型会继续增加分支 |

这已经超过了“窗口只负责组装和转发”的边界。问题不是文件行数本身，而是同一类变更会同时影响 UI、状态、异步任务和旧工作流适配层。`DesktopWindow` 目前是应用控制器、页面控制器、播放器协调器和任务协调器的混合体。

### 建议

按风险从低到高分三步实施：

1. **先建立内部边界，不改用户流程。** 新增轻量的 `DesktopContext`/`WorkspaceContext` 数据对象，集中表达当前 `ProjectSnapshot`、`SubmissionProject`、`ProjectVideo`、当前字幕文档及 generation/token；窗口只负责把 Qt 事件转给控制器。第一步可以仍放在 `qt_app/` 下，避免过早建立过多文件。
2. **抽取页面控制器。** 优先抽出 `SubtitlePageController`（字幕加载、编辑、草稿、保存、压制入口）和 `CoverPageController`（上下文、播放头、封面控件）；控制器通过信号/回调更新窗口，不直接操作另一个页面的控件。
3. **抽取跨页后台任务协调器。** 将 `_run`、generation、取消、任务状态和错误归一化为 `DesktopTaskCoordinator` 或同等小接口。它不依赖 Qt 控件，只发布任务完成/失败/取消事件。这样窗口关闭、最小化和切页不需要分别维护每种任务的特殊逻辑。

不建议现在把 120 个方法机械拆成大量类，也不建议引入全局事件总线；先围绕“字幕页、封面页、后台任务”三个实际变化源拆分即可。

## 2. AutoCover 隔离审查

### 已经做对的部分

- `CoverEditorWidget` 位于 [`desktop/cover.py`](../../src/autoslice/desktop/cover.py)，编辑状态、拖拽反馈和控件事件没有放回主窗口。
- `CoverService` 位于 [`desktop/cover_service.py`](../../src/autoslice/desktop/cover_service.py)，负责取帧、导入图片、草稿读写、预览与导出所需的数据准备；实际 Pillow 渲染继续由 `autoslice_cover.renderer` 完成。
- `CoverCanvas` 单独维护画布交互，封面 service 不依赖旧 Web 页面或自动切片任务状态。
- `DesktopWindow._cover_page()` 只创建编辑器，项目/视频通过 `set_context(project, video)` 注入；当前没有发现封面模块反向导入 Qt 主窗口。
- `cover.py` 使用 `_context_generation` 丢弃切换项目后的迟到取帧/渲染结果，方向正确。

这些事实符合路线图中“AutoCover 是基础封面生产工具，不是 Photoshop，也不把自动分析作为前置条件”的边界。

### 未来风险

1. `DesktopWindow._select_page()` 直接把字幕页的 `_player_position` 转发给 `cover_editor.set_current_playhead()`。这是一条合理的只读桥接，但目前是窗口私有字段之间的隐式协议；未来若封面页需要视频时长、当前媒体或播放状态，容易继续增加跨页字段。
2. `CoverEditorWidget` 自己拥有 service、草稿、预览 timer、任务集合和 context generation。它目前仍可控，但如果加入双比例、素材对象、撤销/重做、AI 候选，单个控件也会成为新的 God Widget。
3. `CoverService` 同时处理持久化草稿、导入路径、取帧和渲染准备。当前范围尚可，但双比例或素材库加入后，应避免把布局策略、文件生命周期和渲染调用继续放在同一个 service。

### 建议

- 保持 AutoCover 只接收不可变的项目/视频上下文和显式播放头快照；不要让它读取 `DesktopWindow`、`MpvAdapter` 或字幕文档。
- 在进入双比例/素材层级前，把 `CoverDraft` 明确分成“编辑文档”和“媒体资产引用”两类数据；渲染器只消费布局快照，草稿存储只消费可序列化文档。
- 给 `CoverService` 增加面向契约的 `load_context / extract_frame / render_preview / export` 边界，未来把草稿读写和媒体操作拆开时不影响控件。
- `set_current_playhead()` 保持单向、只读、无副作用；不要让封面页反向 seek 播放器。这个约束应继续由测试固定。

结论：**AutoCover 当前隔离方向正确，属于“继续加护栏”而不是立即重构。**

## 3. AI 逻辑拆分审查

### 当前分层

`desktop/ai_review.py` 已把以下内容集中起来：`Suggestion`/`AIReviewSession` 数据结构、文档哈希、配置与主播 profile 指纹、缓存键、会话持久化、旧字幕检查器调用以及结果校验。它不直接依赖 Qt，符合“AI adapter 与 UI 分离”的目标。

主窗口仍直接管理：

- 当前 AI 会话、选中建议、pending 过滤；
- 采纳/跳过的撤销与重做栈；
- 后台检查的取消事件、generation、进度和错误提示；
- 侧栏 HTML 差异渲染与建议列表；
- 切页后返回 AI 的项目/视频定位。

因此，AI 的“调用与持久化”已拆出，但“审查工作流状态机”仍在 `DesktopWindow`。现在只有字幕 AI 时尚可，后续封面 AI 或标题候选会迫使窗口出现第二套相似状态。

### 建议

先不拆 transport，也不把所有 AI 统一成一个复杂框架。建议新增一个小的 `AIReviewController`/`SubtitleReviewSession`，负责：

- `start / cancel / finish` 生命周期；
- 对当前文档快照的 generation 校验；
- `accept / skip / undo / redo` 状态转换；
- 生成可供 UI 消费的 `ReviewViewState`。

Qt 侧只保留按钮、列表、差异显示和信号连接。`AIReviewService` 继续负责配置、缓存、旧检查器适配；未来 AutoCover AI 则使用独立的 `CoverAdvisorService`，共享 transport 契约但不共享字幕 prompt、会话字段或审查状态。

另外，`AIReviewService._identity()` 当前每次加载/保存都会解析配置和主播 profile；这在功能正确性上没有问题，但应在 controller 层固定一次检查上下文，避免同一任务期间配置变化造成“开始和保存使用不同身份”的隐式行为。配置变化时应以明确的 stale 结果结束，而不是让窗口自行猜测。

## 4. Project Context / 状态共享审查

### 当前状态

`SubmissionProjectService` 负责扫描投稿根目录，并发布不可变的 `ProjectSnapshot`；项目和视频模型也是 frozen dataclass。这个边界清楚，且符合“项目文件夹是事实来源、数据库/草稿只补充编辑状态”的路线图要求。

但当前完整工作状态被分散在多个对象：

| 状态 | 当前持有者 | 主要问题 |
| --- | --- | --- |
| 扫描结果 | `SubmissionProjectService.snapshot` | 服务是可变 singleton，窗口直接读取，刷新期间缺少显式快照版本 |
| 当前项目/视频 | `DesktopWindow.project`、`document.video`、`video_choice` | 同一事实有多个 UI/对象表示，异步回调需用对象身份和路径双重判断 |
| 字幕文档/选择 | `DesktopWindow.document`、`SubtitleTableModel`、`CueSelection`、`SubtitleTimeline` | 已有契约，但同步依赖窗口大量手工转发 |
| 封面草稿 | `CoverEditorWidget.draft` + `CoverService` | 与字幕草稿的策略独立，跨页上下文由窗口桥接 |
| AI 会话 | `DesktopWindow.ai_session` + 私有缓存 | 会话恢复依赖当前窗口，项目/视频变化时由多个方法清空 |
| 任务状态 | `_jobs`、`_render_jobs`、`CoverEditorWidget._jobs` | 同一应用存在多套线程任务跟踪和取消语义 |
| 会话恢复 | `DesktopStorage` + `DesktopWindow._session` | 恢复字段由窗口自由拼装，缺少版本化的领域上下文契约 |

主要风险是“迟到结果写回错误上下文”：扫描、视频加载、波形、AI 和压制都采用各自的 generation/token/key。现有代码已经多次做了保护，但每新增一种异步功能就需要复制一套判断。

### 建议的最小抽象

新增一个不可变的 `WorkspaceContext`（名称可调整），至少包含：

- `snapshot_version` 或刷新 generation；
- `project_id`、`video_path`；
- 源视频/SRT/校对 SRT 的文件指纹；
- 当前页和当前 cue；
- 可选的字幕文档版本/内容 hash；
- 草稿、AI、波形、压制等派生任务的 context key。

所有后台任务启动时捕获 context，完成时由统一方法判断 `context.is_current()`；任务结果只返回数据，不直接触碰另一个页面的控件。`ProjectSnapshot` 仍保留为扫描结果，不要把它扩展成包含所有编辑状态的巨型对象。

同时建议把“当前项目/视频”作为唯一选择源：组合框、项目按钮、字幕文档和封面页都通过 context 更新，而不是各自推断当前对象。这样可以减少 `_select_real_project`、`_video_changed`、`_loaded` 和封面 `set_context` 之间的隐式耦合。

## 5. 明显需要记录或后续重构的问题

以下问题不要求本轮立即修复，但应进入 backlog 或架构契约：

### P0：进入下一轮功能前应固定

1. **主窗口边界。** 记录 `DesktopWindow` 只负责组装 Qt、页面切换和信号连接；新的业务状态不得继续直接加入窗口，除非说明其归属。
2. **异步上下文一致性。** 所有新后台任务必须携带项目/视频 context key，并在回调处统一丢弃迟到结果；不要只依赖当前控件是否存在。
3. **AutoCover 单向桥接。** 封面只能消费项目、视频和播放头快照，不得反向控制播放器或读取字幕 UI。
4. **AI 用例隔离。** 字幕审查、封面建议、标题候选共享 transport/config 适配即可，不共享 prompt、缓存 schema 或领域状态。

### P1：近期适合小步抽取

1. 抽取 `DesktopTaskCoordinator`，统一 `_Job`、取消、generation、错误和进度事件；先覆盖 AI、取帧/预览、波形和压制四类任务。
2. 抽取 `SubtitlePageController`，把字幕加载、编辑、草稿、保存与压制入口从 `window.py` 移出。
3. 抽取 `AIReviewController`，把建议状态机和撤销历史从 Qt 控件中移出。
4. 为 `WorkspaceContext` 和任务结果增加序列化/版本测试，覆盖刷新项目、切换视频、切页、关闭窗口和重启恢复。

### P2：功能扩展触发时再做

1. AutoCover 进入双比例、贴图层级或撤销/重做前，再拆分 `CoverService` 的草稿/资产/渲染职责。
2. 封面 AI 进入实施时建立独立的 `CoverAdvisorService` 和结构化布局候选契约。
3. 旧 Tk Desktop 与 Qt Desktop 并存期间，明确“兼容入口”与“正式入口”的所有权；不要让两套 UI 继续新增功能。路线图中的 Desktop-15 退役任务应保留为独立收口，不要提前删除兼容实现。
4. 任务类型增多后，再评估是否把 SQLite `TaskStore/TaskRegistry` 适配到桌面端；不要现在为了统一而把短期 UI 任务强行接入旧 Web SSE。

## 6. 建议的验证护栏

后续每次涉及架构边界的改动，建议增加或更新以下离线测试：

- 静态依赖测试：`qt_app/window.py` 不得被 `autoslice_cover` 或旧 Web UI 反向导入；封面 service 不得依赖窗口。
- 上下文测试：切换项目/视频后，迟到的 AI、波形、取帧、预览和压制结果不能更新新上下文。
- 状态机测试：AI 采纳、跳过、撤销、重做与文档内容 hash 始终一致。
- 草稿测试：字幕和封面草稿的源文件变化、项目变化、恢复失败都只产生可见状态，不覆盖正式文件。
- 页面切换测试：封面页消费播放头快照但不触发播放器 seek；切页不丢失当前项目/视频选择。
- 任务测试：取消、关闭、最小化和任务完成顺序不产生重复发布或错误提示覆盖。

现有 `tests/architecture/` 已经覆盖依赖环、AutoCover owner 和兼容性契约；建议在这些测试旁增加面向 Desktop vNext 的 owner/consumer 断言，而不是再添加全仓库固定函数总数断言。

## 最终判断

当前架构可以继续支持路线图中的基础封面和字幕生产线，AutoCover 隔离方向也满足产品边界。真正需要提前控制的是 `DesktopWindow` 的继续膨胀和跨页状态没有统一上下文这两个问题。下一轮适合做“小范围结构治理”：先定义上下文与任务回调契约，再抽取字幕页和 AI 控制器；不需要现在进行大规模目录重组或重写媒体服务。
