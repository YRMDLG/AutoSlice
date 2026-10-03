# ADR-001：桌面 GUI 与播放器底座

- 日期：2026-09-28
- 状态：接受（分阶段迁移；发布依赖另行验收）
- 范围：Desktop-04.6 至封面画布；旧 Web/AutoCover 入口不在本 ADR 的删除范围。

## 问题和仓库证据

Desktop-03/04 的 `桌面端.py` → `autoslice.desktop.app.main()` 使用 Tkinter/ttk，`DesktopApp` 内直接管理页面、项目选择、字幕文档、dirty 状态和大量逐 cue `tk.Text`。`refresh()` 同步扫描，界面没有视频渲染容器、独立任务调度或持久化草稿。`src/autoslice/desktop/projects.py` 和 `subtitles.py` 已把统一投稿项目与旧 SRT 契约隔开，因此 GUI 可换而不必重写解析/保存。旧 `启动.py` 是 Web 服务，不能为连续桌面视频提供可复用基座。当前依赖清单未声明 Qt/mpv，正式发布和 CI 尚未覆盖它们。

后续需求同时包含嵌入视频、长视频 seek、可拖动字幕时间轴、自由封面画布、上下文属性栏、后台任务、快捷键、高 DPI、窗口身份与安装版。Tk 能通过额外 Canvas、主题及播放器绑定逐项实现，但当前应用层与控件强耦合，每项交互会继续放大主线程工作和自绘/平台适配负担。

## 选择

1. 采用 **PySide6 / Qt Widgets** 作为后续 vNext GUI。Qt `QSplitter`、`QStackedWidget`、模型/视图、信号/工作线程、`QGraphicsView`/`QGraphicsScene` 可以对应工作台布局、长列表、任务反馈及画布对象。先迁壳、项目服务和既有 SRT 行为，再引入播放器；Tk 入口保留到回归通过。
2. 播放器首选 **libmpv + Qt 原生子窗口**，用小型 adapter 隔离 HWND、状态和 seek；`QtMultimedia` 保留为可用备选。FFmpeg 继续只管截帧、探测、压制和转码，不自建实时播放解码循环。
3. Windows 发布主线为 **PyInstaller onedir/windowed + Inno Setup**。备线为 Nuitka standalone + Inno Setup；备线只是计划，尚无构建证据。

## 比较

| 方案 | 当前改动成本 | 主要收益 | 主要风险/维护成本 | 决定 |
| --- | --- | --- | --- | --- |
| 继续 Tkinter/ttk | 短期最小，Desktop-04 控件可留用 | 当前 SRT 编辑可直接延续；无新 GUI 依赖 | 视频嵌入、高 DPI、复杂画布与对象交互都需额外自绘/平台胶水；当前逐 cue 控件及同步刷新扩大性能风险 | 停止在 Tk 上扩展 Desktop-05+ |
| 迁 PySide6/Qt Widgets | 需重写 `app.py` 的壳和字幕控件，先补隔离依赖/打包 | 原生窗口、布局、图形场景、快捷键、信号和线程模型覆盖既定需求；项目/SRT service 仍可复用 | Qt 包体积、DLL/插件分发、许可核对、迁移回归 | 选用，分 Desktop-04.6 执行 |
| libmpv | 需绑定 Windows DLL、HWND 和播放事件 | 实测同机原生嵌入、暂停精确 seek 和 D3D11 硬解；FFmpeg 系格式支持面更契合投稿视频 | Windows DLL 约 121 MB；分发许可证、原生窗口层级、设备/编码矩阵和进程崩溃需验证 | Desktop-05 首选 |
| QtMultimedia | Qt 内集成最少 | 同一样本可播放与 seek；减少外部 DLL 供应链 | 尚未证明长录播 seek、各种编码、硬解退回与音画稳定；跨机器后端行为要验证 | 回退候选 |

## 技术验证

- **Qt 壳**：隔离 PySide6 6.11.2 实例显示深色三页布局和字幕三区骨架；Windows 截图目视通过。Qt 官方说明 Qt Widgets/Graphics View 和自动 DPI 抽象：[Qt Widgets](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/)、[High DPI](https://doc.qt.io/qtforpython-6/overviews/qtdoc-highdpi.html)。本机实验设备像素比为 1.0，不能当作高 DPI 通过。
- **mpv**：本机原无 `mpv`/libmpv。下载 [mpv Windows 发布页](https://github.com/shinchiro/mpv-winbuild-cmake/releases/tag/20260926) 的 x86-64 开发包并核对 SHA-256 后，在隔离 Qt `QWidget` 的原生 HWND 上用 libmpv API 加载本机投稿目录下含 Emoji 的 H.264/AAC MP4。初始化/载入/精确定位返回 0，暂停 `time-pos=30.0` 秒、`hwdec-current=d3d11va`。mpv 官方手册明确 Windows `wid` 接收 HWND、`hwdec=auto` 不可用时回退软件解码、`hr-seek` 可用于精确定位：[mpv 手册](https://mpv.io/manual/stable/)。这只是 124 秒样本，未做数小时/多编码矩阵。
- **QtMultimedia**：同一文件 `QMediaPlayer` + `QVideoWidget` 加载，时长 124435 ms、可 seek，30 秒定位后继续播放约两秒读到 31857 ms，未报告错误。[Qt QMediaPlayer](https://doc.qt.io/qtforpython-6/PySide6/QtMultimedia/QMediaPlayer.html)提供 `setPosition()`；真实 seek 质量仍须比较。
- **EXE**：隔离 PyInstaller 6.22.3 构建 Qt Widgets `onedir/windowed` 小程序。误收宿主 Poppler 的 ICU 后导入 QtCore 失败；移走误收 DLL 后 EXE 退出码 0、启动标记成功，PE 子系统 2。说明路线可行，也说明正式构建必须清理 `PATH` 与校验收集清单。Inno Setup、完整应用和 mpv 合包尚未验证。

## 迁移影响和验收点

- Desktop-04.6 将 `DesktopApp` 的 Tk 控件交互改成 Qt 控件/视图，保留 `SubmissionProjectService`、`SubtitleDocument`、`save_corrected_srt` 契约。迁移中不得更改原始 SRT 与 `*_校对.srt` 的保存语义，不接 AI、时间轴或封面业务。
- Desktop-04.6 需接入 `DesktopStorage` 的节流草稿、会话、冲突提示和单实例基础，并在退出/异常重启、中文 Emoji 路径、高 DPI、窗口几何上验收。Desktop-05 专注播放器 adapter 和长视频样本。
- Desktop-05 如发现 mpv 无法在安装包中稳定分发、存在不可接受的窗口层级/崩溃问题，先用同一测试矩阵比较 QtMultimedia，再通过新 ADR 改变播放器选择，不让 Qt UI 直接依赖某一播放器的调用细节。
- 发布前固定依赖版本、提供许可证和第三方通知；mpv 不同构建有不同许可选项，[上游许可说明](https://github.com/mpv-player/mpv/blob/master/Copyright)。验证安装/卸载、无黑窗、图标/任务栏、非开发机器上的 DLL 和 FFmpeg 路径。
