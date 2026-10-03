# 代码审计报告（desktop-vnext-refactor）

审计对象为 `desktop-vnext-refactor` 分支 `7f7f047`，与当时的 `desktop-vnext-dev` 完全一致。审计只读代码，没有修改业务代码。文中的行号、行数和计数都是这个 commit 的快照，代码变化后需要重新核对。

## 1. 结论

**整体不是“屎山”。** 后端已经按 [架构重构说明](../架构重构.md) 做过一轮有护栏的重构，基本功扎实：

- **依赖关系干净**：import 图无环，每项职责有唯一的实现模块，兼容层只做别名转发。
- **错误处理规矩**：67 处宽泛异常捕获里，61 处会记录上报或转换后重新抛出，真正悄悄吞掉的只有 2 处。
- **重复代码少**：跨位置重复约 730 行，占源码 1% 多一点。
- **死代码少**：确认无人调用的函数只有 5 个，约 70 行。
- **低级错误少**：ruff 的潜在缺陷类规则几乎没有命中，抽查的几处也都不会出错。

**技术债集中在少数几处：**

1. **护栏当前是红的**：架构快照过期，测试里替换私有符号的次数超过上限，公开发布扫描也不通过（§3）。
2. **桌面端主窗口是上帝类**：`DesktopWindow` 一个类 2268 行、119 个方法，继承自演示用的预览窗口，旧 Tk 壳也还在（H1、M5、M6）。
3. **流水线参数层层透传**：阶段函数有 28、29 个参数；重试状态恢复函数圈复杂度 87（H2、H3）。
4. **边界扩展是一长串规则**：核心算法函数圈复杂度 79，并大量跨模块调用别人的私有函数（H4、M1）。
5. **Web 主程序是上帝模块**：`web/app.py` 3019 行；7 个后台任务函数开头和结尾的写法重复；还有 9 处经兼容层导入（M2、M3）。
6. **改了名的复制粘贴绕过了护栏**：字幕断行算法、校对状态序列化、两个 Flask 程序的安全后处理（M4）。
7. **前端有千行内联脚本**：主工作台模板里内联了 1061 行脚本，不在 CI 检查范围内（M7）。

## 2. 范围与方法

**范围**

- **源码**：`src/`、`scripts/` 和根目录兼容入口，共 209 个文件、约 60,000 行（含 JS、HTML、CSS）。架构快照统计的生产 Python 是 204 个模块、51,044 行。
- **测试**：119 个测试文件只看结构和覆盖缺口，不逐行审。

**方法**

1. **自动扫描全部文件**，没有安装新工具：
   - ruff 扩展规则：复杂度、参数、分支、宽泛异常、可简化写法、潜在缺陷。
   - 自写的 AST 度量：逐函数统计行数、圈复杂度、嵌套深度、参数数和内嵌函数。
   - 自写的 6 行窗口重复代码检测。
   - 死代码候选检测。
   - 宽泛异常的处理方式分类。
   - 跨模块使用私有符号的统计。
   - git 改动热点统计。
   - 测试引用覆盖统计。
   - 仓库自带的 `scripts/architecture_snapshot.py --check` 和架构测试。
2. **人工精读热点**：
   - `desktop/qt_app/window.py`
   - `qt_preview/window.py`
   - `pipeline.py` 和 `pipeline_retry.py`
   - `analysis/boundaries.py`
   - `web/app.py`
   - `subtitle_workflow.py` 和 `transcription/segments.py`
   - `desktop/subtitles.py`
   - `autoslice_cover/app.py`
   - `topic_v2.html`
3. **判断标准以项目文档为准**：[架构重构说明](../架构重构.md) 的依赖方向和唯一 owner 规则、[Desktop 架构](../desktop-vnext/ARCHITECTURE.md) 的迁移边界。不按个人偏好判断，也不把“函数长”单独当成问题。

**局限**

- 没有逐行精读的模块，结论只来自自动扫描。
- 我自写的圈复杂度会把内嵌函数也算进外层函数，所以 `create_app` 等函数的数值偏高，正文已经单独说明。
- 没有运行真实媒体、模型或 LLM。

## 3. 护栏状态（最先处理）

| 项 | 现状 | 影响 |
|---|---|---|
| 架构快照 | `architecture_snapshot.py --check` 报告快照过期 | CI 的“Check architecture snapshot”步骤会失败 |
| 未登记模块 | `desktop.commands`、`desktop.selection`、`desktop.snap`、`desktop.subtitle_preview`、`desktop.waveform` 共 5 个新模块不在基线里 | 同上 |
| 私有 patch 计数 | 测试对私有符号的 patch 从 17 增加到 20，超过基线上限 | 架构测试的私有 patch 上限检查会失败 |
| 规模变化 | 生产模块 199→204，生产行数 49,030→51,044，import 边 517→524 | 需要审阅后重新生成快照 |
| 公开发布扫描 | `scan_public_release.py` 报告非测试用途的 Windows 绝对路径：`src/autoslice/desktop/projects.py` 和 `docs/desktop-vnext/` 下 3 份文档 | 发布门禁不通过（见 M9） |

建议：先审阅新增的 3 处私有 patch。确有理由就保留并重新生成基线，否则改成通过公开接口测试。之后按项目规定，用脚本重新生成 `architecture_baseline.json`，不能手工修改。另外，桌面端测试在装了 PySide6 的本地环境里有一处稳定崩溃和一处收尾异常，修复见 PR #1。

## 4. 问题清单

严重程度：**高** 表示结构性问题，持续拖累后续开发，或者关系核心业务正确性；**中** 表示局部的组织或一致性问题；**低** 表示清理项。工作量：S 不到半天，M 1～3 天，L 3 天以上，按分步提交估算。

### 高

#### H1 桌面主窗口 `DesktopWindow` 是上帝类

- **位置**：`src/autoslice/desktop/qt_app/window.py:366`，类本身 2268 行，含 119 个方法。
- **证据**：
  - 一个类同时承担 9 类职责：界面搭建（约 500 行）、AI 校对流程、项目扫描与载入、字幕编辑与行内编辑器、时间轴交互、试听与循环、播放器状态、草稿/保存/压制、撤销重做和会话持久化。
  - AI 的撤销重做栈这类业务状态，直接放在窗口对象上。
  - `__init__` 有 127 行。
  - 父类 `PreviewWindow` 在构造时就回调子类重写的界面搭建方法，所以子类必须在 `super().__init__()` 之前先设好 33 个属性；全文件还有 12 处 `hasattr(self, ...)` 防御初始化顺序。
  - 父类 21 个方法中有 12 个被重写，继承基本只用来复用页面骨架。
- **建议**：
  - 窗口改为“组装根”，只负责创建控件和连接信号。
  - 按职责逐个抽出控制器：AI 复核、项目载入、字幕编辑（含行内编辑器）、时间轴（含试听与循环）、播放、压制、会话持久化。每个控制器单独一个提交，测试先行。
  - 不再继承预览窗口，改用组合（见 M6）。
- **风险**：中高，涉及界面行为。目前桌面端有 70 条测试可以兜底。
- **工作量**：L。
- **注意**：`desktop-ui-opus55v2` 分支正在大量修改 `window.py`、`timeline.py`、`theme.py`，两边同时改会产生大量合并冲突，需要先约定先后顺序（§6）。

#### H2 流水线阶段函数参数层层透传

- **位置**：
  - `pipeline_reporting.py:39 prepare_pipeline_report`，29 个参数。
  - `pipeline_artifacts.py:6 persist_pipeline_artifacts`，28 个参数。
  - `pipeline_retry_reporting.py:10 prepare_retry_report`，28 个参数。
  - 还有 `prepare_retry_decisions`（20 个）、`review_pipeline_candidates`（18 个）、`review_retry_candidates_and_titles`（17 个）。
- **证据**：
  - `pipeline.py` 把主流程的局部变量和服务函数一项项摊开传给阶段函数；主流程和重试流程各传一遍，几乎相同（`pipeline.py:737-803` 和 `pipeline.py:1027-1093` 有 11～17 行重复）。
  - 产物路径（`artifact_layout`、各 checkpoint 路径、manifest 路径）、阈值策略（`CLIP_*`、`TOPIC_*` 常量）、服务函数（写产物、写 manifest、更新队列）总是成组出现。
- **建议**：
  - 引入三个参数对象：`PipelineArtifactPaths`（不可变 dataclass）、`ClipPolicy`（阈值）、`PipelineServices`（注入的服务函数）。
  - 这不是项目文档里拒绝过的“统一 `PipelineStage` 类框架”，只是把成组的参数打包，阶段函数的契约不变。
- **风险**：中。签名改动面大，要配合架构测试里的对象身份检查。
- **工作量**：M。

#### H3 重试状态恢复函数过于复杂

- **位置**：`src/autoslice/pipeline_retry.py:9 prepare_retry_pipeline_state`。
- **证据**：
  - 308 行，圈复杂度 87（其中内嵌的 `merge_completed_review_with_baseline` 占 36），嵌套 7 层，16 个参数，其中 11 个是注入的函数。
  - 一个函数里混了四件事：产物定位、旧布局迁移、人工时间轴重建，以及复核检查点的复用和过期判断。
- **建议**：
  - 把“检查点与最新分析的合并、过期判定”抽成 `analysis/checkpoints` 里的纯函数，用数据驱动的单元测试覆盖各种续跑场景。
  - 剩下的部分只做编排。
- **风险**：中高，关系续跑语义。
- **工作量**：M。

#### H4 边界扩展规则链

- **位置**：`src/autoslice/analysis/boundaries.py:139 _expand_clip_mark_with_context`。
- **证据**：
  - 267 行，圈复杂度 79。
  - 依次叠加十几条规则：相关上下文、语义焦点、SC/礼物触发、引入段、画面引入、裁剪、时长上下限、字幕对齐、视频时长截断，最后写元数据。
  - 规则之间靠十几个局部变量互相影响，单条规则没法单独测试，也看不出边界为什么被这样调整。
  - 全文件有 79 处跨模块调用私有函数（M1）。
- **建议**：
  - 先补“刻画测试”：用典型和边缘的 mark/字幕组合，固定当前输出。
  - 再拆成按顺序执行的规则步骤，作用于一个小的边界草稿对象（起止时间、硬结束点、调整原因）。
  - 每一步记录调整原因，方便排查切片边界问题。
- **风险**：高，直接关系 [架构重构说明 §6](../架构重构.md) 的切片业务契约。
- **工作量**：M～L。
- **建议放在最后做。**

### 中

#### M1 跨模块调用私有函数

- **证据**：
  - 生产代码共有 472 处跨模块使用以下划线开头的私有符号，涉及 151 个符号、27 个文件。
  - 去掉两个按设计就要重新导出私有符号的兼容层（`topic_engine` 147 处、`analysis.candidates` 107 处），仍有约 200 处，集中在 `analysis/boundaries.py`（79）、`analysis/review/context_ranges.py`（17）、`pipeline.py`（15）、`analysis/topic/analysis.py`（13）。
  - 被跨模块访问最多的是 `title_analysis`、`danmaku_analysis`、`context_evidence`、`transition_analysis`、`trigger_analysis` 和 `finalization`。
- **问题**：这些函数其实是包内公共接口，名字却标成私有。改函数的人无法判断它有没有外部调用方。
- **建议**：被跨模块调用的函数改成公开名称，兼容层保留旧名作为别名，保证对象身份不变。纯机械改动，可以按模块分批提交。
- **风险**：低中。工作量：M。

#### M2 `web/app.py` 是上帝模块

- **位置**：`src/autoslice/web/app.py`，3019 行，38 个路由，100 个顶层函数。
- **证据**：
  - 7 个后台任务函数（`run_subtitle_review_task` 等，从 1405 行起）的开头完全一样：检查是否已取消、状态置为运行中、定义进度回调。异常收尾也是同一套。
  - `update_task`（615 行）圈复杂度 44。
  - 模块级的锁和事件队列都有正确保护，没有发现并发问题。
- **建议**：
  - 后台任务的固定套路抽成一个统一包装，放进 `web/tasks.py`。
  - 路由按领域拆成 Blueprint：流水线和时间轴、字幕、任务和事件、结果和媒体。
- **风险**：中。工作量：M。

#### M3 生产代码经兼容层导入

- **证据**：
  - 生产代码有 9 处经 `autoslice.topic_engine` 导入：`web/app.py` 1512、1567、1677、1726、1769、2786、2862 行，`launcher.py:553`，`core.py:27`。
  - 这违反了依赖方向的第 2 条硬规则：生产代码应直接依赖真实实现。
  - 有 21 个测试文件引用 `topic_engine`，部分可能是在这里替换函数做测试，属于测试接缝。
- **建议**：改为直接导入真实实现，同步调整测试的替换位置。
- **风险**：低中。工作量：S～M。

#### M4 改了名的复制粘贴绕过了护栏

- **证据**：
  - `subtitle_workflow.py:2090 _split_subtitle_text_for_ass` 和 `transcription/segments.py:206 split_subtitle_text_for_display` 逐行相同，各自计算字宽的函数（`_subtitle_display_text_size` 和 `subtitle_text_size`）也相同。这违反唯一 owner 规则。
  - 校对状态的 JSON 结构在 `subtitle_workflow.py:788-813` 和 `subtitle_workflow.py:889-914` 各拼了一遍。
  - `web/app.py:405` 和 `autoslice_cover/app.py:686` 各写了一份相同的“签发本机会话 Cookie + 局域网模式脱敏”响应后处理。这是安全逻辑，两份一旦不同步，两个程序的脱敏行为就会不一致。
- **建议**：
  - 字幕断行统一由 `transcription.segments` 提供。
  - 状态序列化收成一个函数。
  - 响应后处理收进 `SecurityPolicy`，例如 `finalize_response`。
  - 架构护栏目前只检查同名顶层定义，可以考虑补一项“函数体 AST 相同”的检测。
- **风险**：低。工作量：S。

#### M5 旧 Tk 桌面壳仍是默认入口

- **证据**：
  - Qt 版已经完成 Desktop-04.6 及之后的阶段，但 `src/autoslice/desktop/app.py`（Tk，387 行）还在。
  - `桌面端.py` 和 `python -m autoslice.desktop` 默认启动的仍是 Tk 版，Qt 版要走 `Qt桌面端.py` 或 `python -m autoslice.desktop.qt_app`。
  - Tk 测试和 Qt 测试在同一个进程里跑时，偶尔触发 `Tcl_AsyncDelete`，进程中止。
- **建议**：由维护者决定退役 Tk 壳，或者至少把默认入口切到 Qt。退役时一并清理 Tk 测试。
- **风险**：低，属于产品决策。工作量：S。

#### M6 正式代码依赖“预览”包

- **证据**：
  - 正式窗口用的主题、图标和窗口骨架都放在 `desktop/qt_preview` 里。
  - `qt_preview/window.py` 里还带着写死的示例项目数据，又被当作正式窗口的父类。
  - UI 分支还在往这个包里继续加 `motion`、`popups`、`fonts`、`glyphs` 等正式模块。
- **建议**：把公共主题、图标、动效和窗口骨架移到 `desktop/qt_common`（名称待定），`qt_preview` 只保留一个很薄的演示入口。和 H1 一起做。
- **风险**：低中，主要是导入路径的改动。工作量：S～M。

#### M7 主工作台模板内联千行脚本

- **位置**：`src/autoslice/resources/templates/topic_v2.html`，1286 行，其中内联脚本 1061 行。
- **证据**：
  - 129 个顶层变量、80 个函数，15 个写在标签上的事件属性（`onclick` 等）。
  - CI 只对独立 JS 文件跑 `node --check`，这段脚本不在任何语法检查范围内。
  - 拼接 `innerHTML` 时统一用了 `esc()` 转义，抽查没有发现注入问题。
- **建议**：参照 `subtitle_workflow.js`，抽到 `static/topic_v2.js`，事件改成 `addEventListener` 绑定，并加入 CI 的 `node --check`。
- **风险**：低。工作量：S～M。

#### M9 桌面端写死本机投稿目录，并且重复解析配置

- **位置**：`src/autoslice/desktop/projects.py:13`。
- **证据**：
  - `DEFAULT_SUBMISSION_ROOT` 写死成维护者本机的绝对路径（F 盘下的投稿目录）。
  - `configured_submission_root()` 自己再解析一遍 `AUTOSLICE_SUBMISSION_DIR`，没有复用 `runtime_config.SUBMISSION_DIR`。后者已经是这个配置的唯一 owner，默认值是仓库内的 `submissions`。
  - 结果是同一个配置项有两个默认值，Web 端和桌面端行为不一致。
  - 其他人的电脑上没有这个目录，桌面端首次打开就显示“目录不存在”。
  - 公开发布扫描因此失败；`docs/desktop-vnext/CURRENT.md`、`MIGRATION_MAP.md`、`PRODUCT.md` 也写了同一路径。
- **建议**：桌面端改为使用 `runtime_config.SUBMISSION_DIR`。本机目录通过 `autoslice.local.json` 或环境变量配置；文档里的本机路径改成配置示例。
- **风险**：低。工作量：S。

#### M8 AutoCover `create_app` 塞了所有路由

- **位置**：`src/autoslice_cover/app.py:624`，481 行，25 个路由和 7 个钩子写成闭包。
- **说明**：这是 Flask 常见的应用工厂写法，单个路由都不长（最长的 `preview` 68 行）。圈复杂度 69 主要来自内嵌路由。
- **建议**：按资源、贴图、工作区和任务、渲染和导出拆成 Blueprint。旧 AutoCover UI 已冻结、不再迁移，所以优先级靠后。
- **风险**：低。工作量：S～M。

### 低

| ID | 位置 | 问题 | 建议 |
|---|---|---|---|
| L1 | `subtitle_workflow.py:444 _split_subtitle_cues`（30 行）、`autoslice_cover/app.py:541 _render_task`（20 行）、`desktop/qt_app/timeline.py:190 follow_playback`、`desktop/selection.py:41 retain`、`runtime_config.py:133 template_defaults` | 全仓无任何调用 | 确认无用后删除 |
| L2 | `analysis/topic/normalization.py:139 is_meta_body_line`（圈复杂度 60）；`normalization.py:101/102/209` 集合字面量里有重复元素 | 一长串启发式判断写死在代码里 | 改成规则表，去掉重复项 |
| L3 | `analysis/topic/analysis.py:246`（`except Exception: continue`）、`analysis/manual/timebase.py:232`（返回默认值） | 宽泛捕获后没有任何记录 | 收窄异常类型，加 debug 日志 |
| L4 | `llm/transport.py:852-859` | 闭包引用循环变量 `active_prompt`、`active_tokens`。目前是同步调用，不会出错 | 用默认参数显式绑定，防止以后改成异步调用时出错 |
| L5 | `desktop/waveform.py`（114 行） | 测试里没有任何引用 | 补缓存键和失败路径的单元测试 |
| L6 | ruff 扩展规则共 458 条，绝大多数是复杂度和参数类 | 不宜整体清零 | 遵守项目规定，不做无关格式化，随相关重构逐步消除 |

## 5. 不建议做的事

- **不按行数拆文件**，不建只搬运参数的空壳模块（项目文档的硬规则）。
- **不引入流水线阶段框架**：H2 只做参数对象。
- **不批量格式化**，不为了清零 ruff 告警做无关改动。
- **不改变兼容层的对象身份**：兼容层继续是同一对象的别名。
- **不重构已冻结的旧字幕 UI 和旧 AutoCover UI**，只为桌面端抽取需要的后端能力。

## 6. 建议的重构顺序

每一步都遵守项目流程：一次只动一个 owner、一个逻辑提交、重新生成架构快照、跑 hermetic 测试、等 Windows/Linux CI 双绿。

| 阶段 | 内容 | 前置条件 |
|---|---|---|
| 0 让护栏变绿 | §3：审阅新增的私有 patch、重新生成快照；M9 去掉写死的本机路径，让发布扫描通过；合并 PR #1 的测试修复 | 无 |
| 1 低风险清理 | M4 去重、L1 删死代码、L3/L4、M3 改为直接导入真实实现、M7 抽出内联脚本、M5 由维护者决策 | 阶段 0 |
| 2 桌面端结构 | H1 逐个抽控制器，M6 迁出预览包 | 先和 `desktop-ui-opus55v2` 约定顺序：建议 UI 分支先合入 dev，再做 H1，或者 H1 期间冻结 UI 分支对 `window.py` 的修改 |
| 3 后端组织 | H2 参数对象、M2 Web 任务包装和 Blueprint、M1 私有接口改名、M8 | 阶段 1 |
| 4 核心算法 | H3 检查点合并抽成纯函数、H4 边界规则链 | 先补刻画测试；这一阶段的改动需要单独授权有限真实录播验收 |

## 7. 附：度量摘要

- **函数**：共 1,764 个。超过 100 行的 52 个，超过 200 行的 6 个；圈复杂度超过 20 的 85 个，超过 40 的 10 个；嵌套 5 层及以上的 19 个；参数 8 个及以上的 47 个。
- **最大的类**：`DesktopWindow` 2268 行，`CoverWorkspace` 647 行，`SubtitleTimeline` 604 行，`SecurityPolicy` 538 行，`TaskStore` 538 行。
- **改动最频繁的仍然很大的文件**：`pipeline.py`（50 次提交），`web/app.py`（15 次），`analysis/boundaries.py`（13 次），`reporting.py`（13 次）。
- **测试覆盖**：只有 5 个生产模块在测试中完全没有被引用，其中 `media_preview` 实际上经路由和类名被间接测试，真正的缺口只有 `desktop/waveform.py`。

## 8. 处理记录

阶段 0 和阶段 1 已在 `desktop-vnext-refactor` 上完成，提交到 `desktop-vnext-dev` 等待合并：

| 项 | 处理 |
|---|---|
| §3 护栏 | 合入 PR #1 的测试修复；新增的私有 patch 改为依赖注入或替身对象，回到上限 17；函数计数断言改为读取快照；重新生成快照 |
| M9 | 桌面端改由 `runtime_config` 解析投稿目录，去掉写死的本机路径；文档改为配置说明；发布扫描通过 |
| M4 | 字幕断行和字宽改用 `transcription.segments`；校对状态的结构字段由一个函数生成；两个 Flask 程序的响应收尾移到 `SecurityPolicy.finalize_flask_response` |
| L1 | 删除 `_split_subtitle_cues`、`_render_task`、`retain`、`template_defaults`；`follow_playback` 暂时保留，避免和 UI 分支冲突 |
| L2 | 只去掉集合里的重复项；改成规则表留到以后 |
| L3 | 两处改为只捕获具体异常；项目没有使用 `logging`，不加日志 |
| L4 | 闭包改用默认参数绑定 |
| M3 | Web 主程序和启动器改为直接从真实模块导入，测试的 patch 目标同步修改 |
| M7 | 脚本抽到 `static/topic_v2.js`，默认目录经 JSON 数据块注入，事件改为 `addEventListener` 绑定；CI 对两个外置脚本跑 `node --check` |
| M5 | 等维护者决定 |

阶段 2～4 尚未开始。
