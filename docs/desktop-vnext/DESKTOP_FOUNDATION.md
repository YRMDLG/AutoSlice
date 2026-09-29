# Desktop-04.5 桌面应用基座

更新：2026-09-28。本文区分已验证的技术事实与未来产品实现；[选型记录](ADR-001-desktop-gui-and-player.md)给出比较和证据。

## 定型结论

| 范围 | 决定 | 状态 |
| --- | --- | --- |
| GUI | vNext 后续采用 PySide6 / Qt Widgets；封面画布以 `QGraphicsView` / `QGraphicsScene` 为首选设计起点。Desktop-03/04 的 Tk 入口暂留，先做 Desktop-04.6 短迁移。 | Qt 壳 spike 已验证；生产迁移待实现 |
| 播放器 | Desktop-05 优先用 `libmpv` 嵌入 Qt 原生视频子窗口；播放器 adapter 隔离 UI。`QtMultimedia` 是可运行的回退候选。 | 两者在一条真实 MP4 上已验证；长视频矩阵待验证 |
| Windows 发布 | 主路线：隔离构建环境中的 PyInstaller `--onedir --windowed`，随后 Inno Setup 安装器。备选：Nuitka standalone + Inno Setup；若 libmpv 分发不可行，播放器回退 QtMultimedia。 | 最小无控制台 Qt EXE 已运行；完整程序和安装器待实现 |
| 数据 | 投稿根目录仍是项目事实来源；桌面私有状态位于 `%LOCALAPPDATA%\AutoSlice\`，与源码目录和投稿目录分离。 | 目录与原子 JSON 草稿接口已实现；Tk UI 尚未接入 |

## 真实代码边界

- 独立 vNext 入口是仓库根目录 `桌面端.py` 或 `python -m autoslice.desktop`，调用 `src/autoslice/desktop/app.py:main()` 的 `tk.Tk()` / `mainloop()`。旧 `启动.py` / `autoslice.launcher` 是 Flask + AutoCover 入口，不能拿来当桌面打包入口。
- 当前 Tk 壳的项目扫描由 `DesktopApp.refresh()` 在 UI 回调里同步调用 `SubmissionProjectService.refresh()`；字幕加载和每条 cue 的 `Text` 控件也在 UI 线程。Desktop-04 已有内存撤销、dirty 标志、显式校对保存及离开确认，没有异常退出草稿。既有状态在 `DesktopApp` 对象内，关闭即丢失。
- 旧字幕静帧预览、FFmpeg 截帧/压制和旧 Web 媒体服务不能充当连续播放器。Qt 和 libmpv 都不在当前 `pyproject.toml` / `requirements.txt`，不能把 spike 环境误认为已安装的生产依赖。

## 运行证据及边界

Desktop-04.5 在隔离 Python 3.10 环境安装 PySide6 6.11.2、PyInstaller 6.22.3；所有实验入口和构建产物留在仓库外。实验读取一条实际投稿 `9月27日.mp4`（中文、`【】`、Emoji 路径；H.264 / AAC，1920×1080、60 fps、124.435737 秒），没有编码、切片或写入投稿文件。

1. Qt Widgets 深色三页壳显示正常：三个一级入口、左项目、中工作区/播放器占位、右建议区以及封面占位均可承载；截图目视通过。实验报告 Qt 6.11.2、设备像素比 1.0。**这不等于已经验证 2560×1440 高 DPI。**
2. 经发布包 SHA-256 `b32107527c9fe60e2a032933379625428c32cb594c47a91acf1e4bb46b2bfc57` 校验的 Windows `libmpv-2.dll`，通过 Qt `QWidget.winId()`/`wid` 嵌入成功。`mpv_initialize`、`loadfile`、`seek 30 absolute+exact` 均返回 0；暂停状态读取 `time-pos=30.0` 秒，`hwdec-current=d3d11va`。这是**该样本/这台机器**的结果，不证明所有编码或长录播稳定。
3. 同一 MP4 用 `QMediaPlayer` + `QVideoWidget` 加载成功，`isSeekable=true`，请求定位 30000 ms 后继续播放约两秒，读到 31857 ms，错误字符串为空。QtMultimedia 因此是有实测依据的回退选项。
4. PyInstaller 最小 Qt Widgets `--onedir --windowed` 构建成功，PE 子系统为 2（Windows GUI）。首次启动失败：宿主 `PATH` 中 Codex/Poppler 的 `icuuc.dll` 与 Windows Qt 依赖不兼容，PyInstaller 错收该 DLL；其导出表缺少 Qt6Core 要求的 20 个函数。将误收的 `icuuc.dll`、`icudt78.dll` 从**实验包**移走后，诊断版与无控制台版均退出码 0，启动标记成功。正式构建必须清理构建环境 `PATH`，并检查二进制收集清单，不应靠发布后手工删除。最小包约 156 MB；没有验证完整程序、mpv/FFmpeg 合包或安装器。

## 应用边界与性能规则

- Qt 主线程只做窗口、输入、短状态更新和绘制。投稿扫描、缩略图、波形、解码派生、AI、FFmpeg 长任务放工作线程或独立进程；结果经信号回传 UI。任务需要取消、异常、进度和退出清理。先迁项目/字幕 service，不重写 SRT 解析与保存。
- 播放器 adapter 负责装载/释放 `libmpv`、`seek`、时钟、错误和暂停状态；字幕列表点击只定位，不自动播放。Windows 使用原生 HWND 子窗口，Desktop-05 必须补测 resize、DPI 切换、窗口遮挡/层级、音频、反复开关、长视频连续 seek。优先 `hwdec=auto`，失效时退回软件解码并记录原因；不得让播放器崩溃拖垮正式字幕文件。
- 帧、波形、AI 结果以输入签名/参数版本作缓存键。缓存丢失时可重建；不在 UI 线程同步重建。AI 必须用户主动触发。路径从 `Path` / Unicode 传入，正式支持中文、空格、Emoji、`【】`；调用 FFmpeg 用参数列表，避免自行拼 shell 命令。
- 应用身份、图标、快捷键、窗口尺寸/位置、高 DPI 由 Qt 壳负责；窗口几何恢复要限制在当前可见屏幕，防止显示器变化后窗口丢失。使用可伸缩布局与高分辨率图标，下一阶段实测 2560×1440 及系统缩放。
- 单实例方案：`QLocalServer` / `QLocalSocket` 使用用户作用域的固定名称；第二次启动发送激活请求后退出，主进程将窗口置前。监听异常、陈旧端点、同用户多会话及关闭清理需在 Desktop-04.6 测试；目前**未实现**，不能并行打开同一草稿编辑。
- 日志到 `logs/`，轮转、限制大小，记录组件/错误码/安全路径摘要和异常栈。不得记录 API Key、Cookie、prompt、字幕正文或 AI 响应。API Key 与中转设置放用户私有配置或 Windows 凭据存储，绝不进入投稿项目；原有本机 `api_config.json` 暂保留兼容，不在本任务搬迁。

## 本地目录及恢复协议

桌面默认根目录为 `%LOCALAPPDATA%\AutoSlice\`；测试可通过 `AUTOSLICE_DESKTOP_DATA_DIR` 指向隔离目录。此约定特意不沿用旧 `autoslice.paths.application_data_root()` 的源码仓库回退策略。`src/autoslice/desktop/foundation.py` 的 `DesktopStorage` 延迟建目录，不在导入时写盘。

| 子目录 | 内容与清理边界 |
| --- | --- |
| `sessions/last.json` | 上次页面、项目 ID、视频 ID、字幕处理位置、窗口状态引用；可重建，但删除后失去位置恢复。 |
| `drafts/subtitle/`、`drafts/cover/` | 未正式保存的编辑快照；不得当缓存自动清理。封面草稿格式/接入仍待后续任务。 |
| `cache/thumbnails/`、`cache/waveforms/`、`cache/ai/` | 可删除的派生数据；删除不得损坏投稿视频、SRT、封面或草稿。 |
| `logs/` | 本地轮转诊断日志；独立于项目目录。 |

草稿文件名由类型、项目绝对路径和主源文件路径的 SHA-256 派生，避免标题作为文件名和不同项目撞名。JSON envelope 为 `schema_version=1`、`kind`、`project`、`source`（路径/大小/纳秒修改时间）、`dependencies`（如已有或尚未生成的校对 SRT）、`updated_at`、`payload`。写入采用同目录临时文件、flush/fsync、`os.replace`；`read_draft()` 返回 `ready`、`missing`、`invalid`、`incompatible`、`source_missing`、`source_changed`。版本未知、损坏或正式源文件外部变化时**保留草稿但不自动套用**，由 UI 提供查看/冲突处理。会话 JSON 也带版本，不能解析时退回首次字幕页。

接入流程：打开编辑器时调用 `capture_baseline()` 记录源文件及校对文件签名；编辑时节流保存完整且可序列化的字幕/封面快照与当前位置，并始终沿用打开时的基线，不能在每次写草稿时重新设定基线。启动先扫描真实项目，再读取会话和草稿，展示恢复选择，恢复后标记 dirty；用户明确点“保存校对字幕”才调用现有 `save_corrected_srt` 写正式 `*_校对.srt`，并在确认写入成功后归档或清除对应草稿。原始 SRT 从不因键入或自动恢复被覆盖。旧 `*_校对状态.json` 属正式保存契约，不能被桌面自动草稿替代。当前接口只提供持久化与冲突识别，**尚未接入 Tk，也未实现封面快照、自动节流、恢复弹窗或迁移器**。

## 发布与后续门槛

正常安装版的完成条件是 AutoSlice.exe、桌面快捷方式、开始菜单、卸载入口、AutoSlice 图标和任务栏身份、可固定任务栏、启动无 Python/命令窗/浏览器、窗口几何与高 DPI 恢复。Inno Setup 尚未安装或运行；这轮没有安装器。主路线的 EXE 资源、Qt 插件、`libmpv-2.dll`、FFmpeg/ffprobe 应由固定版本与校验值的构建清单收集，打包后在**干净 Windows 用户环境**验收。备选 Nuitka 尚未实测，不能视为已验证。

mpv Windows 构建会把 FFmpeg 等库静态打进 DLL；实验 DLL 约 121 MB。发布前必须核对所选 mpv/FFmpeg 二进制及 Qt/PySide6 的许可证和再分发材料，选可合法分发的固定构建；实验用第三方开发包并非发布许可结论。参考：[mpv Windows 安装来源](https://mpv.io/installation/)、[mpv 许可说明](https://github.com/mpv-player/mpv/blob/master/Copyright)、[Qt for Python 许可](https://doc.qt.io/qtforpython-6/)、[PyInstaller windowed 选项](https://pyinstaller.org/en/stable/usage.html)、[Inno Setup 图标/卸载项](https://jrsoftware.org/ishelp/topic_setup_uninstalldisplayicon.htm)。

下一项 **Desktop-04.6** 只迁 Qt 壳、统一项目服务接线、现有 SRT 列表/编辑/保存、窗口状态与单实例基础；验证旧入口仍可用后，再开始 Desktop-05 播放器。草稿自动接线应在 04.6 优先完成，不能拖到封面阶段。
