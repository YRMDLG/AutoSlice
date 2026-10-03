# Desktop vNext 架构与迁移契约

## Desktop-08.7 交互边界

- `desktop/selection.py` 的 `CueSelection` 保存 active、anchor 和 selected set；Qt 列表与时间轴只通过它同步，不在控件内部维护第二套多选状态。
- `desktop/commands.py` 的 `CommandDispatcher` 登记命令名与处理函数，窗口快捷键只负责映射；文字编辑焦点关闭时间轴命令作用域，为未来设置页保留统一注册表。
- `desktop/snap.py` 的 `SnapEngine` 按屏幕像素计算阈值并维护吸附目标和脱离迟滞。时间轴只向它提供候选边界，Alt 作为临时 bypass。
- 波形和 `subtitle_preview.py` 生成的 ASS 都属于 DesktopStorage 的派生 cache；UI 线程只调度/接收结果，不能写投稿原件。字幕预览复用 `subtitle_workflow.build_ass_document`，播放器 adapter 只负责加载或移除私有字幕。
- 08.7 的拆分首先是文档内存命令；右侧无 caret 时保持空文本待编辑，禁止 AI 猜断句。正式状态持久化和恢复需要为 synthetic split cue 扩展旧 SRT 状态契约，未在本轮改变旧保存业务。

以 [MIGRATION_MAP.md](MIGRATION_MAP.md) 的 17 项静态审计为迁移依据。新 Desktop 经稳定的 application / service / adapter 边界调用旧能力；UI 只依赖桌面端自己的项目、字幕、画布和任务状态，不直接读写旧页面内部状态。

## 迁移边界

| 方式 | 能力与边界 |
| --- | --- |
| 直接复用 | SRT 解析、验证、序列化、校对保存及状态恢复的 Python 契约；视频/SRT 识别与配对规则；默认字幕样式、参数校验和 ASS 几何换算。保留源 SRT 与 `*_校对.srt` 分离。 |
| 包装复用 | 投稿扫描、AI 字幕检查与缓存、字幕压制/FFmpeg、AutoCover `CoverWorkspace` 扫描与磁盘草稿、素材库和 Python 渲染/导出、模型传输、后台任务注册与资源冲突控制。桌面 adapter 负责路径授权、配置注入、进度、取消、错误和进程生命周期。 |
| 重写交互状态 | AI 建议的采纳/跳过/手工保护/撤销；连续视频播放与 cue 定位；封面画布编辑、双比例同步和属性栏。可复用旧数据字段、画布规格与渲染契约，不移植旧页面内存状态。 |
| 冻结不迁移 | 自动分析/自动切片主流程、旧字幕 UI、旧 AutoCover UI。旧实现可保留作参考和过渡，不成为 vNext 主界面或启动前置条件。 |

## 边界规则

- 投稿根目录沿用已有配置/路径边界（`AUTOSLICE_SUBMISSION_DIR` 或本机 `autoslice.local.json`，未配置时为仓库内 `submissions`），维护者本机在私有配置中指定实际投稿目录；不写死为底层扫描函数的默认值或唯一合法目录。桌面项目层按标题文件夹组织项目，复用现有视频/SRT 扫描与配对规则，并统一供两个功能页使用。当前扫描是请求驱动的刷新，不能假定有文件监听。
- 项目文件夹是项目事实来源；数据库或草稿状态只补充编辑、缓存与任务信息。状态缺失时仍能扫描并打开项目。
- 草稿与正式结果分层：字幕编辑自动保存草稿，显式保存才通过现有契约写校对 SRT；封面编辑状态落盘并能恢复，导出沿用渲染器的暂存提交机制。
- AI 检查结果字段可复用，但建议处理状态须由新应用层管理；模型 transport 可复用，不能把话题分析 prompt 或整条切片流水线带入字幕/封面功能。封面 AI 生成结构化、可编辑布局，由本地渲染器实现成图。
- Desktop-07 的 `desktop/ai_review.py` 是旧 `suggest_subtitle_corrections` 与 Qt 间的 adapter。它把 `SubtitleDocument.entries` 序列化为用户应用数据目录中的临时 SRT，调用旧检查器及其主播词表/固定错词映射和模型 transport；旧检查器的原始缓存也留在私有目录。Qt 只接收 `Suggestion` / `AIReviewSession`，不接旧 Web 页面状态。缓存键覆盖源路径、当前字幕快照、标题、模型与 API 类型、推理配置、主播规则指纹和旧 prompt 版本；处理状态另存私有会话，核对源文件及文档快照后恢复。后台沿用 Qt `QRunnable` 边界，单个检查任务跨页继续，取消在旧检查器回调边界协作完成。详细字段见 [AI_SUGGESTIONS.md](AI_SUGGESTIONS.md)。
- Desktop-08 的边界为 `qt_app/window.py` → `desktop/subtitle_render.py:SubtitleRenderService` → 旧 `subtitle_workflow.burn_subtitles` → FFmpeg。Qt 只传视频、已保存 `*_校对.srt` 的路径与内容指纹；adapter 验证两者在同一目录，将 SRT 副本、ASS、样式 JSON、FFmpeg `.part.mp4` 放在 `%LOCALAPPDATA%\AutoSlice\cache\temp`，复用旧默认样式、默认导出、字体检查、NVENC 回退、进度与视频校验。旧工作流不复制命令拼接到 Qt。适配层把成片排他发布到源视频同目录 `*_字幕版.mp4`；同名时顺延 ` (2)`、` (3)`，绝不替换既有成片或源视频。失败详细记录在 `logs/subtitle-render.log`，临时目录在任务结束后清理。
- 压制由 Qt `QRunnable` 调用，进度经信号回主线程；项目 ID 防止同项目重复任务和状态串线，不同项目可并行。`burn_subtitles` 新增协作取消参数，停止请求会终止正在运行的 FFmpeg。任务只属于当前进程；窗口最小化可继续，退出前须等待停止完成，重启不续跑。无服务进程时不能承诺窗口彻底关闭后仍在后台压制。
- 后台任务可复用 SQLite 注册、冲突与取消契约；进度传递和桌面进程生命周期另适配。重启后的运行中任务按现有语义标记 `interrupted`，不能承诺线程续跑。
- 旧字幕静帧预览不等于连续播放器；已有短片媒体服务绑定自动分析任务清单，不能直接当作任意投稿视频的播放入口。
- AutoCover 的精调 manifest 仅是可选输入；无 manifest、无数据库记录、未运行自动切片时，项目扫描及封面制作仍须可用。

## Desktop-04.5 定型后的桌面基座

技术比较与实测边界见 [ADR-001](ADR-001-desktop-gui-and-player.md)，完整运行/恢复规范见 [DESKTOP_FOUNDATION.md](DESKTOP_FOUNDATION.md)。后续桌面 GUI 采用 PySide6/Qt Widgets；Desktop-03/04 的 Tk 壳保留到 Desktop-04.6 迁移验收通过。Qt 层只调用桌面项目、字幕、状态和任务 adapter，不直接读写旧 Web 页面状态。封面自由画布以 Qt 图形场景为首选起点，不提前复用旧 Web DOM。

播放器以可替换 adapter 接入 libmpv：Qt 负责窗口和交互，libmpv 负责连续播放/seek，FFmpeg 保留探测、截帧及离线输出职责。QtMultimedia 是已做单样本验证的备选。播放器事件通过 Qt 信号交给 UI；扫描、AI、FFmpeg 和缓存生成不得阻塞 UI 线程。

桌面私有状态统一在 `%LOCALAPPDATA%\AutoSlice\`，通过 `autoslice.desktop.foundation.DesktopStorage` 管理会话/草稿/缓存/日志目录。该目录与投稿项目事实来源分离；草稿先原子写入私有目录，显式保存才使用既有校对 SRT 契约。旧源码态 `autoslice.paths` 和 AutoCover 数据目录在迁移期间保留兼容，不被自动搬迁或覆盖。Qt 壳负责单实例、窗口恢复和高 DPI；这些除最小存储接口外尚未接入生产入口。

Windows 发布主路线为隔离构建的 PyInstaller 目录式无控制台 EXE + Inno Setup；备选 Nuitka standalone + Inno Setup。正式安装器、应用图标、任务栏身份及完整依赖分发仍需后续实施和实机验收。

## Desktop-08.7.1 字幕文档与媒体派生物

字幕文档的正式状态包含 `corrections`、`deleted_indices`、`merge_pairs`、`time_overrides` 和 `split_groups`。`split_groups` 以源 cue 为根，保存连续 segment 的稳定 ID、时间和正文；校对 SRT 仍是正式输出，源 SRT 永不覆盖。`SubtitleDocument` 根据当前 entries 重建这些关系，Undo/Redo 只记录一次完整快照。

波形由 `autoslice.desktop.waveform.WaveformCache` 管理。缓存 key 绑定规范化视频路径、文件大小和 `mtime_ns`，数据以 JSON 幅度包络写入应用私有目录。Qt 通过线程池调用 `load_or_generate`，切换项目时用 generation 丢弃迟到结果；时间轴只消费缓存结果，不在 UI 线程解码音频。

边界试听和循环都使用播放器的绝对 seek 与暂停/播放命令。试听保存一个结束边界，循环按 active cue 的起止加减余量，状态轮询到达边界时重新 seek；普通 seek、AI 定位、Q/W 和拆分会清除试听边界，不改字幕文档。
