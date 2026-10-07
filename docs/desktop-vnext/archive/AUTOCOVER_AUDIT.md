# AutoCover vNext 审计与最小工作流

## 审计结论（2026-09-30）

仓库里已经有一套完整的 `src/autoslice_cover/` 生产能力：`video.py` 负责 ffprobe/FFmpeg 取帧和私有缓存，`renderer.py` 负责 16:9/4:3 背景裁切、标题排版、描边与阴影，`titles.py`、`style.py` 和 profile 规则负责标题拆句与视觉建议，`drafts.py` 和旧 Web 页面负责历史封面草稿与预览记录。`CoverWorkspace` 的输入是切片目录，默认会忽略封面/成片目录并使用自己的任务队列；它适合作为旧 AutoCover 兼容入口，不适合作为 Qt 投稿项目的第二套项目扫描器。

Qt vNext 原先只有封面导航和占位画布，没有读取项目图片、保存封面草稿或导出结果的能力。`SubmissionProjectService` 已经把 `submissions/<标题>` 扫描为字幕页和封面页共同使用的 `ProjectSnapshot`，因此 AutoCover-01 直接继承该快照，不再引入旧 `CoverWorkspace` 的独立项目体系。

### 复用、冻结与丢弃

| 处理 | 现有能力 | AutoCover vNext 决定 |
| --- | --- | --- |
| 复用 | `autoslice_cover.video.extract_frame_at_timestamp`、视频缓存与媒体校验 | 从当前投稿视频异步取帧，缓存放 `%LOCALAPPDATA%\\AutoSlice\\cache\\thumbnails\\cover-frames` |
| 复用 | `autoslice_cover.renderer.render_cover`、`TextTransform`、标题模板/调色板 | 先用单比例 16:9，保留已有标题描边和阴影，导出 JPG |
| 复用 | `DesktopStorage` 的原子草稿协议 | 封面草稿按项目+视频独立保存到 `%LOCALAPPDATA%\\AutoSlice\\drafts\\cover` |
| 复用 | 现有 `ProjectSnapshot` / `SubmissionProject` / `ProjectVideo` | 字幕页切到封面页继续使用同一项目和视频 |
| 冻结 | 旧 Web AutoCover 页面、双比例同步、贴图资产库、复杂模板规则 | 作为兼容实现保留，AutoCover-01 不迁移旧页面内存状态 |
| 冻结 | 精调 manifest、自动候选队列、自动封面文案调用 | 可选输入继续有效，但不是打开封面页的前置条件 |
| 丢弃 | 在 Qt 内重新建立切片目录扫描、自动切片入口、自动上传 B 站 | 不进入 vNext 工作流 |

## AutoCover-01 最小工作流

1. 打开投稿项目，字幕页和封面页看到同一个项目/视频。
2. 点击“导入本地图”，或输入视频秒数后点击“从当前视频取帧”。取帧在 Qt 线程池执行。
3. 在 16:9 画布预览标题，修改标题、归一化位置和字号；现有渲染器提供默认描边与阴影。
4. 编辑期间自动保存可恢复草稿，也可显式点击“保存草稿”。底图副本和预览只写应用数据目录。
5. 点击“导出 JPG”，结果写回当前投稿项目目录，文件名为 `AutoCover-视频名.jpg`，同名时递增 `(2)`、`(3)`，从不覆盖源视频或旧封面。
6. AI 入口只显示“封面文案/构图建议（手动触发）”占位提示；AutoCover-01 不会在普通编辑或切页时调用模型。

## 后续分级

- **P0：暂无。** 当前版本已经能继承项目、获取底图、编辑、恢复草稿并导出。
- **P1：** 把当前播放器 playhead 直接传给取帧；补充 4:3 画布和双比例独立状态；增加真正的拖动文字框与背景焦点；接入显式触发且带缓存的文案/构图建议。
- **P2：** 贴图、颜色选择器、模板切换、参考封面分析、批量导出和发布流程。这些不阻塞当前投稿流程。
