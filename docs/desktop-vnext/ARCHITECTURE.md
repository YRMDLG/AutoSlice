# Desktop vNext 架构与迁移约束

> 本文整理桌面端当前的分层边界和长期实现约束。它服务于代码评审和后续迁移，不把一次实现细节当成永远不变的 API；若要改变下列边界，应在 [DECISIONS.md](DECISIONS.md) 留下新的决策记录。

## 1. 总体依赖方向

Desktop vNext 是桌面壳对既有 AutoSlice 能力的编排层，不能把旧 Web 页面、整条自动切片流水线或播放器细节重新复制进窗口控件。

```text
Qt DesktopWindow / 页面路由
        │ 只持有页面、项目和任务入口
        ▼
WorkspaceContext / 项目与当前视频快照
        ├── SubtitleDocument + 字幕编辑服务
        ├── PlayerAdapter + 播放头快照
        ├── SubtitleRenderService + 后台压制
        └── CoverEditorWidget
                ├── CoverCanvas（手势与显示）
                ├── CoverService（取帧、草稿、预览、导出）
                ├── CoverDocument（唯一封面编辑模型）
                └── autoslice_cover.renderer（纯渲染）
```

硬规则：底层领域模块不能反向导入 `DesktopWindow`、Qt 页面或旧 Web UI；生产消费者应依赖真实 owner，而不是继续从根目录兼容模块复制业务逻辑。

## 2. `DesktopWindow` 的膨胀风险

`DesktopWindow` 只负责窗口生命周期、一级导航、页面装配、当前项目/视频选择、简短状态和任务入口。以下状态不应继续塞入窗口：

- 字幕文档的正文、选中集合、时间轴拖动状态和 AI 建议状态；
- AutoCover 的对象、布局、草稿、候选帧、预览和导出历史；
- 播放器的具体 seek、字幕叠加和解码器调用细节；
- 任务的取消、generation、重试、缓存和文件指纹；
- 任何依赖页面控件排列的业务状态机。

当窗口再次出现项目、媒体、封面或 AI 的字段时，应先确认它是否属于 `WorkspaceContext`、页面 service 或独立领域模型；不要通过增加私有字段把窗口变成第二个应用服务。

## 3. `WorkspaceContext` 与状态共享

项目和当前视频是页面之间需要共享的最小上下文。建议上下文至少能表达：

- `project_id`、项目目录、视频路径和稳定视频身份；
- 当前视频的源 SRT、校对 SRT 和文件指纹；
- 当前页面、当前 cue、当前 playhead 快照；
- 字幕文档版本或内容 hash；
- 封面草稿 key、封面文档版本和 active profile；
- 后台任务所属的项目/视频/generation key。

上下文是快照式的：页面读取上下文，明确提交用户动作，不让封面页暗中改变播放器或字幕页。项目目录是事实来源；数据库、草稿和缓存只保存编辑、派生物和任务状态，状态丢失时仍能重新扫描并打开项目。

## 4. 异步任务与 generation/token

取帧、附近帧、预览、另一比例预览、AI 候选和字幕压制都可能晚于用户切换项目或文档。每个任务必须携带可比较的 generation/token：

1. 开始任务时捕获项目、视频、源文件指纹和文档/页面 generation。
2. 任务完成回到 UI 线程时，先比较 token、项目、视频和必要的文档 hash。
3. 任一项不匹配，丢弃结果并记录轻量可见状态，不覆盖当前页面。
4. 取消、关闭、最小化和切页只改变任务可见性或取消意图，不靠控件销毁来猜测任务归属。

切换项目不默认启动全片扫描。附近帧按需生成，预览使用节流，重复输入优先命中缓存；失败要保留当前草稿和可执行重试路径。

## 5. AutoCover 隔离边界

AutoCover 只接收不可变的项目/视频上下文和明确的 playhead 快照；它不得读取 `DesktopWindow`、`MpvAdapter` 或字幕文档的内部状态，也不能反向 seek 播放器。

封面侧的边界如下：

- `CoverCanvas`：负责显示、命中、拖动、缩放和轻量暂态信号，不直接写磁盘，不启动渲染任务。
- `CoverEditorWidget`：负责把用户动作转换成 `CoverDocument` 快照、历史提交、自动保存和预览调度；不实现媒体解码和 Pillow 绘制。
- `CoverService`：负责草稿读写、旧草稿迁移、取帧、素材、预览、导出、风格记忆和导出历史；不持有 Qt 控件布局。
- `CoverDocument`：负责可序列化封面对象、双比例 profile 和对象替换语义；不依赖媒体文件存在。
- `autoslice_cover.renderer`：消费明确的布局/渲染输入，输出预览或最终图片；不反向读取桌面上下文。
- `cover_layout`：把 `CoverDocument` 换算成导出像素下的背景放置、素材框、文字字号与断行。画布显示和导出都只从这里取几何，不各算一套；新增对象类型或样式效果时，两端的绘制都要跟着它改。
- `autoslice_cover.document_layout` / `document_render`：纯 Pillow 的共享排版和图层合成；`composition` 提供显著图，只产出构图建议，不直接改文档。

CoverDocument 与媒体资产引用分开：文档保存对象、变换、文本和资源标识；服务层验证路径、生成缓存和导出文件。封面缺少 manifest、数据库记录或自动切片结果时，项目扫描和封面制作仍然应该可用。

## 6. `CoverDocument` 与 `CoverDraft`

`CoverDocument` 是当前唯一真实封面编辑模型，包含 `BackgroundObject`、`TextObject`、`ImageObject`、`StickerObject` 和 `ShapeObject`，以及 4:3/16:9 profile override。对象使用稳定 id、归一化坐标、z-index、可见/锁定状态，文档采用不可变快照式替换。

`CoverDraft` 只承担 v1～v3 旧草稿兼容和迁移：

- 新代码不得把 CoverDraft 字段当成第二套状态源。
- 读取旧草稿后迁移到 v4 `CoverDocument`，尽量原子保存。
- 新的 A/B、素材、形状、双比例覆盖、锁帧和历史只能落在 CoverDocument。

Undo/Redo 保留完整文档快照，拖动期间使用暂态对象；提交发生在鼠标释放、明确属性变更或候选采纳等边界。

## 7. 字幕模块边界

字幕编辑使用既有 `SubtitleDocument`、选择模型、时间轴、AI review 和压制 service。Desktop vNext 只做适配和编排：

- 原始 SRT 与正式 `*_校对.srt` 的保存契约不变；自动保存写草稿，显式保存才写正式文件。
- 播放器 adapter 只负责播放、暂停、seek、位置和媒体状态，不保存字幕业务状态。
- ASS、波形和预览属于派生缓存；UI 线程只调度和接收结果，不能把缓存写回投稿原件。
- AI 建议的缓存、采纳/跳过、撤销和文档 hash 由字幕应用层管理，不能把旧 Web 页面状态带进 Qt。
- 字幕压制沿用旧 `subtitle_workflow`/FFmpeg owner，通过 `SubtitleRenderService` 在后台调用，不复制命令拼接。

字幕语义已冻结，封面或架构重构不得顺手改变字幕快捷键、选中态、时间轴命中优先级和保存语义。

## 8. 持久化与文件边界

- 项目目录是输入和正式结果的事实来源；用户源视频、源 SRT 和既有成片不能被草稿或预览覆盖。
- 草稿、缓存、波形、预览、AI 会话和导出历史写入桌面应用数据目录或明确的封面输出草稿目录；写入采用临时文件加原子替换。
- Web/LAN 模式的 Host、Origin、token 和允许根目录由 `security_policy` 负责；Desktop Qt 链路不依赖该模块，而是通过项目扫描来源和各 workflow 的局部不变量守住文件边界，例如同目录约束、正式后缀、内容摘要、保护路径排除和冲突递增。
- 因此未来新增“导出到任意目录”“批量处理任意路径”等能力时，必须为新 workflow 明确允许范围和回归测试，不能假设 Desktop 已存在集中式路径安全网关。
- 日志、页面状态和错误出口不泄露不必要的绝对路径、令牌、字幕正文或本机配置；本地调试所需路径信息可以显示，但不得携带 token 或配置正文。
- 源文件指纹变化时，旧草稿、帧图和缓存应标记为失效或要求确认，不能静默套用旧结果。
- 任务失败时清理临时文件，保留草稿、错误原因和可重试入口；重启后的运行中任务按既有语义标记 interrupted，不承诺进程退出后继续执行。

## 9. 性能、并发与可见状态原则

- UI 线程只做轻量状态更新和布局，不执行取帧、整图渲染、波形、FFmpeg、模型调用或全片扫描。
- 用户拖动期间只更新暂态画面和控件反馈；释放鼠标后再写历史、自动保存和排队预览。
- 附近帧、预览和风格/AI 结果按需、可取消、可缓存；切项目时旧 generation 结果不得落到新项目。
- 任务状态按项目和视频隔离，同一项目禁止重复启动相同压制；不同项目是否并行由 service 和资源约束决定。
- 默认 AI 不运行，默认不扫描全片；任何昂贵或有外部副作用的动作都必须由用户明确触发。
- “是否显示”必须由显式状态决定，不能通过当前提示文字、控件是否恰好可见或 routine 文案内容反推业务状态。
- error / warning / success / loading 应有稳定、可见的语义出口；错误不能只写进隐藏控件或被普通状态文案覆盖，失败动作应保留原因和可执行重试入口。
- checkable / selected / locked 等重要状态必须有可见反馈；自动化测试只能验证状态契约，最终还要用真实 Windows 字体和像素结果检查是否真的看得出来。

## 10. 部署与兼容边界

- `src/autoslice` 和 `src/autoslice_cover` 是当前生产 owner；`autocover_tool` 只保留旧入口、兼容导入和本机数据发现，不加入新的业务逻辑。
- `启动.py` 仍是 AutoSlice 与相邻 AutoCover 服务的既有管理入口；桌面 vNext 的开发和打包路径需要与服务入口分开验收。
- Qt 壳、播放器 adapter、字体、FFmpeg 和安装包依赖必须通过单独的发布验收矩阵确认；历史 mpv/QtMultimedia/打包实验只作参考，不能直接当作发布承诺。
- 当前仓库尚无正式 PyInstaller `.spec` 和 Inno Setup `.iss`；因此 onedir + Inno Setup 仍是发布方向，不是已完成能力。
- 正式打包时，可写数据继续放 `%LOCALAPPDATA%\AutoSlice`；本地 token、个人配置、个人样式和测试产物必须在 staging/安装包中显式排除，不能把 `.gitignore` 当成打包规则。
- 默认安装不应要求管理员权限，除非未来出现明确的系统级写入需求；首次安装、升级、卸载、重装和快捷方式启动都要进入发布验收矩阵。
- 部署决策若变化，先更新 `DECISIONS.md` 和对应证据；发布安全与可靠性摘要见 [SECURITY_NOTES.md](SECURITY_NOTES.md)。

## 11. 结构性验收护栏

后续重构至少保持以下检查：

- `DesktopWindow` 不持有封面对象、AI 建议和字幕文档的第二套状态。
- `autoslice_cover` 不反向导入 Qt 窗口、旧 Web 页面或整条切片流水线。
- 项目/视频/generation 不匹配时，迟到的取帧、预览、AI、波形和压制结果不会覆盖当前页面。
- CoverDocument 可以从旧 CoverDraft 迁移、保存、恢复、Undo/Redo，并按两个 profile 导出。
- 字幕草稿、封面草稿和正式输出分层；源 SRT、源视频和既有成片保持不变。
- 自动化检查通过后仍需执行真实字体、真实素材、真实窗口尺寸和连续生产的人工验收。

更早的架构审计和阶段迁移证据保留在 [archive/ARCHITECTURE_REVIEW.md](archive/ARCHITECTURE_REVIEW.md) 等历史文件中。