# AutoSlice Desktop vNext 项目上下文

> 作用：保存长期项目背景、真实使用方式、当前阶段和实现语境。新模型、新 Work 线程或隔一段时间继续开发时，应先读本文件，再读当前规范。

## 1. 项目是什么

AutoSlice Desktop vNext 是一个 Windows 桌面端“速切提质器”，服务于直播切片发布流程。

一句话定位：**面向直播切片员的 AI 辅助速切工作台。**

它不负责视频剪辑创作，而负责把已经在剪映完成的视频片段更快、更稳定地变成可投稿成品。

当前最高优先级不是自动分析直播、自动找片段或自动切片，而是：
- 字幕校对；
- AutoCover 封面制作；
- 字幕压制；
- 项目状态与可恢复工作流。

默认投稿目录固定为：`F:\Videos\投稿`。

## 2. 用户真实生产流程

真实工作流来自长期使用习惯，而不是产品假设：
1. 在剪映完成剪辑、片头片尾和字幕初稿。
2. 导出最终视频与 SRT。
3. 放入 `F:\Videos\投稿\<视频标题>\`。
4. 打开 AutoSlice，刷新项目列表并进入该项目。
5. 在字幕页启动 AI 检查。6. AI 在后台运行时切到 AutoCover 做封面。
7. 回字幕页边看视频边处理建议，只做“采纳 / 跳过”或手工修改。
8. 保存校对字幕。
9. 执行字幕压制。
10. 到 B 站后台上传。

用户不需要额外的复杂“发布前准备”模块；B 站后台沿用历史标题/简介/标签已经够用。

## 3. 为什么要做 Desktop vNext

旧项目逐步变大，Web/服务/桌面/历史 AutoCover 边界复杂，用户希望得到一个更聚焦的 Windows 正常应用：
- 安装一次；
- 桌面快捷方式直接启动；
- 不依赖浏览器；
- 不依赖命令行脚本作为日常入口；
- 日常只看到字幕、封面、设置三个一级入口。

临时开发入口可以继续存在，但发布目标是 `AutoSlice.exe` + 安装器。

桌面技术方向：
- PySide6 / Qt Widgets；
- libmpv 为首选播放器；
- PyInstaller onedir；
- Inno Setup 安装器；
- 无控制台窗口。

## 4. 用户运行环境

主要目标环境：
- Windows；
- CPU：AMD Ryzen 9 4900H；
- 内存：32GB DDR4 2667MHz；
- GPU：NVIDIA RTX 2060 6GB + AMD 集显；
- 显示器：2560×1440；
- 多块约 500GB SSD。

性能设计要基于这台机器的真实体验，而不是假设无限算力。自动分析整段录播、默认全片扫描和高成本 AI 不应成为页面加载依赖。

## 5. Desktop vNext 主导航

一级导航固定：
- 字幕；
- 封面；
- 设置。

不新增常驻 Dashboard。项目状态属于共享上下文，不能为了“完整”再造一套项目管理系统。

## 6. 字幕模块当前定位

字幕是成熟、高频、已冻结语义的工作台。

重点不是继续发明字幕功能，而是：
- 保持边看视频边改字幕的效率；
- 单击进入编辑；
- AI 只给采纳/跳过；
- 时间轴选中、播放位置、hover、playhead 相互分离；
- 草稿可恢复；
- 正式保存明确；
- 压制在后台完成。

字幕模块后续只有真实使用 bug 才应改变交互语义。

## 7. AutoCover 当前定位

AutoCover 是“快速封面编辑器”，不是 Photoshop/Canva 替代品。

目标路径：
`打开项目 → 选帧 → 基础文案 → 调整对象 → 检查比例 → 导出`。

当前 Beta 已经把核心能力铺开，但 UI 还没有收口，当前界面不能视为最终设计。

AutoCover 长期核心：
- 原始视频选帧；
- 4:3 / 16:9 双比例；
- A/B 独立文字对象；
- 背景对象；
- 图片/贴图/简单强调形状；
- 草稿与 Undo/Redo；
- 基础素材库；
- 风格继承；
- AI 显式候选；
- 清晰导出。## 8. AutoCover 的产品经验

“功能存在”不等于“人能用”。

已经反复验证：
- 拖动手感比属性面板更重要；
- 旧版文字拖动、吸附等已经验证好用的行为，应优先继承；
- 一个大 TextObject + 自动换行非常容易产生不可用排版；
- A/B 必须是独立对象；
- 默认布局不能用“顶部塞大字”替代真实构图；
- Canvas / Preview / Export 必须共用一套字体样式和排版语义；
- 单测通过不能替代实机视觉验收；
- 用户真实截图可以推翻“视觉通过”的内部结论。

## 9. AI 的位置

AI 是显式辅助，不是默认依赖。

原则：
- 页面加载不自动调用；
- 项目切换不自动调用；
- 基础文案不依赖 AI；
- 相同输入应优先缓存；
- AI 可以给候选、审核、布局建议；
- AI 不能替用户决定审美；
- AI 不能暗中替换锁定帧；
- AI 输出必须落为可编辑结构，而不是不可编辑整图。

当前 AutoCover AI 仍以本地 mock 三候选为 Beta 契约；真实 relay 属于后续接入。

## 10. 项目当前阶段

截至 2026-10-03：
- Desktop 壳、字幕工作台和压制链路已经形成稳定基线；
- AutoCover 的底层对象模型与大部分 Beta 功能已接通；
- 文档基线已经重新收口；
- 下一阶段不是继续堆功能，而是 AutoCover Beta UI Cleanup + 统一实机验收；
- 字体、默认构图、基础文案、连续生产和信息架构仍需要真实项目集中测试。## 11. 当前代码和文档入口

当前 Desktop vNext 文档入口：
`docs/desktop-vnext/README.md`

生产代码 owner 主要在：
- `src/autoslice`
- `src/autoslice_cover`

历史兼容入口不应成为新业务 owner。

AutoCover 主要代码：
- `src/autoslice/desktop/cover.py`
- `cover_canvas.py`
- `cover_service.py`
- `cover_model.py`
- `cover_migration.py`
- `cover_history.py`
- `cover_copy.py`
- `cover_style.py`
- `cover_assets.py`
- `cover_ai.py`
- `src/autoslice_cover/renderer.py`
- `src/autoslice_cover/text_layout.py`

## 12. 工作区约束

当前开发通常位于 `desktop-vnext-dev`。

重要原则：
- 不覆盖旧稳定 preview 分支；
- 不 force push；
- 用户未要求时不提交、不 push；
- 未跟踪的 `work/` 和未知本地文件不能随手删除；
- 任何大重构先确认当前工作区已有修改，避免覆盖其他来源的工作。

更具体的协作规则见 [COLLABORATION_NOTES.md](COLLABORATION_NOTES.md)。
